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
	def QueueSend(self, raw: data.Transport) -> None:
		"""
		缓存发包，当连接断开时暂时缓存包，等到再次连上时重新发送。隧道自动处理丢包和重发包的情况。raw应当是加密后的。
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def MainLoop(self) -> None:
		"""
		主循环，连接远端，应当在另一线程或协程运行。
		MainLoop实现中需要对isConnected变量进行修改。
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def OnProcess(self, raw: data.Transport) -> bool:
		"""
		处理收到的一切包。在此分流到`DispatchXXX()`处理器。
		返回False退出MainLoop。
		"""
		raise NotImplementedError()
	
class ControlDispatcher(Callback):
	async def DispatchCtrlPackage(self, raw: data.Transport) -> bool:
		"""
		从`OnProcess()`分流出来的处理器。在这里处理收到的控制包，默认作为分流器到`OnCtrlXXX()`处理器。
		返回False退出MainLoop。Ctrl包是没有加密的。
		"""
		pkg = data.Control.FromProtobuf(raw.data)
		self.GetLogger().debug(f"{self.GetName()}] Received control package {raw.seq_id} - {data.ControlDataType.ToString(pkg.data_type)}")
		if pkg.data_type == data.ControlDataType.PRINT:
			printCtrl = data.PrintControlMsg.From(pkg.msg)
			self.GetLogger().info(f"{printCtrl.fromWho}] {printCtrl.message}")
		elif pkg.data_type == data.ControlDataType.EXIT:
			return await self.OnCtrlExit(raw)
		elif pkg.data_type == data.ControlDataType.QUERY_CLIENTS:
			await self.OnCtrlQueryClients(raw)
		elif pkg.data_type == data.ControlDataType.RETRIEVE_PKG:
			parsed = data.RetrieveControlMsg.From(pkg.msg)
			await self.OnCtrlRetrievePackage(raw, parsed)
		elif pkg.data_type == data.ControlDataType.QUERY_SENDS:
			await self.OnCtrlQuerySends(raw)
		else:
			self.GetLogger().error(f"{self.GetName()}] Received unkown control package {raw.seq_id} - {data.ControlDataType.ToString(pkg.data_type)}")
			return True
		return True

	# ====================== 控制包处理器  ===============================

	async def OnCtrlExit(self, raw: data.Transport, ctrl: data.Control) -> bool:
		"""
		处理Exit控制包。对于服务器，该函数应该转发该控制包。对于终端，该函数应当返回False退出MainLoop。
		"""
		self.GetLogger().error(f"{self.GetName()}] Doesn't implement OnCtrlExit() but receive a EXIT control package.")
	
	async def OnCtrlQueryClients(self, raw: data.Transport):
		"""
		处理QueryClients控制包。通常在服务器中使用该函数，获取服务器中活动的所有连接。
		本控制包应当有回复。
		"""
		self.GetLogger().error(f"{self.GetName()}] Doesn't implement OnCtrlQueryClients() but receive a QUERY_CLIENTS control package.")

	async def OnCtrlRetrievePackage(self, raw:data.Transport, retrieve: data.RetrieveControlMsg):
		"""
		处理RetrievePackage控制包。由于网络原因要求重发数据包。
		若服务器有缓存，由服务器回复。若没有缓存，交由终端回复。
		回复不能是RetrievePackage类型。
		"""
		self.GetLogger().error(f"{self.GetName()}] Doesn't implement OnCtrlRetrievePackage() but receive a RETRIEVE_PKG control package.")
	
	async def OnCtrlQuerySends(self, raw: data.Transport):
		"""
		处理QuerySends控制包。获取本端关于远端发送的所有包。通常在服务器中使用该函数。
		本控制包应当有回复。
		"""
		self.GetLogger().error(f"{self.GetName()}] Doesn't implement OnCtrlQuerySends() but receive a QUERY_SENDS control package.")

