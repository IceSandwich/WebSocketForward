import aiohttp
import aiohttp.web_ws
import typing
import abc
import asyncio
import time_utils
import data
import aiohttp.web as web
import encrypt

class Tunnel(abc.ABC):
	"""
	支持重连机制的隧道
	"""
	def __init__(self, name: str = "Tunnel", maxRetries: int = 3, resendDuration: int = time_utils.Minutes(5)):
		self.name = name
		self.maxRetries = maxRetries
		self.resendDuration = resendDuration
		self.isConnected = False
		self.send_queue: asyncio.Queue[data.Transport] = asyncio.Queue()

	def IsConnected(self) -> bool:
		return self.isConnected

	async def OnConnected(self) -> bool:
		"""
		返回False取消连接
		"""
		print(f"{self.name}] Connected.")
		self.isConnected = True
		return True
		
	async def OnDisconnected(self):
		print(f"{self.name}] Disconnected.")
		self.isConnected = False
		
	def OnError(self, ex: typing.Union[None, BaseException]):
		print(f"{self.name}] Error: {ex}")

	async def OnProcess(self, raw: data.Transport):
		pass

	@abc.abstractmethod
	async def QueueToSend(self, raw: data.Transport):
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def MainLoop(self):
		raise NotImplementedError()
	
class WebSocketTunnelBase(Tunnel):
	Headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	MaxMessageSize = 12 * 1024 * 1024 # 12MB
	SafeMaxMsgSize = 10 * 1024 * 1024 # 10MB

	def __init__(self, **kwargs):
		super().__init__(**kwargs)

