import asyncio, logging, os
from urllib.parse import urlparse
from pathlib import Path
import data
import utils, typing
import aiohttp.web as web
import argparse as argp
import tunnel
import encrypt
from PIL import Image
import io
import mimetypes
mimetypes.add_type("text/plain; charset=utf-8", ".woff2")

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.port: int = args.port
		self.prefix: str = args.prefix
		self.img_quality:int = args.prefer_img_quality
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/remote_ws", help="The websocket server to connect to.")
		parser.add_argument("--prefix", type=str, default="http://127.0.0.1:7860", help="The prefix of your requests.")
		parser.add_argument("--port", type=int, default=8130, help="The port to listen on.")
		parser.add_argument("--cipher", type=str, default="xor", help=f"The cipher to use. Available: [{', '.join(encrypt.GetAvailableCipherMethods())}]")
		parser.add_argument("--key", type=str, default="websocket forward", help="The key to use for the cipher. Must be the same with client.")
		parser.add_argument("--prefer_img_quality", type=int, default=75, help="Compress the image to transfer fasterly.")
		return parser

class TypeHintClient(tunnel.HttpUpgradedWebSocketClient):
	"""
	该类仅用于类型提示，不要实例它
	"""
	def __init__(self):
		raise SyntaxError("Do not instantiate this class.")
	
	async def Session(self, request: data.Request) -> typing.Tuple[data.Response, str]:
		"""
		接受无加密的request，返回无加密的response和seq_id。
		"""
		raise NotImplementedError()

	def Stream(self, seq_id: str) -> typing.AsyncGenerator[bytes, None]:
		"""
		返回无加密的SSE数据
		"""
		raise NotImplementedError()

client: typing.Optional[TypeHintClient] = None

class Client(tunnel.HttpUpgradedWebSocketClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server)
		self.conf = config
		
		# debug only. map from seq_id to url. 布尔值表示是否需要打印sample数据，我们只在第一次打印，后续不打印。
		self.tracksse: typing.Dict[str, typing.Tuple[str, bool]] = {}

	def GetName(self) -> str:
		return "Remote"
	
	def GetLogger(self) -> logging.Logger:
		return log

	async def OnConnected(self):
		global client
		client = self
		return await super().OnConnected()
	
	async def OnDisconnected(self):
		global client
		client = None
		return await super().OnDisconnected()
	
	async def Session(self, request: data.Request):
		raw = data.Transport(data.TransportDataType.REQUEST, request.ToProtobuf(), 0, 0)
		log.debug(f"Request {raw.seq_id} - {request.method} {request.url}")
		raw.data = raw.data if self.conf.cipher is None else self.conf.cipher.Encrypt(raw.data)
		rawResp = await super().Session(raw)
		assert(rawResp.data_type == data.TransportDataType.RESPONSE)
		rawResp.data = rawResp.data if self.conf.cipher is None else self.conf.cipher.Decrypt(rawResp.data)
		resp = data.Response.FromProtobuf(rawResp.data)
		if resp.IsSSEResponse():
			log.debug(f"SSE Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code} {len(resp.body)} bytes <<< {repr(resp.body[:50])} ...>>>")
			self.tracksse[raw.seq_id] = [request.url, True]
		else:
			log.debug(f"Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code}")
		return resp, raw.seq_id

	async def Stream(self, seq_id: str):
		async for raw in super().Stream(seq_id):
			rawData = raw.data if self.conf.cipher is None else self.conf.cipher.Decrypt(raw.data)
			yield rawData

	async def OnRecvPackage(self, raw: data.Transport) -> None:
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
		
		return await super().OnRecvPackage(raw)

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

	def readCache(self, filename: str, url: str):
		log.debug(f'Using cache: {url} => {filename}')
		with open(filename, 'rb') as f:
			body = f.read()
		resp = data.Response(url, 200, {
			'Content-Type': mimetypes.guess_type(filename)[0] or "application/octet-stream"
		}, body)
		return resp, utils.NewSeqId()
	
	async def Session(self, request: data.Request):
		parsed_url = urlparse(request.url)
		components = parsed_url.path.strip('/').split('/')
		if len(components) == 0: return await super().Session(request)

		if components[0] in self.assets_dir:
			relativefn = parsed_url.path[1:]
		elif components[0].startswith('file='):
			relativefn = parsed_url.path[len('/file='):]
			if relativefn.startswith(self.sdprefix):
				relativefn = relativefn[len(self.sdprefix):].strip('/')
		else:
			return await super().Session(request)
		targetfn = os.path.join(self.root_dir, relativefn)

		if os.path.exists(targetfn):
			return self.readCache(targetfn, request.url)
		else:
			if relativefn.startswith('outputs/'):
				utils.SetWSFCompress(request, utils.MIMETYPE_WEBP, self.conf.img_quality)
				resp, seq_id = await super().Session(request)
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
				os.makedirs(os.path.dirname(targetfn), exist_ok=True)
				with open(targetfn, 'wb') as f:
					f.write(resp.body)
					log.debug(f'Cache {parsed_url.path} to {targetfn}')
			return resp, seq_id

class HttpServer:
	def __init__(self, conf: Configuration) -> None:
		self.config = conf

	async def processSSE(self, resp: data.Response, seq_id: str):
		yield resp.body
		async for package in client.Stream(seq_id):
			yield package

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		global client
		if client is None or not client.IsConnected():
			return web.Response(status=503, text="No client connected")
			
		# 提取请求的相关信息
		body = await request.read() if request.can_read_body else None
		req = data.Request(self.config.prefix + str(request.url.path_qs), request.method, dict(request.headers), body=body)
		resp, seq_id = await client.Session(req)

		if resp.IsSSEResponse():
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
		
	async def MainLoop(self):
		app = web.Application(client_max_size=100 * 1024 * 1024)
		app.router.add_route('*', '/{tail:.*}', self.MainLoopOnRequest)  # HTTP服务
		
		runner = web.AppRunner(app)
		await runner.setup()
		site = web.TCPSite(runner, "127.0.0.1", port=self.config.port)
		await site.start()
		
		log.info(f"Server started on 127.0.0.1:{self.config.port}")
		await asyncio.Future()

async def main(config: Configuration):
	await asyncio.gather(StableDiffusionCachingClient(config).MainLoop(), HttpServer(config).MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
