import asyncio, aiohttp, logging
from data import SerializableRequest, SerializableResponse
import argparse as argp
import utils
import wsutils

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.forward_prefix: str = args.forward_prefix

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/client_ws")
		parser.add_argument("--forward_prefix", type=str, default="http://127.0.0.1:7860")
		return parser
	
async def sse_process(req: SerializableRequest, resp: aiohttp.ClientResponse, session: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse):
	payload = SerializableResponse(
		req.seq_id,
		req.url,
		resp.status,
		dict(resp.headers),
		None,
		True,
		False
	)
	# 对于 SSE 流，持续读取并转发数据
	async for chunk in resp.content.iter_any():
		if chunk:
			# 将每一行事件发送给客户端
			payload.body = chunk
			await ws.send_bytes(payload.to_protobuf())
	payload.stream_end = True
	log.info(f"SSE Stream end {payload.url}")
	await ws.send_bytes(payload.to_protobuf())
	await resp.release()
	await session.close()

class ClientCallbacks(wsutils.Callbacks):
	def __init__(self, config: Configuration):
		super().__init__("Server")
		self.config = config
		self.queue: asyncio.Queue[SerializableResponse] = asyncio.Queue()
		
	async def OnProcess(self, msg: aiohttp.WSMessage):
		if not self.queue.empty():
			print(f"Resend {len(self.queue)} responses.")
			while not self.queue.empty():
				oldresp = await self.queue.get()
				self.ws.send_bytes(oldresp)
		
		if msg.type != aiohttp.WSMsgType.BINARY: return
		
		req = SerializableRequest.from_protobuf(msg.data)

		# 转发 HTTP 请求到目标服务器
		timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_read=60, sock_connect=60)  # 设置更长的读取和连接超时时间
		session = aiohttp.ClientSession(timeout=timeout)
		resp = await session.request(req.method, self.config.forward_prefix + req.url, headers=req.headers, data=req.body)
	
		if 'text/event-stream' in resp.headers['Content-Type']:
			log.info(f"SSE Proxy {req.method} {req.url}")
			asyncio.ensure_future(sse_process(req, resp, session, self.ws))
		else:
			log.info(f"Proxy {req.method} {req.url} {resp.status} hasBody: {req.body is not None}")
			data = await resp.content.read()
			# 将响应数据通过 WebSocket 返回给客户端
			response = SerializableResponse(
				req.seq_id,
				req.url,
				resp.status,
				dict(resp.headers),
				data,
				False
			)
			await self.queue.put(response)

			await self.ws.send_bytes(response.to_protobuf()) # may raise error, but we can retreie old resp in queue
			await self.queue.get()
			await resp.release()
			await session.close()

async def main(config: Configuration):
	while True:
		cb = ClientCallbacks(config)
		try:
			async def mainloop(ws: aiohttp.ClientWebSocketResponse):
				manager = wsutils.Manager(cb, ws)
				await manager.MainLoop()
			await wsutils.ConnectToServer(config.server, mainloop)
		except KeyboardInterrupt:
			return
		print("Reconnecting...")

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
