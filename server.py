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
		parser.add_argument("--timeout", type=int, default=30, help="The maximum seconds to resend packages.")
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
	
# class IServer(abc.ABC):
# 	@abc.abstractmethod
# 	async def Send(self, raw: protocol.Transport):
# 		raise NotImplementedError()
	
# 	def IsConnected(self) -> bool:
# 		raise NotImplementedError()
	
# 	@abc.abstractmethod
# 	async def SetRouter(self, router: IRouter):
# 		raise NotImplementedError()
	
# 	@abc.abstractmethod
# 	async def MainLoop(self, request: web.BaseRequest):
# 		raise NotImplementedError()
	
class Server:
	HttpUpgradeHearbeatDuration = 0 # set 0 to disable
	WebSocketMaxReadingMessageSize = 12 * 1024 * 1024 # 12MB
	WebSocketSafeMaxMsgSize = 768 * 1024 # 768KB, up to 1MB

	STATUS_INIT = 0
	STATUS_PREPARING = 1
	STATUS_INITIALIZED = 2
	STATUS_CONNECTED = 3

	def __init__(self):
		self.router = None
		self.ws: web.WebSocketResponse = None
		
		self.name = 'UNKNOWN'
		self.status = self.STATUS_INIT

		self.sendQueue: utils.BoundedQueue[protocol.Transport] = utils.BoundedQueue(100)
		self.sendQueueLock = asyncio.Lock()

		self.receiveQueue: utils.BoundedQueue[protocol.PackageId] = utils.BoundedQueue(100)
		self.receiveQueueLock = asyncio.Lock()

		self.clientId = ""

	def GetStatus(self):
		return self.status

	async def SetRouter(self, router: IRouter, server_name: str):
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
		return None
	
	async def Send(self, transport: protocol.Transport):
		async with self.sendQueueLock:
			self.sendQueue.Add(transport)

			if self.status == self.STATUS_CONNECTED:
				await self.ws.send_bytes(transport.Pack())

	async def initialize(self, ws: web.WebSocketResponse):
		pkg = await self.waitForOnePackage(ws)
		if pkg is None:
			log.error(f"{self.name}] Broken ws connection in initialize stage.")
			self.status = self.STATUS_INIT
			return ws
		if type(pkg) != protocol.Control:
			log.error(f"{self.name}] First client package must be control package, but got {protocol.Transport.Mappings.ValueToString(pkg.transportType)}.")
			self.status = self.STATUS_INIT
			return ws
		if pkg.controlType != protocol.Control.HELLO:
			log.error(f"{self.name}] First client control package must be HELLO, but got {protocol.Control.Mappings.ValueToString(pkg.controlType)}.")
			self.status = self.STATUS_INIT
			return ws
		hello = pkg.ToHelloClientControl()

		async with self.sendQueueLock:
			if hello.info.id != self.clientId:
				log.info(f"{self.name}] New client instance. Clear histories.")
				self.sendQueue.Clear()
				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				ret.InitHelloServerControl(protocol.HelloServerControl())
				await ws.send_bytes(ret.Pack())
			elif hello.info.type == protocol.ClientInfo.REMOTE:
				# Remote will send all send package infos to us, we need to filter out items he didn't receive and resend again.
				for pkg in self.sendQueue:
					found = False
					for pkg in hello.pkgs:
						if pkg.IsSamePackage(pkg):
							found = True
					if not found:
						log.debug(f"{self.name}] Resend package {pkg.seq_id}")
						await self.ws.send_bytes(pkg.Pack())
				
				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				ret.InitHelloServerControl(protocol.HelloServerControl())
				await ws.send_bytes(ret.Pack())
			elif hello.info.type == protocol.ClientInfo.CLIENT:
				# Client will query all receive package infos from server, so we need to send the package infos to it.
				ret = protocol.Control()
				ret.SetForResponseTransport(pkg)
				hsc = protocol.HelloServerControl()
				async with self.receiveQueueLock:
					for pkg in self.receiveQueue:
						hsc.AppendPkg(pkg)
				ret.InitHelloServerControl(hsc)
				await ws.send_bytes(ret.Pack())
			else:
				raise Exception(f"Invalid hello type {hello.info.type}")
			
			self.status = self.STATUS_CONNECTED

	async def MainLoop(self, request: web.BaseRequest):
		ws = web.WebSocketResponse(max_msg_size=self.WebSocketMaxReadingMessageSize, heartbeat=None if self.HttpUpgradeHearbeatDuration == 0 else self.HttpUpgradeHearbeatDuration, autoping=True)

		self.status = self.STATUS_PREPARING
		await ws.prepare(request)

		self.status = self.STATUS_INITIALIZED
		await self.initialize(ws)

		self.ws = ws
		self.status = self.STATUS_CONNECTED
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				log.error(f"{self.name}] {ws.exception()}")
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				transport = protocol.Parse(msg.data)
				log.debug(f"{self.name}] Got one package: {transport.seq_id}:{protocol.Transport.Mappings.ValueToString(transport.transportType)} from {transport.sender}")
				return transport
			
		self.status = self.STATUS_INIT
		self.ws = None
		return ws

class Router(IRouter):
	def __init__(self):
		self.servers: typing.Dict[str, Server] = []
		self.serversLock = asyncio.Lock()

	async def GetOrNewServer(self, name: str):
		async with self.serversLock:
			if name not in self.servers:
				self.servers[name] = Server()
				self.servers[name].SetRouter(self, name)
		return self.servers[name]
	
	async def SendMsg(self, dest: str, msg: str):
		ctrl = protocol.Control()
		ctrl.SetSenderReceiver("", dest)
		ctrl.InitPrintControl(msg)
		await self.servers[dest].Send(ctrl)
	
	async def Route(self, raw: protocol.Transport):
		if raw.receiver not in self.servers:
			await self.SendMsg(raw.sender, f"Package {raw.seq_id} attemps to send to {raw.receiver} but not in server's ChatRoom. Drop it.")
			return False
		await self.servers[raw.receiver].Send(raw)
		return True
	


chatroom = Router()



async def main(config: Configuration):
	async def wraploop(request: web.BaseRequest):
		if 'uid' not in request.query:
			return web.Response(
				body="Invalid request.",
				status=403
			)
		uid = request.query['uid']
		server = await chatroom.GetOrNewServer(uid)
		if server.GetStatus() != Server.STATUS_INIT:
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