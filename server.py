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

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1:8030", help="The address to listen on.")
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

client: typing.Union[None, tunnel.WebSocketTunnelServer] = None
remote: typing.Union[None, tunnel.WebSocketTunnelServer] = None
toClient: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(100)
toRemote: utils.BoundedQueue[data.Transport] = utils.BoundedQueue(100)

MaxTimeout = time_utils.Minutes(1) # 丢弃超过1分钟的包

class Common(tunnel.WebSocketTunnelServer):
	def __init__(self, name="WebSocket Common", **kwargs):
		super().__init__(name=name, log=log, **kwargs)

	async def resend(self, queue: utils.BoundedQueue[data.Transport]):
		if queue.IsEmpty(): return

		items = await queue.PopAllElements()
		log.info(f"{self.name}] Resend {len(items)} package.")
		nowtime = time_utils.GetTimestamp()
		for item in items:
			if time_utils.WithInDuration(item.timestamp, nowtime, MaxTimeout):
				await self.QueueToSend(item)
			else:
				log.warning(f"{self.name}] Drop package {item.seq_id} {data.TransportDataType.ToString(item.data_type)} due to outdated.")

class Client(Common):
	def __init__(self, name="WebSocket Client", **kwargs):
		super().__init__(name=name, **kwargs)

	async def OnPreConnected(self) -> bool:
		global client
		if client is not None and client.IsConnected():
			log.error("A client want to connect but already connected to a client..")
			return False
		return await super().OnPreConnected()

	async def OnConnected(self):
		global client
		client = self
		await self.resend(toClient)
		await super().OnConnected()

	async def OnDisconnected(self):
		global client
		client = None
		await super().OnDisconnected()

	async def OnProcess(self, raw: data.Transport):
		global remote
		# drop the message
		if remote is None or not remote.IsConnected():
			await toRemote.Add(raw)
			return

		await remote.QueueToSend(raw)

class Remote(Common):
	def __init__(self, name="WebSocket Remote", **kwargs):
		super().__init__(name=name, **kwargs)

	async def OnPreConnected(self) -> bool:
		global remote
		if remote is not None and remote.IsConnected():
			log.error("A remote want to connect but already connected to a remote.")
			return False
		return await super().OnPreConnected()

	async def OnConnected(self):
		global remote
		remote = self
		await self.resend(toRemote)
		await super().OnConnected()

	async def OnDisconnected(self):
		global remote
		remote = None
		return await super().OnDisconnected()

	async def OnProcess(self, raw: data.Transport):
		global client
		# drop the message
		if client is None or not client.IsConnected(): return

		await client.QueueToSend(raw)

async def main(config: Configuration):
	app = web.Application()
	app.router.add_get('/client_ws', tunnel.WebSocketTunnelServerHandler(Client))
	app.router.add_get('/remote_ws', tunnel.WebSocketTunnelServerHandler(Remote))
	
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
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))