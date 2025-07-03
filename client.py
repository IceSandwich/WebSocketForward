import io
import os
import shutil
import struct
import typing
from urllib.parse import urlparse
import uuid

import aiohttp.client_exceptions
import utils
import encryptor
import time_utils
import protocol
import logging
import asyncio, aiohttp
import aiohttp.web as web
import argparse as argp
import abc
import json
import base64
import requests
from PIL import Image
import math
from collections import OrderedDict

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client", terminalLevel=logging.DEBUG, saveInFile=True)
protocol.log = log
utils.log = log

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.cipher = encryptor.NewCipher(args.cipher, args.key, args.compress)
		log.info(f"Using cipher: {self.cipher.GetName()}")
		
		self.uid: str = args.uid
		self.target_uid: str = args.target_uid

		self.maxRetries:int = args.max_retries
		self.cacheQueueSize: int  = args.cache_queue_size
		self.cacheRecvQueueSize: int = args.cache_recv_queue_size
		self.safeSegmentSize: int  = args.safe_segment_size
		self.allowOFOResend: bool  = args.allow_ofo_resend
		self.resendScheduledTime: int  = args.resend_schedule_time

		self.waitToReconnect: int = args.wait_to_reconnect_time

		self.comfyPreviewSpeed: float = args.comfy_preview_speed

		# debug only
		self.force_id: str = args.force_id

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="Server address")
		parser.add_argument("--cipher", type=str, default="xor", help="Cipher to use for encryption")
		parser.add_argument("--key", type=str, default="必须使用同一个通讯密钥，否则WebSocketForward报错无法正常运行。", help="The key to use for the cipher. Must be the same with remote.")
		parser.add_argument("--uid", type=str, default="Client")
		parser.add_argument("--target_uid", type=str, default="Remote")
		parser.add_argument("--compress", type=str, default="none")

		parser.add_argument("--max_retries", type=int, default=3, help="Maximum number of retries")
		parser.add_argument("--cache_queue_size", type=int, default=128, help="Maximum number of messages to cache. Must smaller than server's cache queue size. Bigger is better since remote need retreive pkg.")
		parser.add_argument("--cache_recv_queue_size", type=int, default=150, help="Must bigger then server's cache queue size.")
		parser.add_argument("--safe_segment_size", type=int, default=768*1024, help="Maximum size of messages to send, in Bytes")
		parser.add_argument("--allow_ofo_resend", action="store_true", help="Allow out of order message to be resend. Disable this feature may results in buffer overflow issue. By default, this feature is disable.")
		parser.add_argument("--resend_schedule_time", type=int, default=time_utils.Seconds(1), help="Interval to resend scheduled packages")

		parser.add_argument("--wait_to_reconnect_time", type=int, default=time_utils.Seconds(2), help="Time to wait before reconnecting")

		parser.add_argument("--force_id", type=str, default="")
		parser.add_argument("--comfy_preview_speed", type=float, default=1.3, help="Speed to preview")
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
		self.plainCipher = encryptor.NewCipher("plain", "")

		self.instance_id = utils.NewId()
		self.status: int = self.STATUS_INIT

		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(config.cacheQueueSize)
		self.sendQueueLock = asyncio.Lock()

		# 只记录非control包
		self.historyRecvQueue: utils.BoundedQueue[protocol.PackageId] = utils.BoundedQueue(self.config.cacheRecvQueueSize)
		self.historyRecvQueueLock = asyncio.Lock()

		# 管理分包传输，key为seq_id
		self.chunks: typing.Dict[str, utils.Chunk] = {}

		self.ws: aiohttp.ClientWebSocketResponse = None

		self.forward_session: aiohttp.ClientSession = None
		self.forward_timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)

		self.ws_timeout = aiohttp.ClientWSTimeout(ws_close=time_utils.Minutes(2))

		self.scheduleSendQueue: asyncio.Queue[protocol.Transport] = asyncio.Queue()

		self.sseCloseSignal: typing.Dict[str, bool] = {}

		self.incrementalJson: typing.Dict[str, typing.List[typing.Tuple[str, typing.Any]]] = {}

	async def waitForOnePackage(self, ws: web.WebSocketResponse):
		try:
			async for msg in ws:
				if msg.type == aiohttp.WSMsgType.ERROR:
					log.error(f"1Pkg] Error: {ws.exception()}", exc_info=True, stack_info=True)
				elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
					transport = protocol.Transport.Parse(msg.data)
					log.debug(f"1Pkg] {protocol.Transport.Mappings.ValueToString(transport.transportType)} {transport.seq_id}:{transport.cur_idx}/{transport.total_cnt} <- {transport.sender}")
					return transport
		except Exception as e:
			log.error(f"1Pkg] Exception: {e}", exc_info=True, stack_info=True)
		log.error(f"1Pkg] WS broken.")
		return None

	async def initialize(self, ws: web.WebSocketResponse):
		async with self.historyRecvQueueLock:
			hcc = protocol.HelloClientControl(protocol.ClientInfo(self.instance_id if self.config.force_id == "" else self.config.force_id, protocol.ClientInfo.CLIENT))
			for pkg in self.historyRecvQueue:
				hcc.AppendReceivedPkgs(protocol.PackageId.FromPackage(pkg))
			# ignore sent packages, let remote retrieve it.
			ctrl = protocol.Control()
			ctrl.InitHelloClientControl(hcc)
			await ws.send_bytes(ctrl.Pack())

			pkg = await self.waitForOnePackage(ws)
			if pkg is None:
				log.error(f"{self.name}] Broken ws connection in initialize stage.")
				self.status = self.STATUS_INIT
				return ws
			if pkg.transportType != protocol.Transport.CONTROL:
				log.error(f"{self.name}] First client package must be control package, but got {protocol.Transport.Mappings.ValueToString(pkg.transportType)}.")
				self.status = self.STATUS_INIT
				return ws
			tmp = protocol.Control()
			tmp.Unpack(pkg)
			pkg = tmp
			if pkg.controlType == protocol.Control.PRINT: # error msg from server
				log.error(f"{self.name}] Error from server: {pkg.DecodePrintMsg()}")
				self.status = self.STATUS_INIT
				return ws
			if pkg.controlType != protocol.Control.HELLO:
				log.error(f"{self.name}] First client control package must be HELLO, but got {protocol.Control.Mappings.ValueToString(pkg.controlType)}.")
				self.status = self.STATUS_INIT
				return ws
			hello = pkg.ToHelloServerControl()

			# server will send all package he received. we need to decide which package to resend.
			async with self.sendQueueLock:
				log.debug(f"Server has received {len(hello.reports)} packages. We had sent {len(self.sendQueue)} packages.")
				for item in self.sendQueue:
					found = False
					for pkg in hello.reports:
						if pkg.IsSamePackage(item):
							found = True
							break
					if found == False:
						# 放入scheduleSendQueue而不是直接发送，是因为当前没有监听收包，万一对方发送大量的包，这里缓冲区就爆了
						# 因此放入schedule里，进入connect状态后，边监听边发送这些包（当开启OFOResend时）。缺点是导致乱序。
						await self.scheduleSendQueue.put(item)

				self.ws = ws
				self.status = self.STATUS_CONNECTED
		return None
	
	async def DirectSend(self, raw: protocol.Transport):
		await self.ws.send_bytes(raw.Pack())

	async def QueueSend(self, raw: protocol.Transport):
		async with self.sendQueueLock:
			# check
			if raw.sender != self.name:
				log.error(f"{self.name}] Send a package {raw.seq_id}:{protocol.Transport.Mappings.ValueToString(raw.transportType)} to {raw.receiver} but it's sender is {raw.sender}, which should be {self.name}. Online fix that.")
				raw.sender = self.name

			if raw.transportType != protocol.Transport.CONTROL:
				self.sendQueue.Add(raw)

			# client不用缓存非连接状态下的发送包，因为在连接开始时，server会报告它收到的包，client在非连接状态下发送的包肯定不会在报告内，因此会被重发
			if self.status == self.STATUS_CONNECTED:
				try:
					await self.DirectSend(raw)
				except Exception as e:
					log.error(f"Failed Cannot Queuesend package {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} of type {protocol.Transport.Mappings.ValueToString(raw.transportType)}, schedule to resend in next connection. err: {e}")
	
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

			log.debug(f"Subpackage {sp.seq_id}:{sp.cur_idx}/{sp.total_cnt} - {len(sp.body)} bytes <<< {repr(sp.body[:utils.LOG_BYTES_LEN])} ...>>> -> {sp.receiver}")
			await self.QueueSend(sp)
		
		# 最后一个包，虽然是Resp/Req类型，但是data其实是传输数据的一部分，不是resp、req包。要全部合成一个才能解析成resp、req包。
		raw.SetBody(splitDatas[-1])
		raw.SetIndex(len(splitDatas) - 1, -1)

		log.debug(f"Subpackage {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[-utils.LOG_BYTES_LEN:])} ...>>> -> {raw.receiver}")
		await self.QueueSend(raw)

	async def processSSESession(self, req: protocol.Request, resp: aiohttp.ClientResponse):
		"""
		由本函数释放resp。
		"""
		cur_idx = 0
		shouldSendEndStreamData = True
		# send Response for the first time and send StreamData for the remain sequences, set total_cnt = -1 to end
		# 对于 SSE 流，持续读取并转发数据
		self.sseCloseSignal[req.seq_id] = False
		try:
			async for chunk in resp.content.iter_any():
				if self.sseCloseSignal[req.seq_id] == True: # remote requires to close sse
					shouldSendEndStreamData = False # remote has already close the stream, we don't need to send end package
					del self.sseCloseSignal[req.seq_id]
					break
				if not chunk: continue
				# 将每一行事件发送给客户端
				if cur_idx == 0:
					raw = protocol.Response(self.config.cipher)
					raw.SetForResponseTransport(req)
					raw.Init(req.url, resp.status, dict(resp.headers), chunk)

					log.debug(f"SSE {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {resp.url} {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> -> {raw.receiver}")
					await self.QueueSend(raw)
				else:
					raw = protocol.StreamData(self.config.cipher)
					raw.SetForResponseTransport(req)
					raw.SetBody(chunk)
					raw.SetIndex(cur_idx, 0)
					
					log.debug(f"SSE {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> -> {raw.receiver}")
					await self.QueueSend(raw)
				# 第一个response的idx为0，第一个sse_subpackage的idx为1
				cur_idx = cur_idx + 1
		except asyncio.exceptions.TimeoutError:
			log.error(f"SSE {req.seq_id} timeout. by default, aiohttp use 300 seconds as timeout.")
		except Exception as e:
			log.error(f'SSE {req.seq_id} end iter unexpectally. err: {e}', exc_info=True, stack_info=True)
		finally:
			if shouldSendEndStreamData:
				raw = protocol.StreamData(self.config.cipher)
				raw.SetForResponseTransport(req)
				raw.SetIndex(cur_idx, -1)
				log.debug(f"SSE {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {resp.url} {len(raw.body)} bytes -x> {raw.receiver}")
				await self.QueueSend(raw)
			else:
				log.debug(f"SSE {raw.seq_id} end because {self.config.target_uid} requires to end.")
			await resp.release()

	async def postprocessWSPackage(self, req: protocol.Request, raw: protocol.WSStreamData) -> bool:
		"""
		返回False表示取消发送
		"""
		return True
	
	async def processWSSession(self, req: protocol.Request, resp: aiohttp.ClientResponse, ws: aiohttp.ClientWebSocketResponse):
		# 第一个response的idx为0，第一个sse_subpackage的idx为1
		cur_idx = 0
		self.sseCloseSignal[req.seq_id] = False
		shouldSendEndStreamData = True
		try:
			async for msg in ws:
				if self.sseCloseSignal[req.seq_id] == True: # remote requires to close sse
					shouldSendEndStreamData = False # remote has already close the stream, we don't need to send end package
					del self.sseCloseSignal[req.seq_id]
					break

				if msg.type == aiohttp.WSMsgType.ERROR:
					self.SendErrMsg(f"WebSocket Forward {req.url} got error: {self.ws.exception()}")
				elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
					if cur_idx == 0:
						# 在Response里装载WSStreamData的pb类型，我们只使用WSStreamData类的body变量
						wsd = protocol.WSStreamData(self.plainCipher)
						wsd.SetBody(msg.data)
						await self.postprocessWSPackage(req, wsd)

						raw = protocol.Response(self.config.cipher)
						raw.SetForResponseTransport(req)
						raw.Init(req.url, resp.status, dict(resp.headers), wsd.PackToPB())
						log.debug(f"WS {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {req.url} {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> -> {raw.receiver}")
						await self.QueueSend(raw)
						cur_idx = cur_idx + 1
					else:
						raw = protocol.WSStreamData(self.config.cipher)
						raw.SetForResponseTransport(req)
						raw.SetIndex(cur_idx, 0)
						raw.SetBody(msg.data)
						shouldSend = await self.postprocessWSPackage(req, raw)
						if shouldSend == False:
							log.debug(f"WS {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> -Skip-x> {raw.receiver}")
						else:
							log.debug(f"WS {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {len(raw.body)} bytes <<< {repr(raw.body[:utils.LOG_BYTES_LEN])} ...>>> -> {raw.receiver}")
							await self.QueueSend(raw)
							cur_idx = cur_idx + 1
		finally:
			if shouldSendEndStreamData:
				raw = protocol.WSStreamData(self.config.cipher)
				raw.SetForResponseTransport(req)
				raw.SetIndex(cur_idx, -1)
				raw.MarkEnd()
				log.debug(f"WS {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} - {req.url} {len(raw.body)} bytes -x> {raw.receiver}")
				await self.QueueSend(raw)
			else:
				log.debug(f"WS {raw.seq_id} end because {self.config.target_uid} requires to end.")
			await ws.close()

	async def optimizeResponsePackage(self, req: protocol.Request, resp: aiohttp.ClientResponse, respData: bytes):
		headers = dict(resp.headers)
		require_mimetype, require_quality = utils.GetWSFCompress(req)
		mimetype, rawData = utils.CompressImage(respData, require_mimetype, require_quality)
		compress_ratio = utils.ComputeCompressRatio(len(respData), len(rawData))
		utils.SetWSFCompress(headers, mimetype, quality=-1)

		raw = protocol.Response(self.config.cipher)
		raw.SetForResponseTransport(req)
		raw.Init(req.url, resp.status, headers, rawData)
		return raw, compress_ratio

	def checkMatchIncrementalJson(self, url: str, compare: typing.List[typing.Tuple[str, typing.Any]]):
		curlen = len(self.incrementalJson[url])
		if curlen > len(compare):
			return False
		for i in range(0, curlen):
			if compare[i][0]!= self.incrementalJson[url][i][0]:
				return False
		return True

	async def processRequestPackage(self, req: protocol.Request):
		log.debug(f"Request {req.seq_id} - {req.method} {req.url} {len(req.body)} bytes")

		for retry in range(self.config.maxRetries):
			try:
				resp = await self.forward_session.request(req.method, req.url, headers=req.headers, data=req.body)
				break
			except aiohttp.ClientConnectorError as e:
				log.error(f'Failed to forward request {req.url}, got ClientConnectorError: {e}')
				rep = protocol.Response(self.config.cipher)
				rep.SetForResponseTransport(req)
				rep.Init(req.url, 502, { 'Content-Type': 'text/plain; charset=utf-8', }, f'=== Proxy server cannot request: {e}'.encode('utf8'))
				return await self.QueueSendSmartSegments(rep)
			except aiohttp.client_exceptions.ClientOSError as e:
				log.error(f"Failed to forward request {req.url}, current times: {retry}, got ClientOSError: {e}")
				retry += 1
				if retry >= self.config.maxRetries:
					await self.SendErrMsg(f"Tried {self.config.maxRetries} times but still cannot forward request {req.seq_id} for url: {req.url}. Got ClientOSError: {e}. Client abort.")
					raise e
				
		if utils.HasWSUpgarde(resp):
			try:
				ws = await self.forward_session.ws_connect(req.url, method=req.method, headers=req.headers)
				asyncio.ensure_future(self.processWSSession(req, resp, ws))
			except Exception as e:
				self.SendErrMsg(f"Failed to forward ws request {req.url}, got: {e}")
			return
		
		if 'Content-Type' in resp.headers and 'text/event-stream' in resp.headers['Content-Type']:
			# log.debug(f"SSE Response {req.seq_id} - {req.method} {resp.url} {resp.status}")
			asyncio.ensure_future(self.processSSESession(req, resp))
			return
		
		respData = await resp.content.read()

		if utils.HasIncremental(req):
			startIdx = int(req.headers["WSF-INCREMENTAL"]) if req.headers["WSF-INCREMENTAL"] != "" else -1
			dt = utils.OrderedDictToList(json.loads(respData, object_pairs_hook=OrderedDict))
			if req.url not in self.incrementalJson or startIdx == -1 or startIdx > len(self.incrementalJson[req.url]) or self.checkMatchIncrementalJson(req.url, dt) == False:
				self.incrementalJson[req.url] = dt

				raw = protocol.Response(self.config.cipher)
				raw.SetForResponseTransport(req)
				raw.Init(req.url, resp.status, dict(resp.headers), respData)
				raw.headers["WSF-INCREMENTAL"] = str(len(self.incrementalJson[req.url]))
				raw.headers["WSF-STATE"] = "INIT"
				await self.QueueSendSmartSegments(raw)
			else:
				self.incrementalJson[req.url] = dt
				ret = utils.OrderedDictListToJson(self.incrementalJson[req.url][startIdx:])

				raw = protocol.Response(self.config.cipher)
				raw.SetForResponseTransport(req)
				raw.Init(req.url, resp.status, dict(resp.headers), ret.encode('utf-8'))
				raw.headers["WSF-INCREMENTAL"] = str(len(self.incrementalJson[req.url]))
				raw.headers["WSF-STATE"] = "APPEND"
				await self.QueueSendSmartSegments(raw)

			await resp.release()
			return

		if utils.HasWSFCompress(req): # optimize for image requests
			raw, compress_ratio = await self.optimizeResponsePackage(req, resp, respData)
			if compress_ratio == 0.0:
				log.debug(f"Response {req.seq_id} - {req.method} {resp.url} {resp.status} {len(respData)} bytes")
			else:
				log.debug(f"Response(Compress {int(compress_ratio*10000)/100}%) {raw.seq_id} - {req.method} {resp.url} {resp.status} {len(raw.body)} bytes")
			await self.QueueSendSmartSegments(raw)
		else:
			log.debug(f"Response {req.seq_id} - {req.method} {resp.url} {resp.status} {len(respData)} bytes")
			raw = protocol.Response(self.config.cipher)
			raw.SetForResponseTransport(req)
			raw.Init(req.url, resp.status, dict(resp.headers), respData)
			await self.QueueSendSmartSegments(raw)

		await resp.release()
	
	async def processControlPackage(self, pkg: protocol.Control) -> bool:
		"""
		返回False退出MainLoop
		"""
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
				log.debug(f"{self.name}] Retrieve package {pi.seq_id}:{pi.cur_idx}/{pi.total_cnt}.")
		else:
			log.error(f"{self.name}] Unsupported control type({protocol.Control.Mappings.ValueToString(ctrlType)})")
		return True

	async def SendMsg(self, msg: str):
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver(self.config.uid, self.config.target_uid)
		ctrl.InitPrintControl(f"{self.name}] {msg}")
		log.info(f"Send Msg] {msg} -> {self.config.target_uid}")
		await self.DirectSend(ctrl)

	async def SendErrMsg(self, msg: str):
		"""
		send and log to error
		"""
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver(self.config.uid, self.config.target_uid)
		ctrl.InitPrintControl(f"{self.name}] {msg}")
		log.error(f"Send Msg] {msg} -> {self.config.target_uid}")
		await self.DirectSend(ctrl)

	async def OnPackage(self, pkg: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage, protocol.StreamData, protocol.Control]) -> bool:
		"""
		返回False退出MainLoop
		"""
		if pkg.transportType != protocol.Transport.CONTROL:
			async with self.historyRecvQueueLock:
				self.historyRecvQueue.Add(protocol.PackageId.FromPackage(pkg))

		if type(pkg) == protocol.StreamData:
			if pkg.total_cnt != -1:
				log.error(f"{self.name}] Client don't process normal StreamData. Only receive end stream StreamData. Ignore {pkg.seq_id}:{pkg.cur_idx}/{pkg.total_cnt} <<< {repr(pkg.body[:utils.LOG_BYTES_LEN * 2])} ...>>> <- {pkg.sender}")
			elif pkg.seq_id not in self.sseCloseSignal:
				log.error(f"{self.name}] {pkg.sender} requires to close stream {pkg.seq_id} but we don't listen that stream. Ignore.")
			else:
				log.debug(f"Stream {pkg.seq_id}:{pkg.cur_idx}/{pkg.total_cnt} - {len(pkg.body)} bytes <x- {pkg.sender}")
				self.sseCloseSignal[pkg.seq_id] = True
			return
		
		if type(pkg) == protocol.Control:
			return await self.processControlPackage(pkg)
		else:
			if type(pkg) == protocol.Subpackage or pkg.total_cnt == -1:
				combine = await self.processSubpackage(pkg)
				if combine is None:
					return True
				pkg = combine
			if pkg.transportType == protocol.Transport.REQUEST:
				await self.processRequestPackage(pkg)
			else:
				log.error(f"{self.name}] Unsupported package type({protocol.Transport.Mappings.ValueToString(pkg.transportType)})")
		return True

	async def resendScheduledPackages(self):
		log.debug(f"Schedule] Resend {self.scheduleSendQueue.qsize()} packages.")
		while not self.scheduleSendQueue.empty():
			item = await self.scheduleSendQueue.get()
			log.debug(f"Schedule] Resend {protocol.Transport.Mappings.ValueToString(item.transportType)} {item.seq_id}:{item.cur_idx}/{item.total_cnt} -> {item.receiver}")
			await self.DirectSend(item)
			await asyncio.sleep(self.config.resendScheduledTime)

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

	async def MainLoop(self):
		curTries = 0
		exitSignal = False
		self.forward_session = aiohttp.ClientSession(timeout=self.forward_timeout)
		while curTries < self.config.maxRetries:
			try:
				async with aiohttp.ClientSession() as session:
					async with session.ws_connect(self.url, headers=self.HttpUpgradedWebSocketHeaders, max_msg_size=self.WebSocketMaxReadingMessageSize, timeout=self.ws_timeout) as ws:
						log.info(f"Initializing...")
						self.status = self.STATUS_INITIALIZING
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
									log.error(f"{self.name}] WSException: {self.ws.exception()}", exc_info=True, stack_info=True)
								elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
									parsed = protocol.Parse(msg.data, self.config.cipher)
									shouldRuninng = await self.OnPackage(parsed)
									if shouldRuninng == False:
										exitSignal = True
										break
			except aiohttp.client_exceptions.ClientConnectorError as e:
				log.error(f"{self.name}] ClientConnectorError: {e}", exc_info=True, stack_info=True)
			except aiohttp.client_exceptions.WSServerHandshakeError as e:
				log.error(f"{self.name}] WSServerHandshakeError: {e}", exc_info=True, stack_info=True)
			
			self.status = self.STATUS_INIT
			if exitSignal == False:
				log.info(f"{self.name}] Disconnected. Attempt to reconnect.")
				curTries += 1
				await asyncio.sleep(self.config.waitToReconnect)
				log.info(f"{self.name}] Reconnecting({curTries}/{self.config.maxRetries})...")
			else:
				break
		bakHandler = self.ws
		await self.forward_session.close()
		await self.ws.close()
		self.ws = None
		log.info(f"Program exit normally.")
		return bakHandler
	

