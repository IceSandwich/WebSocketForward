import aiohttp
import asyncio

headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
async def main():
	cur_retries = 1
	max_retries = 3
	while cur_retries <= max_retries:
		async with aiohttp.ClientSession() as session:
			async with session.ws_connect("http://127.0.0.1:12228/client_ws", headers=headers, max_msg_size=1024*1024) as ws:
				print("Connected to server")
				cur_retries = 1
				async for msg in ws:
					if msg.type == aiohttp.WSMsgType.ERROR:
						print(f"Error: {ws.exception()}")
					elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
						print(f"Got: {msg.data}")
		print("Disconnected to server. Reconnect...")
		cur_retries = cur_retries + 1

if __name__ == "__main__":
	asyncio.run(main())