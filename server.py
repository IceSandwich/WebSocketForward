import asyncio, aiohttp, logging
import aiohttp.web as web
import utils
import wsutils
import argparse as argp

log = logging.getLogger(__name__)
utils.SetupLogging(log, 'server')

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

clientOH = wsutils.OnceHandler("Client")
remoteOH = wsutils.OnceHandler("Remote")

class ClientWS(wsutils.Callbacks):
    def __init__(self):
        super().__init__("Client")
        
    async def OnProcess(self, msg: aiohttp.WSMessage):
        if remoteOH.IsConnected():
            await remoteOH.GetHandler().send_bytes(msg.data)
        else:
            print(f"{self.name}] Remote is not connected.")

class RemoteWS(wsutils.Callbacks):
    def __init__(self):
        super().__init__("Remote")
        
    async def OnProcess(self, msg: aiohttp.WSMessage):
        if clientOH.IsConnected():
            await clientOH.GetHandler().send_bytes(msg.data)
        else:
            print(f"{self.name}] Client is not connected.")

clientOH.SetCallbacks(ClientWS())
remoteOH.SetCallbacks(RemoteWS())

async def main(config: Configuration):
	app = web.Application()
	app.router.add_get('/client_ws', clientOH.Handler)
	app.router.add_get('/remote_ws', remoteOH.Handler)
	
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