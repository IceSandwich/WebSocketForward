import encryptor
import protocol
import utils
import time_utils

import asyncio, logging, os, typing, re, json, aiohttp
from urllib.parse import urlparse
import aiohttp.web as web
import argparse as argp
import heapq

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote", terminalLevel=logging.DEBUG, saveInFile=True)

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		if args.cipher != "":
			self.cipher = encryptor.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")

		self.prefix: str = args.prefix
		self.port: int = args.port

		self.uid: str = args.uid
		self.target_uid: str = args.target_uid

		self.img_quality:int = args.prefer_img_quality

		self.maxRetries:int = args.max_retries
		self.cacheQueueSize: int  = args.cache_queue_size
		self.timeout = time_utils.Seconds(args.timeout)
		self.maxHeapSSEBufferSize: int = args.max_heapsse_buffer_size
		self.safeSegmentSize: int  = args.safe_segment_size
		self.streamRetrieveTimeout: int = args.stream_retrieve_timeout
		self.streamRetrieveTimes: int = args.stream_retrieve_times

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="The websocket server to connect to.")
		parser.add_argument("--cipher", type=str, default="xor", help=f"The cipher to use. Available: [{', '.join(encryptor.GetAvailableCipherMethods())}]")
		parser.add_argument("--key", type=str, default="WebSocket@Forward通讯密钥，必须跟另一端保持一致。", help="The key to use for the cipher. Must be the same with client.")

		parser.add_argument("--prefix", type=str, default="http://127.0.0.1:7860", help="The prefix of your requests.")
		parser.add_argument("--port", type=int, default=8130, help="The port to listen on.")

		parser.add_argument("--uid", type=str, default="Remote")
		parser.add_argument("--target_uid", type=str, default="Client")

		parser.add_argument("--prefer_img_quality", type=int, default=75, help="Compress the image to transfer fasterly.")

		parser.add_argument("--max_retries", type=int, default=3, help="Maximum number of retries")
		parser.add_argument("--cache_queue_size", type=int, default=100, help="Maximum number of messages to cache")
		parser.add_argument("--timeout", type=int, default=60, help="The maximum seconds to resend packages.")
		parser.add_argument("--max_heapsse_buffer_size", type=int, default=20)
		parser.add_argument("--safe_segment_size", type=int, default=768*1024, help="Maximum size of messages to send, in Bytes")
		parser.add_argument("--stream_retrieve_timeout", type=int, default=time_utils.Seconds(3))
		parser.add_argument("--stream_retrieve_times", type=int, default=3)

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

		# 用于重发的队列，已经发出的包不要存在这里（事实上client无需保存发出的包）
		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(self.config.cacheQueueSize)
		self.sendQueueLock = asyncio.Lock()

		self.sessionMap: typing.Dict[str, protocol.Transport] = {}
		self.sessionCondition = asyncio.Condition()

		self.controlSessionMap: typing.Dict[str, protocol.Control] = {}
		self.controlSessionCondition = asyncio.Condition()

		# 最小堆，根据cur_idx排序
		self.streamHeap: typing.Dict[str, typing.List[protocol.StreamData]] = {}
		self.streamCondition = asyncio.Condition()

		self.ws: aiohttp.ClientWebSocketResponse = None
		self.timer = utils.Timer()

		self.exitFunc: typing.Coroutine[typing.Any, typing.Any, typing.Any] = None

	def DefineExitFunc(self, exitFunc: typing.Coroutine[typing.Any, typing.Any, typing.Any]):
		self.exitFunc = exitFunc

	async def DirectSend(self, raw: protocol.Transport):
		await self.ws.send_bytes(raw.Pack())

	async def QueueSend(self, raw: protocol.Transport):
		async with self.sendQueueLock:
			raw.timestamp = time_utils.GetTimestamp()
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
			else:
				self.sendQueue.Add(raw)

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

			log.debug(f"Send subpackage({sp.seq_id}:{sp.cur_idx}/{sp.total_cnt}) - {len(sp.body)} bytes <<< {repr(sp.body[:50])} ...>>>")
			await self.QueueSend(sp)
		
		# 最后一个包，虽然是Resp/Req类型，但是data其实是传输数据的一部分，不是resp、req包。要全部合成一个才能解析成resp、req包。
		raw.SetBody(splitDatas[-1])
		raw.SetIndex(len(splitDatas) - 1, -1)

		log.debug(f"Send end subpackage({raw.seq_id}:{raw.cur_idx}/{raw.total_cnt}) {len(raw.body)} bytes <<< {repr(raw.body[:50])} ...>>>")
		await self.QueueSend(raw)

	async def Session(self, req: protocol.Request) -> protocol.Response:
		"""
		发送一个req，等待回应一个resp。适合于非流的数据传输。
		"""
		req.SetSenderReceiver(self.config.uid, self.config.target_uid)
		await self.QueueSendSmartSegments(req)

		async with self.sessionCondition:
			await self.sessionCondition.wait_for(lambda: req.seq_id in self.sessionMap)
			resp = self.sessionMap[req.seq_id]
			del self.sessionMap[req.seq_id]

			assert resp.transportType == protocol.Transport.RESPONSE, f"Session {req.seq_id} only accept Response package but got {protocol.Transport.Mappings.ValueToString(resp.transportType)}"
			return resp
		
	async def SessionControl(self, raw: protocol.Control):
		"""
		SessionControl使用的是DirectSend而非QueueSend。
		"""
		await self.DirectSend(raw)

		async with self.controlSessionCondition:
			await self.controlSessionCondition.wait_for(lambda: raw.seq_id in self.controlSessionMap)
			resp = self.controlSessionMap[raw.seq_id]
			del self.controlSessionMap[raw.seq_id]
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
			def __init__(that):
				super().__init__(self.config.streamRetrieveTimeout)
			
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
						log.warning(f"Receive older stream package {seq_id}, the expected one is {expectIdx}, and received is {self.streamHeap[seq_id][0].cur_idx}. Drop it.")
						heapq.heappop(self.streamHeap[seq_id]) # drop
						continue

					# 过新的包
					log.warning(f"Stream {seq_id} expect {expectIdx} but heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}. Wait for it or require resend after timeout.")
					if state == self.STREAM_STATE_NONE:
						async with stateLock:
							state = self.STREAM_STATE_WAIT_TO_RETRIEVE

							# 启动计时器
							cb = RetrieveTimerCallback()
							stateTimestamp = cb.GetTimestamp()
						await self.timer.AddTask(cb)
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
						if sendRetreveTimes <= self.config.streamRetrieveTimes:
							print(f"Stream  {seq_id} send retrieve request, cur times: {sendRetreveTimes}")
							raw = protocol.Control()
							raw.SetForResponseTransport(self.streamHeap[seq_id][0])
							raw.InitRetrievePkg(protocol.PackageId(
								cur_idx=expectIdx,
								total_cnt=0,
								seq_id=seq_id
							))
							await self.DirectSend(raw)
							sendRetreveTimes = sendRetreveTimes + 1
							continue
						else:
							log.error(f"Stream {seq_id} try to retrieve {expectIdx} in {sendRetreveTimes} times but still cannot get the sequence idx {expectIdx}, heap: {[x.cur_idx for x in self.streamHeap[seq_id]]}. Pop one anyway.")

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
	
	async def waitForOnePackage(self, ws: web.WebSocketResponse):
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				log.error(f"{self.name}] Error on waiting 1 package: {ws.exception()}")
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Parse(msg.data)
				log.debug(f"{self.name}] Got one package: {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} from {transport.sender}")
				return transport
		return None

	async def initialize(self, ws: web.WebSocketResponse):
		async with self.historyRecvQueueLock:
			hcc = protocol.HelloClientControl(protocol.ClientInfo(self.instance_id, protocol.ClientInfo.REMOTE))
			for pkg in self.historyRecvQueue:
				hcc.Append(pkg)
			ctrl = protocol.Control()
			ctrl.InitHelloClientControl(hcc)
			await ws.send_bytes(ctrl.Pack())

			pkg = await self.waitForOnePackage(ws)
			if pkg is None:
				log.error(f"{self.name}] Broken ws connection in initialize stage.")
				self.status = self.STATUS_INIT
				return ws
			if type(pkg) != protocol.Control:
				log.error(f"{self.name}] First client package must be control package, but got {protocol.Transport.Mappings.ValueToString(pkg.transportType)}.")
				self.status = self.STATUS_INIT
				return ws
			if pkg.controlType == protocol.Control.PRINT: # error msg from server
				log.error(f"{self.name}] Error from server: {pkg.DecodePrintMsg()}")
				self.status = self.STATUS_INIT
				return ws
			if pkg.controlType != protocol.Control.HELLO:
				log.error(f"{self.name}] First client control package must be HELLO, but got {protocol.Control.Mappings.ValueToString(pkg.controlType)}.")
				self.status = self.STATUS_INIT
				return ws
			hello = pkg.ToHelloServerControl()

			now = time_utils.GetTimestamp()
			async with self.sendQueueLock:
				for item in self.sendQueue:
					if time_utils.WithInDuration(item.timestamp, now, self.config.timeout):
						log.debug(f"{self.name}] Resend package {item.seq_id}")
						await ws.send_bytes(item.Pack())
					else:
						log.warning(f"{self.name}] Package {item.seq_id} skip resend because it's out-dated({now} - {item.timestamp} = {now - item.timestamp} > {self.config.timeout}).")
				self.sendQueue.Clear()

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
		log.debug(f"Receive {raw.seq_id} subpackage({raw.cur_idx}/{raw.total_cnt}) - {len(raw.body)} bytes <<< {repr(raw.body[:50])} ...>>>")
		await self.chunks[raw.seq_id].Put(raw)
		if not self.chunks[raw.seq_id].IsFinish():
			# current package is a subpackage. we should not invoke internal callbacks until all package has received.
			return None
		ret = self.chunks[raw.seq_id].Combine()
		del self.chunks[raw.seq_id]
		return ret
	
	async def OnPackage(self, pkg: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage, protocol.StreamData, protocol.Control]) -> bool:
		if type(pkg) == protocol.Control:
			if pkg.controlType == protocol.Control.PRINT:
				log.info(f"% {pkg.DecodePrintMsg()}")
			elif pkg.controlType in [protocol.Control.QUERY_CLIENTS]:
				async with self.controlSessionCondition:
					self.controlSessionMap[pkg.seq_id] = pkg
					self.controlSessionCondition.notify_all()
			else:
				log.error(f"{self.name}] Unsupported control type({protocol.Control.Mapping.ValueToString(pkg.controlType)})")
		else:
			async with self.historyRecvQueueLock:
				self.historyRecvQueue.Add(protocol.PackageId.FromPackage(pkg))

			if type(pkg) == protocol.StreamData: # 流包的total_cnt=-1表示结束，为了区分Subpackage，先判断
				async with self.streamCondition:
					if pkg.seq_id not in self.streamHeap:
						self.streamHeap[pkg.seq_id] = []

					if pkg.total_cnt == -1:
						log.debug(f"Stream <<<End {pkg.seq_id} {len(pkg.body)} bytes")
					else:
						log.debug(f"Stream {pkg.seq_id}:{pkg.cur_idx} - {len(pkg.body)} bytes <<< {repr(pkg.body[:50])} ... >>>")

					heapq.heappush(self.streamHeap[pkg.seq_id], pkg)

					if len(self.streamHeap[pkg.seq_id]) > self.config.maxHeapSSEBufferSize:
						log.warning(f"Stream {pkg.seq_id} seems dead. Drop cache packages. headq: {[x.cur_idx for x in self.streamHeap[pkg.seq_id]]}")
						del self.streamHeap[pkg.seq_id]
					
					self.streamCondition.notify_all()
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

	async def MainLoop(self):
		curTries = 0
		exitSignal = False
		self.timer.Start()
		while curTries < self.config.maxRetries:
			async with aiohttp.ClientSession() as session:
				async with session.ws_connect(self.url, headers=self.HttpUpgradedWebSocketHeaders, max_msg_size=self.WebSocketMaxReadingMessageSize) as ws:
					self.status = self.STATUS_INITIALIZING
					initRet = await self.initialize(ws)
					if initRet is  None:
						self.ws = ws
						self.status = self.STATUS_CONNECTED
						curTries = 0
						log.info(f"Start mainloop...")

						async for msg in self.ws:
							if msg.type == aiohttp.WSMsgType.ERROR:
								log.error(f"{self.name}] WSException: {self.ws.exception()}")
							elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
								parsed = protocol.Parse(msg.data, self.config.cipher)
								shouldRuninng = await self.OnPackage(parsed)
								if shouldRuninng == False:
									exitSignal = True
									break
			
			self.status = self.STATUS_INIT
			if exitSignal == False:
				log.info(f"{self.name}] Reconnecting({curTries}/{self.config.maxRetries})...")
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
		assert resp.controlType == protocol.Control.QUERY_CLIENTS, f"ControlQuery {raw.seq_id} require to receive a QueryClients package but got {protocol.Control.Mapping.ValueToString(resp.controlType)}"

		return [ x.id for x in resp.ToQueryClientsControl() ]

	async def ControlExit(self):
		"""
		Exit控制包是没有返回的。
		"""
		raw = protocol.Control()
		raw.SetSenderReceiver(self.config.uid, self.config.target_uid)
		raw.InitQuitSignal()
		await self.DirectSend(raw)

