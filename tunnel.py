import typing, abc, logging, queue, heapq
import asyncio, aiohttp
import time_utils, data, encrypt, utils
import aiohttp.web as web

tunlog = logging.getLogger(__name__)
utils.SetupLogging(tunlog, "tunnel", saveInFile=False)

class Tunnel(abc.ABC):
	"""
	支持重连机制的隧道
	"""
	def __init__(self, name: str = "Tunnel", maxRetries: int = 3, resendDuration: int = time_utils.Seconds(10), log: logging.Logger = tunlog):
		self.name = name
		self.maxRetries = maxRetries
		self.resendDuration = resendDuration
		self.isConnected = False
		self.log = log

	def IsConnected(self) -> bool:
		return self.isConnected
	
	async def OnPreConnected(self) -> bool:
		"""
		连接前可以在此函数判断是否需要取消连接，返回False取消连接。
		"""
		return True

	async def OnConnected(self):
		self.log.info(f"{self.name}] Connected.")
		self.isConnected = True
		
	async def OnDisconnected(self):
		self.log.info(f"{self.name}] Disconnected.")
		self.isConnected = False
		
	def OnError(self, ex: typing.Union[None, BaseException]):
		self.log.info(f"{self.name}] Error: {ex}")

	async def OnProcess(self, raw: data.Transport):
		"""
		在此处理你的数据包
		"""
		pass

	@abc.abstractmethod
	async def DirectSend(self, raw: data.Transport):
		"""
		直接发送包，raw应当是加密后的。
		"""
		raise NotImplementedError()

	@abc.abstractmethod
	async def MainLoop(self):
		raise NotImplementedError()
	
