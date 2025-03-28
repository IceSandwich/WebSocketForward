import asyncio
import aiohttp
from aiohttp import web
import aiohttp.locks
from data import SerializableRequest, SerializableResponse
import config
import typing
import utils
import queue

Server_BaseURL = "127.0.0.1"
Server_Port = 8030

class WebSocketManager:
	def __init__(self) -> None:
		self.ws = web.WebSocketResponse(max_msg_size=config.WS_MAX_MSG_SIZE)
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
			# async with self.sseCondition:
			# 	await self.sseCondition.wait_for(lambda: not self.ssePool[url].empty())
			# 	data = self.ssePool[url].get()
			# 	if data.stream_end is True:
			# 		del self.ssePool[url]
			# 		print("session sse end: ", url)
			# 	else:
			# 		print("session sse: ", data.url)

	async def MainLoop(self, request: web.BaseRequest):
		await self.ws.prepare(request)
		self.isConnected = True
		if self.onConnected:
			self.onConnected()
		else:
			print("Websocket connected.")

		try:
			async for msg in self.ws:
				if msg.type == aiohttp.WSMsgType.ERROR:
					if self.onError != None:
						self.onError(self.ws.exception())
					else:
						print(f"WebSocket error: {self.ws.exception()}")
				elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
					resp = SerializableResponse.from_protobuf(msg.data)
					if resp.sse_ticket:
						print("recv sse <= ", resp.url)
						isFirstSSE = resp.url not in self.ssePool
						if isFirstSSE:
							self.ssePool[resp.url] = asyncio.Queue()
						else:
							print("to queue <= ", resp.url)
							await self.ssePool[resp.url].put(resp)
						if isFirstSSE:
							async with self.recvCondition:
								print("to map <= ", resp.url)
								self.recvMaps[resp.url] = resp
								self.recvCondition.notify_all()
					else:
						async with self.recvCondition:
							# print("recv <= ", resp.url)
							self.recvMaps[resp.url] = resp
							self.recvCondition.notify_all()
		finally:
			self.isConnected = False
			if self.onDisconnected:
				self.onDisconnected()
			else:
				print("Websocket disconnected.")

		return self.ws
	

# 存储WebSocket连接
client: WebSocketManager = None

async def websocket_handler(request):
	global client
	if client != None:
		print("Already has connections")
		return None

	client = WebSocketManager()
	def disconnected():
		global client
		client = None
		print("Websocket disconnected.")
	client.onDisconnected = disconnected
	return await client.MainLoop(request)


async def http_handler(request: web.Request):
	if client is None or client.IsClosed():
		return web.Response(status=503, text="No client connected")
		
	# 提取请求的相关信息
	req = SerializableRequest(str(request.url.path_qs), request.method, dict(request.headers), None if not request.body_exists else request.content.read_nowait())
	# print("from browser: ", req.url)
	resp = await client.Session(req)

	if resp.sse_ticket:
		async def sse_response(resp):
			print("into sse response", resp.url)
			yield resp.body
			async for package in client.SessionSSE(resp.url):
				print("got <= ", package.url, package.body)
				yield package.body
		print("Got sse, return sse response", resp.url)
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=sse_response(resp)
		)

		# while True:
		# 	sseresp = await client.SessionSSE(resp.url)
		# 	print("got <= ", sseresp.url)
		# 	if sseresp.stream_end:
		# 		print("end <= ", sseresp.url)
		# 		return response
		# 	await response.write(sseresp.body)
	else:
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=resp.body
		)

async def main():
	app = web.Application()
	app.router.add_get('/ws', websocket_handler)  # WebSocket升级端点
	app.router.add_route('*', '/{tail:.*}', http_handler)  # HTTP服务
	
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, Server_BaseURL, Server_Port)
	await site.start()
	
	print(f"Server started on {Server_BaseURL}:{Server_Port}")
	await asyncio.Future()

if __name__ == "__main__":
	asyncio.run(main())