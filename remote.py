import asyncio, logging, os, typing, re, json, aiohttp
from urllib.parse import urlparse
import aiohttp.web as web
import argparse as argp
import data, tunnel, utils, encrypt

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")

		self.prefix: str = args.prefix
		self.port: int = args.port

		self.uid: str = args.uid
		self.target_uid: str = args.target_uid

		self.img_quality:int = args.prefer_img_quality

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="The websocket server to connect to.")
		parser.add_argument("--cipher", type=str, default="xor", help=f"The cipher to use. Available: [{', '.join(encrypt.GetAvailableCipherMethods())}]")
		parser.add_argument("--key", type=str, default="WebSocket@Forward通讯密钥，必须跟另一端保持一致。", help="The key to use for the cipher. Must be the same with client.")

		parser.add_argument("--prefix", type=str, default="http://127.0.0.1:7860", help="The prefix of your requests.")
		parser.add_argument("--port", type=int, default=8130, help="The port to listen on.")

		parser.add_argument("--uid", type=str, default="Remote")
		parser.add_argument("--target_uid", type=str, default="Client")

		parser.add_argument("--prefer_img_quality", type=int, default=75, help="Compress the image to transfer fasterly.")
		return parser

class Client(tunnel.HttpUpgradedWebSocketClient):
	def __init__(self, config: Configuration):
		self.conf = config
		
		# debug only. map from seq_id to url. 布尔值表示是否需要打印sample数据，我们只在第一次打印，后续不打印。
		self.tracksse: typing.Dict[str, typing.Tuple[str, bool]] = {}

		super().__init__(f"{config.server}?uid={config.uid}")

	def GetName(self) -> str:
		return self.conf.uid
	
	def GetLogger(self) -> logging.Logger:
		return log
	
	def GetUID(self) -> str:
		return self.conf.uid
	
	def GetTargetUID(self) -> str:
		return self.conf.target_uid
	
	async def Session(self, request: data.Request):
		"""
		发送Request包，返回Response包，内部会处理加解密的工作。
		"""
		raw = data.Transport(data.TransportDataType.REQUEST, request.ToProtobuf(), self.conf.uid, self.conf.target_uid)
		log.debug(f"Request {raw.seq_id} - {request.method} {request.url} " + ("" if request.body is None else f"{len(request.body)} bytes"))
		raw.data = raw.data if self.conf.cipher is None else self.conf.cipher.Encrypt(raw.data)
		rawResp = await super().Session(raw)
		assert(rawResp.data_type == data.TransportDataType.RESPONSE)
		rawResp.data = rawResp.data if self.conf.cipher is None else self.conf.cipher.Decrypt(rawResp.data)
		resp = data.Response.FromProtobuf(rawResp.data)
		if resp.IsSSEResponse():
			log.debug(f"SSE Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code} {len(resp.body)} bytes <<< {repr(resp.body[:50])} ...>>>")
			self.tracksse[raw.seq_id] = [request.url, True]
		else:
			log.debug(f"Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code} {len(resp.body)} bytes")
		return resp, raw.seq_id
	
	async def ControlQuery(self):
		"""
		Query控制包以Control的形式返回。
		"""
		raw = data.Control(data.ControlDataType.QUERY_CLIENTS, '')
		rawResp: data.Transport = await super().SessionControl(raw, self.conf.uid)
		assert(rawResp.data_type == data.TransportDataType.CONTROL)
		resp = data.Control.FromProtobuf(rawResp.data)
		assert(resp.data_type == data.ControlDataType.QUERY_CLIENTS)
		return json.loads(resp.msg)
	
	async def ControlExit(self):
		"""
		Exit控制包是没有返回的。
		"""
		raw = data.Control(data.ControlDataType.EXIT, self.conf.target_uid)
		await super().SessionControl(raw, self.conf.uid)

	async def Stream(self, seq_id: str):
		async for raw in super().Stream(seq_id):
			rawData = raw.data if self.conf.cipher is None else self.conf.cipher.Decrypt(raw.data)
			yield rawData

	async def SendStreamEndSignal(self, seq_id: str):
		pkg = data.Transport(data.TransportDataType.STREAM_SUBPACKAGE, b'', self.conf.uid, self.conf.target_uid, seq_id=seq_id)
		pkg.cur_idx = 0 # Fixme: ?
		pkg.total_cnt = -1
		await self.QueueSend(pkg)

	async def OnCtrlQuerySends(self, raw: data.Transport):
		return await self.putToSessionCtrlQueue(raw)

	async def DispatchOtherPackage(self, raw: data.Transport) -> None:
		assert(raw.data_type == data.TransportDataType.RESPONSE)
		rawData = raw.data if self.conf.cipher is None else self.conf.cipher.Decrypt(raw.data)

		try:
			resp = data.Response.FromProtobuf(rawData)
		except Exception as e:
			log.error(f"Failed to parse {raw.seq_id} response, maybe the cipher problem: {e}")
			log.error(f"{raw.seq_id}] Source raw data: {raw.data[:100]}")
			log.error(f"{raw.seq_id}] Target raw data: {rawData[:100]}")
			raise e # rethrow the exception
		
		if resp.IsSSEResponse():
			log.debug(f"SSE >>>Begin {raw.seq_id} - {resp.url} {len(resp.body)} bytes")
			self.tracksse[raw.seq_id] = [resp.url, True]
		
		return await super().DispatchOtherPackage(raw)