class TunnelClient(Tunnel):
	"""
	在客户端使用的隧道，支持重连机制和重发机制，支持分包接收
	"""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)

		# 管理分包传输，key为seq_id
		self.chunks: typing.Dict[str, utils.Chunk] = {}

		# 用于重发的队列，保存的是加密后的包
		self.sendQueue: asyncio.Queue[data.Transport] = asyncio.Queue()

		self.sessionMap: typing.Dict[str, data.Transport] = {}
		self.sessionCondition = asyncio.Condition()

		self.streamHeap: typing.Dict[str, typing.List[data.Transport]] = {}
		self.streamCondition = asyncio.Condition()

	async def Session(self, raw: data.Transport):
		"""
		发送一个raw，等待回应一个raw。适合于非流的数据传输。
		注意：需要在OnRecvPackage()调用putSession()将包放到session中该函数才起效。
		"""
		async with self.sessionCondition:
			self.sessionMap[raw.seq_id] = None
		await self.QueueSendSmartSegments(raw)

		async with self.sessionCondition:
			await self.sessionCondition.wait_for(lambda: self.sessionMap[raw.seq_id] is not None)
			resp = self.sessionMap[raw.seq_id]
			del self.sessionMap[raw.seq_id]
			return resp
		
	async def Stream(self, seq_id: str):
		"""
		注意：需要在OnRecvStreamPackage()调用putStream()将包放到stream中该函数才起效。
		"""
		async with self.streamCondition:
			self.streamHeap[seq_id] = []

		for i in range(1, 999):
			async with self.streamCondition:
				await self.streamCondition.wait_for(lambda: len(self.streamHeap[seq_id]) > 0 or self.streamHeap[seq_id][0].cur_idx == i)
				item = heapq.heappop(self.streamHeap[seq_id])
				if item.IsEndPackage():
					del self.streamHeap[seq_id]
			if item.IsEndPackage():
				yield item
				return
			yield item

	async def putSession(self, raw: data.Transport):
		async with self.sessionCondition:
			self.sessionMap[raw.seq_id] = raw
			self.sessionCondition.notify_all()

	async def putStream(self, raw: data.Transport):
		if raw.seq_id not in self.streamHeap:
			self.log.error(f"{self.name}] Stream subpackage {raw.seq_id} is not listening. Drop this package.")
		else:
			async with self.streamCondition:
				heapq.heappush(self.streamHeap[raw.seq_id], raw)
				self.streamCondition.notify_all()

	@abc.abstractmethod
	async def OnRecvStreamPackage(self, raw: data.Transport):
		"""
		仅处理流包，raw必然是STREAM_SUBPACKAGE
		"""
		raise NotImplementedError

	@abc.abstractmethod
	async def OnRecvPackage(self, raw: data.Transport):
		"""
		处理包，不包括流包，如果是多包将合并再调用该函数。
		"""
		raise NotImplementedError

	async def OnProcess(self, raw: data.Transport):
		if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE:
			return await self.OnRecvStreamPackage(raw)
		elif raw.data_type == data.TransportDataType.SUBPACKAGE or raw.total_cnt == -1:
			"""
			分包规则；前n-1个包为subpackage，最后一个包为resp、req。
			返回None表示Segments还没收全。若收全了返回合并的data.Transport对象。
			单包或者流包直接返回。
			"""
			invalidEndPackage = raw.seq_id not in self.chunks and raw.total_cnt <= 0
			if invalidEndPackage:
				self.log.error(f"{self.name}] Invalid end subpackage {raw.seq_id} not in chunk but has total_cnt {raw.total_cnt}")
			else:
				if raw.seq_id not in self.chunks:
					self.chunks[raw.seq_id] = utils.Chunk(raw.total_cnt)
				await self.chunks[raw.seq_id].Put(raw)
				if not self.chunks[raw.seq_id].IsFinish(): return
				ret = self.chunks[raw.seq_id].Combine()
				self.log.debug(f"Combined {self.chunks[raw.seq_id].total_cnt} packages into one {raw.seq_id} {data.TransportDataType.ToString(raw.data_type)}.")
				del self.chunks[raw.seq_id]
				raw = ret

		await self.OnRecvPackage(raw)

	async def QueueSendSmartSegments(self, raw: data.Transport):
		"""
		自动判断是否需要分包，若需要则分包发送，否则单独发送。raw应该是加密后的包。
		多个Subpackage，先发送Subpackage，然后最后一个发送源data_type。
		分包不支持流包的分包。
		"""
		if raw.IsStreamPackage():
			raise Exception("Stream package cannot use in QueueSendSmartSegments(), please use QueueToSend() instead.")
		
		splitDatas: typing.List[bytes] = [ raw.data[i: i + WebSocketTunnelBase.SafeMaxMsgSize] for i in range(0, len(raw.data), WebSocketTunnelBase.SafeMaxMsgSize) ]
		if len(splitDatas) == 1: #包比较小，单独发送即可
			raw.MarkAsSinglePackage()
			return await self.QueueToSend(raw)
		self.log.debug(f"Transport {raw.seq_id} is too large({len(raw.data)}), will be split into {len(splitDatas)} packages to send.")

		subpackageRaw = raw.CloneWithSameSeqID()
		for i, item in enumerate(splitDatas[:-1]):
			subpackageRaw.ResetData(item, data.TransportDataType.SUBPACKAGE)
			subpackageRaw.SetPackages(total = len(splitDatas), cur = i)
			await self.QueueToSend(subpackageRaw)
		
		raw.ResetData(splitDatas[-1])
		raw.SetPackages(total = data.Transport.END_PACKAGE, cur = len(splitDatas) - 1)
		await self.QueueToSend(raw)

	async def resend(self):
		self.log.info(f"{self.name}] Resend {len(self.sendQueue)} requests.")
		nowtime = time_utils.GetTimestamp()
		while self.IsConnected() and not self.sendQueue.empty():
			item = await self.sendQueue.get()
			if time_utils.WithInDuration(item.timestamp, nowtime, self.resendDuration):
				try:
					await self.DirectSend(item)
				except aiohttp.ClientConnectionResetError as e:
					if not self.IsConnected():
						self.log.error(f"{self.name}] Failed to resend {item.seq_id} but luckily it's ClientConnectionResetError in disconnected mode so we plan to resent it next time: {e}")
						await self.sendQueue.put(item) # put it back
						return # wait for next resend
					else:
						self.log.error(f"{self.name}] Failed to resend {item.seq_id}, err: {e}")
						return # we have to drop this request
			else:
				self.log.warning(f"{self.name}] Skip resending{item.seq_id} because the package is too old.")

	async def QueueToSend(self, raw: data.Transport):
		"""
		加入发送队列，当发送失败会尝试重发。raw应当是已经加密的数据包。
		"""
		if self.isConnected:
			try:
				await self.DirectSend(raw)
			except aiohttp.ClientConnectionResetError:
				# send failed, caching data
				self.log.debug(f"{self.name}] Cache {raw.seq_id} due to aiohttp.ClientConnectionResetError")
				raw.RenewTimestamp() # use local timestamp
				await self.sendQueue.put(raw)
			except Exception as e:
				# drop package for unknown reason
				self.log.error(f"{self.name}] Unknown exception on QueueToSend({raw.seq_id}): {e}")
		else:
			# not connected, cacheing data
			raw.RenewTimestamp() # use local timestamp
			await self.sendQueue.put(raw)

	def checkTransportDataType(self, raw: data.Transport, expect: typing.List[data.TransportDataType]):
		if raw.data_type not in expect:
			self.log.error(f"{self.name}] Recv package({data.TransportDataType.ToString(raw.data_type)}) should be in [{', '.join([data.TransportDataType.ToString(x) for x in expect])}]")
			return False
		return True
	
