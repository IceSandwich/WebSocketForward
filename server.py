import protocol 
import time_utils
import utils

import asyncio, aiohttp
import aiohttp.web as web
import abc
import typing
import logging
import argparse as argp
import os
import random

log = logging.getLogger(__name__)
utils.SetupLogging(log, "server", logging.INFO if os.name != "nt" else logging.DEBUG, True)
protocol.log = log
utils.log = log

DROP_RANDOM_RATE = 0.08

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.timeout = time_utils.Seconds(args.timeout)
		self.cacheSize: int = args.cache_size
		self.listen_route: str = args.listen_route
		self.allowOFOResend: bool  = args.allow_ofo_resend
		self.dropRandom: bool = args.drop_random

		if self.dropRandom:
			log.warning("Server is running on drop random mode. Pay attention that this option is only for debug use.")

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1:8030", help="The address to listen on.")
		parser.add_argument("--cache_size", type=int, default=140, help="The maximum number of packages to cache. Set 0 to disable cache.")
		parser.add_argument("--timeout", type=int, default=60, help="The maximum seconds to resend packages.")
		parser.add_argument("--listen_route", type=str, default='/wsf/ws')
		parser.add_argument("--allow_ofo_resend", action="store_true", help="Allow out of order message to be resend. Disable this feature may results in buffer overflow issue. By default, this feature is disable.")

		parser.add_argument("--drop_random", action='store_true', help='Drop package at random rate. Use in debug only.')
		return parser

	def GetListenedAddress(self):
		return self.server

	def IsTCPAddress(self):
		return not self.server.endswith('.sock')

	def SeparateTCPAddress(self):
		splitIdx = self.server.index(':')
		server_name = self.server[:splitIdx]
		server_port = int(self.server[splitIdx+1:])
		return server_name, server_port
	
class IRouter(abc.ABC):
	@abc.abstractmethod
	async def Route(self, raw: protocol.Transport) -> bool:
		raise NotImplementedError()
	
	@abc.abstractmethod
	async def SendMsg(self, dest: str, msg:str, skip: typing.List[str] = []):
		"""
		向dest发送msg。
		如果dest为""，则向所有连接的客户端（除了skip列表内的）发送
		"""
		raise NotImplementedError()
	
	@abc.abstractmethod
	def ReportConnectedClients(self) -> protocol.QueryClientsControl:
		raise NotImplementedError()