class TunnelClientDispatcher(ControlDispatcher):

	# ================= 从 `OnProcess()` 分流处理的处理器 ======================

	async def DispatchStreamPackage(self, raw: data.Transport) -> None:
		"""
		从`OnProcess()`分流出来的处理器。在这里处理所有流包，默认调用`putToStreamQueue()`将包放入到`Stream()`队列。
		如果你需要重写该函数并且希望Stream()起作用，记得调用`putToStreamQueue()`。
		"""
		return await self.putToStreamQueue(raw)
	

	async def DispatchOtherPackage(self, raw: data.Transport) -> None:
		"""
		从`OnProcess()`分流出来的处理器。在这里处理除了流包和控制包的数据包，默认调用`putToSessionQueue()`将包放入到`Session()`队列
		如果你需要重写该函数并且希望Session()起作用，记得调用`putToSessionQueue()`。
		"""
		return await self.putToSessionQueue(raw)
	
	@abc.abstractmethod
	async def DispatchSubpackage(self, raw: data.Transport) -> typing.Optional[data.Transport]:
		"""
		从`OnProcess()`分流出来的处理器。在这里处理Subpackage以及它的最后一个包。
		返回None表示分包还没收完，本轮对raw的处理应当就此终止。若已经收完，返回重组后的Transport。
		"""
		raise NotImplementedError()

	# ====================    处理器接口    ==============================

	@abc.abstractmethod
	def putToStreamQueue(self, raw: data.Transport):
		"""
		将流包推到Stream()队列。若需要Stream()生效，必须在`DispatchStreamPackage()`调用本函数。
		"""
		raise NotImplementedError()

	@abc.abstractmethod
	async def putToSessionQueue(self, raw: data.Transport):
		"""
		将包推到Session()队列。若需要Session()生效，必须在`DispatchOtherPackage()`调用本函数。
		"""
		raise NotImplementedError()

	@abc.abstractmethod
	async def putToSessionCtrlQueue(self, raw:data.Transport):
		"""
		将包推到SessionControl()队列。若需要SessionControl()生效，必须在`DispatchCtrlPackage()`调用本函数。
		"""
		raise NotImplementedError()

	# ================== OnProcess 实现  ==================

	async def OnProcess(self, raw: data.Transport) -> bool:
		if raw.data_type == data.TransportDataType.STREAM_SUBPACKAGE: # 流包的total_cnt=-1表示结束，为了区分Subpackage，先判断它
			await self.DispatchStreamPackage(raw)
			return True
		elif raw.data_type == data.TransportDataType.CONTROL:
			return await self.DispatchCtrlPackage(raw)
		elif raw.data_type == data.TransportDataType.SUBPACKAGE or raw.total_cnt == -1:
			ret = await self.DispatchSubpackage(raw)
			if ret is None: return True
			raw = ret
		
		await self.DispatchOtherPackage(raw)
		return True

