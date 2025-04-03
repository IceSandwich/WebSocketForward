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

	def SessionSSE(self, seq_id: str) -> typing.AsyncGenerator[bytes, None]:
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
		# 放sse_subpackage的容器，内容已经解密了的
		self.ssePool: typing.Dict[str, asyncio.PriorityQueue[data.Transport]] = {}
		self.sseCondition = asyncio.Condition()

		self.recvMaps: typing.Dict[str, data.Response] = {}
		self.recvCondition = asyncio.Condition()

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
		await self.DirectSend(raw)

		async with self.recvCondition:
			await self.recvCondition.wait_for(lambda: raw.seq_id in self.recvMaps)
			resp = self.recvMaps[raw.seq_id]
			del self.recvMaps[raw.seq_id]
			if resp.IsSSEResponse():
				log.debug(f"SSE Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code}")
			else:
				log.debug(f"Response {raw.seq_id} - {request.method} {resp.url} {resp.status_code}")
			return resp, raw.seq_id

	async def SessionSSE(self, seq_id: str):
		while True:
			item = await self.ssePool[seq_id].get()
			if item.total_cnt == -1:
				del self.ssePool[seq_id]
				yield item.data
				return
			yield item.data

	async def processSSE(self, raw: data.Transport):
		async with self.sseCondition:
			rawData = raw.data if self.conf.cipher is None else self.conf.cipher.Decrypt(raw.data)
			if raw.total_cnt == -1: # 结束SSE流，结束包是不带数据的
				log.debug(f"SSE <<<End {raw.seq_id} - {self.tracksse[raw.seq_id][0]} {len(rawData)} bytes")
				del self.tracksse[raw.seq_id]
			else:
				log.debug(f"SSE Stream {raw.seq_id} - {len(rawData)} bytes")
				if self.tracksse[raw.seq_id][1]: # 打印第一个sse的sample
					log.debug(f"SSE Package <<< {repr(raw.data[:50])} ...>>> to <<< {repr(rawData[:50])} ...>>>")
					self.tracksse[raw.seq_id][1] = False # 关闭后续的sse打印sample
			raw.data = rawData
			await self.ssePool[raw.seq_id].put(raw)
			self.sseCondition.notify_all()

	async def OnRecvStreamPackage(self, raw: data.Transport) -> None:
		return await self.processSSE(raw)

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
			self.ssePool[raw.seq_id] = asyncio.PriorityQueue()
			async with self.recvCondition:
				self.recvMaps[raw.seq_id] = resp
				self.recvCondition.notify_all()
		else:
			async with self.recvCondition:
				# log.debug(f"recv http: {resp.url}")
				self.recvMaps[raw.seq_id] = resp
				self.recvCondition.notify_all()

class HttpServer:
	def __init__(self, conf: Configuration) -> None:
		self.config = conf

	async def processSSE(self, resp: data.Response, seq_id: str):
		yield resp.body
		async for package in client.SessionSSE(seq_id):
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
