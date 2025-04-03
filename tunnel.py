import typing, abc, logging, queue, heapq
import asyncio, aiohttp
import time_utils, data, encrypt, utils
import aiohttp.web as web

class Callback(abc.ABC):
	def __init__(self):
		self.isConnected = False

	def IsConnected(self) -> bool:
		return self.isConnected

	# =================== 以下函数由用户实现 ==========================

	@abc.abstractmethod
	def GetName(self) -> str:
		raise NotImplementedError()
	
	@abc.abstractmethod
	def GetLogger(self) -> logging.Logger:
		raise NotImplementedError()
	
	async def OnPreConnected(self) -> bool:
		"""
		连接前可以在此函数判断是否需要取消连接，返回False取消连接。
		"""
		return True

	async def OnConnected(self):
		self.GetLogger().info(f"{self.GetName()}] Connected.")
		
	async def OnDisconnected(self):
		self.GetLogger().info(f"{self.GetName()}] Disconnected.")
		
	def OnError(self, ex: typing.Union[None, BaseException]):
		self.GetLogger().info(f"{self.GetName()}] Error: {ex}")

	# ================== 以下函数由隧道实现 ==========================

	@abc.abstractmethod
	def DirectSend(self, raw: data.Transport) -> None:
		"""
		直接发送包，raw应当是加密后的。
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def MainLoop(self) -> None:
		"""
		主循环，连接远端，应当在另一线程或协程运行。
		"""
		raise NotImplementedError()
	
class TunnelClient(Callback):
	"""
	在客户端使用的隧道，支持重连机制和重发机制，支持分包接收
	"""

	def __init__(self, maxRetries: int = 3, resendDuration: int = time_utils.Seconds(10)):
		super().__init__()

		self.maxRetries = maxRetries
		self.resendDuration = resendDuration

		# 管理分包传输，key为seq_id
		self.chunks: typing.Dict[str, utils.Chunk] = {}

		# 用于重发的队列，保存的是加密后的包
		self.cacheQueue: asyncio.Queue[data.Transport] = asyncio.Queue()

		self.sessionMap: typing.Dict[str, data.Transport] = {}
		self.sessionCondition = asyncio.Condition()

		self.streamHeap: typing.Dict[str, typing.List[data.Transport]] = {}
		self.streamCondition = asyncio.Condition()


		self.sendQueue: asyncio.Queue[data.Transport] = asyncio.Queue()

	# ======================= 以下函数由用户实现 ============================
	
	@abc.abstractmethod
	async def OnRecvPackage(self, raw: data.Transport) -> None:
		"""
		在这里处理除了流包
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def OnRecvStreamPackage(self, raw: data.Transport) -> None:
		"""
		在这里处理流包
		"""
		raise NotImplementedError()
	
	# ==================== 以下函数无需再实现 ==============================
	
	async def resendCachePackages(self):
		"""
		重发因网络原因未发送的包，应当在重新连接上时立刻调用。
		"""
		self.GetLogger().info(f"{self.GetName()}] Resend {len(self.sendQueue)} requests.")
		nowtime = time_utils.GetTimestamp()
		while self.IsConnected() and not self.sendQueue.empty():
			item = await self.sendQueue.get()
			if time_utils.WithInDuration(item.timestamp, nowtime, self.resendDuration):
				try:
					await self.DirectSend(item)
				except aiohttp.ClientConnectionResetError as e:
					if not self.IsConnected():
						self.GetLogger().error(f"{self.GetName()}] Failed to resend {item.seq_id} but luckily it's ClientConnectionResetError in disconnected mode so we plan to resent it next time: {e}")
						await self.sendQueue.put(item) # put it back
						return # wait for next resend
					else:
						self.GetLogger().error(f"{self.GetName()}] Failed to resend {item.seq_id}, err: {e}")
						return # we have to drop this request
			else:
				self.GetLogger().warning(f"{self.GetName()}] Skip resending{item.seq_id} because the package is too old.")
	
	async def processPackage(self, raw: data.Transport) -> None:
		"""
		处理收到的一切包。在此分流到OnRecvStreamPackage()和OnRecvPackage()。
		"""
		if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE:
			return await self.OnRecvStreamPackage(raw)
		
		await self.OnRecvPackage(raw)
		# if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE or raw.total_cnt == -1:
		# 	if raw.seq_id not in self.chunks:
		# 		assert(raw.total_cnt != -1)
		# 		self.chunks[raw.seq_id] = utils.Chunk(raw.total_cnt)
		# 	await self.chunks[raw.seq_id].Put(raw)