class RPCInfo(abc.ABC):
	def __init__(self, name: str, params: typing.List[typing.Dict[str, str]]) -> None:
		self.name = name
		self.params = params

	def GetName(self) -> str:
		return self.name
	
	def GetParamsDef(self) -> typing.List[typing.Dict[str, str]]:
		return self.params
	
	@abc.abstractmethod
	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		raise NotImplementedError()
	
class SayHello(RPCInfo):
	def __init__(self, client: Client):
		super().__init__("SayHello", [{
			"name": "message",
			"type": "string",
		}])
		self.client = client

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		await self.client.SendMsg(f"SayHello rpc called with message: {arg.params['message']}")
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.JSON, {
			"message": arg.params["message"],
		})
		return rpcrc
	
class RPCAdd(RPCInfo):
	def __init__(self):
		super().__init__("Add", [{
			"name": "a",
			"type": "int",
		}, {
			"name": "b",
			"type": "int",
		}])

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		a: int = arg.params["a"]
		b: int = arg.params["b"]
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.JSON, {
			"result": a + b,
		})
		return rpcrc

class RPCListFile(RPCInfo):
	def __init__(self):
		super().__init__("ListFile", [{
			"name": "path",
			"type": "string",
		}])

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		path: str = arg.params["path"]
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.JSON, {
			"result": [{
				"name": x,
				"type": os.path.isdir(os.path.join(path, x)) and "dir" or "file",
			} for x in os.listdir(path)]
		})
		return rpcrc
	