class TunnelClient(TunnelClientDispatcher):
	"""
	在客户端使用的隧道，支持重连机制和重发机制，支持分包接收
	"""
	MaxHeapBufferSize = 20

	def __init__(self, maxRetries: int = 3, resendDuration: int = time_utils.Seconds(10), cacheQueueSize: int = 100, safeSegmentSize: int = 768 * 1024):
		super().__init__()

		self.maxRetries = maxRetries
		self.resendDuration = resendDuration
		self.safeSegmentSize = safeSegmentSize

		# 管理分包传输，key为seq_id
		self.chunks: typing.Dict[str, utils.Chunk] = {}

		# 用于重发的队列，已经发出的包不要存在这里（事实上client无需保存发出的包）
		self.sendQueue: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(cacheQueueSize)
		self.sendQueueLock = asyncio.Lock()

		# 只记录非control包
		self.historyRecvQueue: utils.BoundedQueue[data.PackageIdentification] = utils.BoundedQueue(cacheQueueSize)
		self.historyRecvQueueLock = asyncio.Lock()

		self.sessionMap: typing.Dict[str, data.Transport] = {}
		self.sessionCondition = asyncio.Condition()

		self.controlSessionMap: typing.Dict[str, data.Transport] = {}
		self.controlSessionCondition = asyncio.Condition()

		# 最小堆，根据cur_idx排序
		self.streamHeap: typing.Dict[str, typing.List[data.Transport]] = {}
		self.streamCondition = asyncio.Condition()

		self.timer = utils.Timer()

		self.instanceId = time_utils.GetTimestamp()

	@abc.abstractmethod
	def GetUID(self) -> str:
		raise NotImplementedError()
	
	@abc.abstractmethod
	def GetTargetUID(self) -> str:
		raise NotImplementedError()

	# =================== TunnelClientDispatcher 实现 ====================

	async def putToStreamQueue(self, raw: data.Transport):
		if raw.seq_id not in self.streamHeap:
			# self.GetLogger().error(f"{self.GetName()}] Stream subpackage {raw.seq_id} is not listening. Drop this package.")
			async with self.streamCondition:
				self.streamHeap[raw.seq_id] = []

		async with self.streamCondition:
			if raw.total_cnt == -1:
				self.GetLogger().debug(f"Stream <<<End {raw.seq_id} {len(raw.data)} bytes")
			else:
				self.GetLogger().debug(f"Stream {raw.seq_id} - {raw.cur_idx}-ith - {len(raw.data)} bytes")

			heapq.heappush(self.streamHeap[raw.seq_id], raw)

			if len(self.streamHeap[raw.seq_id]) > self.MaxHeapBufferSize:
				self.GetLogger().warning(f"Stream {raw.seq_id} seems dead. Drop cache packages. headq: {[x.cur_idx for x in self.streamHeap[raw.seq_id]]}")
				del self.streamHeap[raw.seq_id]
			# print(f"Stream got {raw.seq_id} - {raw.cur_idx}-ith, current heaq: {[x.cur_idx for x in self.streamHeap[raw.seq_id]]}")
			self.streamCondition.notify_all()

	async def putToSessionQueue(self, raw: data.Transport):
		async with self.sessionCondition:
			self.sessionMap[raw.seq_id] = raw
			self.sessionCondition.notify_all()

	async def putToSessionCtrlQueue(self, raw:data.Transport):
		async with self.controlSessionCondition:
			self.controlSessionMap[raw.seq_id] = raw
			self.controlSessionCondition.notify_all()
	
	async def DispatchSubpackage(self, raw: data.Transport) -> typing.Optional[data.Transport]:
		# 分包规则；前n-1个包为subpackage，最后一个包为resp、req。
		# 返回None表示Segments还没收全。若收全了返回合并的data.Transport对象。
		# 单包或者流包直接返回。

		# seq_id not in chunks and total_cnt = 32     =>     new chunk container
		# seq_id in chunks and total_cnt = 32         =>     save to existed chunk container
		# seq_id in chunks and total_cnt = -1         =>     end subpackage
		# seq_id not in chunks and total_cnt = -1     =>     error, should not happend
		if raw.seq_id not in self.chunks and raw.total_cnt <= 0:
			self.GetLogger().error(f"Invalid end subpackage {raw.seq_id} not in chunk but has total_cnt {raw.total_cnt}")
			return None
		
		if raw.seq_id not in self.chunks:
			self.chunks[raw.seq_id] = utils.Chunk(raw.total_cnt, 0)
		self.GetLogger().debug(f"Receive {raw.seq_id} subpackage({raw.cur_idx}/{raw.total_cnt}) - {len(raw.data)} bytes <<< {repr(raw.data[:50])} ...>>>")
		await self.chunks[raw.seq_id].Put(raw)
		if not self.chunks[raw.seq_id].IsFinish():
			# current package is a subpackage. we should not invoke internal callbacks until all package has received.
			return None
		ret = self.chunks[raw.seq_id].Combine()
		del self.chunks[raw.seq_id]
		return ret

	async def OnProcess(self, raw: data.Transport) -> bool:
		# hook
		if raw.data_type != data.TransportDataType.CONTROL:
			async with self.historyRecvQueueLock:
				self.historyRecvQueue.Add(data.PackageIdentification.FromTransport(raw))
		return await super().OnProcess(raw)

	# ===========================  Callback  ===========================

	async def QueueSend(self, raw: data.Transport):
		async with self.sendQueueLock:
			if self.IsConnected():
				try:
					await self.DirectSend(raw)
				except aiohttp.ClientConnectionResetError as e:
					# send failed, caching data
					self.GetLogger().error(f"{self.GetName()}] Disconnected while sending package. Cacheing package: {raw.seq_id}")
					self.sendQueue.Add(raw)
				except Exception as e:
					# skip package for unknown reason
					self.GetLogger().error(f"{self.GetName()}] Unknown exception on QueueSend(): {e}")
			else:
				self.sendQueue.Add(raw)

	# ========================== TunnelClient ==========================

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

	async def SessionControl(self, raw: data.Control, target_uid=None):
		"""
		SessionControl使用的是DirectSend而非QueueSend。
		"""
		tp = data.Transport(data.TransportDataType.CONTROL, raw.ToProtobuf(), from_uid=self.GetUID(), to_uid=target_uid if target_uid is not None else '')
		await self.DirectSend(tp)

		if raw.data_type in data.ControlDataType.NoResp:
			return None # 没有回复的类型

		async with self.controlSessionCondition:
			await self.controlSessionCondition.wait_for(lambda: tp.seq_id in self.controlSessionMap)
			resp = self.controlSessionMap[tp.seq_id]
			del self.controlSessionMap[tp.seq_id]
			return data.Control.FromProtobuf(resp.data)

	STREAM_STATE_NONE = 0
	STREAM_STATE_WAIT_TO_RETRIEVE = 1
	STREAM_STATE_SHOULD_RETRIEVE = 2

	STREAM_MAX_SEND_RETRIEVE_TIMES = 3 # 最多发送多少次retrieve包，当达到上限，无论如何都弹出一个元素
	STREAM_SCHEDULE_RETRIEVE_DURATION = time_utils.Seconds(3) # 当出现顺序不对时，等待多久才发送Retrieve包

	async def Stream(self, seq_id: str):
		# print(f"Listening stream on {seq_id}...")
		expectIdx = 1 # 当前期望得到的cur_idx
		sendRetreveTimes = 0 # 当前已经发送多少次retrieve包

		state = self.STREAM_STATE_NONE
		stateTimestamp = time_utils.GetTimestamp()
		stateLock = asyncio.Lock()
		class RetrieveTimerCallback(utils.TimerTask):
			def __init__(that):
				super().__init__(self.STREAM_SCHEDULE_RETRIEVE_DURATION)
			
			async def Run(that):
				nonlocal state
				with stateLock:
					if stateTimestamp == that.timestamp:
						print(f"Stream {seq_id} change from {state} to should retrieve")
						state = self.STREAM_STATE_SHOULD_RETRIEVE
					else:
						print(f"Stream {seq_id} timetask outdate. expect {that.timestamp} but got {stateTimestamp}")

		while True:
			async with self.streamCondition:
				if seq_id not in self.streamHeap or len(self.streamHeap[seq_id]) <= 0:
					await self.streamCondition.wait_for(lambda: seq_id in self.streamHeap and len(self.streamHeap[seq_id]) > 0)

				if self.streamHeap[seq_id][0].cur_idx != expectIdx:
					# 过旧的包
					if self.streamHeap[seq_id][0].cur_idx < expectIdx:
						self.GetLogger().warning(f"Receive older stream package {seq_id}, the expected one is {expectIdx}, and received is {self.streamHeap[seq_id][0].cur_idx}. Drop it.")
						heapq.heappop(self.streamHeap[seq_id]) # drop
						continue

					# 过新的包
					print(f"Stream {seq_id} expect {expectIdx} but heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}. Do sth.")
					if state == self.STREAM_STATE_NONE:
						async with stateLock:
							state = self.STREAM_STATE_WAIT_TO_RETRIEVE

							# 启动计时器
							cb = RetrieveTimerCallback()
							stateTimestamp = cb.GetTimestamp()
						self.timer.AddTask(cb)
						print(f"Stream {seq_id} change from none to wait to retrieve")
						continue
					elif state == self.STREAM_STATE_WAIT_TO_RETRIEVE:
						# 等待期间避免占用cpu
						# 不直接在这里等3s而是开一个协程等待，是因为希望在这3s内依然能够检测是否有正确的包出现，一旦正确的包出现了，会重置stateTimestamp使得计时器失效。
						# 这样不用完全耗掉3s也能顺利处理包。
						self.streamCondition.release() # 在这等待的1s内依然可以往队列加内容
						await asyncio.sleep(time_utils.Seconds(1))
						await self.streamCondition.acquire()
						print(f"Stream {seq_id} still waitting")
						continue
					elif state == self.STREAM_STATE_SHOULD_RETRIEVE:
						if sendRetreveTimes <= self.STREAM_MAX_SEND_RETRIEVE_TIMES:
							print(f"Stream  {seq_id} send retrieve request, cur times: {sendRetreveTimes}")
							retrieveRaw = data.Transport(
								data.TransportDataType.CONTROL,
								data.Control(
									data.ControlDataType.RETRIEVE_PKG, 
									data.RetrieveControlMsg(seq_id=seq_id, expect_idx=expectIdx, total_cnt=self.streamHeap[seq_id][0].total_cnt).Serialize()
								).ToProtobuf(),
								self.streamHeap[seq_id][0].to_uid,
								self.streamHeap[seq_id][0].from_uid
							)
							await self.DirectSend(retrieveRaw)
							sendRetreveTimes = sendRetreveTimes + 1
							continue
						else:
							self.GetLogger().error(f"Stream {seq_id} try to retrieve {expectIdx} in {sendRetreveTimes} times but still cannot get the sequence idx {expectIdx}, heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}. Pop one anyway.")

				# 重置计数器
				print(f"Stream {seq_id} reset every things, cur {expectIdx}")
				sendRetreveTimes = 0
				async with stateLock:
					state = self.STREAM_STATE_NONE
					stateTimestamp = -1

				item = heapq.heappop(self.streamHeap[seq_id])
				expectIdx = expectIdx + 1
				# print(f"SSE pop stream {seq_id} - {item.cur_idx}")
				if item.total_cnt == -1:
					del self.streamHeap[seq_id]
			yield item
			if item.total_cnt == -1:
				break
		# print(f"End listening stream on {seq_id}...")
	
	async def QueueSendSmartSegments(self, raw: data.Transport):
		"""
		自动判断是否需要分包，若需要则分包发送，否则单独发送。raw应该是加密后的包。
		多个Subpackage，先发送Subpackage，然后最后一个发送源data_type。
		分包不支持流包的分包，因为分包需要用到序号，而流包的序号有特殊含义和发包顺序。如果你希望序号不变，请使用QueueSend()。
		"""
		if raw.IsStreamPackage():
			raise Exception("Stream package cannot use in QueueSendSmartSegments(), please use QueueToSend() instead.")
		
		splitDatas: typing.List[bytes] = [ raw.data[i: i + self.safeSegmentSize] for i in range(0, len(raw.data), self.safeSegmentSize) ]
		if len(splitDatas) == 1: #包比较小，单独发送即可
			raw.total_cnt = 1
			raw.cur_idx = 0
			return await self.QueueSend(raw)
		self.GetLogger().debug(f"Transport {raw.seq_id} is too large({len(raw.data)}), will be split into {len(splitDatas)} packages to send.")

		subpackageRaw = raw.CloneWithSameSeqID()
		subpackageRaw.data_type = data.TransportDataType.SUBPACKAGE
		subpackageRaw.total_cnt = len(splitDatas)
		for i, item in enumerate(splitDatas[:-1]):
			subpackageRaw.data = item
			subpackageRaw.cur_idx = i
			self.GetLogger().debug(f"Send subpackage({subpackageRaw.cur_idx}/{subpackageRaw.total_cnt}) {subpackageRaw.seq_id} {len(subpackageRaw.data)} bytes <<< {repr(subpackageRaw.data[:50])} ...>>>")
			await self.QueueSend(subpackageRaw)
		
		# 最后一个包，虽然是Resp/Req类型，但是data其实是传输数据的一部分，不是resp、req包。要全部合成一个才能解析成resp、req包。
		raw.data = splitDatas[-1]
		raw.total_cnt = data.Transport.END_PACKAGE
		raw.cur_idx = len(splitDatas) - 1
		self.GetLogger().debug(f"Send end subpackage({raw.cur_idx}/{raw.total_cnt}) {raw.seq_id} {len(raw.data)} bytes <<< {repr(raw.data[:50])} ...>>>")
		await self.QueueSend(raw)

	async def preMainLoop(self):
		async with self.historyRecvQueueLock: # long context to prevent OnProcess() to modify the cacheQueue.
			hc = data.HistoryClientControlMsg(self.instanceId)
			for item in self.historyRecvQueue:
				hc.Add(item)

			ctrlRaw = data.Transport(
				data.TransportDataType.CONTROL,
				data.Control(
					data.ControlDataType.HISTORY_CLIENT,
					hc.Serialize()
				).ToProtobuf(),
				self.GetUID(),
				""
			)
			await self.DirectSend(ctrlRaw)

			async with self.sendQueueLock:
				for item in self.sendQueue:
					await self.DirectSend(item)
				self.sendQueue.Clear()

				self.isConnected = True