class TunnelServer(Callback):
	@abc.abstractmethod
	async def Prepare(self, request: web.BaseRequest) -> None:
		"""
		为request准备回应
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	def GetHandler(self) -> typing.Any:
		"""
		获取句柄
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def ProcessPackage(self, raw: data.Transport) -> None:
		"""
		处理收到的一切包。
		"""

HttpUpgradedWebSocketHeaders = {"Upgrade": "websocket", "Connection": "Upgrade"}
WebSocketMaxMessageSize = 12 * 1024 * 1024 # 12MB
WebSocketSafeMaxMsgSize = 10 * 1024 * 1024 # 10MB

class HttpUpgradedWebSocketClient(TunnelClient):
	"""
	在客户端使用的Websocket隧道，支持重连机制和重发机制，支持分包接收
	"""
	def __init__(self, url: str, **kwargs):
		super().__init__(**kwargs)

		self.url = url
		self.handler: typing.Optional[aiohttp.ClientWebSocketResponse] = None

	async def DirectSend(self, raw: data.Transport):
		await self.handler.send_bytes(raw.ToProtobuf())

	async def MainLoop(self):
		curTries = 0
		while curTries < self.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=HttpUpgradedWebSocketHeaders, max_msg_size=WebSocketMaxMessageSize) as ws:
					if await self.OnPreConnected() == False:
						break
					self.isConnected = True
					self.handler = ws
					await self.OnConnected()
					curTries = 0 # reset tries
					if not self.cacheQueue.empty():
						asyncio.create_task(self.resendCachePackages())
					async for msg in self.handler:
						if msg.type == aiohttp.WSMsgType.ERROR:
							self.OnError(self.handler.exception())
						elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
							parsed = data.Transport.FromProtobuf(msg.data)
							await self.processPackage(parsed)
			curTries += 1
			self.GetLogger().info(f"{self.GetName()}] Reconnecting({curTries}/{self.maxRetries})...")
			self.isConnected = False
			await self.OnDisconnected()
		bakHandler = self.handler
		self.handler = None
		return bakHandler
	
class HttpUpgradedWebSocketServer(TunnelServer):
	"""
	在服务器端使用的WebSocket隧道，因为服务器无需重连机制，也无需重发包
	"""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.ws = web.WebSocketResponse(max_msg_size=WebSocketMaxMessageSize)

	async def Prepare(self, request: web.BaseRequest):
		await self.ws.prepare(request)

	def GetHandler(self):
		return self.ws

	async def MainLoop(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				self.OnError(self.ws.exception())
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = data.Transport.FromProtobuf(msg.data)
				await self.ProcessPackage(transport)

	async def DirectSend(self, raw: data.Transport):
		return await self.ws.send_bytes(raw.ToProtobuf())

def HttpUpgradedWebSocketServerHandler(cls: typing.Type[HttpUpgradedWebSocketServer], **kwargs):
	async def mainloop(request: web.BaseRequest):
		instance = cls(**kwargs)
		if await instance.OnPreConnected() == False:
			return None
		await instance.Prepare(request)
		instance.isConnected = True
		await instance.OnConnected()
		try:
			await instance.MainLoop()
		except Exception as ex:
			instance.OnError(ex)
		instance.isConnected = False
		await instance.OnDisconnected()
		return instance.GetHandler()
	return mainloop