import typing
import aiohttp
import aiohttp.web as web
import aiohttp.web_ws as web_ws

WS_MAX_MSG_SIZE = 12 * 1024 * 1024

class Callbacks:
	def __init__(self, name: str = "Websocket"):
		self.name = name
		self.ws: typing.Union[web.WebSocketResponse, None] = None

	def SetWebsocket(self, ws: web.WebSocketResponse):
		self.ws = ws
		
	def OnConnected(self):
		print(f"{self.name} connected.")
		
	def OnDisconnected(self):
		print(f"{self.name} disconnected.")
		
	def OnError(self, ex: typing.Union[None, BaseException]):
		print(f"{self.name} error: {ex}")
		
	async def OnProcess(self, msg: web_ws.WSMessage):
		pass

class Manager:
	def __init__(self, callbacks = Callbacks(), ws = web.WebSocketResponse(max_msg_size=WS_MAX_MSG_SIZE)):
		self.ws = ws
		self.isConnected = False
		self.callbacks = callbacks
		
	def IsClosed(self):
		return self.ws.closed
	
	async def MainLoop(self):
		self.callbacks.SetWebsocket(self.ws)
		async for msg in self.ws:
				if msg.type == aiohttp.WSMsgType.ERROR:
					self.callbacks.OnError(self.ws.exception())
				elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
					await self.callbacks.OnProcess(msg)

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		self.callbacks.SetWebsocket(self.ws)
		await self.ws.prepare(request)
		self.isConnected = True
		self.callbacks.OnConnected()

		try:
			await self.MainLoop()
		finally:
			self.isConnected = False
			self.callbacks.OnDisconnected()

		return self.ws

class OnceHandler:
	def __init__(self, name: str):
		self.name = name
		self.client: typing.Union[None, Manager] = None
		self.callbacks = Callbacks()

	def SetCallbacks(self, callbacks: Callbacks):
		self.callbacks = callbacks
		disConnected = self.callbacks.OnDisconnected
		def hookDisconnected():
			disConnected()
			self.client = None
		self.callbacks.OnDisconnected = hookDisconnected

	def IsConnected(self):
		return self.client is not None

	def GetHandler(self):
		return self.client.ws
		
	async def Handler(self, request: web.BaseRequest):
		if self.client is not None:
			print(f"{self.name} has already connected.")
			return None
		
		self.client = Manager(self.callbacks)
		return await self.client.MainLoopOnRequest(request)

async def ConnectToServer(server: str, callback):
    # 建立WebSocket连接
	headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	async with aiohttp.ClientSession() as session:
		async with session.ws_connect(server, headers=headers, max_msg_size=WS_MAX_MSG_SIZE) as ws:
			print("Connected to server")
			await callback(ws)