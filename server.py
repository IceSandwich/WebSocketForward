import asyncio
import aiohttp
from aiohttp import web
from data import SerializableRequest, SerializableResponse
import config
import typing

Server_BaseURL = "127.0.0.1"
Server_Port = 8030

class WebSocketManager:
	def __init__(self) -> None:
		self.ws = web.WebSocketResponse(max_msg_size=config.WS_MAX_MSG_SIZE)
		self.isConnected = False
		self.onConnected: typing.Callable[[]] = None
		self.onDisconnected: typing.Callable[[]] = None
		self.onError: typing.Callable[[BaseException]] = None

		self.recvData: bytes = None
		self.recvEvent = asyncio.Event()

	def IsClosed(self):
		return self.ws.closed
		
	async def Recv(self):
		# print("Listening...")
		await self.recvEvent.wait()
		# print("Recv data....", self.recvData)
		return self.recvData

	async def Send(self, data: bytes):
		return await self.ws.send_bytes(data)

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
					# print("Got data...")
					self.recvData = msg.data
					self.recvEvent.set()
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
		
	# 将HTTP请求转发给客户端
	async with aiohttp.ClientSession() as session:
		# 提取请求的相关信息
		request_data = SerializableRequest(str(request.url.path_qs), request.method, dict(request.headers))

		await client.Send(request_data.to_protobuf())
		recv = await client.Recv()

		response = SerializableResponse.from_protobuf(recv)
		return web.Response(
			status=response.status_code,
			headers=response.headers,
			body=response.body
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