class Server:
	HttpUpgradeHearbeatDuration = 0 # set 0 to disable
	WebSocketMaxReadingMessageSize = 12 * 1024 * 1024 # 12MB
	WebSocketSafeMaxMsgSize = 768 * 1024 # 768KB, up to 1MB

	STATUS_INIT = 0
	STATUS_PREPARING = 1
	STATUS_INITIALIZING = 2
	STATUS_CONNECTED = 3

	def __init__(self, config: Configuration):
		self.router: IRouter = None
		self.ws: web.WebSocketResponse = None
		self.config = config
		
		self.name = 'UNKNOWN'
		self.status = self.STATUS_INIT

		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(config.cacheSize)
		self.sendQueueLock = asyncio.Lock()

		self.receiveQueue: utils.BoundedQueue[protocol.PackageId] = utils.BoundedQueue(config.cacheSize)
		self.receiveQueueLock = asyncio.Lock()

		self.clientId = ""
		self.clientType = protocol.ClientInfo.UNKNOWN

		self.scheduleSendQueue: asyncio.Queue[protocol.Transport] = asyncio.Queue()

	def GetStatus(self):
		return self.status
	
	def GetClientType(self):
		return self.clientType

	def SetRouter(self, router: IRouter, server_name: str):
		self.router = router
		self.name = server_name

	async def waitForOnePackage(self, ws: web.WebSocketResponse):
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				log.error(f"{self.name}]1Pkg] Error on waiting 1 package: {ws.exception()}", exc_info=True, stack_info=True)
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Transport.Parse(msg.data)
				log.debug(f"{self.name}]1Pkg] Got one package: {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} from {transport.sender}")
				return transport
		log.error(f"{self.name}]1Pkg] WS Broken.")
		return None
	
	async def DirectSend(self, transport: protocol.Transport):
		await self.ws.send_bytes(transport.Pack())
	
	async def Send(self, transport: protocol.Transport):
		"""
		发送数据包。
		自动缓存非Control包，若当前连接是断开时计划下次连接再发送。
		若发送Control包，仅当当前状态是连接时才发送，否则丢弃包。
		"""
		if self.config.dropRandom and random.random() < DROP_RANDOM_RATE:
			log.warning(f"{self.name}] Drop send package {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} -> {transport.receiver}")
			return

		async with self.sendQueueLock:
			# check
			if transport.receiver != self.name:
				log.error(f"{self.name}] Send a package {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} to {self.name} but it's receiver is {transport.receiver}, sent by {transport.sender}. Online fix that.")
				transport.receiver = self.name

			# donot cache control package
			if transport.transportType != protocol.Transport.CONTROL:
				transport.timestamp = time_utils.GetTimestamp()
				self.sendQueue.Add(transport)

			if self.status == self.STATUS_CONNECTED:
				try:
					await self.DirectSend(transport)
				except Exception as e:
					if self.clientType == protocol.ClientInfo.REMOTE:
						pass
					elif transport.transportType != protocol.Transport.CONTROL: # 不缓存Control包
						log.error(f"{self.name}] Failed to send {protocol.Transport.Mappings.ValueToString(transport.transportType)} package {transport.seq_id}:{transport.cur_idx}/{transport.total_cnt}, schedule to resend in next connection(without control pkgs). err: {e}")
						await self.scheduleSendQueue.put(transport)
			else:
				if self.clientType == protocol.ClientInfo.REMOTE:
					# Remote 在重连时可以从sendQueue里找它没收到的包，因此无需放入scheduleSendQueue。
					# 而 Client 没有这个能力，需要服务端缓存这些包等到下次连接后发送。
					pass
				elif transport.transportType != protocol.Transport.CONTROL: # 不缓存Control包
					log.debug(f"Send] Caching {protocol.Transport.Mappings.ValueToString(transport.transportType)} package {transport.seq_id}:{transport.cur_idx}/{transport.total_cnt}(without control pkgs) because the instance is disconnected.")
					await self.scheduleSendQueue.put(transport)

	async def SendMsg(self, msg: str, ws: typing.Optional[web.WebSocketResponse] = None):
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver("", self.name)
		ctrl.InitPrintControl(msg)
		if ws is None:
			await self.Send(ctrl)
		else:
			await ws.send_bytes(ctrl.Pack())

	async def initialize(self, ws: web.WebSocketResponse):
		"""
		返回None表示可以继续，返回web.WebSocketResponse表示需要断开连接。
		"""
		async def errorMsg(msg: str, sendBack: bool = True):
			log.error(f"{self.name}] {msg}")
			if sendBack:
				await self.SendMsg(msg, ws=ws)
			self.status = self.STATUS_INIT

		pkg = await self.waitForOnePackage(ws)
		if pkg is None:
			await errorMsg("Broken ws connection in initialize stage.", sendBack=False)
			return ws
		if pkg.transportType != protocol.Transport.CONTROL:
			await errorMsg(f"First client package must be control package, but got {protocol.Transport.Mappings.ValueToString(pkg.transportType)}.")
			return ws
		tmp = protocol.Control()
		tmp.Unpack(pkg)
		pkg = tmp
		if pkg.controlType != protocol.Control.HELLO:
			await errorMsg(f"First client control package must be HELLO, but got {protocol.Control.Mappings.ValueToString(pkg.controlType)}.")
			return ws
		hello = pkg.ToHelloClientControl()

		self.clientType = hello.info.type

		async with self.sendQueueLock:
			if hello.info.id != self.clientId:
				log.info(f"{self.name}] New client instance {hello.info.id}. Clear histories.")
				self.clientId = hello.info.id
				self.sendQueue.Clear()
				self.scheduleSendQueue = asyncio.Queue()
				async with self.receiveQueueLock:
					self.receiveQueue.Clear()
				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				ret.InitHelloServerControl(protocol.HelloServerControl())
				await ws.send_bytes(ret.Pack())
				await asyncio.sleep(time_utils.Seconds(1))
			elif hello.info.type == protocol.ClientInfo.REMOTE:
				async with self.receiveQueueLock:
					# Remote will send all send package infos to us, we need to filter out items he didn't receive and resend again.
					log.info(f"{self.name}] Old remote instance {hello.info.id}. Remote reports {len(hello.received_pkgs)} received packages and {len(hello.sent_pkgs)} sent packages. Server had sent {len(self.sendQueue)} packages and received {len(self.receiveQueue)} packages.")

					ret = protocol.Control()
					ret.SetForResponseTransport(pkg)
					hsc = protocol.HelloServerControl()

					now = time_utils.GetTimestamp()
					# size: sendQueue << hello.received_pkgs
					for item in self.sendQueue:
						found = False
						for pkg in hello.received_pkgs:
							if pkg.IsSamePackage(item):
								found = True
								break
						if not found:
							if time_utils.WithInDuration(item.timestamp, now, self.config.timeout):
								await self.scheduleSendQueue.put(item)
								hsc.AppendReportPkg(protocol.PackageId.FromPackage(pkg))
							else:
								log.warning(f"{self.name}] Package {pkg.seq_id} skip resend because it's out-dated({now} - {item.timestamp} = {now - item.timestamp} > {self.config.timeout}).")

					# size: hello.sent_pkgs << receiveQueue
					for item in hello.sent_pkgs:
						found = False
						for pkg in self.receiveQueue:
							if pkg.IsSamePackage(item):
								found = True
								break
						if not found:
							hsc.AppendRequirePkg(item)

				ret.InitHelloServerControl(hsc)
				await ws.send_bytes(ret.Pack())
				await asyncio.sleep(time_utils.Seconds(1))
			elif hello.info.type == protocol.ClientInfo.CLIENT:
				# Client will query all receive package infos from server, so we need to send the package infos to it.
				log.info(f"{self.name}] Old client instance {hello.info.id}. Client reports {len(hello.received_pkgs)} received packages. Server had received {len(self.receiveQueue)} pacakges and sent {len(self.sendQueue)} packages.")

				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				hsc = protocol.HelloServerControl()
				# size: sendQueue << hello.received_pkgs
				for pkg in self.sendQueue:
					found = False
					for item in hello.received_pkgs:
						if item.IsSamePackage(pkg):
							found = True
							break
					if not found:
						await self.scheduleSendQueue.put(item)
				
				async with self.receiveQueueLock:
					for pkg in self.receiveQueue:
						hsc.AppendReportPkg(pkg)
				ret.InitHelloServerControl(hsc)
				await ws.send_bytes(ret.Pack())
				await asyncio.sleep(time_utils.Seconds(1))
			else:
				log.error(f"{self.name}] Invalid hello type {hello.info.type}")
				self.status = self.STATUS_INIT
				return ws
				# raise Exception(f"Invalid hello type {hello.info.type}")
			
			self.ws = ws
			self.status = self.STATUS_CONNECTED

		return None
	
	async def processControlPackage(self, raw: protocol.Transport):
		pkg = protocol.Control()
		pkg.Unpack(raw)

		ctrlType = pkg.GetControlType()
		if ctrlType == protocol.Control.QUERY_CLIENTS:
			# check
			if pkg.receiver != "":
				log.error(f"{self.name}] Receive QueryClientsControl package {pkg.seq_id} but it's receiver is not server. Online fix it.")
				pkg.receiver = ""

			qcc = self.router.ReportConnectedClients()
			ret = protocol.Control()
			ret.SetForResponseTransport(pkg)
			ret.InitQueryClientsControl(qcc)
			log.debug(f"{self.name}] Query clients: {len(qcc)} clients[{', '.join([x.id for x in qcc.connected])}].")
			await self.Send(ret)
		elif ctrlType == protocol.Control.RETRIEVE_PKG:
			pi = pkg.DecodeRetrievePkg()
			found = False
			async with self.sendQueueLock:
				for item in self.sendQueue:
					if pi.IsSamePackage(item):
						found = True
						log.debug(f"Hit cache for package {item.seq_id}:{item.cur_idx}/{item.total_cnt} for query {pi.seq_id}:{pi.cur_idx}/{pi.total_cnt}.")
						await self.DirectSend(item)
						break
			if found == False:
				log.debug(f"Not hit cache and route to {pkg.receiver} for package {pi.seq_id}:{pi.cur_idx}/{pi.total_cnt}")
				await self.router.Route(pkg)
		elif ctrlType == protocol.Control.PRINT:
			if pkg.receiver == "":
				log.info(f"% {pkg.DecodePrintMsg()}")
			else:
				await self.router.Route(pkg)
		elif pkg.receiver == "":
			await self.router.SendMsg(pkg.sender, f"Server] Unsupported control type({protocol.Control.Mappings.valueToString(ctrlType)}) to server. Drop control package {pkg.seq_id}.")
		else:
			await self.router.Route(pkg)


	async def OnPackage(self, raw: protocol.Transport):
		if self.config.dropRandom and random.random() < DROP_RANDOM_RATE:
			log.warning(f"{self.name}] Drop received package {raw.seq_id}:{protocol.Transport.Mappings.ValueToString(raw.transportType)} <- {raw.sender}")
			return

		if raw.transportType == protocol.Transport.CONTROL:
			await self.processControlPackage(raw)
		else:
			async with self.receiveQueueLock:
				self.receiveQueue.Add(protocol.PackageId.FromPackage(raw))

			await self.router.Route(raw)

	async def OnConnected(self):
		log.info(f"{self.name}] Connected.")
		await self.router.SendMsg("", f"{self.name}] Connected.", skip=[self.name])

	async def OnDisconnected(self):
		log.info(f"{self.name}] Disconnected.")
		await self.router.SendMsg("", f"{self.name}] Disconnected.", skip=[self.name])

	async def resendScheduledPackages(self) -> bool:
		log.debug(f"{self.name}] Schedule resend {self.scheduleSendQueue.qsize()} packages.")
		try:
			while not self.scheduleSendQueue.empty():
				item = await self.scheduleSendQueue.get()
				log.debug(f"{self.name}] Schedule resend {item.seq_id}")
				await self.DirectSend(item)
			return True
		except Exception as e:
			self.scheduleSendQueue = asyncio.Queue()
			log.error(f"{self.name}] Schedule resend error: {e}", exc_info=True, stack_info=True)
			return False

	async def MainLoop(self, request: web.BaseRequest):
		ws = web.WebSocketResponse(max_msg_size=self.WebSocketMaxReadingMessageSize, heartbeat=None if self.HttpUpgradeHearbeatDuration == 0 else self.HttpUpgradeHearbeatDuration, autoping=True)

		self.status = self.STATUS_PREPARING
		log.debug(f"{self.name}] Preparing...")
		await ws.prepare(request)

		self.status = self.STATUS_INITIALIZING
		log.debug(f"{self.name}] Initializing...")
		try:
			prepareRet = await self.initialize(ws)
		except Exception as e:
			log.error(f"{self.name}] Error in initialize. {e}", exc_info=True, stack_info=True)
			prepareRet = ws
		if prepareRet is not None:
			self.status = self.STATUS_INIT
			log.error(f"{self.name}] Failed to initialize. Return to init status.")
			return prepareRet

		self.ws = ws
		self.status = self.STATUS_CONNECTED
		await self.OnConnected()
		shouldGoing = True
		if self.config.allowOFOResend:
			asyncio.ensure_future(self.resendScheduledPackages())
		else:
			shouldGoing = await self.resendScheduledPackages()
		if shouldGoing:
			try:
				async for msg in ws:
					if msg.type == aiohttp.WSMsgType.ERROR:
						log.error(f"{self.name}] {ws.exception()}", exc_info=True, stack_info=True)
					elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
						transport = protocol.Transport.Parse(msg.data)
						log.debug(f"{self.name}] {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)}:{transport.cur_idx}/{transport.total_cnt} route {transport.sender} => {transport.receiver}")

						# check
						if transport.sender != self.name:
							log.error(f"{self.name}] Received a package {transport.seq_id} from {self.name} but it's sender is `{transport.sender}`, to `{transport.receiver}`. Online fix that.")
							transport.sender = self.name

						await self.OnPackage(transport)
			except Exception as e:
				log.error(f"{self.name}] Mainloop error: {e}", exc_info=True, stack_info=True)
			
		self.status = self.STATUS_INIT
		await self.OnDisconnected()
		log.debug(f"{self.name}] Disconnected. Return to init status.")
		self.ws = None
		return ws

