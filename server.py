import protocol 
import time_utils
import utils

import asyncio, aiohttp
import aiohttp.web as web
import abc
import typing
import logging
import argparse as argp

log = logging.getLogger(__name__)
utils.SetupLogging(log, "server", logging.INFO, True)

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.timeout = time_utils.Seconds(args.timeout)
		self.cacheSize: int = args.cache_size
		self.listen_route: str = args.listen_route

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1:8030", help="The address to listen on.")
		parser.add_argument("--cache_size", type=int, default=128, help="The maximum number of packages to cache. Set 0 to disable cache.")
		parser.add_argument("--timeout", type=int, default=60, help="The maximum seconds to resend packages.")
		parser.add_argument("--listen_route", type=str, default='/wsf/ws')
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
				log.error(f"{self.name}] Error on waiting 1 package: {ws.exception()}")
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Parse(msg.data)
				log.debug(f"{self.name}] Got one package: {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} from {transport.sender}")
				return transport
		log.error(f"{self.name}] WS Broken.")
		return None
	
	async def DirectSend(self, transport: protocol.Transport):
		await self.ws.send_bytes(transport.Pack())
	
	async def Send(self, transport: protocol.Transport):
		"""
		发送数据包。
		自动缓存非Control包，若当前连接是断开时计划下次连接再发送。
		若发送Control包，仅当当前状态是连接时才发送，否则丢弃包。
		"""
		async with self.sendQueueLock:
			# donot cache control package
			if transport.transportType != protocol.Transport.CONTROL:
				transport.timestamp = time_utils.GetTimestamp()
				self.sendQueue.Add(transport)

			# check
			if transport.receiver != self.name:
				log.error(f"{self.name}] Send a package {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} to {self.name} but it's receiver is {transport.receiver}, sent by {transport.sender}. Online fix that.")
				transport.receiver = self.name

			if self.status == self.STATUS_CONNECTED:
				await self.DirectSend(transport)

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
		if type(pkg) != protocol.Control:
			await errorMsg(f"First client package must be control package, but got {protocol.Transport.Mappings.ValueToString(pkg.transportType)}.")
			return ws
		if pkg.controlType != protocol.Control.HELLO:
			await errorMsg(f"First client control package must be HELLO, but got {protocol.Control.Mappings.ValueToString(pkg.controlType)}.")
			return ws
		hello = pkg.ToHelloClientControl()

		self.clientType = hello.info.type

		async with self.sendQueueLock:
			if hello.info.id != self.clientId:
				log.info(f"{self.name}] New client instance. Clear histories.")
				self.clientId = hello.info.id
				self.sendQueue.Clear()
				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				ret.InitHelloServerControl(protocol.HelloServerControl())
				await ws.send_bytes(ret.Pack())
			elif hello.info.type == protocol.ClientInfo.REMOTE:
				# Remote will send all send package infos to us, we need to filter out items he didn't receive and resend again.
				log.info(f"{self.name}] Old client instance. Exchange packages...")

				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				ret.InitHelloServerControl(protocol.HelloServerControl())
				await ws.send_bytes(ret.Pack())
				await asyncio.sleep(time_utils.Seconds(1))

				log.info(f"{self.name}] Resend process... Check {len(self.sendQueue)} packages.")
				now = time_utils.GetTimestamp()
				for pkg in self.sendQueue:
					found = False
					for pkg in hello.pkgs:
						if pkg.IsSamePackage(pkg):
							found = True
					if not found:
						if time_utils.WithInDuration(pkg.timestamp, now, self.config.timeout):
							log.debug(f"{self.name}] Resend package {pkg.seq_id}")
							await ws.send_bytes(pkg.Pack())
						else:
							log.warning(f"{self.name}] Package {pkg.seq_id} skip resend because it's out-dated({now} - {pkg.timestamp} = {now - pkg.timestamp} > {self.config.timeout}).")
				
			elif hello.info.type == protocol.ClientInfo.CLIENT:
				# Client will query all receive package infos from server, so we need to send the package infos to it.
				log.info(f"{self.name}] Old client instance. Exchange packages...")

				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				hsc = protocol.HelloServerControl()
				async with self.receiveQueueLock:
					log.info(f"{self.name}] Resend process... Check {len(self.receiveQueue)} packages.")
					for pkg in self.receiveQueue:
						hsc.AppendPkg(pkg)
				log.debug(f"{self.name}] Hello contains {len(hsc)} packages received by Server.")
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

	async def OnPackage(self, raw: protocol.Transport):
		if raw.transportType == protocol.Transport.CONTROL:
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
							log.debug(f"Hit cache for package {item.seq_id}.")
							await self.DirectSend(item)
							break
				if found == False:
					log.debug(f"Not hit cache and route to {pkg.receiver} for package {pi.seq_id}")
					await self.router.Route(pkg)
			elif ctrlType == protocol.Control.PRINT:
				if pkg.receiver == "":
					log.info(f"% {pkg.DecodePrintMsg()}")
				else:
					await self.router.Route(pkg)
			elif pkg.receiver == "":
				await self.router.SendMsg(pkg.sender, f"Server] Unsupported control type({protocol.Control.Mapping.valueToString(ctrlType)}) to server. Drop control package {pkg.seq_id}.")
			else:
				await self.router.Route(pkg)
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

	async def MainLoop(self, request: web.BaseRequest):
		ws = web.WebSocketResponse(max_msg_size=self.WebSocketMaxReadingMessageSize, heartbeat=None if self.HttpUpgradeHearbeatDuration == 0 else self.HttpUpgradeHearbeatDuration, autoping=True)

		self.status = self.STATUS_PREPARING
		log.debug(f"{self.name}] Preparing...")
		await ws.prepare(request)

		self.status = self.STATUS_INITIALIZING
		log.debug(f"{self.name}] Initializing...")
		prepareRet = await self.initialize(ws)
		if prepareRet is not None:
			self.status = self.STATUS_INIT
			log.debug(f"{self.name}] Failed to initialize. Return to init status.")
			return prepareRet

		self.ws = ws
		self.status = self.STATUS_CONNECTED
		await self.OnConnected()
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				log.error(f"{self.name}] {ws.exception()}")
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Transport.Parse(msg.data)
				log.debug(f"{self.name}] {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)}:{transport.cur_idx}/{transport.total_cnt} route {transport.sender} => {transport.receiver}")

				# check
				if transport.sender != self.name:
					log.error(f"{self.name}] Received a package {transport.seq_id} from {self.name} but it's sender is {transport.sender}, to {transport.receiver}. Online fix that.")
					transport.sender = self.name

				await self.OnPackage(transport)
			
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
