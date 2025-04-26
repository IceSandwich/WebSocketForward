import typing
import utils
import encryptor
import time_utils
import protocol
import logging
import asyncio, aiohttp
import aiohttp.web as web
import argparse as argp

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client", terminalLevel=logging.DEBUG, saveInFile=True)

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		if args.cipher != "":
			self.cipher = encryptor.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")
		self.uid: str = args.uid
		self.target_uid: str = args.target_uid

		self.maxRetries:int = args.max_retries
		self.cacheQueueSize: int  = args.cache_queue_size
		self.safeSegmentSize: int  = args.safe_segment_size

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="Server address")
		parser.add_argument("--cipher", type=str, default="xor", help="Cipher to use for encryption")
		parser.add_argument("--key", type=str, default="WebSocket@Forward通讯密钥，必须跟另一端保持一致。", help="The key to use for the cipher. Must be the same with remote.")
		parser.add_argument("--uid", type=str, default="Client")
		parser.add_argument("--target_uid", type=str, default="Remote")

		parser.add_argument("--max_retries", type=int, default=3, help="Maximum number of retries")
		parser.add_argument("--cache_queue_size", type=int, default=100, help="Maximum number of messages to cache")
		parser.add_argument("--safe_segment_size", type=int, default=768*1024, help="Maximum size of messages to send, in Bytes")
		return parser
	