class RPCCopyFile(RPCInfo):
	def __init__(self):
		super().__init__("CopyFile", [{
			"name": "srcFilename",
			"type": "string",
		}, {
			"name": "dstDir",
			"type": "string",
		}])

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		srcFilename = arg.params["srcFilename"]
		dstDir = arg.params["dstDir"]
		if os.path.isdir(os.path.join(dstDir, srcFilename)):
			rpcrc = protocol.RPCResponseControl()
			rpcrc.Init(protocol.RPCResponseControl.JSON, {
				"error": f"{srcFilename} is not a file, but a dir",
			})
			return rpcrc
		try:
			shutil.copyfile(srcFilename, os.path.join(dstDir, os.path.basename(srcFilename)))
		except Exception as e:
			rpcrc = protocol.RPCResponseControl()
			rpcrc.Init(protocol.RPCResponseControl.JSON, {
				"error": f"Can't copy dir {srcFilename} to {dstDir}: {e}",
			})
			return rpcrc
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.JSON, {
			"result": [{
				"name": x,
				"type": os.path.isdir(os.path.join(dstDir, x)) and "dir" or "file",
			} for x in os.listdir(dstDir)]
		})
		return rpcrc
	
class RPCDeleteFile(RPCInfo):
	def __init__(self):
		super().__init__("DeleteFile", [{
			"name": "filename",
			"type": "string",
		}])

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		filename = arg.params["filename"]
		if os.path.isdir(filename):
			rpcrc = protocol.RPCResponseControl()
			rpcrc.Init(protocol.RPCResponseControl.JSON, {
				"error": f"{filename} is not a file, but a dir",
			})
			return rpcrc
		try:
			os.remove(filename)
		except Exception as e:
			rpcrc = protocol.RPCResponseControl()
			rpcrc.Init(protocol.RPCResponseControl.JSON, {
				"error": f"Can't delete file {filename}: {e}",
			})
			return rpcrc
		basedir = os.path.dirname(filename)
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.JSON, {
			"result": [{
				"name": x,
				"type": os.path.isdir(os.path.join(basedir, x)) and "dir" or "file",
			} for x in os.listdir(basedir)]
		})
		return rpcrc

