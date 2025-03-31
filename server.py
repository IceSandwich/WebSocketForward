import asyncio
import aiohttp.web as web
from data import Transport
import argparse as argp
import tunnel
import typing
import utils, logging

log = logging.getLogger(__name__)
utils.SetupLogging(log, "server")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="127.0.0.1:8030")
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

class Client(tunnel.WebSocketTunnelServer):
	def __init__(self, name="WebSocket Client", **kwargs):
		super().__init__(name=name, **kwargs)

	async def OnConnected(self):
		global client
		if client is not None and client.IsConnected():
			print("A client want to connect but already connected to a client..")
			return False
		client = self
		return await super().OnConnected()

	async def OnDisconnected(self):
		global client
		client = None
		return await super().OnDisconnected()

	async def OnProcess(self, raw: Transport):
		# drop the message
		if remote is None or not remote.IsConnected(): return

		await remote.QueueToSend(raw)

class Remote(tunnel.WebSocketTunnelServer):
	def __init__(self, name="WebSocket Remote", **kwargs):
		super().__init__(name=name, **kwargs)

	async def OnConnected(self):
		global remote
		if remote is not None and remote.IsConnected():
			print("A remote want to connect but already connected to a remote.")
			return False
		remote = self
		return await super().OnConnected()

	async def OnDisconnected(self):
		global remote
		remote = None
		return await super().OnDisconnected()

	async def OnProcess(self, raw: Transport):
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
	
	print(f"Server started on {config.GetListenedAddress()}")
	await asyncio.Future()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))