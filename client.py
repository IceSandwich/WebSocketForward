import asyncio
import aiohttp
from aiohttp import web, ClientSession
from data import SerializableRequest, SerializableResponse
import config
import utils, typing, requests

# 服务器S的地址
SERVER_URL = "http://127.0.0.1:8030/ws"
FORWARD_PREFIX = "http://127.0.0.1:7860"

pq = utils.ProxyRequest()

async def websocket_client():
	# 建立WebSocket连接
	headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	async with ClientSession() as session:
		async with session.ws_connect(SERVER_URL, headers=headers, max_msg_size=config.WS_MAX_MSG_SIZE) as ws:
			print("Connected to server")
			
			# 保持连接
			async for msg in ws:
				if msg.type == aiohttp.WSMsgType.BINARY:
					req = SerializableRequest.from_protobuf(msg.data)

					# 转发 HTTP 请求到目标服务器
					session = aiohttp.ClientSession()
					# print("from browser: ", req.url)
					resp = await session.request(req.method, FORWARD_PREFIX + req.url, headers=req.headers, data=req.body)
				
					if 'text/event-stream' in resp.headers['Content-Type']:
						async def sse_process(req, resp):
							print("sse => ", resp.url)
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
									print("send => ", payload.url)
									await ws.send_bytes(payload.to_protobuf())
							payload.stream_end = True
							print("end => ", payload.url)
							await ws.send_bytes(payload.to_protobuf())
							await resp.release()
							await session.close()
						asyncio.ensure_future(sse_process(req, resp))
					else:
						data = await resp.content.read()
						# print("read => ", resp.url, data[:100])
						# 将响应数据通过 WebSocket 返回给客户端
						response = SerializableResponse(
							req.url,
							resp.status,
							dict(resp.headers),
							# resp.raw.read(),
							data,
							False
						)

						await ws.send_bytes(response.to_protobuf())
						await resp.release()
						await session.close()
					
				elif msg.type == aiohttp.WSMsgType.CLOSED:
					print("Server closed connection")
					break
				elif msg.type == aiohttp.WSMsgType.ERROR:
					print(f"Server raised error {msg.data}")
					break

if __name__ == "__main__":
	asyncio.run(websocket_client())