class TunnelServer(ControlDispatcher):
	def __init__(self, cacheSize: int = 100, timeout: int = time_utils.Seconds(10), **kwargs):
		# 缓存收到的包，从remote发到server的包，一来让remote知道server已经收到哪些包，二来让client从这个队列retrieve包。
		self.receiveQueue: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(cacheSize)
		self.receiveQueueLock = asyncio.Lock()

		# 转发的队列，从server发到client的包，当连接断开时，QueueSend将包缓存到这里，终端以为已经发出
		# 所有发送的包都存在这里，包含历史发送的包
		self.cacheSendQueue: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(cacheSize)
		self.cacheSendQueueLock = asyncio.Lock()

		self.timeout = timeout
		self.instanceId = -1

		super().__init__(**kwargs)
	
	@abc.abstractmethod
	async def waitForOnePackage(self) -> typing.Optional[data.Transport]:
		raise NotImplementedError()

	async def OnCtrlQuerySends(self, raw: data.Transport):
		"""
		公布receiveQueue给终端，比如remote
		"""
		print("==== Got query send")
		ret = data.QuerySendsControlMsg()
		async with self.receiveQueueLock:
			for item in self.receiveQueue:
				ret.Add(data.PackageIdentification.FromTransport(item))
		print("===send back", ret.Serialize())
		resp = data.Transport(
			data.TransportDataType.CONTROL,
			data.Control(
				data.ControlDataType.QUERY_SENDS,
				ret.Serialize()
			).ToProtobuf(),
			raw.to_uid,
			raw.from_uid,
			seq_id=raw.seq_id
		)
		print("==== Send back")
		await self.DirectSend(resp)

	@abc.abstractmethod
	async def DispatchOtherPackage(self, raw: data.Transport):
		"""
		处理非流包
		"""
		raise NotImplementedError()

	async def OnProcess(self, raw: data.Transport) -> bool:
		if raw.data_type == data.TransportDataType.CONTROL:
			return await self.DispatchCtrlPackage(raw)
		
		await self.DispatchOtherPackage(raw)
		return True
	
	async def preMainLoop(self):
		async with self.cacheSendQueueLock: # long context to block QueueSend to modify cacheSendQueue at the same time
			ctrlRaw = await self.waitForOnePackage()
			if ctrlRaw.data_type != data.TransportDataType.CONTROL:
				raise RuntimeError(f"The first package of client should be control but got {data.TransportDataType.ToString(ctrlRaw.data_type)}.")
			ctrl = data.Control.FromProtobuf(ctrlRaw.data)
			if ctrl.data_type != data.ControlDataType.HISTORY_CLIENT:
				raise RuntimeError(f"The first package of client should be a HistoryClient control but got {data.ControlDataType.ToString(ctrl.data_type)}.")
			history = data.HistoryClientControlMsg.From(ctrl.msg)

			if history.GetInstanceId() != self.instanceId: # 此时client已经不是原来的实例了，缓存的包不要发送
				self.instanceId = history.GetInstanceId()
				self.cacheSendQueue.Clear()
				print("client is new. clear cacheSendQueue.")
				return
		
			if self.cacheSendQueue.IsEmpty(): return

			# 上面client报告了自己收到哪些包。
			# server发出的包应当client都收到，现在看看server发出的包中client哪些没收到，然后重发。
			sendMask = [True] * len(self.cacheSendQueue)
			for iq, item in enumerate(self.cacheSendQueue):
				for i in range(len(history)):
					if history[i].IsSamePackage(item):
						sendMask[iq] = False
						break

			self.GetLogger().info(f"{self.GetName()}] Resend {len([x for x in sendMask if x == True])} packages.")

			nowtime = time_utils.GetTimestamp()
			for iq, item in enumerate(self.cacheSendQueue):
				if sendMask[iq] == False: continue
				if time_utils.WithInDuration(item.timestamp, nowtime, self.timeout):
					await self.DirectSend(item)
					self.GetLogger().debug(f"{self.GetName()}] Sent {item.seq_id}")
				else:
					self.GetLogger().warning(f"{self.GetName()}] Drop resend package {item.seq_id} {data.TransportDataType.ToString(item.data_type)} due to outdated.")

			self.isConnected = True # 当释放锁时，如果不设置为True，可能在QueueSend处只缓存不发包。

	async def QueueSend(self, raw: data.Transport) -> None:
		async with self.cacheSendQueueLock:
			raw.RenewTimestamp()
			self.cacheSendQueue.Add(raw)

			if self.IsConnected():
				try:
					await self.DirectSend(raw)
				except Exception as e:
					self.GetLogger().error(f"{self.GetName()}] Cannot send {raw.seq_id}, err: {e}.")

		