class WebSocketTunnelBase(Tunnel):
	Headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	MaxMessageSize = 12 * 1024 * 1024 # 12MB
	SafeMaxMsgSize = 10 * 1024 * 1024 # 10MB

	def __init__(self, **kwargs):
		super().__init__(**kwargs)

class WebSocketTunnelServer(WebSocketTunnelBase):
	"""
	在服务器端使用的WebSocket隧道，因为服务器无需重连机制，也无需重发包
	"""
	def __init__(self, name="WebSocket Server", **kwargs):
		super().__init__(name=name, **kwargs)
		self.ws = web.WebSocketResponse(max_msg_size=WebSocketTunnelBase.MaxMessageSize)

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
				await self.OnProcess(transport)

	async def DirectSend(self, raw: data.Transport):
		return await self.ws.send_bytes(raw.ToProtobuf())

def WebSocketTunnelServerHandler(cls: typing.Type[WebSocketTunnelServer], **kwargs):
	async def mainloop(request: web.BaseRequest):
		instance = cls(**kwargs)
		if await instance.OnPreConnected() == False:
			return None
		await instance.Prepare(request)
		await instance.OnConnected()
		try:
			await instance.MainLoop()
		except Exception as ex:
			instance.OnError(ex)
		await instance.OnDisconnected()
		return instance.GetHandler()
	return mainloop

class WebSocketTunnelClient(TunnelClient):
	"""
	在客户端使用的Websocket隧道，支持重连机制和重发机制，支持分包接收
	"""
	def __init__(self, url:str, name="WebSocket Client", **kwargs):
		super().__init__(name=name, **kwargs)
		self.url = url
		
		self.handler: typing.Union[None, aiohttp.ClientWebSocketResponse] = None

		self.timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_read=60, sock_connect=60)
		# self.session = aiohttp.ClientSession()

	async def DirectSend(self, raw: data.Transport):
		await self.handler.send_bytes(raw.ToProtobuf())

	async def MainLoop(self):
		curTries = 0
		while curTries < self.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=WebSocketTunnelBase.Headers, max_msg_size=WebSocketTunnelBase.MaxMessageSize) as ws:
					self.handler = ws
					if await self.OnPreConnected() == False:
						break
					await self.OnConnected()
					curTries = 0 # reset tries
					if not self.sendQueue.empty():
						asyncio.create_task(self.resend())
					async for msg in self.handler:
						if msg.type == aiohttp.WSMsgType.ERROR:
							self.OnError(self.handler.exception())
						elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
							parsed = data.Transport.FromProtobuf(msg.data)
							await self.OnProcess(parsed)
			curTries += 1
			self.log.info(f"{self.name}] Reconnecting({curTries}/{self.maxRetries})...")
			await self.OnDisconnected()
		bakHandler = self.handler
		self.handler = None
		return bakHandler