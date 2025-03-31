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
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/remote_ws")
		parser.add_argument("--port", type=int, default=8130)
		parser.add_argument("--cipher", type=str, default="xor")
		parser.add_argument("--key", type=str, default="websocket forward")
		return parser
	
class IClient(tunnel.WebSocketTunnelClient):
	async def Session(self, request: data.Request) -> typing.Tuple[data.Response, str]:
		"""
		返回回应和seq_id
		"""
		raise NotImplementedError
	async def SessionSSE(self, seq_id: str) -> bytes:
		raise NotImplementedError

client: typing.Union[None, IClient] = None

class Client(IClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server, name="WebSocket Remote")
		self.conf = config

		self.recvMaps: typing.Dict[str, data.Response] = {}
		self.recvCondition = asyncio.Condition()

		self.ssePool: typing.Dict[str, asyncio.PriorityQueue[data.Transport]] = {} # a pool with sse_subpackage
		self.sseCondition = asyncio.Condition()

	def OnConnected(self) -> bool:
		global client
		client = self
		return super().OnConnected()
	
	async def OnDisconnected(self):
		global client
		client = None
		return super().OnDisconnected()
	
	async def Session(self, request: data.Request):
		"""
		接受无加密的request，返回无加密的response
		"""
		package = data.Transport(data.TransportDataType.REQUEST, None, 0, 0)
		print(f"Session {request.url}")
		body = request.body
		await self.QueueSendSegments(package, request, body, cipher=self.conf.cipher)

		async with self.recvCondition:
			await self.recvCondition.wait_for(lambda: package.seq_id in self.recvMaps)
			resp = self.recvMaps[package.seq_id]
			del self.recvMaps[package.seq_id]
			return resp, package.seq_id
		
	async def SessionSSE(self, seq_id: str):
		expect_idx = 1 # 跳过了response，第一个sse_subpackage是1
		last_len = self.ssePool[seq_id].qsize()
		while True:
			async with self.sseCondition:
				await self.sseCondition.wait_for(lambda: not self.ssePool[seq_id].empty() and self.ssePool[seq_id].qsize() != last_len)
				item = await self.ssePool[seq_id].get()
				if item.cur_idx != expect_idx:
					await self.ssePool[seq_id].put(item)
					last_len = self.ssePool[seq_id].qsize() # to avoid the dead loop
					continue
				expect_idx = expect_idx + 1
				item.data = self.conf.cipher.Decrypt(item.data) if self.conf.cipher is not None else item.data
				if item.total_cnt == -1: # -1 means the end of sse
					del self.ssePool[seq_id]
					yield item.data
					break
				yield item.data
				
	async def processSSE(self, raw: data.Transport, resp: data.Response = None):
		isFirstSSE = raw.seq_id not in self.ssePool
		if isFirstSSE:
			print(f"SSE {resp.url} {raw.seq_id}")
			self.ssePool[raw.seq_id] = asyncio.PriorityQueue()
		else:
			async with self.sseCondition:
				await self.ssePool[raw.seq_id].put(raw)
				self.sseCondition.notify_all()
		if isFirstSSE:
			async with self.recvCondition:
				self.recvMaps[raw.seq_id] = resp
				self.recvCondition.notify_all()
	
	async def OnProcess(self, raw: data.Transport):
		if raw.data_type not in [data.TransportDataType.RESPONSE, data.TransportDataType.SUBPACKAGE, data.TransportDataType.SSE_SUBPACKAGE]: return

		if raw.data_type == data.TransportDataType.SUBPACKAGE:
			isGotPackage, package = await self.ReadSegments(raw)
			if isGotPackage == False: return
			raw = package
		elif raw.data_type == data.TransportDataType.SSE_SUBPACKAGE:
			await self.processSSE(raw)
			return
		# raw must contain data.Response now

		rawData = raw.data if self.conf.cipher is None else self.conf.cipher.Decrypt(raw.data)
		resp = data.Response.FromProtobuf(rawData)

		if resp.sse_ticket:
			await self.processSSE(raw, resp)
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
			package: bytes
			yield package

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		global client
		if client is None or not client.IsConnected():
			return web.Response(status=503, text="No client connected")
			
		# 提取请求的相关信息
		body = await request.read() if request.can_read_body else None
		req = data.Request(str(request.url.path_qs), request.method, dict(request.headers), body=body)
		log.info(f"{req.method} {req.url}")
		resp, seq_id = await client.Session(req)

		if resp.sse_ticket:
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
		
		print(f"Server started on 127.0.0.1:{self.config.port}")
		await asyncio.Future()

async def main(config: Configuration):
    await asyncio.gather(Client(config).MainLoop(), HttpServer(config).MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