class Router(IRouter):
	def __init__(self):
		self.servers: typing.Dict[str, Server] = {}
		self.serversLock = asyncio.Lock()

	async def GetOrNewServer(self, name: str, **kwargs) -> Server:
		async with self.serversLock:
			if name not in self.servers:
				self.servers[name] = Server(**kwargs)
				self.servers[name].SetRouter(self, name)
		return self.servers[name]
	
	async def SendMsg(self, dest: str, msg: str, skip: typing.List[str] = []):
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver("", dest)
		ctrl.InitPrintControl(msg)
		if dest == "":
			for name, server in self.servers.items():
				if name not in skip:
					ctrl.receiver = name
					await server.Send(ctrl)
		else:
			await self.servers[dest].Send(ctrl)
	
	async def Route(self, raw: protocol.Transport):
		if raw.receiver not in self.servers:
			await self.SendMsg(raw.sender, f"Server] Package {raw.seq_id} attemps to send to {raw.receiver} but not in server's ChatRoom. Drop it.")
			return False
		await self.servers[raw.receiver].Send(raw)
		return True
	
	def ReportConnectedClients(self) -> protocol.QueryClientsControl:
		ret = protocol.QueryClientsControl()
		for name, server in self.servers.items():
			if server.GetStatus() == server.STATUS_CONNECTED:
				ci = protocol.ClientInfo(name, server.GetClientType())
				ret.Append(ci)
		return ret

chatroom = Router()

async def main(config: Configuration):
	async def wraploop(request: web.BaseRequest):
		if 'uid' not in request.query:
			return web.Response(
				body="Invalid request.",
				status=403
			)
		uid = request.query['uid']
		log.debug(f"App] Connect request from {uid}")
		server = await chatroom.GetOrNewServer(uid, config = config)
		if server.GetStatus() != Server.STATUS_INIT:
			log.error(f"{uid}] Already connected. current status: {server.GetStatus()}")
			return web.Response(
				body='Already connected.',
				status=403
			)

		return await server.MainLoop(request)

	app = web.Application()
	app.router.add_get(config.listen_route, wraploop)
	
	runner = web.AppRunner(app)
	await runner.setup()
	if config.IsTCPAddress():
		server_name, server_port = config.SeparateTCPAddress()
		site = web.TCPSite(runner, server_name, server_port)
	else:
		site = web.UnixSite(runner, config.server)
	await site.start()
	
	log.info(f"Server started on {config.GetListenedAddress()}")
	log.info(f"Listen to url: {config.listen_route}")
	await asyncio.Future()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))
