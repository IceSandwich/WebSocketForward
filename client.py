import asyncio, aiohttp, logging
import data
import argparse as argp
import utils
import tunnel
import encrypt
import typing

log = logging.getLogger(__name__)
utils.SetupLogging(log, "client")

class Configuration:
	def __init__(self, args):
		self.server: str = args.server
		if args.cipher != "":
			self.cipher = encrypt.NewCipher(args.cipher, args.key)
		else:
			self.cipher = None
		if self.cipher is not None:
			log.info(f"Using cipher: {self.cipher.GetName()}")
		self.uid: str = args.uid

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/wsf/ws", help="Server address")
		parser.add_argument("--cipher", type=str, default="xor", help="Cipher to use for encryption")
		parser.add_argument("--key", type=str, default="WebSocket@Forward通讯密钥，必须跟另一端保持一致。", help="The key to use for the cipher. Must be the same with remote.")
		parser.add_argument("--uid", type=str, default="Client")
		return parser

class Connection:
	"""
	保存TCP连接
	"""
	def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, seq_id: str, display: str = ""):
		self.queue: asyncio.Queue[bytes] = asyncio.Queue()
		self.reader = reader
		self.writer = writer
		self.seq_id = seq_id
		
		# debug only
		self.display = display
		self.isClosed = False

		raise RuntimeError("Not ready to use.")

	async def CopyToSender(self, callback: tunnel.Callback):
		"""
		TCP流量这里没有加密，大部分是TLS数据。
		"""
		raw = data.Transport(data.TransportDataType.STREAM_SUBPACKAGE, None, "", "", seq_id=self.seq_id)
		raw.total_cnt = 0
		raw.cur_idx = 0
		try:
			while not self.reader.at_eof():
				pkg = await self.reader.read(4096)
				if not pkg: break
				raw.data = pkg
				log.debug(f'TCP {self.seq_id} Send {len(raw.data)} bytes')
				await callback.DirectSend(raw)
				raw.cur_idx = raw.cur_idx + 1
		except Exception as e:
			print(f"LocalToRemote {self.display} error: {e}")
		finally:
			if self.isClosed == False:
				raw.data = b''
				raw.total_cnt = -1
				await callback.DirectSend(raw)

				self.isClosed = True
				await self.queue.put(b'')

	async def copyFromReader(self):
		while True:
			item = await self.queue.get()
			if item == b'' and self.isClosed:
				print(f"Close connection {self.display}")
				self.isClosed = True
				self.reader.feed_eof()
				break
			print(f"Recv {len(item)} bytes")
			self.writer.write(item)
			await self.writer.drain()

	async def PutData(self, raw: bytes):
		pass

	async def MainLoop(self, callback: tunnel.Callback):
		await callback.OnConnected()
		await asyncio.gather(self.CopyToSender(), self.copyFromReader())
		await callback.OnDisconnected()

	async def Close(self):
		self.isClosed = True
		await self.queue.put(b'') # inform signal

