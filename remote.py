import aiohttp.client_exceptions
import encryptor
import protocol
import utils
import time_utils

import asyncio, logging, os, typing, re, json, aiohttp
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import aiohttp.web as web
import argparse as argp
import heapq
import PIL.ImageFile
import math
import shutil
from collections import OrderedDict

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote", terminalLevel=logging.DEBUG, saveInFile=True)
protocol.log = log
utils.log = log

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.cipher = encryptor.NewCipher(args.cipher, args.key, args.compress)
		log.info(f"Using cipher: {self.cipher.GetName()}")

		self.prefix: str = args.prefix
		self.port: int = args.port

		self.uid: str = args.uid
		self.target_uid: str = args.target_uid

		self.img_quality:int = args.prefer_img_quality

		self.maxRetries:int = args.max_retries
		self.cacheQueueSize: int  = args.cache_queue_size
		self.cachePkgQueueSize: int = args.cache_pkg_queue_size
		self.timeout = time_utils.Seconds(args.timeout)
		self.maxHeapSSEBufferSize: int = args.max_heapsse_buffer_size
		self.safeSegmentSize: int  = args.safe_segment_size
		self.streamRetrieveTimeout: int = args.stream_retrieve_timeout
		self.streamRetrieveTimes: int = args.stream_retrieve_times
		self.allowOFOResend: bool  = args.allow_ofo_resend

		# debug only
		self.force_id: str = args.force_id

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="The websocket server to connect to.")
		parser.add_argument("--cipher", type=str, default="xor", help=f"The cipher to use. Available: [{', '.join(encryptor.GetAvailableCipherMethods())}]")
		parser.add_argument("--key", type=str, default="必须使用同一个通讯密钥，否则WebSocketForward报错无法正常运行。", help="The key to use for the cipher. Must be the same with client.")

		parser.add_argument("--prefix", type=str, default="http://127.0.0.1:8188", help="The prefix of your requests.")
		parser.add_argument("--port", type=int, default=8130, help="The port to listen on.")

		parser.add_argument("--uid", type=str, default="Remote")
		parser.add_argument("--target_uid", type=str, default="Client")
		parser.add_argument("--compress", type=str, default="none", help=f"The compress method to use. Available: [{', '.join(encryptor.GetAvailableCompressMethods())}]")

		parser.add_argument("--prefer_img_quality", type=int, default=75, help="Compress the image to transfer fasterly.")

		parser.add_argument("--max_retries", type=int, default=3, help="Maximum number of retries")
		parser.add_argument("--cache_queue_size", type=int, default=150, help="Maximum number of messages to cache. Must bigger than server's cache queue size.")
		parser.add_argument("--cache_pkg_queue_size", type=int, default=128, help="Cache send history packages. Must be small or equal to client's cache queue size.")
		parser.add_argument("--timeout", type=int, default=60, help="The maximum seconds to resend packages.")
		parser.add_argument("--max_heapsse_buffer_size", type=int, default=20)
		parser.add_argument("--safe_segment_size", type=int, default=768*1024, help="Maximum size of messages to send, in Bytes")
		parser.add_argument("--stream_retrieve_timeout", type=int, default=time_utils.Seconds(3))
		parser.add_argument("--stream_retrieve_times", type=int, default=3)
		parser.add_argument("--allow_ofo_resend", action="store_true", help="Allow out of order message to be resend. Disable this feature may results in buffer overflow issue. By default, this feature is disable.")

		parser.add_argument("--force_id", type=str, default="")
		return parser

