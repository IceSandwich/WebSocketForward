import asyncio, typing
import aiohttp.web as web
import argparse as argp
import typing
import data, tunnel, utils, logging, time_utils

log = logging.getLogger(__name__)
utils.SetupLogging(log, "server")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.hasCache: bool = args.cache
		self.timeout = time_utils.Seconds(args.timeout)
		self.cacheSize: int = args.cache_size

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1:8030", help="The address to listen on.")
		parser.add_argument("--cache", action='store_true', help="Resend package when reconnecting.")
		parser.add_argument("--cache_size", type=int, default=128, help="The maximum number of packages to cache.")
		parser.add_argument("--timeout", type=int, default=10, help="The maximum seconds to resend packages.")
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

servers: typing.List[typing.Optional[tunnel.HttpUpgradedWebSocketServer]] = [None] * 2
cacheQueues: typing.List[utils.BoundedQueue[data.Transport]] = [ utils.BoundedQueue(conf.cacheSize) for _ in range(len(servers))]

class Client(tunnel.HttpUpgradedWebSocketServer):
	def __init__(self, cur_id: int, opposite_id: int, name="WebSocket Client", **kwargs):
		super().__init__(**kwargs)
		self.cur_id = cur_id
		self.opposite_id = opposite_id
		self.name = name

	def GetName(self) -> str:
		return self.name
	
	def GetLogger(self) -> logging.Logger:
		return log

	async def resend(self):
		if self.cacheQueue.IsEmpty(): return

		items = await self.cacheQueue.PopAllElements()
		log.info(f"{self.name}] Resend {len(items)} package.")
		nowtime = time_utils.GetTimestamp()
		for item in items:
			if time_utils.WithInDuration(item.timestamp, nowtime, conf.timeout):
				await self.DirectSend(item)
			else:
				log.warning(f"{self.name}] Drop package {item.seq_id} {data.TransportDataType.ToString(item.data_type)} due to outdated.")

	async def OnPreConnected(self) -> bool:
		if servers[self.cur_id] is not None and servers[self.cur_id].IsConnected():
			log.warning(f"A connection to {self.name} but already connected.")
			return False
		return await super().OnPreConnected()
	
	async def OnConnected(self):
		servers[self.cur_id] = self
		if conf.hasCache:
			await self.resend()
		await super().OnConnected()

	async def OnDisconnected(self):
		servers[self.cur_id] = None
		await super().OnDisconnected()

	async def ProcessPackage(self, raw: data.Transport):
		if servers[self.opposite_id] is None or not servers[self.opposite_id].IsConnected():
			if conf.hasCache:
				raw.RenewTimestamp() # use server's timestamp
				await cacheQueues[self.opposite_id].Add(raw)
		else:
			log.debug(f"{self.name}] Transfer {raw.seq_id}")
			await servers[self.opposite_id].DirectSend(raw)

async def main(config: Configuration):
	app = web.Application()
	app.router.add_get('/client_ws', tunnel.HttpUpgradedWebSocketServerHandler(Client, cur_id=0, opposite_id=1, name="Client"))
	app.router.add_get('/remote_ws', tunnel.HttpUpgradedWebSocketServerHandler(Client, cur_id=1, opposite_id=0, name="Remote"))
	
	runner = web.AppRunner(app)
	await runner.setup()
	if config.IsTCPAddress():
		server_name, server_port = config.SeparateTCPAddress()
		site = web.TCPSite(runner, server_name, server_port)
	else:
		site = web.UnixSite(runner, config.server)
	await site.start()
	
	log.info(f"Server started on {config.GetListenedAddress()}")
	await asyncio.Future()

if __name__ == "__main__":
	asyncio.run(main(conf))