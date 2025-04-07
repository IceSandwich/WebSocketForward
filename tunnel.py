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
	
	async def OnPreConnected(self) -> str:
		"""
		连接前可以在此函数判断是否需要取消连接，返回非空字符串取消连接。
		"""
		return ''

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
		直接发送包，raw应当是加密后的。这是一个底层api，请优先使用QueueSend。
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def MainLoop(self) -> None:
		"""
		主循环，连接远端，应当在另一线程或协程运行。
		MainLoop实现中需要对isConnected变量进行修改。
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

		self.controlSessionMap: typing.Dict[str, data.Transport] = {}
		self.controlSessionCondition = asyncio.Condition()

		# 最小堆，根据cur_idx排序
		self.streamHeap: typing.Dict[str, typing.List[data.Transport]] = {}
		self.streamCondition = asyncio.Condition()

	# ======================= 以下函数由用户实现 ============================
	
	async def OnRecvPackage(self, raw: data.Transport) -> None:
		"""
		在这里处理除了流包和控制包的数据包，默认将包推到Session()队列。
		如果你需要重写该函数并且希望Session()起作用，记得调用TunnelClient.OnRecvPackage()。
		"""
		async with self.sessionCondition:
			self.sessionMap[raw.seq_id] = raw
			self.sessionCondition.notify_all()
	
	async def OnRecvStreamPackage(self, raw: data.Transport) -> None:
		"""
		在这里处理流包，默认将流包推到Stream()队列。如果需要Stream()起作用，必须调用TunnelClient.OnRecvStreamPackage()。
		"""
		if raw.seq_id not in self.streamHeap:
			# self.GetLogger().error(f"{self.GetName()}] Stream subpackage {raw.seq_id} is not listening. Drop this package.")
			async with self.streamCondition:
				self.streamHeap[raw.seq_id] = []

		async with self.streamCondition:
			if raw.total_cnt == -1:
				self.GetLogger().debug(f"Stream <<<End {raw.seq_id} {len(raw.data)} bytes")
			else:
				self.GetLogger().debug(f"Stream {raw.seq_id} - {len(raw.data)} bytes")
			heapq.heappush(self.streamHeap[raw.seq_id], raw)
			if len(self.streamHeap[raw.seq_id]) > 20:
				self.GetLogger().warning(f"Stream {raw.seq_id} seems dead. Drop cache packages. headq: {[x.cur_idx for x in self.streamHeap[raw.seq_id]]}")
				del self.streamHeap[raw.seq_id]
			# print(f"Stream got {raw.seq_id} - {raw.cur_idx}-ith, current heaq: {[x.cur_idx for x in self.streamHeap[raw.seq_id]]}")
			self.streamCondition.notify_all()
	
	async def OnRecvCtrlPackage(self, raw: data.Transport) -> bool:
		"""
		返回False退出MainLoop。Ctrl包是没有加密的。默认将包推到SessionControl()队列。
		"""
		async with self.controlSessionCondition:
			self.controlSessionMap[raw.seq_id] = raw
			self.controlSessionCondition.notify_all()
		return True

	# ==================== 以下函数无需再实现 ==============================
	
	async def QueueSend(self, raw: data.Transport):
		"""
		缓存发包，当连接断开时暂时缓存包，等到再次连上时重新发送。
		"""
		if self.isConnected:
			try:
				await self.DirectSend(raw)
			except aiohttp.ClientConnectionResetError as e:
				# send failed, caching data
				self.GetLogger().error(f"{self.GetName()}] Disconnected while sending package. Cacheing package: {raw.seq_id}")
				await self.cacheQueue.put(raw)
			except Exception as e:
				# skip package for unknown reason
				self.GetLogger().error(f"{self.GetName()}] Unknown exception on QueueToSend(): {e}")
		else:
			# not connected, cacheing data
			self.GetLogger().error(f"{self.GetName()}] Not connected client. Cacheing package: {raw.seq_id}")
			await self.cacheQueue.put(raw)

	async def Session(self, raw: data.Transport):
		"""
		发送一个raw，等待回应一个raw。适合于非流的数据传输。
		注意：需要调用TunnelClient.OnRecvPackage()将包放到session队列中，该函数才起效。
		"""
		await self.QueueSend(raw)

		async with self.sessionCondition:
			await self.sessionCondition.wait_for(lambda: raw.seq_id in self.sessionMap)
			resp = self.sessionMap[raw.seq_id]
			del self.sessionMap[raw.seq_id]
			return resp

	async def SessionControl(self, raw: data.Control, uid: str):
		tp = data.Transport(data.TransportDataType.CONTROL, raw.ToProtobuf(), from_uid=uid, to_uid='')
		await self.DirectSend(tp)

		if raw.data_type in [data.ControlDataType.EXIT, data.ControlDataType.PRINT]:
			return # 没有回复的类型，应在OnRecvCtrlPackage()过滤

		async with self.controlSessionCondition:
			await self.controlSessionCondition.wait_for(lambda: tp.seq_id in self.controlSessionMap)
			resp = self.controlSessionMap[tp.seq_id]
			del self.controlSessionMap[tp.seq_id]
			return resp

	async def Stream(self, seq_id: str):
		# print(f"Listening stream on {seq_id}...")
		expectIdx = 1
		waitForSequencePkg = 0
		maxWaitSequences = 5
		while True:
			async with self.streamCondition:
				await self.streamCondition.wait_for(lambda: seq_id in self.streamHeap and len(self.streamHeap[seq_id]) > 0)
				if self.streamHeap[seq_id][0].cur_idx != expectIdx:
					if waitForSequencePkg == maxWaitSequences:
						self.GetLogger().error(f"Stream {seq_id} recv {maxWaitSequences}+ packages but not in sequences, maybe lost package. expect: {expectIdx}, heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}")
						# pop one anyway
					else:
						waitForSequencePkg = waitForSequencePkg + 1
						continue
				waitForSequencePkg = 1
				item = heapq.heappop(self.streamHeap[seq_id])
				expectIdx = expectIdx + 1
				# print(f"SSE pop stream {seq_id} - {item.cur_idx}")
				if item.total_cnt == -1:
					del self.streamHeap[seq_id]
			yield item
			if item.total_cnt == -1:
				break
		# print(f"End listening stream on {seq_id}...")
	
	async def resendCachePackages(self):
		"""
		重发因网络原因未发送的包，应当在重新连接上时立刻调用。
		"""
		self.GetLogger().info(f"{self.GetName()}] Resend {len(self.cacheQueue)} requests.")
		nowtime = time_utils.GetTimestamp()
		while self.IsConnected() and not self.cacheQueue.empty():
			item = await self.cacheQueue.get()
			if time_utils.WithInDuration(item.timestamp, nowtime, self.resendDuration):
				try:
					await self.DirectSend(item)
				except aiohttp.ClientConnectionResetError as e:
					if not self.IsConnected():
						self.GetLogger().error(f"{self.GetName()}] Failed to resend {item.seq_id} but luckily it's ClientConnectionResetError in disconnected mode so we plan to resent it next time: {e}")
						await self.cacheQueue.put(item) # put it back
						return # wait for next resend
					else:
						self.GetLogger().error(f"{self.GetName()}] Failed to resend {item.seq_id}, err: {e}")
						return # we have to drop this request
			else:
				self.GetLogger().warning(f"{self.GetName()}] Skip resending{item.seq_id} because the package is too old.")
	
	async def processPackage(self, raw: data.Transport) -> bool:
		"""
		处理收到的一切包。在此分流到OnRecvStreamPackage()、OnRecvCtrlPackage()和OnRecvPackage()。
		返回False退出MainLoop。
		"""
		if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE:
			await self.OnRecvStreamPackage(raw)
			return True
		elif raw.data_type == data.TransportDataType.CONTROL:
			return await self.OnRecvCtrlPackage(raw)
		
		await self.OnRecvPackage(raw)
		return True
		# if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE or raw.total_cnt == -1:
		# 	if raw.seq_id not in self.chunks:
		# 		assert(raw.total_cnt != -1)
		# 		self.chunks[raw.seq_id] = utils.Chunk(raw.total_cnt)
		# 	await self.chunks[raw.seq_id].Put(raw)

class TunnelServer(Callback):
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
		exitSignal = False
		while curTries < self.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=HttpUpgradedWebSocketHeaders, max_msg_size=WebSocketMaxMessageSize) as ws:
					if await self.OnPreConnected() != '':
						break
					self.isConnected = True
					self.handler = ws
					await self.OnConnected()
					curTries = 0 # reset tries
					if not self.cacheQueue.empty():
						await self.resendCachePackages()
					async for msg in self.handler:
						if msg.type == aiohttp.WSMsgType.ERROR:
							self.OnError(self.handler.exception())
						elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
							parsed = data.Transport.FromProtobuf(msg.data)
							if await self.processPackage(parsed) == False:
								exitSignal = True
								break
			self.isConnected = False
			if exitSignal == False:
				self.GetLogger().info(f"{self.GetName()}] Reconnecting({curTries}/{self.maxRetries})...")
				curTries += 1
			await self.OnDisconnected()
			if exitSignal:
				break
		bakHandler = self.handler
		self.handler = None
		return bakHandler

class HttpUpgradedWebSocketServer(TunnelServer):
	"""
	在服务器端使用的WebSocket隧道，服务器无需重连机制，支持重发包
	"""
	def __init__(self, cacheSize: int = 100, timeout: int = 10, **kwargs):
		self.ws: web.WebSocketResponse = None
		self.cacheQueue: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(cacheSize)
		self.timeout = timeout
		self.useCache = (cacheSize != 0)
		self.GetLogger().info(f"{self.GetName()}] Use cache queue.")
		super().__init__(**kwargs)

	async def MainLoop(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				self.OnError(self.ws.exception())
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = data.Transport.FromProtobuf(msg.data)
				await self.ProcessPackage(transport)

	async def DirectSend(self, raw: data.Transport):
		return await self.ws.send_bytes(raw.ToProtobuf())
	
	async def resend(self):
		if not self.useCache: return
		if self.cacheQueue.IsEmpty(): return

		items = await self.cacheQueue.PopAllElements()
		self.GetLogger().info(f"{self.GetName()}] Resend {len(items)} package.")
		nowtime = time_utils.GetTimestamp()
		for item in items:
			if time_utils.WithInDuration(item.timestamp, nowtime, self.timeout):
				await self.DirectSend(item)
				self.GetLogger().debug(f"{self.GetName()}] Sent {item.seq_id}")
			else:
				self.GetLogger().warning(f"{self.GetName()}] Drop resend package {item.seq_id} {data.TransportDataType.ToString(item.data_type)} due to outdated.")
	
	async def QueueSend(self, raw: data.Transport):
		if not self.useCache:
			return await self.DirectSend(raw)
		
		if self.IsConnected():
			try:
				await self.DirectSend(raw)
			except Exception as e:
				self.GetLogger().error(f"{self.GetName()}] Cannot send {raw.seq_id}, err: {e}. Put it in cache.")
				raw.RenewTimestamp() # use server's timestamp
				await self.cacheQueue.Add(raw)
		else:
			self.GetLogger().debug(f"{self.GetName()} is not connected. Queue {raw.seq_id} to send.")
			raw.RenewTimestamp()
			await self.cacheQueue.Add(raw)

	async def MainLoopWithRequest(self, request: web.BaseRequest):
		preconnected = await self.OnPreConnected()
		preconnected = preconnected.strip()
		if preconnected != '':
			return utils.ReponseText(preconnected, 403)
		
		self.ws = web.WebSocketResponse(max_msg_size=WebSocketMaxMessageSize)
		await self.ws.prepare(request)
		self.isConnected = True
		await self.OnConnected()
		await self.resend()
		try:
			await self.MainLoop()
		except Exception as ex:
			self.OnError(ex)
		self.isConnected = False
		await self.OnDisconnected()
		bakws = self.ws
		self.ws = None
		return bakws