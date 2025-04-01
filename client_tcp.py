import logging, aiohttp, asyncio, typing
import utils, encrypt, tunnel, data
import argparse as argp

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client_tcp", terminalLevel=logging.DEBUG)

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/client_ws", help="Server address")
		parser.add_argument("--cipher", type=str, default="xor", help="Cipher to use for encryption")
		parser.add_argument("--key", type=str, default="websocket forward", help="The key to use for the cipher. Must be the same with remote.")
		return parser

class Connection:
	def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, display: str = ""):
		self.queue: asyncio.Queue[bytes] = asyncio.Queue()
		self.reader = reader
		self.writer = writer
		
		# debug only
		self.display = display
	
		self.taskRemoteToLocal: asyncio.Task = None
		self.taskLocalToRemote: asyncio.Task = None

		self.isClosed = False


class Client(tunnel.WebSocketTunnelClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server)
		self.config = config
		self.recvMap: typing.Dict[str, Connection] = {}

	async def OnConnected(self):
		await super().OnConnected()

	async def OnDisconnected(self):
		await super().OnDisconnected()

	async def Send(self, raw: data.Transport):
		"""
		raw必须是未加密的
		"""
		if self.config.cipher is not None:
			raw.data = self.config.cipher.Encrypt(raw.data)
		# await self.handler.send_bytes(raw.ToProtobuf())
		await self.QueueToSend(raw)

	async def localToRemote(self, seq_id: str):
		try:
			while not self.recvMap[seq_id].reader.at_eof():
				pkg = await self.recvMap[seq_id].reader.read(4096)
				if not pkg: break
				raw = data.Transport(data.TransportDataType.TCP_MESSAGE, pkg, 0, 0, seq_id=seq_id)
				print(f"Send >>> {len(raw.data)} bytes")
				await self.Send(raw)
		except Exception as e:
			print(f"LocalToRemote {self.recvMap[seq_id].display} error: {e}")
		finally:
			if self.recvMap[seq_id].isClosed == False:
				raw = data.Transport(data.TransportDataType.TCP_MESSAGE, b'', 0, 0, seq_id=seq_id)
				raw.total_cnt = -1
				await self.Send(raw)

				self.recvMap[seq_id].isClosed = True
				await self.recvMap[seq_id].queue.put(b'')
				self.recvMap[seq_id].isClosed = True

	async def remoteToLocal(self, seq_id: str):
		while True:
			item = await self.recvMap[seq_id].queue.get()
			if item == b'' and self.recvMap[seq_id].isClosed:
				print(f"Close connection {self.recvMap[seq_id].display}")
				self.recvMap[seq_id].isClosed = True
				self.recvMap[seq_id].reader.feed_eof()
				break
			print(f"Recv {len(item)} bytes")
			self.recvMap[seq_id].writer.write(item)
			await self.recvMap[seq_id].writer.drain()

	async def OnProcess(self, raw: data.Transport):
		if raw.data_type not in [data.TransportDataType.TCP_CONNECT, data.TransportDataType.TCP_MESSAGE]: return

		if self.config.cipher is not None: raw.data = self.config.cipher.Decrypt(raw.data)

		if raw.data_type == data.TransportDataType.TCP_MESSAGE and raw.seq_id in self.recvMap:
			await self.recvMap[raw.seq_id].queue.put(raw.data)

		# 表示连接或者取消连接（当total_cnt = -1）
		if raw.data_type == data.TransportDataType.TCP_CONNECT:
			if raw.total_cnt != -1: # 开启连接
				package = data.TCPConnect.FromProtobuf(raw.data)
				print(f"Connect {package.host}:{package.port}")

				# 连接到目标主机
				resp = data.Transport(data.TransportDataType.RESPONSE, None, 0, 0, raw.seq_id)
				try:
					remote_reader, remote_writer = await asyncio.open_connection(package.host, package.port)
					self.recvMap[raw.seq_id] = Connection(remote_reader, remote_writer, display=f"{package.host}:{package.host}")
					# self.recvMap[raw.seq_id].taskRemoteToLocal = asyncio.create_task(self.remoteToLocal(raw.seq_id))
					# self.recvMap[raw.seq_id].taskLocalToRemote = asyncio.create_task(self.localToRemote(raw.seq_id))
					async def gatherToClose(seq_id: str):
						# await asyncio.gather(self.recvMap[raw.seq_id].taskLocalToRemote, self.recvMap[raw.seq_id].taskRemoteToLocal)
						await asyncio.gather(self.remoteToLocal(seq_id), self.localToRemote(seq_id))
						del self.recvMap[seq_id]
					asyncio.create_task(gatherToClose(raw.seq_id))
					resp.data = data.Response("", 200, {},
						b'HTTP/1.1 200 Connection Established\r\n\r\n'
					).ToProtobuf()
					await self.Send(resp)
				except Exception as e:
					print(f"Failed to connnect {package.host}:{package.port}, err: {e}")
					resp.data = data.Response("", 502, {},
						b'HTTP/1.1 502 Bad Gateway\r\n\r\n'
					).ToProtobuf()
					await self.Send(resp)
					return
			else: # 关闭连接
				self.recvMap[raw.seq_id].isClosed = True
				await self.recvMap[raw.seq_id].queue.put(b'') # inform signal


async def main(config: Configuration):
	client = Client(config)
	await client.MainLoop()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))