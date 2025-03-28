import asyncio, aiohttp, argparse, logging
from data import SerializableRequest, SerializableResponse
import utils

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.forward_prefix: str = args.forward_prefix

	@classmethod
	def SetupParser(cls, parser: argparse.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/ws")
		parser.add_argument("--forward_prefix", type=str, default="http://127.0.0.1:7860")
		return parser
	
async def sse_process(req: SerializableRequest, resp: aiohttp.ClientResponse, session: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse):
	payload = SerializableResponse(
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

async def websocket_process(config: Configuration, ws: aiohttp.ClientWebSocketResponse):
	# 保持连接
	async for msg in ws:
		if msg.type == aiohttp.WSMsgType.BINARY:
			req = SerializableRequest.from_protobuf(msg.data)

			# 转发 HTTP 请求到目标服务器
			session = aiohttp.ClientSession()
			resp = await session.request(req.method, config.forward_prefix + req.url, headers=req.headers, data=req.body)
		
			if 'text/event-stream' in resp.headers['Content-Type']:
				log.info(f"SSE Proxy {req.method} {req.url}")
				asyncio.ensure_future(sse_process(req, resp, session, ws))
			else:
				log.info(f"Proxy {req.method} {req.url} {resp.status}")
				data = await resp.content.read()
				# 将响应数据通过 WebSocket 返回给客户端
				response = SerializableResponse(
					req.url,
					resp.status,
					dict(resp.headers),
					data,
					False
				)

				await ws.send_bytes(response.to_protobuf())
				await resp.release()
				await session.close()
			
		elif msg.type == aiohttp.WSMsgType.CLOSED:
			log.info("Server closed connection")
			break
		elif msg.type == aiohttp.WSMsgType.ERROR:
			log.error(f"Server raised error {msg.data}")
			break

async def main(config: Configuration):
	# 建立WebSocket连接
	headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	async with aiohttp.ClientSession() as session:
		async with session.ws_connect(config.server, headers=headers, max_msg_size=utils.WS_MAX_MSG_SIZE) as ws:
			print("Connecting to server")
			await websocket_process(config, ws)

if __name__ == "__main__":
	argparse = argparse.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