HttpUpgradedWebSocketHeaders = {"Upgrade": "websocket", "Connection": "Upgrade"}
HttpUpgradeHearbeatDuration = 0 # set 0 to disable
WebSocketMaxReadingMessageSize = 12 * 1024 * 1024 # 12MB
WebSocketSafeMaxMsgSize = 768 * 1024 # 768KB, up to 1MB

class HttpUpgradedWebSocketClient(TunnelClient):
	"""
	在客户端使用的Websocket隧道，支持重连机制和重发机制，支持分包接收
	"""
	def __init__(self, url: str, safeSegmentSize: int = WebSocketSafeMaxMsgSize, **kwargs):
		super().__init__(safeSegmentSize = safeSegmentSize, **kwargs)

		self.url = url
		self.handler: typing.Optional[aiohttp.ClientWebSocketResponse] = None

	async def DirectSend(self, raw: data.Transport):
		await self.handler.send_bytes(raw.ToProtobuf())

	async def MainLoop(self):
		curTries = 0
		exitSignal = False
		self.timer.Start()
		while curTries < self.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=HttpUpgradedWebSocketHeaders, max_msg_size=WebSocketMaxReadingMessageSize) as ws:
					if await self.OnPreConnected() != '':
						break
					self.handler = ws
					await self.OnConnected()
					curTries = 0 # reset tries
					await self.preMainLoop()
					self.isConnected = True
					async for msg in self.handler:
						if msg.type == aiohttp.WSMsgType.ERROR:
							self.OnError(self.handler.exception())
						elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
							parsed = data.Transport.FromProtobuf(msg.data)
							shouldRuninng = await self.OnProcess(parsed)
							if shouldRuninng == False:
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
		await self.timer.Stop()
		return bakHandler