class StableDiffusionCachingClient(Client):
	def __init__(self, config: Configuration):
		super().__init__(config)
		
		self.root_dir = 'cache_sdwebui'
		self.assets_dir = ['assets', 'webui-assets']
		self.sdprefix = 'D:/GITHUB/stable-diffusion-webui-forge/'
		
		os.makedirs(self.root_dir,exist_ok=True)
		for assets_fd in self.assets_dir:
			os.makedirs(os.path.join(self.root_dir, assets_fd), exist_ok=True)

		#http://127.0.0.1:7860/assets/Index-_i8s1y64.js
		#http://127.0.0.1:7860/webui-assets/fonts/sourcesanspro/6xKydSBYKcSV-LCoeQqfX1RYOo3i54rwlxdu.woff2
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/extensions/a1111-sd-webui-tagcomplete/tags/temp/wc_yaml.json?1743699414113=
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-grids/2025-04-04/grid-0037.png
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-images/2025-04-04/00146-3244803616.png
		#http://127.0.0.1:7860/file=extensions/a1111-sd-webui-tagcomplete/javascript/ext_umi.js?1729071885.9683821=
		#http://127.0.0.1:7860/file=C:/Users/xxx/AppData/Local/Temp/gradio/tmpey_xm_6q.png

	def readCache(self, filename: str, url: str):
		log.debug(f'Using cache: {url} => {filename}')
		with open(filename, 'rb') as f:
			body = f.read()
		resp = data.Response(url, 200, {
			'Content-Type': utils.GuessMimetype(filename),
			'Content-Length': str(len(body))
		}, body)
		return resp, utils.NewSeqId()
	
	async def Session(self, request: data.Request):
		parsed_url = urlparse(request.url)
		components = parsed_url.path.strip('/').split('/')

		if components[0] in self.assets_dir:
			relativefn = parsed_url.path[1:]
		elif components[0].startswith('file='):
			relativefn = parsed_url.path[len('/file='):]
			if relativefn.startswith(self.sdprefix):
				relativefn = relativefn[len(self.sdprefix):].strip('/')
			pattern = r'^C:/Users/[a-zA-Z0-9._-]+/AppData/Local/Temp/'
			if re.match(pattern, relativefn):
				# 使用re.sub()去掉匹配的前缀部分
				relativefn = re.sub(pattern, '', relativefn)
		elif parsed_url.path == '/':
			relativefn = 'index.html' # cache index.html
		else:
			return await super().Session(request)
		targetfn = os.path.join(self.root_dir, relativefn)

		if os.path.exists(targetfn):
			return self.readCache(targetfn, request.url)
		else:
			if relativefn.startswith('outputs/'):
				utils.SetWSFCompress(request, utils.MIMETYPE_WEBP, self.conf.img_quality)
				resp, seq_id = await super().Session(request)
				if resp.status_code != 200:
					return resp, seq_id
				img = utils.DecodeImageFromBytes(resp.body)
				os.makedirs(os.path.dirname(targetfn), exist_ok=True)
				img.save(targetfn)
				log.info(f'Save result: {targetfn}')
				with open(targetfn, 'rb') as f:
					resp.body = f.read()
					if 'Content-Length' in resp.headers:
						resp.headers['Content-Length'] = str(len(resp.body))
			else:
				resp, seq_id = await super().Session(request)
				if resp.status_code != 200:
					return resp, seq_id
				try:
					dirname = os.path.dirname(targetfn)
					os.makedirs(dirname, exist_ok=True)
				except Exception as e:
					log.error(f"Failed to create dir: {dirname} for request url: {request.url}")
				with open(targetfn, 'wb') as f:
					f.write(resp.body)
					log.debug(f'Cache {parsed_url.path} to {targetfn}')
			return resp, seq_id