class Client:
	HttpUpgradedWebSocketHeaders = {"Upgrade": "websocket", "Connection": "Upgrade"}
	WebSocketMaxReadingMessageSize = 12 * 1024 * 1024 # 12MB

	STATUS_INIT = 0
	STATUS_INITIALIZING = 1
	STATUS_CONNECTED = 2

	def __init__(self, config: Configuration):
		self.config = config
		self.url = f'{config.server}?uid={config.uid}'
		self.name = self.config.uid

		self.status = self.STATUS_INIT
		self.instance_id = utils.NewId()

		# 管理分包传输，key为seq_id
		self.chunks: typing.Dict[str, utils.Chunk] = {}

		# 只记录非control包
		self.historyRecvQueue: utils.BoundedQueue[protocol.PackageId] = utils.BoundedQueue(self.config.cacheQueueSize)
		self.historyRecvQueueLock = asyncio.Lock()

		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(self.config.cachePkgQueueSize)
		self.sendQueueLock = asyncio.Lock()

		# 用于重发的队列，已经发出的包不要存在这里（事实上client无需保存发出的包）
		# self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(self.config.cacheQueueSize)
		# self.sendQueueLock = asyncio.Lock()
		# init阶段会将sendQueue的包转移到scheduleSendQueue，在connect阶段开始的时候边监听边重发包。
		# 如果在init直接发包，可能没到达connect阶段的时候就收到大量的包，但没有监听，导致缓冲区溢出。
		self.scheduleSendQueue: asyncio.Queue[protocol.Transport] = asyncio.Queue()

		self.sessionMap: typing.Dict[str, protocol.Transport] = {}
		self.sessionCondition = asyncio.Condition()

		self.controlSessionMap: typing.Dict[str, protocol.Control] = {}
		self.controlSessionCondition = asyncio.Condition()

		# 最小堆，根据cur_idx排序
		self.streamHeap: typing.Dict[str, typing.List[protocol.StreamData]] = {}
		self.streamCondition = asyncio.Condition()

		self.ws: aiohttp.ClientWebSocketResponse = None
		self.ws_timeout = aiohttp.ClientWSTimeout(ws_close=time_utils.Minutes(2))
		self.timer = utils.Timer()

		self.exitFunc: typing.Coroutine[typing.Any, typing.Any, typing.Any] = None

		self.nocipher = encryptor.NewCipher('plain', '')

	def DefineExitFunc(self, exitFunc: typing.Coroutine[typing.Any, typing.Any, typing.Any]):
		self.exitFunc = exitFunc

	async def DirectSend(self, raw: protocol.Transport):
		await self.ws.send_bytes(raw.Pack())

	async def QueueSend(self, raw: protocol.Transport):
		async with self.sendQueueLock:
			# check
			if raw.sender != self.name:
				log.error(f"{self.name}] Send a package {raw.seq_id}:{protocol.Transport.Mappings.ValueToString(raw.transportType)} to {raw.receiver} but it's sender is {raw.sender}, which should be {self.name}. Online fix that.")
				raw.sender = self.name
				
			if raw.transportType != protocol.Transport.CONTROL:
				raw.timestamp = time_utils.GetTimestamp()
				self.sendQueue.Add(raw)

			if self.status == self.STATUS_CONNECTED:
				try:
					await self.DirectSend(raw)
				except aiohttp.ClientConnectionResetError as e:
					# send failed, caching data
					log.error(f"{self.name}] Disconnected while sending package. Cacheing package: {raw.seq_id}")
					self.sendQueue.Add(raw)
				except Exception as e:
					# skip package for unknown reason
					log.error(f"{self.name}] Unknown exception on QueueSend(): {e}")

	async def QueueSendSmartSegments(self, raw: typing.Union[protocol.Request, protocol.Response]):
		"""
		自动判断是否需要分包，若需要则分包发送，否则单独发送。raw应该是加密后的包。
		多个Subpackage，先发送Subpackage，然后最后一个发送源data_type。
		分包不支持流包的分包，因为分包需要用到序号，而流包的序号有特殊含义和发包顺序。如果你希望序号不变，请使用QueueSend()。
		"""
		if raw.body is None:
			return await self.QueueSend(raw)
		
		splitDatas: typing.List[bytes] = [ raw.body[i: i + self.config.safeSegmentSize] for i in range(0, len(raw.body), self.config.safeSegmentSize) ]
		if len(splitDatas) == 1: #包比较小，单独发送即可
			return await self.QueueSend(raw)
		log.debug(f"Transport {raw.seq_id} is too large({len(raw.body)}), will be split into {len(splitDatas)} packages to send.")

		for i, item in enumerate(splitDatas[:-1]):
			sp = protocol.Subpackage(self.config.cipher)
			sp.seq_id = raw.seq_id
			sp.SetSenderReceiver(raw.sender, raw.receiver)
			sp.SetIndex(i, len(splitDatas))
			sp.SetBody(item)

			log.debug(f"Subpackage {sp.seq_id}:{sp.cur_idx}/{sp.total_cnt} - {len(sp.body)} bytes <<< {repr(sp.body[:utils.LOG_BYTES_LEN])} ...>>> -> {sp.receiver}")
			await self.QueueSend(sp)
		
		# 最后一个包，虽然是Resp/Req类型，但是data其实是传输数据的一部分，不是resp、req包。要全部合成一个才能解析成resp、req包。
		raw.SetBody(splitDatas[-1])
		raw.SetIndex(len(splitDatas) - 1, -1)

		log.debug(f"Subpackage {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[-utils.LOG_BYTES_LEN:])} ...>>> -x> {sp.receiver}")
		await self.QueueSend(raw)

	async def Session(self, req: protocol.Request) -> protocol.Response:
		"""
		发送一个req，等待回应一个resp。适合于非流的数据传输。
		"""
		req.SetSenderReceiver(self.config.uid, self.config.target_uid)
		log.debug(f"Request {req.url} {req.method} { 0 if req.body is None else len(req.body)} bytes -> {req.receiver}")
		await self.QueueSendSmartSegments(req)

		async with self.sessionCondition:
			await self.sessionCondition.wait_for(lambda: req.seq_id in self.sessionMap)
			resp = self.sessionMap[req.seq_id]
			del self.sessionMap[req.seq_id]

			assert resp.transportType == protocol.Transport.RESPONSE, f"Session {req.seq_id} only accept Response package but got {protocol.Transport.Mappings.ValueToString(resp.transportType)}"
			resp: protocol.Response
			log.debug(f"Response {resp.url} {resp.status} {len(resp.body)} bytes <- {resp.sender}")
			return resp
		
	async def SessionControl(self, raw: protocol.Control):
		"""
		SessionControl使用的是DirectSend而非QueueSend。
		"""
		log.debug(f"SessionControl {protocol.Control.Mappings.ValueToString(raw.controlType)} -> {raw.receiver}")
		await self.DirectSend(raw)

		async with self.controlSessionCondition:
			await self.controlSessionCondition.wait_for(lambda: raw.seq_id in self.controlSessionMap)
			resp = self.controlSessionMap[raw.seq_id]
			del self.controlSessionMap[raw.seq_id]
			log.debug(f"SessionControl {protocol.Control.Mappings.ValueToString(resp.controlType)} <- {resp.sender}")
			return resp
		
	STREAM_STATE_NONE = 0
	STREAM_STATE_WAIT_TO_RETRIEVE = 1
	STREAM_STATE_SHOULD_RETRIEVE = 2

	async def Stream(self, seq_id: str):
		expectIdx = 1 # 当前期望得到的cur_idx
		sendRetreveTimes = 0 # 当前已经发送多少次retrieve包

		state = self.STREAM_STATE_NONE
		stateTimestamp = time_utils.GetTimestamp()
		stateLock = asyncio.Lock()
		class RetrieveTimerCallback(utils.TimerTask):
			def __init__(that, target_state: int):
				super().__init__(self.config.streamRetrieveTimeout)
				that.target_state = target_state
			
			async def Run(that):
				nonlocal state
				nonlocal stateTimestamp
				nonlocal stateLock
				async with stateLock:
					if stateTimestamp == that.timestamp:
						log.debug(f"Stream {seq_id} change from {state} to {that.target_state}")
						state = that.target_state
					else:
						log.debug(f"Stream {seq_id} timetask outdate. expect {that.timestamp} but got {stateTimestamp}, current state: {state}, want to change to {that.target_state}")

		try:
			while True:
				async with self.streamCondition:
					if seq_id not in self.streamHeap or len(self.streamHeap[seq_id]) <= 0:
						await self.streamCondition.wait_for(lambda: seq_id in self.streamHeap and len(self.streamHeap[seq_id]) > 0)

					if self.streamHeap[seq_id][0].cur_idx != expectIdx:
						# 过旧的包
						if self.streamHeap[seq_id][0].cur_idx < expectIdx:
							log.warning(f"Stream {seq_id}:{self.streamHeap[seq_id][0].cur_idx} < {expectIdx} - Drop this older package.")
							heapq.heappop(self.streamHeap[seq_id]) # drop
							continue

						# 过新的包
						if state == self.STREAM_STATE_NONE:
							log.warning(f"Stream {seq_id}:{self.streamHeap[seq_id][0].cur_idx} > {expectIdx} - Receive this newer package, current heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}.")
							async with stateLock:
								state = self.STREAM_STATE_WAIT_TO_RETRIEVE

								# 启动计时器
								cb = RetrieveTimerCallback(self.STREAM_STATE_SHOULD_RETRIEVE)
								stateTimestamp = cb.GetTimestamp()
							await self.timer.AddTask(cb)
							log.debug(f"Stream {seq_id} - Start timer, wait for {expectIdx}")
							continue
						elif state == self.STREAM_STATE_WAIT_TO_RETRIEVE:
							# 等待期间避免占用cpu
							# 不直接在这里等3s而是开一个协程等待，是因为希望在这3s内依然能够检测是否有正确的包出现，一旦正确的包出现了，会重置stateTimestamp使得计时器失效。
							# 这样不用完全耗掉3s也能顺利处理包。
							self.streamCondition.release() # 在这等待的1s内依然可以往队列加内容
							await asyncio.sleep(time_utils.Seconds(1))
							await self.streamCondition.acquire()
							log.debug(f"Stream {seq_id} - Still waiting {expectIdx}")
							continue
						elif state == self.STREAM_STATE_SHOULD_RETRIEVE:
							if sendRetreveTimes <= self.config.streamRetrieveTimes:
								log.debug(f"Stream {seq_id} - Send retrieve request, cur times: {sendRetreveTimes}, waiting {expectIdx}")
								raw = protocol.Control()
								raw.SetForResponseTransport(self.streamHeap[seq_id][0])
								raw.InitRetrievePkg(protocol.PackageId(cur_idx=expectIdx, total_cnt=0, seq_id=seq_id))
								await self.DirectSend(raw)
								sendRetreveTimes = sendRetreveTimes + 1

								async with stateLock:
									state = self.STREAM_STATE_WAIT_TO_RETRIEVE

									# 启动计时器
									cb = RetrieveTimerCallback(self.STREAM_STATE_SHOULD_RETRIEVE)
									stateTimestamp = cb.GetTimestamp()
								await self.timer.AddTask(cb)
								continue
							else:
								log.error(f"Stream {seq_id} - Failed to retrieve {expectIdx} in {sendRetreveTimes} times, current heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}. Pop one anyway.")

					# 重置计数器
					sendRetreveTimes = 0
					async with stateLock:
						state = self.STREAM_STATE_NONE
						stateTimestamp = -1

					item = heapq.heappop(self.streamHeap[seq_id])
					expectIdx = expectIdx + 1
					if item.total_cnt == -1:
						log.debug(f"Stream {seq_id}:{item.cur_idx}/{item.total_cnt} - {len(item.body)} bytes <<< {repr(item.body[:utils.LOG_BYTES_LEN])} ...>>> <x- {item.sender}")
						del self.streamHeap[seq_id]
					else:
						log.debug(f"Stream {seq_id}:{item.cur_idx}/{item.total_cnt} - {len(item.body)} bytes <<< {repr(item.body[:utils.LOG_BYTES_LEN])} ...>>> <- {item.sender}")
				yield item
				if item.total_cnt == -1:
					break
		except GeneratorExit:
			item = protocol.StreamData(self.config.cipher)
			item.SetSenderReceiver(self.config.uid, self.config.target_uid, seq_id=seq_id)
			item.SetIndex(expectIdx, -1)
			log.debug(f"Stream {seq_id}:{item.cur_idx}/{item.total_cnt} -x> {item.receiver}")
			await self.QueueSend(item)
	
	async def waitForOnePackage(self, ws: web.WebSocketResponse):
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				log.error(f"1Pkg] Error: {ws.exception()}", exc_info=True, stack_info=True)
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Transport.Parse(msg.data)
				log.debug(f"1Pkg] {protocol.Transport.Mappings.ValueToString(transport.transportType)} {transport.seq_id}:{transport.cur_idx}/{transport.total_cnt} <- {transport.sender}")
				return transport
		log.error(f"1Pkg] WS broken.")
		return None

	async def initialize(self, ws: web.WebSocketResponse):
		# 不能收也不能发
		async with self.historyRecvQueueLock:
			async with self.sendQueueLock:
				# 暴露自己收到的包和发出的包
				hcc = protocol.HelloClientControl(protocol.ClientInfo(self.instance_id if self.config.force_id == "" else self.config.force_id, protocol.ClientInfo.REMOTE))
				for pkg in self.historyRecvQueue:
					hcc.AppendReceivedPkgs(pkg)
				for pkg in self.sendQueue:
					hcc.AppendSentPkgs(protocol.PackageId.FromPackage(pkg))
				log.debug(f"Initialize] Reporting {len(hcc.received_pkgs)} received packages and {len(hcc.sent_pkgs)} sent packages to server.")
				ctrl = protocol.Control()
				ctrl.SetSenderReceiver(self.name, "")
				ctrl.InitHelloClientControl(hcc)
				await ws.send_bytes(ctrl.Pack())

				pkg = await self.waitForOnePackage(ws)
				if pkg is None:
					log.error(f"Initialize] Broken ws connection in initialize stage.")
					self.status = self.STATUS_INIT
					return ws
				if pkg.transportType != protocol.Transport.CONTROL:
					log.error(f"Initialize] First package {pkg.seq_id}:{pkg.cur_idx}/{pkg.total_cnt} is {protocol.Transport.Mappings.ValueToString(pkg.transportType)} which should be {protocol.Transport.Mappings.ValueToString(protocol.Transport.CONTROL)}.")
					self.status = self.STATUS_INIT
					return ws
				tmp = protocol.Control()
				tmp.Unpack(pkg)
				pkg = tmp
				if pkg.controlType == protocol.Control.PRINT: # error msg from server
					log.error(f"Initialize] Error from server: {pkg.DecodePrintMsg()}")
					self.status = self.STATUS_INIT
					return ws
				if pkg.controlType != protocol.Control.HELLO:
					log.error(f"Initialize] First control package {pkg.seq_id}:{pkg.cur_idx}/{pkg.total_cnt} is {protocol.Control.Mappings.ValueToString(pkg.controlType)} which should be {protocol.Control.Mappings.ValueToString(protocol.Control.HELLO)}.")
					self.status = self.STATUS_INIT
					return ws
				hello = pkg.ToHelloServerControl()

				log.info(f"Initialize] Server will send {len(hello.reports)} packages to us and require us to resend {len(hello.requires)} packages to server.")

				now = time_utils.GetTimestamp()
				for item in hello.requires:
					found = False
					for pkg in self.sendQueue:
						if item.IsSamePackage(pkg):
							found = True
							if time_utils.WithInDuration(pkg.timestamp, now, self.config.timeout):
								await self.scheduleSendQueue.put(pkg)
							else:
								log.warning(f"Initialize] Package {item.seq_id} skip resend because it's out-dated({now} - {pkg.timestamp} = {now - pkg.timestamp} > {self.config.timeout}).")
							break
					if not found:
						log.error(f"Initialize] Sever require resend {item.seq_id} but not in sendQueue. Should not happend. The maybe the wrong configuration in server code.")
				
				log.debug(f"Initialize] Done.")
				self.ws = ws
				self.status = self.STATUS_CONNECTED
		
		return None
	
	async def processSubpackage(self, raw: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage]) -> typing.Optional[protocol.Transport]:
		# 分包规则；前n-1个包为subpackage，最后一个包为resp、req。
		# 返回None表示Segments还没收全。若收全了返回合并的data.Transport对象。
		# 单包或者流包直接返回。

		# seq_id not in chunks and total_cnt = 32     =>     new chunk container
		# seq_id in chunks and total_cnt = 32         =>     save to existed chunk container
		# seq_id in chunks and total_cnt = -1         =>     end subpackage
		# seq_id not in chunks and total_cnt = -1     =>     error, should not happend
		if raw.seq_id not in self.chunks and raw.total_cnt <= 0:
			log.error(f"Invalid end subpackage {raw.seq_id} not in chunk but has total_cnt {raw.total_cnt}")
			return None
		
		if raw.seq_id not in self.chunks:
			self.chunks[raw.seq_id] = utils.Chunk(raw.total_cnt, 0)
		await self.chunks[raw.seq_id].Put(raw)
		if not self.chunks[raw.seq_id].IsFinish():
			log.debug(f"Subpackage {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> <- {raw.sender}")
			# current package is a subpackage. we should not invoke internal callbacks until all package has received.
			return None
		log.debug(f"Subpackage {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[-utils.LOG_BYTES_LEN:])} ...>>> <x- {raw.sender}")
		ret = self.chunks[raw.seq_id].Combine()
		del self.chunks[raw.seq_id]
		return ret
	
	async def putInControlSession(self, pkg: protocol.Control):
		async with self.controlSessionCondition:
			self.controlSessionMap[pkg.seq_id] = pkg
			self.controlSessionCondition.notify_all()

	async def processControlPackage(self, pkg: protocol.Control):
		if pkg.controlType == protocol.Control.PRINT:
			log.info(f"% {pkg.DecodePrintMsg()}")
		elif pkg.controlType in [protocol.Control.QUERY_CLIENTS]:
			await self.putInControlSession(pkg)
		else:
			log.error(f"OnPackage] Unsupported control type({protocol.Control.Mappings.ValueToString(pkg.controlType)})")

	async def processStreamDataPackage(self, pkg: protocol.StreamData):
		async with self.streamCondition:
			if pkg.seq_id not in self.streamHeap:
				self.streamHeap[pkg.seq_id] = []

			heapq.heappush(self.streamHeap[pkg.seq_id], pkg)

			if len(self.streamHeap[pkg.seq_id]) > self.config.maxHeapSSEBufferSize:
				log.warning(f"Stream {pkg.seq_id} seems dead. Drop cache packages. headq: {[x.cur_idx for x in self.streamHeap[pkg.seq_id]]}")
				del self.streamHeap[pkg.seq_id]
			
			self.streamCondition.notify_all()

	async def OnPackage(self, pkg: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage, protocol.StreamData, protocol.Control]) -> bool:
		if type(pkg) == protocol.Control:
			log.debug(f"Control {protocol.Control.Mappings.ValueToString(pkg.controlType)} {pkg.seq_id}:{pkg.cur_idx}/{pkg.total_cnt} <- {pkg.sender}")
			await self.processControlPackage(pkg)
		else:
			async with self.historyRecvQueueLock:
				self.historyRecvQueue.Add(protocol.PackageId.FromPackage(pkg))

			if type(pkg) == protocol.StreamData: # 流包的total_cnt=-1表示结束，为了区分Subpackage，先判断
				await self.processStreamDataPackage(pkg)
			elif type(pkg) == protocol.Subpackage or pkg.total_cnt == -1:
				combine = await self.processSubpackage(pkg)
				if combine is not None:
					async with self.sessionCondition:
						self.sessionMap[pkg.seq_id] = pkg
						self.sessionCondition.notify_all()
			else:
				async with self.sessionCondition:
					self.sessionMap[pkg.seq_id] = pkg
					self.sessionCondition.notify_all()
		return True

	async def resendScheduledPackages(self):
		log.debug(f"Schedule] Resend {self.scheduleSendQueue.qsize()} packages.")
		while not self.scheduleSendQueue.empty():
			item = await self.scheduleSendQueue.get()
			log.debug(f"Schedule] Resend {protocol.Transport.Mappings.ValueToString(item.transportType)} {item.seq_id}:{item.cur_idx}/{item.total_cnt} -> {item.receiver}")
			await self.DirectSend(item)

	async def MainLoop(self):
		curTries = 0
		exitSignal = False
		self.timer.Start()
		while curTries < self.config.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=self.HttpUpgradedWebSocketHeaders, max_msg_size=self.WebSocketMaxReadingMessageSize, timeout=self.ws_timeout) as ws:
					self.status = self.STATUS_INITIALIZING
					log.info(f"Initializing...")
					initRet = await self.initialize(ws)
					if initRet is None:
						self.ws = ws
						self.status = self.STATUS_CONNECTED
						curTries = 0
						log.info(f"Start mainloop...")

						if self.config.allowOFOResend:
							asyncio.ensure_future(self.resendScheduledPackages())
						else:
							await self.resendScheduledPackages()
						async for msg in self.ws:
							if msg.type == aiohttp.WSMsgType.ERROR:
								log.error(f"Tunnel] Exception: {self.ws.exception()}", stack_info=True, exc_info=True)
							elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
								parsed = protocol.Parse(msg.data, self.config.cipher)
								shouldRuninng = await self.OnPackage(parsed)
								if shouldRuninng == False:
									exitSignal = True
									break
			
			self.status = self.STATUS_INIT
			if exitSignal == False:
				log.info(f"Tunnel] Reconnecting({curTries}/{self.config.maxRetries})...")
				curTries += 1
			else:
				break
		self.ws = None
		await self.timer.Stop()
		log.info("Exit client mainloop.")
		if self.exitFunc is not None:
			log.info("Invoke exitFunc")
			await self.exitFunc()

	async def ControlQuery(self):
		"""
		Query控制包以Control的形式返回。
		"""
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, "")
		raw.InitQueryClientsControl(protocol.QueryClientsControl())
		resp = await self.SessionControl(raw)
		assert resp.controlType == protocol.Control.QUERY_CLIENTS, f"ControlQuery {raw.seq_id} require to receive a QueryClients package but got {protocol.Control.Mappings.ValueToString(resp.controlType)}"

		return [ x.id for x in resp.ToQueryClientsControl() ]

	async def ControlExit(self):
		"""
		Exit控制包是没有返回的。
		"""
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, self.config.target_uid)
		raw.InitQuitSignal()
		await self.DirectSend(raw)

	async def ControlRPCQuery(self):
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, self.config.target_uid)
		raw.InitRPCQueryControl()
		resp = await self.SessionControl(raw)
		assert resp.controlType == protocol.Control.QUERY_RPC, f"ControlRPCQuery {raw.seq_id} require to receive a QueryRPC package but got {protocol.Control.Mappings.ValueToString(resp.controlType)}"
		rpcq = resp.ToRPCQueryControl()
		return rpcq.ToDict()

	async def ControlRPCCall(self, data: protocol.RPCCallControl):
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, self.config.target_uid)
		raw.InitRPCCallControl(data)
		resp = await self.SessionControl(raw)
		assert resp.controlType == protocol.Control.RPC_RESP, f"ControlRPCCall {raw.seq_id} require to receive a RPCResponse package but got {protocol.Control.Mappings.ValueToString(resp.controlType)}"
		rpcc = resp.ToRPCResponseControl()
		return rpcc.ToDict()
	
	async def ControlRPCProgress(self, task: str):
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, self.config.target_uid)
		raw.InitRPCProgressControl(task)
		resp = await self.SessionControl(raw)
		assert resp.controlType == protocol.Control.RPC_RESP, f"ControlRPCProgress {raw.seq_id} require to receive a RPCResponse package but got {protocol.Control.Mappings.ValueToString(resp.controlType)}"
		rpcc = resp.ToRPCResponseControl()
		return rpcc.ToDict()