class WebSocketTunnelServer(WebSocketTunnelBase):
	def __init__(self, name="WebSocket Server", **kwargs):
		super().__init__(name=name, **kwargs)
		self.ws = web.WebSocketResponse(max_msg_size=WebSocketTunnelBase.MaxMessageSize)

	async def MainLoop(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				self.OnError(self.ws.exception())
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = data.Transport.FromProtobuf(msg.data)
				await self.OnProcess(transport)

	async def QueueToSend(self, raw: data.Transport):
		await self.ws.send_bytes(raw.ToProtobuf())

def WebSocketTunnelServerHandler(cls: typing.Type[WebSocketTunnelServer]):
    async def mainloop(request: web.BaseRequest):
        instance = cls()
        if await instance.OnConnected() == False:
            return None
        await instance.ws.prepare(request)
        try:
            await instance.MainLoop()
        except Exception as ex:
            instance.OnError(ex)
        await instance.OnDisconnected()
        return instance.ws
    return mainloop

class Chunk:
	def __init__(self, total_cnt: int):
		self.total_cnt = total_cnt
		self.data: typing.List[bytes] = [None] * total_cnt
		self.cur_idx = 0
		self.template: data.Transport = None

	def IsFinish(self):
		return self.cur_idx >= self.total_cnt

	def Put(self, raw: typing.Union[data.Transport]):
		if raw.data_type not in [data.TransportDataType.SUBPACKAGE, data.TransportDataType.RESPONSE, data.TransportDataType.REQUEST]:
			raise Exception("unknown type")
		if self.IsFinish():
			raise Exception("already full.")
		
		self.data[raw.cur_idx] = raw.body
		if raw.data_type != data.TransportDataType.SUBPACKAGE:
			self.template = raw
		
		self.cur_idx = self.cur_idx + 1
	
	def Combine(self):
		raw = b''.join(self.data)
		self.template.body = raw
		return self.req

class WebSocketTunnelClient(WebSocketTunnelBase):
	def __init__(self, url:str, name="WebSocket Client", **kwargs):
		super().__init__(name=name, **kwargs)
		self.url = url
		
		self.handler: typing.Union[None, aiohttp.ClientWebSocketResponse] = None

		self.timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_read=60, sock_connect=60)
		# self.session = aiohttp.ClientSession()

		self.chunks: typing.Dict[str, Chunk] = {}
		
	async def resend(self):
		nowtime = time_utils.GetTimestamp()
		while self.IsConnected() and not self.send_queue.empty():
			item = await self.send_queue.get()
			if time_utils.WithInDuration(item.timestamp, nowtime, self.resendDuration):
				try:
					await self.handler.send_bytes(item.ToProtobuf())
				except aiohttp.ClientConnectionResetError:
					await self.send_queue.put(item) # put it back
					return # wait for next resend
			else:
				print(f"{self.name}] Skip resending a package because the package is toooo old.")

	async def ReadSegments(self, raw: data.Transport):
		"""
		返回一个元组[Bool, Object]。
		返回True表示已经完成整个segments的读取或者是raw不是Subpackage类型。
		若为前者，返回合并后的data.Transport对象。
		"""
		if raw.data_type != data.TransportDataType.SUBPACKAGE: return True, None
		if raw.seq_id not in self.chunks:
			self.chunks[raw.seq_id] = Chunk(raw.total_cnt)
		self.chunks[raw.seq_id].Put(raw)
		if not self.chunks[raw.seq_id].IsFinish():
			return False, None
		raw = self.chunks[raw.seq_id].Combine()
		del self.chunks[raw.seq_id]
		return True, raw

	async def QueueSendSegments(self, raw: data.Transport, template: typing.Union[data.Response, data.Request], buffer: typing.Union[None, bytes], cipher: encrypt.Cipher = None):
		"""
		先发送Subpackage，然后最后发送Response/Request。buffer应当是未加密的数据。
		"""
		splitDatas: typing.List[bytes] = [] if buffer is None else [ buffer[i: i + WebSocketTunnelBase.SafeMaxMsgSize] for i in range(0, len(buffer), WebSocketTunnelBase.SafeMaxMsgSize) ]
		data_type = data.TransportDataType.RESPONSE if type(template) == data.Response else data.TransportDataType.REQUEST
		if len(splitDatas) <= 1:
			# 只有一个Subpackage，直接发送即可
			template.body = buffer
			raw.RenewTimestamp()
			raw.data_type = data_type
			raw.data = template.ToProtobuf()
			if cipher is not None:
				raw.data = cipher.Encrypt(raw.data)
			await self.QueueToSend(raw)
		else:
			# 多个Subpackage，先发送Subpackage，然后最后一个发送Response/Request
			raw.total_cnt = len(splitDatas) # 总共需要发送的次数
			raw.data_type = data.TransportDataType.SUBPACKAGE
			for i, item in enumerate(splitDatas[:-1]):
				raw.RenewTimestamp()
				raw.cur_idx = i
				raw.data = item
				if cipher is not None:
					raw.data = cipher.Encrypt(raw.data)
				await self.QueueToSend(raw)
			
			raw.RenewTimestamp()
			raw.cur_idx = len(splitDatas) - 1
			raw.data_type = data_type # 最后一个的类型为Response/Request
			template.body = splitDatas[-1]
			raw.data = template.ToProtobuf()
			if cipher is not None:
				raw.data = cipher.Encrypt(raw.data)
			await self.QueueToSend(raw)

	async def QueueToSend(self, raw: data.Transport):
		if self.isConnected:
			try:
				await self.handler.send_bytes(raw.ToProtobuf())
			except aiohttp.ClientConnectionResetError:
				# send failed, caching data
				await self.send_queue.put(raw)
			except Exception as e:
				# skip package for unknown reason
				print(f"{self.name}] Unknown exception on QueueToSend(): {e}")
		else:
			# not connected, cacheing data
			await self.send_queue.put(raw)
			
	async def MainLoop(self):
		curTries = 0
		while curTries < self.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=WebSocketTunnelBase.Headers, max_msg_size=WebSocketTunnelBase.MaxMessageSize) as ws:
					self.handler = ws
					if await self.OnConnected() == False:
						break
					curTries = 0
					if not self.send_queue.empty():
						print(f"{self.name}] Resend {len(self.send_queue)} requests.")
						asyncio.create_task(self.resend())
					async for msg in self.handler:
						if msg.type == aiohttp.WSMsgType.ERROR:
							self.OnError(self.handler.exception())
						elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
							parsed = data.Transport.FromProtobuf(msg.data)
							await self.OnProcess(parsed)
			curTries += 1
			print(f"{self.name}] Reconnecting({curTries}/{self.maxRetries})...")
			await self.OnDisconnected()
		bakHandler = self.handler
		self.handler = None
		return bakHandler