class HttpServer:
	def __init__(self, conf: Configuration, client: Client) -> None:
		self.config = conf
		self.client = client

	async def processSSE(self, resp: data.Response, seq_id: str):
		yield resp.body
		async for package in self.client.Stream(seq_id):
			yield package

	async def processSSE2(self, writer: web.StreamResponse, resp: data.Response, seq_id: str):
		print("=== OpenSSE " + seq_id)
		await writer.write(resp.body)
		try:
			async for package in self.client.Stream(seq_id):
				await writer.write(package)
		except aiohttp.ClientConnectionResetError:
			print("===="  + seq_id + "关闭了")
			await self.client.SendStreamEndSignal(seq_id)
		print("==== CloseSSE " + seq_id)
		return writer

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		# 提取请求的相关信息
		body = await request.read() if request.can_read_body else None
		req = data.Request(self.config.prefix + str(request.url.path_qs), request.method, dict(request.headers), body=body)
		resp, seq_id = await self.client.Session(req)

		if resp.IsSSEResponse():
			# response = web.StreamResponse(
			# 	status=resp.status_code,
			# 	headers=resp.headers,
			# )
			# await response.prepare(request)
			# return await self.processSSE2(response, resp, seq_id)
			return web.Response(
				status=resp.status_code,
				headers=resp.headers,
				body=self.processSSE(resp, seq_id)
			)
		else:
			return web.Response(
				status=resp.status_code,
				headers=resp.headers,
				body=resp.body
			)
		
	async def handlerControlPage(self, request: web.BaseRequest):
		return web.FileResponse(os.path.join(os.getcwd(), 'assets', 'control.html'))
	
	async def handlerControlQuery(self, request: web.BaseRequest):
		jsonObj = await self.client.ControlQuery()
		output = {
			"connected": self.config.target_uid in jsonObj and jsonObj[self.config.target_uid]
		}
		return web.json_response(output)
	
	async def handlerControlExit(self, request: web.BaseRequest):
		await self.client.ControlExit()
		return web.Response(status=200)
		
	async def MainLoop(self):
		app = web.Application(client_max_size=100 * 1024 * 1024)
		app.router.add_get("/wsf-control", self.handlerControlPage)
		app.router.add_get("/wsf-control/query", self.handlerControlQuery)
		app.router.add_post("/wsf-control/exit", self.handlerControlExit)
		app.router.add_route('*', '/{tail:.*}', self.MainLoopOnRequest)  # HTTP服务
		
		runner = web.AppRunner(app)
		await runner.setup()
		site = web.TCPSite(runner, "127.0.0.1", port=self.config.port)
		await site.start()
		
		log.info(f"Server started on 127.0.0.1:{self.config.port}")
		await asyncio.Future()

async def main(config: Configuration):
	client = StableDiffusionCachingClient(config)
	httpserver = HttpServer(config, client)
	await asyncio.gather(client.MainLoop(), httpserver.MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	loop = asyncio.get_event_loop()
	try:
		loop.run_until_complete(main(conf))
	except KeyboardInterrupt:
		print(f"==== Mainloop exit due to keyboard interrupt")
