import asyncio, aiohttp
import aiohttp.web as web

class Handler:
	def __init__(self) -> None:
		self.ws: web.WebSocketResponse = None
		
	async def MainLoop(self):
		async for msg in self.ws:
			if msg.type == aiohttp.WSMsgType.ERROR:
				print(f"Error: {self.ws.exception()}")
			elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
				print(f"Recv: {msg.data}")
    
	async def Process(self, request: web.BaseRequest):
		if self.ws is not None:
			print("already connected")
			return None
		self.ws = web.WebSocketResponse()
		await self.ws.prepare(request)
		print("Connected")
		await self.MainLoop()
		print("Disconnected")
		bakws = self.ws
		self.ws = None
		return bakws
		
client = Handler()

async def main():
	app = web.Application()
	app.router.add_get('/client_ws', client.Process)
	
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, "127.0.0.1", 12228)
	await site.start()
	
	print(f"Server started on 127.0.0.1:12228")
	await asyncio.Future()

if __name__ == "__main__":
	asyncio.run(main())