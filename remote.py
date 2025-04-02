import asyncio, logging
import data
import utils, typing
import aiohttp.web as web
import argparse as argp
import tunnel
import encrypt

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.port: int = args.port
		self.prefix: str = args.prefix
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
		return parser

class TypeHintClient(tunnel.WebSocketTunnelClient):
	"""
	该类仅用于类型提示，不要实例它
	"""
	def __init__(self):
		raise SyntaxError("Do not instantiate this class.")
	
	async def Session(self, request: data.Request) -> typing.Tuple[data.Response, str]:
		pass
	def Stream(self, seq_id: str) -> typing.AsyncGenerator[bytes, None]:
		pass

client: typing.Union[None, TypeHintClient] = None

class Client(tunnel.WebSocketTunnelClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server, name="WebSocket Remote")
		self.conf = config

		# debug only. map from seq_id to url. 布尔值表示是否需要打印sample数据，我们只在第一次打印，后续不打印。
		self.tracksse: typing.Dict[str, typing.Tuple[str, bool]] = {}
		self.trackLock = asyncio.Lock()

	async def OnConnected(self):
		global client
		client = self
		await super().OnConnected()
	
	async def OnDisconnected(self):
		global client
		client = None
		await super().OnDisconnected()
	
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
			async with self.trackLock:
				self.tracksse[raw.seq_id] = [request.url, True]
		else:
			log.debug(f"Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code}")
		return resp, raw.seq_id
	
	async def Stream(self, seq_id: str):
		url = self.tracksse[seq_id][0]
		async for item in super().Stream(seq_id):
			chunk = item.data if self.conf.cipher is None else self.conf.cipher.Decrypt(item.data)
			isFirstTime = self.tracksse[seq_id][1]
			if isFirstTime:
				async with self.trackLock:
					self.tracksse[seq_id][1] = False
				log.debug(f"SSE >>>Begin {seq_id} - {url} {len(chunk)} bytes <<< {repr(chunk[:50])} ...>>>")
			else:
				log.debug(f"SSE Stream {seq_id} - {len(chunk)} bytes <<< {repr(chunk[:50])} ...>>>")
			yield chunk
		log.debug(f"SSE <<<End {seq_id} - {url}")
		async with self.trackLock:
			del self.tracksse[seq_id]

	async def OnRecvStreamPackage(self, raw: data.Transport):
		await self.putStream(raw)

	async def OnRecvPackage(self, raw: data.Transport):
		if self.checkTransportDataType(raw, [data.TransportDataType.RESPONSE]) == False: return
		await self.putSession(raw)

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
    await asyncio.gather(Client(config).MainLoop(), HttpServer(config).MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