class Client(tunnel.HttpUpgradedWebSocketClient):
	def __init__(self, config: Configuration):
		self.config = config
		self.forward_session: aiohttp.ClientSession = None

		self.tcpConnections: typing.Dict[str, Connection] = {}
		super().__init__(f"{config.server}?uid={config.uid}")

	def GetLogger(self) -> logging.Logger:
		return log
	
	def GetName(self) -> str:
		return self.config.uid
	
	async def OnConnected(self):
		self.forward_session = aiohttp.ClientSession()
		return await super().OnConnected()
	
	async def OnDisconnected(self):
		await self.forward_session.close()
		await super().OnDisconnected()

	async def OnRecvStreamPackage(self, raw: data.Transport) -> None:
		raise RuntimeError('Client should not receive stream package.')
	
	async def Session(self, raw: data.Transport):
		raise RuntimeError("Client doesn't support session.")
	
	async def SessionControl(self, raw: data.Control, uid: str):
		raise RuntimeError("Client doesn't support session control.")
	
	async def Stream(self, seq_id: str):
		raise RuntimeError("Client doesn't support stream.")
	
	async def OnRecvCtrlPackage(self, raw: data.Transport) -> bool:
		ctrl = data.Control.FromProtobuf(raw.data)
		if ctrl.data_type == data.ControlDataType.EXIT:
			log.info(f"{self.GetName()}] Receive exit signal. Exit the program.")
			return False
		elif ctrl.data_type == data.ControlDataType.PRINT:
			printStruct = data.PrintControlMsg.From(ctrl.msg)
			log.info(f"CTRL {printStruct.fromWho}] {printStruct.message}")
		return True
	
	async def OnRecvPackage(self, raw: data.Transport) -> None:
		"""
		返回False退出
		"""
		if raw.data_type == data.TransportDataType.REQUEST:
			await self.processRequestPackage(raw)
		elif raw.data_type == data.TransportDataType.TCP_CONNECT:
			await self.processTCPConnectPackage(raw)
		else:
			raise RuntimeError(f"Unsupported package data type: {data.TransportDataType.ToString(raw.data_type)}")

	async def processSSE(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		"""
		由processSSE()释放resp。raw是模板，函数里会填充数据再发送。
		"""
		raw.cur_idx = 0
		raw.total_cnt = 0
		raw.SwapSenderReciver()
		# send data.Response for the first time and send data.Subpackage for the remain sequences, set total_cnt = -1 to end
		# 对于 SSE 流，持续读取并转发数据
		try:
			async for chunk in resp.content.iter_any():
				if not chunk: continue
				# 将每一行事件发送给客户端
				if raw.cur_idx == 0:
					raw.data_type = data.TransportDataType.RESPONSE
					raw.data = data.Response(
						req.url,
						resp.status,
						dict(resp.headers),
						chunk
					).ToProtobuf()
					log.debug(f"SSE >>>Begin {raw.seq_id} - {resp.url} {len(raw.data)} bytes")
					rawData = raw.data if self.config.cipher is None else self.config.cipher.Encrypt(raw.data)

					# log SSE Package at the first time
					log.debug(f"SSE Package <<< {repr(raw.data[:50])} ...>>> to <<< {repr(rawData[:50])} ...>>>")
					raw.data = rawData
					# print(f"Send first sse {raw.seq_id} - {raw.cur_idx}-ith")

					await self.QueueSend(raw)
				else:
					raw.data_type = data.TransportDataType.STREAM_SUBPACKAGE
					log.debug(f"SSE Stream {raw.seq_id} - {len(chunk)} bytes")
					# print(f"Send SSE {raw.seq_id} - {raw.cur_idx}-ith")
					raw.data = chunk if self.config.cipher is None else self.config.cipher.Encrypt(chunk)
					await self.QueueSend(raw)
				# 第一个response的idx为0，第一个sse_subpackage的idx为1
				raw.cur_idx = raw.cur_idx + 1
		except Exception as e:
			log.error(f'SSE {raw.seq_id} end iter unexpectally. err: {e}')
		finally:
			raw.data_type = data.TransportDataType.STREAM_SUBPACKAGE
			raw.total_cnt = -1 # -1 表示结束
			raw.data = b''
			log.debug(f"SSE <<<End {raw.seq_id} - {resp.url} {len(raw.data)} bytes")
			# print(f"Send End SSE {raw.seq_id} - {raw.cur_idx}-ith")
			await self.QueueSend(raw)
			await resp.release()

	async def processRegularRequestPackage(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		"""
		resp由本函数释放。
		"""
		log.debug(f"Response {raw.seq_id} - {req.method} {resp.url} {resp.status}")

		respData = await resp.content.read()
		raw.data_type = data.TransportDataType.RESPONSE
		raw.data = data.Response(
			req.url,
			resp.status,
			dict(resp.headers),
			respData
		).ToProtobuf()
		raw.data = raw.data if self.config.cipher is None else self.config.cipher.Encrypt(raw.data)
		raw.SwapSenderReciver()
		await self.QueueSend(raw)
		await resp.release()
		
	async def processCompressImageRequestPackage(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		"""
		resp由本函数释放。
		"""
		respData = await resp.content.read()
		headers = dict(resp.headers)
		require_mimetype, require_quality = utils.GetWSFCompress(req)
		mimetype, rawData = utils.CompressImage(respData, require_mimetype, require_quality)
		compress_ratio = utils.ComputeCompressRatio(len(respData), len(rawData))
		utils.SetWSFCompress(headers, mimetype, quality=-1)

		log.debug(f"Response(Compress {int(compress_ratio*10000)/100}%) {raw.seq_id} - {req.method} {resp.url} {resp.status}")

		raw.data_type = data.TransportDataType.RESPONSE
		raw.data = data.Response(
			req.url,
			resp.status,
			headers,
			rawData
		).ToProtobuf()
		raw.data = raw.data if self.config.cipher is None else self.config.cipher.Encrypt(raw.data)
		raw.SwapSenderReciver()
		await self.QueueSend(raw)
		await resp.release()

	async def processRequestPackage(self, raw: data.Transport):
		rawData = raw.data if self.config.cipher is None else self.config.cipher.Decrypt(raw.data)

		try:
			req = data.Request.FromProtobuf(rawData)
		except Exception as e:
			log.error(f"Failed to parse {raw.seq_id} request, maybe the cipher problem: {e}")
			log.error(f"{raw.seq_id}] Source raw data: {raw.data[:100]}")
			log.error(f"{raw.seq_id}] Target raw data: {rawData[:100]}")
			raise e # rethrow the exception
		
		log.debug(f"Request {raw.seq_id} - {req.method} {req.url}")

		try:
			resp = await self.forward_session.request(req.method, req.url, headers=req.headers, data=req.body)
		except aiohttp.ClientConnectorError as e:
			log.error(f'Failed to forward request {req.url}: {e}')
			raw.data_type = data.TransportDataType.RESPONSE
			raw.data = data.Response(req.url, 502,
				{
					'Content-Type': 'text/plain; charset=utf-8',
				},
				f'=== Proxy server cannot request: {e}'.encode('utf8'),
			).ToProtobuf()
			raw.data = raw.data if self.config.cipher is None else self.config.cipher.Encrypt(raw.data)
			raw.SwapSenderReciver()
			return await self.QueueSend(raw)

		if 'text/event-stream' in resp.headers['Content-Type']:
			log.debug(f"SSE Response {raw.seq_id} - {req.method} {resp.url} {resp.status}")
			asyncio.ensure_future(self.processSSE(raw, req, resp))
		elif utils.HasWSFCompress(req):
			await self.processCompressImageRequestPackage(raw, req, resp)
		else:
			await self.processRegularRequestPackage(raw, req, resp)

	async def processTCPConnectPackage(self, raw: data.Transport):
		raise RuntimeError("Not ready to use")
	
		if raw.total_cnt != -1: # 开启连接
			package = data.TCPConnect.FromProtobuf(raw.data)
			print(f"Connect {package.host}:{package.port}")

			# 连接到目标主机
			resp = data.Transport(data.TransportDataType.RESPONSE, None, "", "", raw.seq_id)
			try:
				remote_reader, remote_writer = await asyncio.open_connection(package.host, package.port)
				self.tcpConnections[raw.seq_id] = Connection(remote_reader, remote_writer, display=f"{package.host}:{package.host}")

				class CB(tunnel.Callback):
					def __init__(self, seq_id: str):
						self.seq_id = seq_id
					async def OnConnected(that):
						pass
					async def OnDisconnected(that):
						del self.tcpConnection[that.seq_id]
					async def DirectSend(that, raw: data.Transport) -> None:
						return await self.QueueSend(raw)
				
				asyncio.create_task(self.tcpConnections[raw.seq_id].MainLoop(CB(raw.seq_id)))

				resp.data = data.Response("", 200, {},
					b'HTTP/1.1 200 Connection Established\r\n\r\n'
				).ToProtobuf()
				resp.data = resp.data if self.config.cipher is None else self.config.cipher.Encrypt(resp.data)
				await self.QueueSend(resp)
			except Exception as e:
				log.error(f"TCP] Failed to connnect {package.host}:{package.port}, err: {e}")
				resp.data = data.Response("", 502, {},
					b'HTTP/1.1 502 Bad Gateway\r\n\r\n'
				).ToProtobuf()
				resp.data = resp.data if self.config.cipher is None else self.config.cipher.Encrypt(resp.data)
				await self.QueueSend(resp)
		else: # 关闭连接
			self.tcpConnections[raw.seq_id].Close()

async def main(config: Configuration):
	client = Client(config)
	await client.MainLoop()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))