class TaskManager:
	def __init__(self):
		self.tasks: typing.Dict[str, typing.Any] = {}

	def AddTask(self, taskData: typing.Any) -> str:
		name = uuid.uuid4().hex[:8]
		self.tasks[name] = taskData
		return name

	def UpdateTask(self, taskid: str, taskData: typing.Any):
		self.tasks[taskid] = taskData
	
	def HasTask(self, taskid: str) -> bool:
		return taskid in self.tasks
	
	def GetData(self, taskid: str):
		return self.tasks[taskid]

class RPCDownloadFile(RPCInfo):
	def __init__(self, tm: TaskManager):
		super().__init__("DownloadFile", [{
			"name": "url",
			"type": "string",
			"default": "https://speed.cloudflare.com/__down?bytes=10485760",
		}, {
			"name": "path",
			"type": "string",
			"default": "D:/GITHUB/stable-diffusion-webui-forge/models/Lora/Illustrious/Characters",
		}])
		self.tm = tm
	
	async def startDownload(self, resp: requests.Response, filename: str, taskid: str):
		data = self.tm.GetData(taskid)
		total = data["total"]
		
		with open(filename, 'wb') as f:
			for chunk in resp.iter_content(chunk_size=1024):
				if chunk:
					f.write(chunk)
					data["current"] = data["current"] + len(chunk)
		# for i in range(0, total, 128):
		# 	data["current"] = i
		# 	await asyncio.sleep(time_utils.Seconds(2))
		# 	print("downloading: ", data["current"])

		data["current"] = data["total"]
		resp.close()
		log.info(f"finish download {filename}")

	def resp(self, t: int, data: typing.Any):
		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(t, data)
		return rpcrc

	async def Invoke(self, arg: protocol.RPCCallControl) -> protocol.RPCResponseControl:
		url: str = arg.params["url"]
		path: str = arg.params["path"]

		if os.path.isfile(path):
			return self.resp(protocol.RPCResponseControl.ERROR, {
				"error": f"{path} file already exists",
			})

		try:
			response = requests.get(url, stream=True)
		except Exception as e:
			return self.resp(protocol.RPCResponseControl.ERROR, {
				"error": f"request failed: {e}",
			})
		
		if response.status_code != 200:
			return self.resp(protocol.RPCResponseControl.ERROR, {
				"error": f"download failed: {response.text}",
				"code": response.status_code,
			})

		if os.path.isdir(path):
			# 尝试从 Content-Disposition 获取文件名
			filename = None
			if "Content-Disposition" in response.headers:
				content_disposition = response.headers["Content-Disposition"]
				parts = content_disposition.split(";")
				for part in parts:
					if part.strip().startswith("filename="):
						filename = part.split("=")[1].strip().strip('"')
						break

			# 如果 Content-Disposition 没有提供，就从 URL 获取
			if not filename:
				parsed_url = urlparse(url)
				filename = os.path.basename(parsed_url.path)

			path = os.path.join(path, filename)
		else:
			pardir = os.path.dirname(path)
			if pardir:
				log.debug(f"Create folder {pardir} for path {path}")
				os.makedirs(pardir, exist_ok=True)
		
		total_size = int(response.headers.get('content-length', 0))
		# response = None
		# total_size = 5000

		taskid = self.tm.AddTask({
			"current": 0,
			"total": total_size,
		})

		log.info(f"start download {url} to {path}")
		asyncio.ensure_future(self.startDownload(response, path, taskid))

		rpcrc = protocol.RPCResponseControl()
		rpcrc.Init(protocol.RPCResponseControl.PROGRESS, {
			"task": taskid,
			"filename": path,
		})
		return rpcrc