class StableDiffusionCachingClient(Client):
	def __init__(self, config: Configuration):
		super().__init__(config)
		
		self.root_dir = 'cache_sdwebui'
		self.assets_dir = ['assets', 'webui-assets']
		self.sdprefix = 'D:/GITHUB/stable-diffusion-webui-forge/'
		
		os.makedirs(self.root_dir,exist_ok=True)
		for assets_fd in self.assets_dir:
			os.makedirs(os.path.join(self.root_dir, assets_fd), exist_ok=True)

		#http://127.0.0.1:7860/assets/Index-_i8s1y64.js
		#http://127.0.0.1:7860/webui-assets/fonts/sourcesanspro/6xKydSBYKcSV-LCoeQqfX1RYOo3i54rwlxdu.woff2
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/extensions/a1111-sd-webui-tagcomplete/tags/temp/wc_yaml.json?1743699414113=
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-grids/2025-04-04/grid-0037.png
		#http://127.0.0.1:7860/file=D:/GITHUB/stable-diffusion-webui-forge/outputs/txt2img-images/2025-04-04/00146-3244803616.png
		#http://127.0.0.1:7860/file=extensions/a1111-sd-webui-tagcomplete/javascript/ext_umi.js?1729071885.9683821=
		#http://127.0.0.1:7860/file=C:/Users/xxx/AppData/Local/Temp/gradio/tmpey_xm_6q.png

	def readCache(self, filename: str, url: str):
		log.debug(f'Using cache: {url} => {filename}')
		with open(filename, 'rb') as f:
			body = f.read()
		resp = protocol.Response(None)
		resp.SetSenderReceiver(self.config.target_uid, self.config.uid)
		resp.Init(url, 200, {
			'Content-Type': utils.GuessMimetype(filename),
			'Content-Length': str(len(body))
		}, body)
		return resp
	
	async def Session(self, request: protocol.Request):
		parsed_url = urlparse(request.url)
		components = parsed_url.path.strip('/').split('/')

		if components[0] in self.assets_dir:
			relativefn = parsed_url.path[1:]
		elif components[0].startswith('file='):
			relativefn = parsed_url.path[len('/file='):]
			if relativefn.startswith(self.sdprefix):
				relativefn = relativefn[len(self.sdprefix):].strip('/')
			pattern = r'^C:/Users/[a-zA-Z0-9._-]+/AppData/Local/Temp/'
			if re.match(pattern, relativefn):
				# 使用re.sub()去掉匹配的前缀部分
				relativefn = re.sub(pattern, '', relativefn)
		elif parsed_url.path == '/':
			relativefn = 'index.html' # cache index.html
		else:
			return await super().Session(request)
		targetfn = os.path.join(self.root_dir, relativefn)

		if os.path.exists(targetfn):
			return self.readCache(targetfn, request.url)
		else:
			if relativefn.startswith('outputs/'):
				utils.SetWSFCompress(request, utils.MIMETYPE_WEBP, self.config.img_quality)
				resp = await super().Session(request)
				if resp.status != 200:
					return resp
				img = utils.DecodeImageFromBytes(resp.body)
				os.makedirs(os.path.dirname(targetfn), exist_ok=True)
				img.save(targetfn)
				log.info(f'Save result: {targetfn}')
				with open(targetfn, 'rb') as f:
					resp.body = f.read()
					if 'Content-Length' in resp.headers:
						resp.headers['Content-Length'] = str(len(resp.body))
			else:
				resp = await super().Session(request)
				if resp.status != 200:
					return resp
				try:
					dirname = os.path.dirname(targetfn)
					os.makedirs(dirname, exist_ok=True)
				except Exception as e:
					log.error(f"Failed to create dir: {dirname} for request url: {request.url}, error: {e}")
				with open(targetfn, 'wb') as f:
					f.write(resp.body)
					log.debug(f'Cache {parsed_url.path} to {targetfn}')
			return resp

