import asyncio, typing
import aiohttp.web as web
import argparse as argp
import typing
import json
import data, tunnel, utils, logging, time_utils

log = logging.getLogger(__name__)
utils.SetupLogging(log, "server")

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
		parser.add_argument("--timeout", type=int, default=10, help="The maximum seconds to resend packages.")
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

argparse = argp.ArgumentParser()
argparse = Configuration.SetupParser(argparse)
args = argparse.parse_args()
conf = Configuration(args)

class ChatRoom:
	def __init__(self):
		self.servers: typing.Dict[str, tunnel.HttpUpgradedWebSocketServer] = {}

	def Add(self, uid: str, server: tunnel.HttpUpgradedWebSocketServer) -> bool:
		"""
		如果uid存在返回False
		"""
		if uid in self.servers: return False
		self.servers[uid] = server
		log.info(f"ChatRoom] {server.GetName()} joined.")
		return True

	def Remove(self, uid: str) -> bool:
		"""
		如果uid不存在返回False
		"""
		if uid not in self.servers: return False
		log.info(f"ChatRoom] {self.servers[uid].GetName()} left.")
		del self.servers[uid]
		return True
	
	def GetServer(self, uid:str):
		return self.servers[uid]
	
	def Has(self, uid:str):
		return uid in self.servers
	
	@classmethod
	def NewPrintControlTransport(cls, msg: str, fromWho="Server"):
		return data.Transport(
			data.TransportDataType.CONTROL,
			data.Control(
				data.ControlDataType.PRINT, 
				data.PrintControlMsg(fromWho, msg).Serialize()
			).ToProtobuf(),
			"server",
			"connected-devices"
		)

	async def Boardcast(self, msg: str, skip_lists: typing.List[str], fromWho="Server"):
		"""
		广播信息
		"""
		log.info(f"Boardcast {msg} from {fromWho}")
		pkg = self.NewPrintControlTransport(msg, fromWho)
		for uid, server in self.servers.items():
			if uid in skip_lists: continue
			if server.IsConnected():
				await server.DirectSend(pkg)

	async def Route(self, raw: data.Transport) -> bool:
		if raw.to_uid not in self.servers:
			pkg = self.NewPrintControlTransport(f'Server cannot dispath your package because no target uid {raw.to_uid} register in server.')
			pkg.to_uid = raw.from_uid
			await self.servers[raw.from_uid].DirectSend(pkg)
			return False
		await self.servers[raw.to_uid].QueueSend(raw)
		return True
	
	def GetServerInfos(self):
		return {
			name: item.IsConnected()
			for name, item in self.servers.items()
		}

chatroom = ChatRoom()

class Client(tunnel.HttpUpgradedWebSocketServer):
	def __init__(self, uid: str, **kwargs):
		self.uid = uid
		super().__init__(**kwargs)

	def GetName(self) -> str:
		return self.uid
	
	def GetLogger(self) -> logging.Logger:
		return log

	async def OnPreConnected(self) -> str:
		if self.IsConnected():
			log.warning(f"A connection to {self.GetName()} but already connected.")
			return 'Already connected. Refuse to connect.'
		return await super().OnPreConnected()
	
	async def OnConnected(self):
		await chatroom.Boardcast(f"{self.GetName()} connected.", [self.uid])
		await super().OnConnected()

	async def OnDisconnected(self):
		await chatroom.Boardcast(f"{self.GetName()} disconnected.", [self.uid])
		await super().OnDisconnected()

	async def processControlPackage(self, raw: data.Transport):
		pkg = data.Control.FromProtobuf(raw.data)
		if pkg.data_type == data.ControlDataType.QUERY_CLIENTS:
			output = chatroom.GetServerInfos()
			resp = data.Control(data.ControlDataType.QUERY_CLIENTS, json.dumps(output))
			raw.SwapSenderReciver()
			raw.data = resp.ToProtobuf()
			await self.DirectSend(raw)
		elif pkg.data_type == data.ControlDataType.PRINT:
			log.info("CTRL Server] " + pkg.msg)
		elif pkg.data_type == data.ControlDataType.EXIT:
			if not chatroom.Has(pkg.msg):
				log.error(f"{self.GetName()}] Attempt to exit {pkg.msg} but not found in chatroom.")
			else:
				await chatroom.GetServer(pkg.msg).QueueSend(raw)

	async def ProcessPackage(self, raw: data.Transport):
		log.debug(f"{self.GetName()}] Received package {raw.seq_id} - {data.TransportDataType.ToString(raw.data_type)}")
		if raw.data_type == data.TransportDataType.CONTROL:
			return await self.processControlPackage(raw)
		
		if raw.from_uid != self.uid:
			log.warning(f"{self.GetName()}] Package {raw.seq_id} 's from_uid is not the right uid. Expect: {self.uid}. Got {raw.from_uid}.")
			raw.from_uid = self.uid
		await chatroom.Route(raw)

async def main(config: Configuration):
	async def wraploop(request: web.BaseRequest):
		if 'uid' not in request.query:
			return web.Response(
				body="Invalid request.",
				status=403
			)
		uid = request.query['uid']
		if not chatroom.Has(uid):
			client = Client(uid=uid, cacheSize=config.cacheSize, timeout=config.timeout)
			if chatroom.Add(uid, client) == False:
				raise RuntimeError("should not happend")

		server = chatroom.GetServer(uid)
		return await server.MainLoopWithRequest(request)

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
	log.info(f"Route: {config.listen_route}")
	# log.info(f"Please run remote.py to connect `{config.remote_listen}` and client.py to connect `{config.client_listen}`.")
	await asyncio.Future()

if __name__ == "__main__":
	asyncio.run(main(conf))