class StableDiffusionClient(Client):
	def __init__(self, config: Configuration):
		super().__init__(config)
		self.rpcs: typing.Dict[str, RPCInfo] = {}

		self.tm = TaskManager()
		self.AddRPC(SayHello(self))
		self.AddRPC(RPCAdd())
		self.AddRPC(RPCDownloadFile(self.tm))
		self.AddRPC(RPCListFile())
		self.AddRPC(RPCCopyFile())
		self.AddRPC(RPCDeleteFile())

		self.shouldProcessNextWSMsg = True
		self.cachedSteps: typing.Dict[int, typing.List[int]] = {}

	def AddRPC(self, rpc: RPCInfo):
		self.rpcs[rpc.GetName()] = rpc

	async def processControlPackage(self, pkg: protocol.Control) -> bool:
		ctrlType = pkg.GetControlType()

		if ctrlType == protocol.Control.QUERY_RPC:
			rpcqc = protocol.RPCQueryControl()
			for name, rpc in self.rpcs.items():
				rpcqc.Add(name, rpc.GetParamsDef())
			rpcraw = protocol.Control()
			rpcraw.SetForResponseTransport(pkg)
			rpcraw.InitRPCQueryControl(rpcqc)
			await self.DirectSend(rpcraw)
			return True
		
		if ctrlType == protocol.Control.RPC_CALL:
			rpcc = pkg.ToRPCCallControl()
			if rpcc.name in self.rpcs:
				rpcresp = await self.rpcs[rpcc.name].Invoke(rpcc)
				rpcrespraw = protocol.Control()
				rpcrespraw.SetForResponseTransport(pkg)
				rpcrespraw.InitRPCResponseControl(rpcresp)
				await self.DirectSend(rpcrespraw)
			else:
				await self.SendMsg(f"{self.name}] No rpc function for name: {rpcc.name}")
			return True
		
		if ctrlType == protocol.Control.RPC_PROGRESS:
			taskid = pkg.GetRPCProgressTaskId()
			if self.tm.HasTask(taskid):
				respc = protocol.RPCResponseControl()
				respc.Init(protocol.RPCResponseControl.PROGRESS, self.tm.GetData(taskid))
				raw = protocol.Control()
				raw.SetForResponseTransport(pkg)
				raw.InitRPCResponseControl(respc)
				await self.DirectSend(raw)
			else:
				await self.SendMsg(f"{self.name}] No task for id {taskid}")
			return True

		return await super().processControlPackage(pkg)
	
	async def optimizeResponsePackage(self, req: protocol.Request, resp: aiohttp.client_exceptions.ClientResponse, respData: bytes):
		urlpath = urlparse(req.url)
		headers = dict(resp.headers)
		if urlpath.path == "/internal/progress":
			respjson = json.loads(respData)
			compress_ratio = 0.0
			if respjson["live_preview"] != None and respjson["live_preview"].startswith("data:image/png;base64"):
				# 假设 base64_png_str 是你的 base64 PNG 字符串（不带前缀）
				base64_png_str: str =  respjson["live_preview"][len("data:image/png;base64,"):] # 示例截断

				# 解码 base64 为图像字节流
				png_data = base64.b64decode(base64_png_str)

				require_mimetype, require_quality = utils.GetWSFCompress(req)
				mimetype, rawData = utils.CompressImage(png_data, require_mimetype, require_quality)
				compress_ratio = utils.ComputeCompressRatio(len(respData), len(rawData))
				base64_avif_str = base64.b64encode(rawData).decode('utf-8')

				if mimetype != utils.MIMETYPE_WEBP:
					raise Exception(f"Unsupported mimetype: {mimetype}")
				
				respjson["live_preview"] = "data:image/avif;base64," + base64_avif_str

				respData = json.dumps(respjson).encode('utf-8')
				headers["Content-Length"] = str(len(respData))

			raw = protocol.Response(self.config.cipher)
			raw.SetForResponseTransport(req)
			raw.Init(req.url, resp.status, headers, respData)
			return raw, compress_ratio
				
		return await super().optimizeResponsePackage(req, resp, respData)
	
	# 来自 comfyUI/server.py BinaryEventTypes类
	COMFY_BinaryEventTypes_PREVIEW_IMAGE = 1
	COMFY_BinaryEventTypes_UNENCODED_PREVIEW_IMAGE = 2
	COMFY_BinaryEventTypes_TEXT = 3

	# 来自 comfyUI/server.py send_image()函数
	COMFY_ImageType_JPEG = 1
	COMFY_ImageType_PNG = 2

	async def postprocessWSPackage(self, req: protocol.Request, raw: protocol.WSStreamData) -> bool:
		parsed_url = urlparse(req.url)
		if parsed_url.path == '/ws':
			if raw.GetType() == protocol.WSStreamData.TYPE_TEXT:
				jsonText = json.loads(raw.GetTextData())
				if jsonText["type"] == 'progress':
					self.maxSteps = jsonText["data"]["max"]
					if self.maxSteps <= 10:
						self.shouldProcessNextWSMsg = True
						return True
					if self.maxSteps not in self.cachedSteps:
						estimateNum = int(pow(math.e, math.log(self.maxSteps)/self.config.comfyPreviewSpeed)) # solve x**rate = steps
						self.cachedSteps[self.maxSteps] = [int(x**self.config.comfyPreviewSpeed)+1 for x in range(estimateNum+1)]
						self.cachedSteps[self.maxSteps].append(self.maxSteps)
					self.shouldProcessNextWSMsg = jsonText["data"]["value"] in self.cachedSteps[self.maxSteps]
					if self.shouldProcessNextWSMsg == False:
						return False
				else:
					self.shouldProcessNextWSMsg = True
				return True
			
			if raw.GetType() == protocol.WSStreamData.TYPE_BIN:
				# comfy 的数据为 flatten([packed, data])，而packed是使用struct.pack(">I", event)得到的
				message = raw.GetBinData()
				# 1. 确定 packed 的字节大小
				packed_size = struct.calcsize(">I")
				# 2. 从 message 中提取 packed 的字节部分
				packed = message[:packed_size]
				# 3. 解包 packed
				event: int = struct.unpack(">I", packed)[0]
				# 4. 剩余的部分就是 data
				data = message[packed_size:]

				# 这个是推理过程中预览的图像
				if event == self.COMFY_BinaryEventTypes_PREVIEW_IMAGE and utils.HasWSFCompress(req):
					if self.shouldProcessNextWSMsg == False: # skip this preview image
						self.shouldProcessNextWSMsg = True
						return False
					
					# 预览的数据为 bytesIO([header, Image])，而header是使用struct.pack(">I", type_num)得到的
					# 读取头部，获取 type_num
					header_size = struct.calcsize(">I")
					type_num: int = struct.unpack(">I", data[:header_size])[0]

					mimetype, quality = utils.GetWSFCompress(req)
					mimetype, finalImageData = utils.CompressImage(data[header_size:], mimetype, quality)

					compress_ratio = utils.ComputeCompressRatio(len(data) - header_size, len(finalImageData))

					# 重新打包
					bytesIO = io.BytesIO()
					header = struct.pack(">I", type_num)
					bytesIO.write(header)
					bytesIO.write(finalImageData)
					preview_bytes = bytesIO.getvalue()

					packed = struct.pack(">I", event)
					message = bytearray(packed)
					message.extend(preview_bytes)

					raw.SetBody(message)
					log.debug(f"WS(Compress {int(compress_ratio*10000)/100}%) {raw.seq_id} - {req.method} {req.url} {len(raw.body)} bytes")

				self.shouldProcessNextWSMsg = True
				return True
			
		return True

async def main(config: Configuration):
	client = StableDiffusionClient(config)
	try:
		await client.MainLoop()
	except Exception as e:
		log.error(f"Exception in mainloop: {e}.", exc_info=True, stack_info=True)

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))