class HttpServer:
	def __init__(self, conf: Configuration, client: Client) -> None:
		self.config = conf
		self.client = client

		self.client.DefineExitFunc(self.exitFunc)
		self.site: web.TCPSite = None
		self.runner: web.AppRunner = None

	async def exitFunc(self):
		await self.site.stop()
		await self.runner.shutdown()

	async def processSSE(self, resp: protocol.Response):
		yield resp.body
		async for package in self.client.Stream(resp.seq_id):
			yield package.body

	async def MainLoopOnRequest(self, request: web.BaseRequest):
		# 提取请求的相关信息
		req = protocol.Request(self.config.cipher)
		req.Init(self.config.prefix + str(request.url.path_qs), request.method, dict(request.headers))
		req.SetBody(await request.read() if request.can_read_body else None)
		
		resp = await self.client.Session(req)

		if 'Content-Type' in resp.headers and 'text/event-stream' in resp.headers['Content-Type']:
			return web.Response(
				status=resp.status,
				headers=resp.headers,
				body=self.processSSE(resp)
			)
		else:
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
		
	async def MainLoop(self):
		app = web.Application(client_max_size=100 * 1024 * 1024)
		app.router.add_get("/wsf-control", self.handlerControlPage)
		app.router.add_get("/wsf-control/query", self.handlerControlQuery)
		app.router.add_post("/wsf-control/exit", self.handlerControlExit)
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