class Client:
	HttpUpgradedWebSocketHeaders = {"Upgrade": "websocket", "Connection": "Upgrade"}
	WebSocketMaxReadingMessageSize = 12 * 1024 * 1024 # 12MB

	STATUS_INIT = 0
	STATUS_INITIALIZING = 1
	STATUS_CONNECTED = 2

	def __init__(self, config: Configuration):
		self.config = config
		self.name = self.config.uid
		self.url = f'{config.server}?uid={config.uid}'

		self.instance_id = utils.NewId()
		self.status: int = self.STATUS_INIT

		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(config.cacheQueueSize)
		self.sendQueueLock = asyncio.Lock()

		self.ws: aiohttp.ClientWebSocketResponse = None

		self.forward_session: aiohttp.ClientSession = None

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
		ctrl = protocol.Control()
		ctrl.InitHelloClientControl(protocol.HelloClientControl(protocol.ClientInfo(self.instance_id, protocol.ClientInfo.CLIENT)))
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

		# if hello.IsEmpty(): # it means this is the new instance?
		# 	return

		# server will send all package he received. we need to decide which package to resend.
		async with self.sendQueueLock:
			for item in self.sendQueue:
				found = False
				for pkg in hello.pkgs:
					if pkg.IsSamePackage(item):
						found = True
						break
				if found == False:
					log.debug(f"{self.name}] Resend package {item.seq_id}")
					await ws.send_bytes(item.Pack())


			self.ws = ws
			self.status = self.STATUS_CONNECTED
		
		return None
	
	async def DirectSend(self, raw: protocol.Transport):
		await self.ws.send_bytes(raw.Pack())

	async def QueueSend(self, raw: protocol.Transport):
		async with self.sendQueueLock:
			if raw.transportType != protocol.Transport.CONTROL:
				self.sendQueue.Add(raw)

			if self.status == self.STATUS_CONNECTED:
				try:
					await self.DirectSend(raw)
				except Exception as e:
					log.error(f"{self.name}] Cannot Queuesend package {raw.seq_id} of type {protocol.Transport.Mappings.ValueToString(raw.transportType)}, err: {e}")
	
	async def QueueSendSmartSegments(self, raw: typing.Union[protocol.Request, protocol.Response]):
		"""
		自动判断是否需要分包，若需要则分包发送，否则单独发送。raw应该是加密后的包。
		多个Subpackage，先发送Subpackage，然后最后一个发送源data_type。
		分包不支持流包的分包，因为分包需要用到序号，而流包的序号有特殊含义和发包顺序。如果你希望序号不变，请使用QueueSend()。
		"""
		if raw.body is None or len(raw.body) == 0:
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

	async def processSSESession(self, req: protocol.Request, resp: aiohttp.ClientResponse):
		"""
		由processSSE()释放resp。raw是模板，函数里会填充数据再发送。
		"""
		cur_idx = 0
		# send data.Response for the first time and send data.Subpackage for the remain sequences, set total_cnt = -1 to end
		# 对于 SSE 流，持续读取并转发数据
		try:
			async for chunk in resp.content.iter_any():
				if not chunk: continue
				# 将每一行事件发送给客户端
				if cur_idx == 0:
					raw = protocol.Response(self.config.cipher)
					raw.SetForResponseTransport(req)
					raw.Init(req.url, resp.status, dict(resp.headers), chunk)

					log.debug(f"SSE >>>Begin {raw.seq_id} - {resp.url} {len(raw.body)} bytes <<< {repr(raw.body[:50])} ...>>>")
					await self.QueueSend(raw)
				else:
					raw = protocol.StreamData(self.config.cipher)
					raw.SetForResponseTransport(req)
					raw.SetBody(chunk)
					raw.SetIndex(cur_idx, 0)
					
					log.debug(f"SSE {raw.seq_id} - {cur_idx}-ith - {len(raw.body)} bytes <<< {repr(raw.body[:50])} ...>>>")
					await self.QueueSend(raw)
				# 第一个response的idx为0，第一个sse_subpackage的idx为1
				cur_idx = cur_idx + 1
		except Exception as e:
			log.error(f'SSE {raw.seq_id} end iter unexpectally. err: {e}')
		finally:
			raw = protocol.StreamData(self.config.cipher)
			raw.SetForResponseTransport(req)
			raw.SetIndex(cur_idx, -1)
			log.debug(f"SSE <<<End {raw.seq_id} - {resp.url} {len(raw.body)} bytes")
			await self.QueueSend(raw)
			await resp.release()
		
	async def processRequestPackage(self, req: protocol.Request):
		log.debug(f"Request {req.seq_id} - {req.method} {req.url} {len(req.body)} bytes")

		try:
			resp = await self.forward_session.request(req.method, req.url, headers=req.headers, data=req.body)
		except aiohttp.ClientConnectorError as e:
			log.error(f'Failed to forward request {req.url}: {e}')
			rep = protocol.Response(self.config.cipher)
			rep.SetForResponseTransport(req)
			rep.Init(req.url, 502, { 'Content-Type': 'text/plain; charset=utf-8', }, f'=== Proxy server cannot request: {e}'.encode('utf8'))
			return await self.QueueSendSmartSegments(rep)

		if 'text/event-stream' in resp.headers['Content-Type']:
			log.debug(f"SSE Response {req.seq_id} - {req.method} {resp.url} {resp.status}")
			asyncio.ensure_future(self.processSSESession(req, resp))
			return
		
		respData = await resp.content.read()
		if utils.HasWSFCompress(req): # optimize for image requests
			headers = dict(resp.headers)
			require_mimetype, require_quality = utils.GetWSFCompress(req)
			mimetype, rawData = utils.CompressImage(respData, require_mimetype, require_quality)
			compress_ratio = utils.ComputeCompressRatio(len(respData), len(rawData))
			utils.SetWSFCompress(headers, mimetype, quality=-1)

			raw = protocol.Response(self.config.cipher)
			raw.SetForResponseTransport(req)
			raw.Init(req.url, resp.status, headers, rawData)
			log.debug(f"Response(Compress {int(compress_ratio*10000)/100}%) {raw.seq_id} - {req.method} {resp.url} {resp.status} {len(raw.body)} bytes")
			await self.QueueSendSmartSegments(raw)
		else:
			log.debug(f"Response {req.seq_id} - {req.method} {resp.url} {resp.status} {len(respData)} bytes")
			raw = protocol.Response(self.config.cipher)
			raw.SetForResponseTransport(req)
			raw.Init(req.url, resp.status, dict(resp.headers), respData)
			await self.QueueSendSmartSegments(raw)

		await resp.release()
	
	async def SendMsg(self, msg: str):
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver(self.config.uid, self.config.target_uid)
		ctrl.InitPrintControl(f"{self.name}] {msg}")
		await self.DirectSend(ctrl)

	async def OnPackage(self, pkg: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage, protocol.StreamData, protocol.Control]) -> bool:
		"""
		返回False退出MainLoop
		"""
		if type(pkg) == protocol.Control:
			ctrlType = pkg.GetControlType()
			if ctrlType == protocol.Control.QUIT_SIGNAL:
				log.info(f"{self.name}] Receive quit signal from {pkg.sender}. Schedule exit mainloop.")
				return False
			elif ctrlType == protocol.Control.PRINT:
				log.info(f"% {pkg.DecodePrintMsg()}")
			elif ctrlType == protocol.Control.RETRIEVE_PKG:
				pi = pkg.DecodeRetrievePkg()
				found = False
				async with self.sendQueueLock:
					for item in self.sendQueue:
						if pi.IsSamePackage(item):
							found = True
							await self.DirectSend(item)
							break
				if found == False:
					await self.SendMsg(f"Cannot retreive package {pi.seq_id} because it doesn't exist in send queue.")
			else:
				log.error(f"{self.name}] Unsupported control type({protocol.Control.Mapping.ValueToString(ctrlType)})")
		elif type(pkg) == protocol.Request:
			await self.processRequestPackage(pkg)
		elif type(pkg) == protocol.Subpackage:
			log.error(f"{self.name}] Receive subpackage {pkg.seq_id} but not implement. Help wanted.")
		else:
			log.error(f"{self.name}] Unsupported package type({protocol.Transport.Mappings.ValueToString(pkg.transportType)})")
		return True

	async def MainLoop(self):
		curTries = 0
		exitSignal = False
		self.forward_session = aiohttp.ClientSession()
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
		bakHandler = self.ws
		self.ws = None
		await self.forward_session.close()
		return bakHandler
	
async def main(config: Configuration):
	client = Client(config)
	await client.MainLoop()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))