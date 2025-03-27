import asyncio
import aiohttp
from aiohttp import web, ClientSession
from data import SerializableRequest, SerializableResponse
import config

# 服务器S的地址
SERVER_URL = "http://127.0.0.1:8030/ws"
FORWARD_PREFIX = "http://127.0.0.1:8080"

# 创建一个 aiohttp 客户端会话，用于转发 HTTP 请求
async def forward_http_request(http_method, url, headers, body):
    async with aiohttp.ClientSession() as session:
        async with session.request(http_method, url, headers=headers, data=body) as response:
            response_data = await response.read()
            return response.status, response.headers, response_data

async def websocket_client():
	# 建立WebSocket连接
	headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
	async with ClientSession() as session:
		async with session.ws_connect(SERVER_URL, headers=headers, max_msg_size=config.WS_MAX_MSG_SIZE) as ws:
			print("Connected to server")
			
			# # 启动本地HTTP服务
			# app = web.Application()
			# app.router.add_route('*', '/{tail:.*}', forward_to_local)
			# runner = web.AppRunner(app)
			# await runner.setup()
			# site = web.TCPSite(runner, 'localhost', 8000)
			# await site.start()
			
			# 保持连接
			async for msg in ws:
				if msg.type == aiohttp.WSMsgType.BINARY:
				# if msg.type == aiohttp.WSMsgType.TEXT:
					req = SerializableRequest.from_protobuf(msg.data)
					print("recv ===> ", req)
					# 转发 HTTP 请求到目标服务器
					status_code, response_headers, response_body = await forward_http_request(req.method, FORWARD_PREFIX + req.url, req.headers, None)

					print("send <=== ", status_code)
					print("resp <==", response_body[:20])
					# 将响应数据通过 WebSocket 返回给客户端
					response_data = SerializableResponse(
						status_code,
						dict(response_headers),
						response_body
					)

					await ws.send_bytes(response_data.to_protobuf())
					
				elif msg.type == aiohttp.WSMsgType.CLOSED:
					print("Server closed connection")
					break
				elif msg.type == aiohttp.WSMsgType.ERROR:
					print(f"Server raised error {msg.data}")
					break

if __name__ == "__main__":
	asyncio.run(websocket_client())