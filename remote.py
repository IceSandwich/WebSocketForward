import asyncio, aiohttp, logging
from data import SerializableRequest, SerializableResponse
import utils, typing
import aiohttp.web as web
import argparse as argp
import wsutils
import os

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.port: int = args.port

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1/remote_ws")
		parser.add_argument("--port", type=int, default=1091)
		return parser

class WSCallbacks(wsutils.Callbacks):
	def __init__(self, config: Configuration):
		super().__init__("Remote")
		self.conf = config

		self.recvMaps: typing.Dict[str, SerializableResponse] = {}
		self.recvCondition = asyncio.Condition()
		self.ssePool: typing.Dict[str, asyncio.Queue[SerializableResponse]] = {}

	def IsClosed(self):
		return self.ws.closed

	def OnDisconnected(self):
		os._exit(-1)
	
	async def Session(self, request: SerializableRequest):
		await self.ws.send_bytes(request.to_protobuf())
		async with self.recvCondition:
			await self.recvCondition.wait_for(lambda: request.seq_id in self.recvMaps)
			resp = self.recvMaps[request.seq_id]
			del self.recvMaps[request.seq_id]
			return resp
		
	async def SessionSSE(self, seq_id: str):
		while True:
			data = await self.ssePool[seq_id].get()
			if data.stream_end:
				del self.ssePool[seq_id]
				yield data
				break
			yield data

	async def OnProcess(self, msg: aiohttp.WSMessage):
		resp = SerializableResponse.from_protobuf(msg.data)
		if resp.sse_ticket:
			isFirstSSE = resp.seq_id not in self.ssePool
			log.debug(f"recv sse: {resp.url} isFirst: {isFirstSSE} isEnd: {resp.stream_end}")
			if isFirstSSE:
				self.ssePool[resp.seq_id] = asyncio.Queue()
			else:
				await self.ssePool[resp.seq_id].put(resp)
			if isFirstSSE:
				async with self.recvCondition:
					self.recvMaps[resp.seq_id] = resp
					self.recvCondition.notify_all()
		else:
			async with self.recvCondition:
				log.debug(f"recv http: {resp.url}")
				self.recvMaps[resp.seq_id] = resp
				self.recvCondition.notify_all()

wsCB: WSCallbacks = None

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

async def http_handler(request: web.Request):
	if wsCB is None or wsCB.IsClosed():
		return web.Response(status=503, text="No client connected")
		
	# 提取请求的相关信息
	body = await request.read() if request.has_body else None
	req = SerializableRequest(str(request.url.path_qs), request.method, dict(request.headers), body)
	log.info(f"{req.method} {req.url} hasBody: {body is not None}")
	resp = await wsCB.Session(req)

	if resp.sse_ticket:
		async def sse_response(resp: SerializableResponse):
			yield resp.body
			async for package in wsCB.SessionSSE(resp.seq_id):
				yield package.body
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=sse_response(resp)
		)
	else:
		return web.Response(
			status=resp.status_code,
			headers=resp.headers,
			body=resp.body
		)

async def ws_main(config: Configuration):
    async def mainloop(ws: aiohttp.ClientWebSocketResponse):
        global wsCB
        wsCB = WSCallbacks(config)
        wsManager = wsutils.Manager(wsCB, ws)
        await wsManager.MainLoop()
    await wsutils.ConnectToServer(config.server, mainloop)

async def http_main(config: Configuration):
	app = web.Application()
	app.router.add_route('*', '/{tail:.*}', http_handler)  # HTTP服务
	
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, "127.0.0.1", port=config.port)
	await site.start()
	
	print(f"Server started on 127.0.0.1:{config.port}")
	await asyncio.Future()

async def main(config: Configuration):
    await asyncio.gather(ws_main(conf), http_main(conf))

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
