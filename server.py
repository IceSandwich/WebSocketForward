import asyncio
import aiohttp
from aiohttp import web
from data import SerializableRequest, SerializableResponse
import protocol_pb2

Server_BaseURL = "127.0.0.1"
Server_Port = 8080

# 存储WebSocket连接
client_ws: web.WebSocketResponse = None

async def websocket_handler(request):
	global client_ws
	if client_ws != None:
		print("Already has connections")
		return "Already has connections"
	
	ws = web.WebSocketResponse(max_msg_size=12*1024*1024)
	await ws.prepare(request)
	
	client_ws = ws
	print("Client connected")
	
	try:
		async for msg in ws:
			if msg.type == aiohttp.WSMsgType.TEXT:
				await ws.send_str(f"Echo: {msg.data}")
			elif msg.type == aiohttp.WSMsgType.ERROR:
				print(f"WebSocket error: {ws.exception()}")
	finally:
		client_ws = None
		print("Client disconnected")
	
	return ws

async def http_handler(request: web.Request):
	if client_ws is None or client_ws.closed:
		return web.Response(status=503, text="No client connected")
	
	# 创建一个锁来同步 receive 操作
	receive_lock = asyncio.Lock()
	
	# 将HTTP请求转发给客户端
	async with aiohttp.ClientSession() as session:
		# 提取请求的相关信息
		request_data = SerializableRequest(str(request.url.path_qs), request.method, dict(request.headers))

		await client_ws.send_bytes(request_data.to_protobuf())
		async with receive_lock:  # 确保一次只有一个 receive 操作
			recv = await client_ws.receive_bytes()

		response = SerializableResponse.from_json(recv)

		return web.Response(
			status=response.status,
			headers=response.headers,
			body=response.body
		)

async def main():
	app = web.Application()
	app.router.add_get('/ws', websocket_handler)  # WebSocket升级端点
	app.router.add_route('*', '/{tail:.*}', http_handler)  # HTTP服务
	
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, Server_BaseURL, Server_Port)
	await site.start()
	
	print(f"Server started on {Server_BaseURL}:{Server_Port}")
	await asyncio.Future()

if __name__ == "__main__":
	asyncio.run(main())