class ImageInstance:
	def __init__(self, img: PIL.ImageFile.ImageFile, prompt: str):
		self.img = img
		self.metadata = self.img.getexif()
		self.metadata[270] = json.dumps({  # 270 corresponds to 'ImageDescription'
			"prompt": prompt
		})

class StableDiffusionGridCacheInfo():
	def __init__(self, width: int, height: int, gridUrl: str, imglist: typing.List[str], prompts: typing.List[str]):
		self.width = width
		self.height = height
		self.gridUrl = gridUrl
		self.imglist = imglist
		self.prompts = prompts
		self.take = 0
		self.takeLock = asyncio.Lock()
		self.trigger = asyncio.Event()
		self.ns = int(math.ceil(math.sqrt(len(self.imglist))))
		self.nh = int(math.ceil(len(self.imglist) / self.ns))
		self.img: PIL.ImageFile.ImageFile = None

	async def WaitAndGet(self, url: str):
		if url not in self.imglist:
			log.error(f"Attempt to wait {url} but not in list: {self.imglist}")
			return None, False
		loc = self.imglist.index(url)
		
		await self.trigger.wait()
		isLastOneToTake = False
		async with self.takeLock:
			self.take += 1
			if self.take == len(self.imglist):
				isLastOneToTake = True

		if self.img is None:
			log.error(f"Not hit grid {self.gridUrl} for image {url}")
			return None, isLastOneToTake
		
		x = loc % self.ns
		y = int(math.floor(loc / self.ns))

		img = self.img.crop(( x * self.width, y * self.height, (x + 1) * self.width, (y + 1) * self.height ))
		return ImageInstance(img, self.prompts[loc]), isLastOneToTake

	def Set(self, img: PIL.ImageFile.ImageFile):
		if img.width == self.width * self.ns and img.height == self.height * self.nh:
			self.img = img
		self.trigger.set()

