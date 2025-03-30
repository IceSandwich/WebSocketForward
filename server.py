import asyncio, aiohttp, typing, argparse, logging
from data import SerializableRequest, SerializableResponse
import aiohttp.web as web
import utils

log = logging.getLogger(__name__)
utils.SetupLogging(log, 'server')

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.port: int = args.port

	@classmethod
	def SetupParser(cls, parser: argparse.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1")
		parser.add_argument("--port", type=int, default=8030)
		return parser

class WebSocketManager:
	def __init__(self) -> None:
		self.ws = aiohttp.web.WebSocketResponse(max_msg_size=utils.WS_MAX_MSG_SIZE)
		self.isConnected = False
		self.onConnected: typing.Callable[[]] = None
		self.onDisconnected: typing.Callable[[]] = None
		self.onError: typing.Callable[[BaseException]] = None

		self.recvMaps: typing.Dict[str, SerializableResponse] = {}
		self.recvCondition = asyncio.Condition()

		self.ssePool: typing.Dict[str, asyncio.Queue[SerializableResponse]] = {}

	def IsClosed(self):
		return self.ws.closed
	
	async def Session(self, request: SerializableRequest):
		await self.ws.send_bytes(request.to_protobuf())
		async with self.recvCondition:
			await self.recvCondition.wait_for(lambda: request.url in self.recvMaps)
			resp = self.recvMaps[request.url]
			del self.recvMaps[request.url]
			return resp
		
	async def SessionSSE(self, url: str):
		while True:
			data = await self.ssePool[url].get()
			if data.stream_end:
				del self.ssePool[url]
				yield data
				break
			yield data

	async def MainLoop(self, request: web.BaseRequest):
		await self.ws.prepare(request)
		self.isConnected = True
		if self.onConnected:
			self.onConnected()
		else:
			log.info("Websocket connected.")

		try:
			async for msg in self.ws:
				if msg.type == aiohttp.WSMsgType.ERROR:
					if self.onError != None:
						self.onError(self.ws.exception())
					else:
						log.error(f"WebSocket error: {self.ws.exception()}")
				elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
					resp = SerializableResponse.from_protobuf(msg.data)
					if resp.sse_ticket:
						isFirstSSE = resp.url not in self.ssePool
						log.debug(f"recv sse: {resp.url} isFirst: {isFirstSSE} isEnd: {resp.stream_end}")
						if isFirstSSE:
							self.ssePool[resp.url] = asyncio.Queue()
						else:
							await self.ssePool[resp.url].put(resp)
						if isFirstSSE:
							async with self.recvCondition:
								self.recvMaps[resp.url] = resp
								self.recvCondition.notify_all()
					else:
						async with self.recvCondition:
							log.debug(f"recv http: {resp.url}")
							self.recvMaps[resp.url] = resp
							self.recvCondition.notify_all()
		finally:
			self.isConnected = False
			if self.onDisconnected:
				self.onDisconnected()
			else:
				log.info(f"Websocket disconnected.")

		return self.ws
	

# 存储WebSocket连接
client: typing.Union[None, WebSocketManager] = None

async def websocket_handler(request):
	global client
	if client != None:
		log.info(f"Already has connections")
		return None

	client = WebSocketManager()
	def disconnected():
		global client
		client = None
		log.info(f"Websocket disconnected.")
	client.onDisconnected = disconnected
	return await client.MainLoop(request)


async def http_handler(request: web.Request):
	if client is None or client.IsClosed():
		return web.Response(status=503, text="No client connected")
		
	# 提取请求的相关信息
	body = await request.read() if request.has_body else None
	req = SerializableRequest(str(request.url.path_qs), request.method, dict(request.headers), body)
	log.info(f"{req.method} {req.url} hasBody: {body is not None}")
	resp = await client.Session(req)

	if resp.sse_ticket:
		async def sse_response(resp):
			yield resp.body
			async for package in client.SessionSSE(resp.url):
				yield package.body
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=sse_response(resp)
		)
	else:
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=resp.body
		)

async def main(config: Configuration):
	app = web.Application()
	app.router.add_get('/ws', websocket_handler)  # WebSocket升级端点
	app.router.add_route('*', '/{tail:.*}', http_handler)  # HTTP服务
	
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, config.server, config.port)
	await site.start()
	
	print(f"Server started on {config.server}:{config.port}")
	await asyncio.Future()

if __name__ == "__main__":
	argparse = argparse.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))