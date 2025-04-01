import tunnel, utils, encrypt, data
import asyncio, logging, typing
import argparse as argp
from client_tcp import Connection

log = logging.getLogger(__name__)
utils.SetupLogging(log, "remote_tcp", terminalLevel=logging.DEBUG)

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		self.port: int = args.port
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/remote_ws", help="The websocket server to connect to.")
		parser.add_argument("--port", type=int, default=1090, help="The port to listen on.")
		parser.add_argument("--cipher", type=str, default="xor", help=f"The cipher to use. Available: [{', '.join(encrypt.GetAvailableCipherMethods())}]")
		parser.add_argument("--key", type=str, default="websocket forward", help="The key to use for the cipher. Must be the same with client.")
		return parser

class IClient(tunnel.WebSocketTunnelClient):
	async def Session(self, raw: data.Transport) -> data.Transport:
		"""
		接受无加密的request，返回无加密的response。
		"""
		raise NotImplementedError
	async def SessionTCP(self, seq_id: str) -> typing.Generator[bytes, None, None]:
		raise NotImplementedError
	async def Send(self, raw: data.Transport) -> None:
		"""
		raw必须是未加密的
		"""
		raise NotImplementedError

client: typing.Union[None, IClient] = None

class Client(IClient):
	def __init__(self, config: Configuration, name="WebSocket Remote", **kwargs):
		super().__init__(config.server, name=name, **kwargs)
		self.config = config
		
		self.recvMaps: typing.Dict[str, asyncio.Queue[data.Transport]] = {}
	
	async def OnConnected(self):
		global client
		client = self
		return await super().OnConnected()
	
	async def OnDisconnected(self):
		global client
		client = None
		return await super().OnDisconnected()

	async def Send(self, raw: data.Transport):
		if self.config.cipher is not None: raw.data = self.config.cipher.Encrypt(raw.data)
		# await self.handler.send_bytes(raw.ToProtobuf())
		await self.QueueToSend(raw)
	
	async def Session(self, raw: data.Transport) -> data.Transport:
		self.recvMaps[raw.seq_id] = asyncio.Queue()
		await self.Send(raw)
		
		resp = await self.recvMaps[raw.seq_id].get()
		del self.recvMaps[raw.seq_id]
		return resp

	async def SessionTCP(self, seq_id: str):
		self.recvMaps[seq_id] = asyncio.Queue()
		while True:
			item = await self.recvMaps[seq_id].get()
			if item.data == b'' and item.total_cnt == -1:
				del self.recvMaps[seq_id]
				break
			yield item.data
		
	async def OnProcess(self, raw: data.Transport):
		if raw.data_type not in [data.TransportDataType.RESPONSE, data.TransportDataType.TCP_MESSAGE]: return
		if self.config.cipher is not None: raw.data = self.config.cipher.Decrypt(raw.data)

		# print(f"Got {len(raw.data)} bytes of {raw.seq_id} ({raw.seq_id in self.recvMaps})")
		if raw.seq_id in self.recvMaps:
			await self.recvMaps[raw.seq_id].put(raw)

class ProxyServer:
	def __init__(self, config: Configuration):
		self.config = config
		self.recvMap: typing.Dict[str, Connection] = {}
		
	def parseMessage(self, message: str):
		"""
		解析请求，获取目标主机和端口
		"""
		log.debug(f"parse >>>\n{repr(message)}\n<<<")
		lines = message.split('\n')
		host_port = lines[0].split(' ')[1]
		host, port = host_port.split(':')
		port = int(port)
		return host, port

	async def localToRemote(self, seq_id: str):
		try:
			while not self.recvMap[seq_id].reader.at_eof():
				pkg = await self.recvMap[seq_id].reader.read(4096)
				if not pkg: break
				raw = data.Transport(data.TransportDataType.TCP_MESSAGE, pkg, 0, 0, seq_id=seq_id)
				if client is not None:
					log.debug(f"Send {seq_id} - {self.recvMap[seq_id].display} - {len(raw.data)} bytes")
					await client.Send(raw)
		except Exception as e:
			log.error(f"Error {seq_id} - {self.recvMap[seq_id].display} - LocalToRemote error: {e}")
		finally:
			if self.recvMap[seq_id].isClosed == False:
				raw = data.Transport(data.TransportDataType.TCP_MESSAGE, b'', 0, 0, seq_id=seq_id)
				raw.total_cnt = -1
				await client.Send(raw)
				self.recvMap[seq_id].isClosed = True
	
	async def remoteToLocal(self, seq_id: str):
		async for item in client.SessionTCP(seq_id):
			item: bytes
			log.debug(f"Recv {seq_id} - {self.recvMap[seq_id].display} - {len(item)} bytes")
			self.recvMap[seq_id].writer.write(item)
			await self.recvMap[seq_id].writer.drain()
		self.recvMap[seq_id].writer.close()
		await self.recvMap[seq_id].writer.wait_closed()

		self.recvMap[seq_id].isClosed = True
		self.recvMap[seq_id].reader.feed_eof()
	
	async def handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
		# try:
		if True:
			# 读取客户端请求
			try:
				request = (await reader.read(4096)).decode('utf-8')
			except Exception as e:
				request = b''
			if request == b'':
				writer.close()
				await writer.wait_closed()
				return
			# print(f"Received request:\n{request}")

			host, port = self.parseMessage(request)
			if client is None:
				log.error(f"CONNECT {host}:{port} failed. Client doesn't connect to server.")
				writer.close()
				await writer.wait_closed()
				return
			
			raw = data.Transport(
				data.TransportDataType.TCP_CONNECT,
				data.TCPConnect(host, port).ToProtobuf(),
				remote_id=0,
				client_id=0
			)
			log.debug(f"Session {host}:{port}")
			resp = await client.Session(raw)
			assert(resp.data_type == data.TransportDataType.RESPONSE)

			resp = data.Response.FromProtobuf(resp.data)
			writer.write(resp.body)
			await writer.drain()
			
			if resp.status_code != 200:
				log.error(f"Session {host}:{port} - {raw.seq_id} - Close due to status code not 200: {resp.status_code}")
				writer.close()
				await writer.wait_closed()
				return
			
			self.recvMap[raw.seq_id] = Connection(reader, writer, display=f"{host}:{port}")
			log.info(f"Session {self.recvMap[raw.seq_id].display} - {raw.seq_id}")

			# 等待转发任务完成
			await asyncio.gather(
				self.localToRemote(raw.seq_id),
				self.remoteToLocal(raw.seq_id)
			)
			log.info(f"Close {raw.seq_id} - {self.recvMap[raw.seq_id].display}")
			del self.recvMap[raw.seq_id]

		# except Exception as e:
		# 	print(f"Error handling client: {e}")
		# finally:
		writer.close()
		await writer.wait_closed()
	
	async def MainLoop(self):
		# 创建 socket 并监听端口
		server = await asyncio.start_server(self.handler, '127.0.0.1', self.config.port)
		async with server:
			log.info(f"Async proxy server listening on port {self.config.port}...")
			await server.serve_forever()
		
async def main(config: Configuration):
	await asyncio.gather(Client(config).MainLoop(), ProxyServer(config).MainLoop())

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)
	asyncio.run(main(conf))