class StreamListenInfo:
	TYPE_UNKNOWN = 0
	TYPE_WEBUI111_SSE_EVENT = 1
	TYPE_COMFY_WS = 2
	def __init__(self):
		self.dtype = self.TYPE_UNKNOWN
		self.event_id = ""
		self.url = ""

class MessageStreamProcessor:
	def __init__(self):
		self.buffer = ""  # 用于存储流中的字符数据

	def Process(self, new_data: str):
		# 将新获取的字符数据追加到缓冲区
		self.buffer += new_data

		ret: typing.List[str] = []
		# 尝试分割消息
		while True:
			if len(self.buffer) == 0:
				break
			if not self.buffer.startswith("data: "):
				raise Exception("buffer should starts with data")
			end_index = self.buffer.find("\n\n")  # 查找一条消息的结束
			
			# 如果找到了一个完整的消息
			if end_index != -1:
				# 提取完整的消息
				message = self.buffer[len("data: "): end_index]
				ret.append(message)  # 将消息加入到消息列表

				# 将缓冲区更新为剩余的数据（去掉已处理的部分）
				self.buffer = self.buffer[end_index + len("\n\n"):]

			# 如果没有找到完整的消息，说明数据不完整，等待下一次流数据
			else:
				break
		return ret
class StableDiffusionCachingClient(Client):
	CLIENT_BACKEND_UNKNOWN = 0
	CLIENT_BACKEND_SDFORGE = 1
	CLIENT_BACKEND_COMFY = 2

	def __init__(self, config: Configuration):
		super().__init__(config)
		
		self.root_dir = 'cache_sdwebui'
		self.assets_dir = ['/assets', '/webui-assets', "/templates"]
		self.sdDirPattern = 'D:/GITHUB/stable-diffusion-webui-forge/'
		self.tmpDirPattern = r'^C:/Users/[a-zA-Z0-9._-]+/AppData/Local/Temp/'
		self.keep_list = ["outputs", "always_local", "models", "comfy_output"]
		self.alwayslocal_dir = os.path.join(self.root_dir, "always_local")
		self.use_localfiles = {
			"extensions/a1111-sd-webui-tagcomplete/tags/e621.csv": os.path.join(self.alwayslocal_dir, "e621.csv"),
			"extensions/a1111-sd-webui-tagcomplete/tags/extra-quality-tags.csv": os.path.join(self.alwayslocal_dir, "extra-quality-tags.csv"),
			"html/card-no-preview.png": os.path.join(self.alwayslocal_dir, "card-no-preview.png")
		}
		self.comfyHistoryTrackId = ""
		self.comfyHistory = OrderedDict()
		
		os.makedirs(self.root_dir,exist_ok=True)
		for assets_fd in self.assets_dir:
			os.makedirs(self.root_dir + assets_fd, exist_ok=True)

		#http://127.0.0.1:7860/assets/Index-_i8s1y64.js
		#http://127.0.0.1:7860/webui-assets/fonts/sourcesanspro/6xKydSBYKcSV-LCoeQqfX1RYOo3i54rwlxdu.woff2
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/extensions/a1111-sd-webui-tagcomplete/tags/temp/wc_yaml.json?1743699414113=
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-grids/2025-04-04/grid-0037.png
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-images/2025-04-04/00146-3244803616.png
		#http://127.0.0.1:7860/file=extensions/a1111-sd-webui-tagcomplete/javascript/ext_umi.js?1729071885.9683821=
		#http://127.0.0.1:7860/file=C:/Users/xxx/AppData/Local/Temp/gradio/tmpey_xm_6q.png
		#http://127.0.0.1:7860/sd_extra_networks/thumb?filename=D:/GITHUB/stable-diffusion-webui-forge/models/Lora/Illustrious/xxx.png&mtime=1749270957.098788

		self.nocipher = encryptor.NewCipher('plain', '')

		# session_hash to event_id
		self.requireSession: typing.Dict[str, str] = {}
		# key is stream seq id, value is StreamListenInfo, 监听Stream流
		self.listenInStream: typing.Dict[str, StreamListenInfo] = {}

		# grid url to StableDiffusionGridCacheInfo
		self.imageGridWaitList: typing.Dict[str, StableDiffusionGridCacheInfo] = {}
		# image url to grid url
		self.imageWaitList: typing.Dict[str, str] = {}

		self.backendType = self.CLIENT_BACKEND_UNKNOWN

		self.clean()

	def clean(self):
		for dname in os.listdir(self.root_dir):
			if dname in self.keep_list:
				continue
			
			fn = os.path.join(self.root_dir, dname)
			if os.path.isdir(fn):
				log.info(f"Delete cache folder: {fn}")
				shutil.rmtree(fn)
			else:
				log.info(f"Delete cache file: {fn}")
				os.remove(fn)
	
	async def Stream(self, seq_id: str):
		if seq_id not in self.listenInStream or self.listenInStream[seq_id].dtype not in [StreamListenInfo.TYPE_WEBUI111_SSE_EVENT]:
			if seq_id in self.listenInStream:
				log.error(f"Unknown stream type to listen: {self.listenInStream[seq_id].dtype}, fallback to normal call.")
			
			async for package in super().Stream(seq_id):
				yield package
			return
		
		if self.listenInStream[seq_id].dtype == StreamListenInfo.TYPE_WEBUI111_SSE_EVENT:
			msgproc = MessageStreamProcessor()
			print("===Capture: ", seq_id, "event:", self.listenInStream[seq_id].event_id)
			del self.listenInStream[seq_id]

			# 寻找可能的最终图的url，包括grid和每个image的url，这样就知道grid的url对应哪些图片
			async for package in super().Stream(seq_id):
				body = package.body.decode('utf-8')
				msgs = msgproc.Process(body)
				for msg in msgs:
					sdata = json.loads(msg)
					if sdata['msg'] == 'process_completed' and sdata["success"] and len(sdata["output"]["data"]) == 4 and type(sdata["output"]["data"][0]) == list and len(sdata["output"]["data"][0]) > 0 and type(sdata["output"]["data"][1]) == str:
						cur_event_id = sdata["event_id"]
						imageInfos: typing.List[typing.Any] = sdata["output"]["data"][0]
						gridUrls = [ x["image"]["url"] for x in imageInfos if '-grids' in x["image"]["url"] ]
						if len(gridUrls) != 1:
							log.error(f"{self.name}] grid url more than 1. unexpected case need to consider: {sdata}")
						else:
							# /file=D:\\GITHUB\\stable-diffusion-webui-forge\\outputs\\txt2img-grids\\2025-05-19\\grid-0057.png
							task = json.loads(sdata["output"]["data"][1])
							all_prompts: typing.List[str] = task["all_prompts"]
							gridUrl: str =  urlparse(gridUrls[0].replace('\\', '/')).path
							imgUrls = [ urlparse(x["image"]["url"].replace('\\', '/')).path for x in imageInfos if '-grids' not in x["image"]["url"] ]
							for imgUrl in imgUrls:
								self.imageWaitList[imgUrl] = gridUrl
							self.imageGridWaitList[gridUrl] = StableDiffusionGridCacheInfo(task["width"], task["height"], gridUrl, imgUrls, all_prompts)
							log.info(f"{self.name}] detected grid image: {gridUrl}")
				yield package
		# elif self.listenInStream[seq_id].dtype == StreamListenInfo.TYPE_COMFY_WS:
		# 	async for package in super().Stream(seq_id):
		# 		package
	
	def useCache(self, request: protocol.Request, filename: str):
		with open(filename, 'rb') as f:
			body = f.read()
		resp = protocol.Response(self.nocipher)
		resp.SetSenderReceiver(self.config.target_uid, self.config.uid)
		resp.Init(request.url, 200, {
			'Content-Type': utils.GuessMimetype(filename),
			'Content-Length': str(len(body))
		}, body)
		return resp

	async def cacheOrRequest(self, request: protocol.Request, filename: str):
		if os.path.exists(filename):
			log.debug(f'Using cache: {request.url} => {filename}')
			return self.useCache(request, filename)

		resp = await super().Session(request)
		if resp.status != 200:
			return resp
		try:
			dirname = os.path.dirname(filename)
			os.makedirs(dirname, exist_ok=True)
		except Exception as e:
			log.error(f"Failed to create dir: {dirname} for request url: {request.url}, error: {e}", exc_info=True, stack_info=True)
		with open(filename, 'wb') as f:
			f.write(resp.body)
			log.debug(f'Cache {request.url} to {filename}')
		return resp
		
	async def cacheImageOrRequest(self, request: protocol.Request, filename: str):
		if os.path.exists(filename):
			log.debug(f'Using cache: {request.url} => {filename}')
			return self.useCache(request, filename)
		
		urlpath = urlparse(request.url)
		if urlpath.path in self.imageWaitList:
			gridUrl = self.imageWaitList[urlpath.path]
			img, shouldDelete = await self.imageGridWaitList[gridUrl].WaitAndGet(urlpath.path)

			if shouldDelete:
				for imgUrl in self.imageGridWaitList[gridUrl].imglist:
					del self.imageWaitList[imgUrl]
				del self.imageGridWaitList[gridUrl]
				log.info(f"Delete grid info: {gridUrl}")

			if img is not None:
				os.makedirs(os.path.dirname(filename), exist_ok=True)
				img.img.save(filename, exif=img.metadata)
				log.info(f'Save result from grid: {filename}')
				return self.useCache(request, filename)
			
			log.error(f"Not hit grid, got ret None for img {request.url} but in waitlist")
		
		utils.SetWSFCompress(request, utils.MIMETYPE_WEBP, self.config.img_quality)
		resp = await super().Session(request)
		if resp.status != 200:
			return resp
		img = utils.DecodeImageFromBytes(resp.body)
		os.makedirs(os.path.dirname(filename), exist_ok=True)
		img.save(filename)
		log.info(f'Save result: {filename}')
		if urlpath.path in self.imageGridWaitList:
			self.imageGridWaitList[urlpath.path].Set(img)
		return self.useCache(request, filename)

	async def Session(self, request: protocol.Request):
		parsed_url = urlparse(request.url)

		# 缓存assets的路径
		if parsed_url.path.startswith(tuple(self.assets_dir)):
			if self.backendType == self.CLIENT_BACKEND_COMFY:
				cachefn = os.path.join(self.alwayslocal_dir, parsed_url.path[1:])
			else:
				cachefn = os.path.join(self.root_dir, parsed_url.path[1:])
			return await self.cacheOrRequest(request, cachefn)
		
		# 在sse流中找可能的最终图的url
		if parsed_url.path == '/queue/join':
			querys = json.loads(request.body)
			if querys["trigger_id"] != 16:
				return await super().Session(request)
			
			session_hash: str = querys["session_hash"]
			print("Infer session hash: ", session_hash)
			resp = await super().Session(request)
			ans = json.loads(resp.body)
			eventId: str = ans["event_id"]

			self.requireSession[session_hash] = eventId
			return resp
		
		# 哪个sse流中最有可能包括最终图的url
		if parsed_url.path == "/queue/data":
			querys = parse_qs(parsed_url.query)
			session_hash = querys["session_hash"][0]
			# print("Query session hash: ", session_hash)
			if session_hash in self.requireSession:
				self.listenInStream[request.seq_id] = self.requireSession[session_hash]
				del self.requireSession[session_hash]
				# print("== got /queue/data for session_hash: ", session_hash, "seq:", request.seq_id)
			return await super().Session(request)
		
		# 最终图
		if parsed_url.path.startswith("/file="):
			filepath = parsed_url.path[len("/file="):]

			if re.match(self.tmpDirPattern, filepath):
				# 使用re.sub()去掉匹配的前缀部分
				cachefn = os.path.join(self.root_dir, re.sub(self.tmpDirPattern, '', parsed_url.path[len("/file="):]))
				if cachefn.endswith(".png"):
					return await self.cacheImageOrRequest(request, cachefn)
				else:
					return await self.cacheOrRequest(request, cachefn)

			if filepath.startswith(self.sdDirPattern):
				relativefn = filepath[len(self.sdDirPattern):]
			else:
				relativefn = filepath

			cachefn = os.path.join(self.root_dir, relativefn)
			if relativefn.startswith("outputs"):
				return await self.cacheImageOrRequest(request, cachefn)
			elif relativefn in self.use_localfiles:
				return await self.cacheOrRequest(request, self.use_localfiles[relativefn])
			else:
				return await self.cacheOrRequest(request, cachefn)
		
		# lora预览图
		if parsed_url.path == "/sd_extra_networks/thumb":
			querys = parse_qs(parsed_url.query)
			filepath = querys["filename"][0]

			if filepath.startswith(self.sdDirPattern):
				relativefn = filepath[len(self.sdDirPattern):]
				cachefn = os.path.join(self.root_dir, relativefn)
				if relativefn.startswith("models/Lora") and relativefn.endswith('.png'):
					return await self.cacheImageOrRequest(request, cachefn)
				else:
					return await self.cacheOrRequest(request, cachefn)
			
			cachefn = os.path.join(self.root_dir, filepath)
			return await self.cacheOrRequest(request, cachefn)
				
		if parsed_url.path == "/":
			cachefn = os.path.join(self.root_dir, "index.html")
			resp = await self.cacheOrRequest(request, cachefn)
			try:
				html_str = resp.body[:120].decode('utf-8')
				# 使用正则表达式匹配<title>标签内容
				match = re.search(r'<title>(.*?)</title>', html_str, re.IGNORECASE)
				if match:
					title = match.group(1).lower()
					if 'comfy' in title:
						self.backendType = self.CLIENT_BACKEND_COMFY
						log.info(f"Detected ComfyUI backend.")
					else: # TODO: check forge title and make a condition
						self.backendType = self.CLIENT_BACKEND_SDFORGE
						log.info(f"Detected SDForge backend.")
			except Exception as e:
				log.error(f"cannot extract title from index.html, got {e}")
			return resp

		# 进度
		if parsed_url.path == "/internal/progress":
			utils.SetWSFCompress(request, utils.MIMETYPE_WEBP, self.config.img_quality)
			return await super().Session(request)

		if parsed_url.path in ["/favicon.ico", "/theme.css"] or parsed_url.path.startswith("/custom_component"):
			cachefn = os.path.join(self.root_dir, parsed_url.path[1:])
			return await self.cacheOrRequest(request, cachefn)
		
		# comfy ui 最终图
		if parsed_url.path == "/api/view":
			querys = parse_qs(parsed_url.query)
			goodpath = True
			for requireQuery in ["filename", "subfolder", "type"]:
				if requireQuery not in querys:
					goodpath = False
					break

			if goodpath == False:
				log.error(f"Cannot decide the saved location of output file: {request.url} , disable cache function and image optimize")
				return await super().Session(request)
			else:
				if querys["type"][0] != "output":
					log.error(f"Unknown /api/view type: {querys['type'][0]} , disable cache function and image optimize")
					return await super().Session(request)
				else:
					cachefn = os.path.join(self.root_dir, f"comfy_{querys['type'][0]}", querys["subfolder"][0], querys["filename"][0])
					return await self.cacheImageOrRequest(request, cachefn)
		
		# comfy ui 历史节点存储
		if parsed_url.path == "/api/history" and request.method == 'GET':
			request.headers["WSF-INCREMENTAL"] = self.comfyHistoryTrackId
			ret = await super().Session(request)
			state = ret.headers["WSF-STATE"]
			if state == 'INIT':
				self.comfyHistory = json.loads(ret.body, object_pairs_hook=OrderedDict)
			elif state == 'APPEND':
				incData = json.loads(ret.body, object_pairs_hook=OrderedDict)
				for key, value in incData.items():
					self.comfyHistory[key] = value
			else:
				raise Exception(f"Unknown WSF-STATE: {state}")
			self.comfyHistoryTrackId = ret.headers["WSF-INCREMENTAL"]

			newJSON = json.dumps(self.comfyHistory, ensure_ascii=False)

			ret.body = newJSON.encode('utf8')
			if 'Content-Length' in ret.headers:
				ret.headers['Content-Length'] = str(len(ret.body))
			del ret.headers["WSF-STATE"]
			del ret.headers["WSF-INCREMENTAL"]
			return ret
				
		# comfy ui 缓存
		if os.path.splitext(parsed_url.path)[1].lower() in [".css", ".js", ".woff2", ".svg", ".json", ".webp"]:
			cachefn = os.path.join(self.root_dir, parsed_url.path[1:])
			return await self.cacheOrRequest(request, cachefn)
		
		return await super().Session(request)

	async def processControlPackage(self, pkg: protocol.Control):
		if pkg.controlType in [protocol.Control.QUERY_RPC, protocol.Control.RPC_RESP]:
			await self.putInControlSession(pkg)
			return
		
		return await super().processControlPackage(pkg)