class HttpUpgradedWebSocketServer(TunnelServer):
	"""
	在服务器端使用的WebSocket隧道，服务器无需重连机制，支持重发包
	"""
	def __init__(self, **kwargs):
		self.ws: web.WebSocketResponse = None
		self.isConnectedLock = asyncio.Lock()

		super().__init__(**kwargs)

	async def MainLoop(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				self.OnError(self.ws.exception())
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = data.Transport.FromProtobuf(msg.data)
				await self.OnProcess(transport)

	async def waitForOnePackage(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				self.OnError(self.ws.exception())
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = data.Transport.FromProtobuf(msg.data)
				return transport
		return None
	
	async def DirectSend(self, raw: data.Transport):
		return await self.ws.send_bytes(raw.ToProtobuf())
	
	async def MainLoopWithRequest(self, request: web.BaseRequest):
		async with self.isConnectedLock:
			preconnected = await self.OnPreConnected()
			preconnected = preconnected.strip()
			if preconnected != '':
				return utils.ReponseText(preconnected, 403)
			
			self.ws = web.WebSocketResponse(max_msg_size=WebSocketMaxReadingMessageSize, heartbeat=None if HttpUpgradeHearbeatDuration == 0 else HttpUpgradeHearbeatDuration, autoping=True)
			await self.ws.prepare(request)
			self.isConnected = True
		await self.OnConnected()
		await self.preMainLoop()
		try:
			await self.MainLoop()
		except Exception as ex:
			self.OnError(ex)
		async with self.isConnectedLock:
			self.isConnected = False
			await self.OnDisconnected()
			bakws = self.ws
			self.ws = None
		return bakws