class HttpServer:
	def __init__(self, conf: Configuration, client: Client) -> None:
		self.config = conf
		self.client = client

		self.client.DefineExitFunc(self.exitFunc)
		self.site: web.TCPSite = None
		self.runner: web.AppRunner = None

		self.plainCipher = encryptor.NewCipher('plain', '')

	async def exitFunc(self):
		await self.site.stop()
		await self.runner.shutdown()

	async def processSSE(self, resp: protocol.Response, ss: web.StreamResponse):
		try:
			await ss.write(resp.body)
			async for package in self.client.Stream(resp.seq_id):
				await ss.write(package.body)
		except aiohttp.client_exceptions.ClientConnectionResetError:
			pass # invoke GeneratorExit in client.Stream()
		except Exception as e:
			log.error(f"Unknown exception: {e}", exc_info=True, stack_info=True)

	async def processWS(self, resp: protocol.Response, ws: web.WebSocketResponse):
		try:
			wsd = protocol.WSStreamData(self.plainCipher)
			wsd.data = resp.body
			wsd.Unpack(wsd)
			if wsd.GetType() == protocol.WSStreamData.TYPE_TEXT:
				await ws.send_str(wsd.GetTextData())
			elif wsd.GetType() == protocol.WSStreamData.TYPE_BIN:
				await ws.send_bytes(wsd.GetBinData())
			else:
				log.fatal(f"Unsupported ws stream data type: {wsd.GetType()}")

			async for package in self.client.Stream(resp.seq_id):
				wsd = protocol.WSStreamData(self.config.cipher)
				wsd.Unpack(package)

				if wsd.GetType() == protocol.WSStreamData.TYPE_TEXT:
					await ws.send_str(wsd.GetTextData())
				elif wsd.GetType() == protocol.WSStreamData.TYPE_BIN:
					await ws.send_bytes(wsd.GetBinData())
				elif wsd.GetType() == protocol.WSStreamData.TYPE_END:
					break
				else:
					log.error(f"Unsupported ws stream data type: {wsd.GetType()}")
		except aiohttp.client_exceptions.ClientConnectionResetError:
			pass # invoke GeneratorExit in client.Stream()
		except Exception as e:
			log.error(f"Unknown exception: {e}", exc_info=True, stack_info=True)
		finally:
			# TODO: tell client the websocket has been closed
			log.info("WebSocket closed")

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		# 提取请求的相关信息
		headers = dict(request.headers)

		# comfy ui, prefer compress images
		if request.url.path == '/ws':
			utils.SetWSFCompress(headers, quality=self.config.img_quality)
		
		req = protocol.Request(self.config.cipher)
		req.Init(self.config.prefix + str(request.url.path_qs), request.method, headers)
		req.SetBody(await request.read() if request.can_read_body else None)
		
		resp = await self.client.Session(req)

		if utils.HasWSUpgarde(resp):
			ws = web.WebSocketResponse(autoping=True)
			await ws.prepare(request)
			await self.processWS(resp, ws)
			return ws

		if 'Content-Type' in resp.headers and 'text/event-stream' in resp.headers['Content-Type']:
			ss = web.StreamResponse(status=resp.status, headers=resp.headers)
			await ss.prepare(request)
			await self.processSSE(resp, ss)
			return ss
		
		return web.Response(
			status=resp.status,
			headers=resp.headers,
			body=resp.body
		)
		
	async def handlerControlPage(self, request: web.BaseRequest):
		return web.FileResponse(os.path.join(os.getcwd(), 'assets', 'control.html'))
	
	async def handlerControlQuery(self, request: web.BaseRequest):
		jsonObj = await self.client.ControlQuery()
		output = {
			"connected": self.config.target_uid in jsonObj
		}
		return web.json_response(output)
	
	async def handlerControlExit(self, request: web.BaseRequest):
		await self.client.ControlExit()
		return web.Response(status=200)
	
	async def handlerRPCPage(self, request: web.BaseRequest):
		return web.FileResponse(os.path.join(os.getcwd(), 'assets', 'rpc.html'))
	
	async def handlerFSPage(self, request: web.BaseRequest):
		return web.FileResponse(os.path.join(os.getcwd(), 'assets', 'filesystem.html'))

	async def handlerRPCQuery(self, request: web.BaseRequest):
		jsonObj = await self.client.ControlRPCQuery()
		return web.json_response(jsonObj)
	
	async def handlerRPCCall(self, request: web.BaseRequest):
		params = await request.json()
		rpcc = protocol.RPCCallControl(params['name'], params['params'])		
		jsonObj = await self.client.ControlRPCCall(rpcc)
		return web.json_response(jsonObj)
	
	async def handlerRPCProgress(self, request: web.BaseRequest):
		task: str = request.query["task"]
		jsonObj = await self.client.ControlRPCProgress(task)
		return web.json_response(jsonObj)
		
	async def MainLoop(self):
		app = web.Application(client_max_size=1024 * 1024 * 1024)
		app.router.add_get("/wsf-control", self.handlerControlPage)
		app.router.add_get("/wsf-control/query", self.handlerControlQuery)
		app.router.add_post("/wsf-control/exit", self.handlerControlExit)
		app.router.add_get("/wsf-rpc", self.handlerRPCPage)
		app.router.add_get("/wsf-rpc/query", self.handlerRPCQuery)
		app.router.add_post("/wsf-rpc/call", self.handlerRPCCall)
		app.router.add_get("/wsf-rpc/progress", self.handlerRPCProgress)
		app.router.add_get("/wsf-fs", self.handlerFSPage)
		app.router.add_route('*', '/{tail:.*}', self.MainLoopOnRequest)  # HTTP服务
		
		self.runner = web.AppRunner(app)
		await self.runner.setup()
		self.site = web.TCPSite(self.runner, "127.0.0.1", port=self.config.port)
		await self.site.start()
		
		log.info(f"Server started on 127.0.0.1:{self.config.port}")
		await asyncio.Future()

async def main(config: Configuration):
	client = StableDiffusionCachingClient(config)
	# client = Client(config)
	httpserver = HttpServer(config, client)
	await asyncio.gather(client.MainLoop(), httpserver.MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	loop = asyncio.get_event_loop()
	try:
		loop.run_until_complete(main(conf))
	except KeyboardInterrupt:
		print(f"==== Mainloop exit due to keyboard interrupt")
