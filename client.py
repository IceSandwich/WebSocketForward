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

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/client_ws", help="Server address")
		parser.add_argument("--cipher", type=str, default="xor", help="Cipher to use for encryption")
		parser.add_argument("--key", type=str, default="websocket forward", help="The key to use for the cipher. Must be the same with remote.")
		return parser
	
class Client(tunnel.HttpUpgradedWebSocketClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server)
		self.config = config
		self.forward_session: aiohttp.ClientSession = None

	def GetLogger(self) -> logging.Logger:
		return log
	
	def GetName(self) -> str:
		return "Client"
	
	async def OnConnected(self):
		self.forward_session = aiohttp.ClientSession()
		return await super().OnConnected()
	
	async def OnDisconnected(self):
		await self.forward_session.close()
		await super().OnDisconnected()

	async def processSSE(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		"""
		由processSSE()释放resp。raw是模板，函数里会填充数据再发送。
		"""
		raw.cur_idx = 0
		raw.total_cnt = 0
		# send data.Response for the first time and send data.Subpackage for the remain sequences, set total_cnt = -1 to end
		# 对于 SSE 流，持续读取并转发数据
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

				await self.DirectSend(raw)
			else:
				raw.data_type = data.TransportDataType.STREAM_SUBPACKAGE
				log.debug(f"SSE Stream {raw.seq_id} - {len(chunk)} bytes")
				# print(f"Send SSE {raw.seq_id} - {raw.cur_idx}-ith")
				raw.data = chunk if self.config.cipher is None else self.config.cipher.Encrypt(chunk)
				await self.DirectSend(raw)
			# 第一个response的idx为0，第一个sse_subpackage的idx为1
			raw.cur_idx = raw.cur_idx + 1
		
		raw.data_type = data.TransportDataType.STREAM_SUBPACKAGE
		raw.total_cnt = -1 # -1 表示结束
		raw.data = b''
		log.debug(f"SSE <<<End {raw.seq_id} - {resp.url} {len(raw.data)} bytes")
		# print(f"Send End SSE {raw.seq_id} - {raw.cur_idx}-ith")
		await self.DirectSend(raw)
		await resp.release()

	async def OnRecvStreamPackage(self, raw: data.Transport) -> None:
		raise RuntimeError('should not happened')

	async def OnRecvPackage(self, raw: data.Transport) -> None:
		assert(raw.data_type == data.TransportDataType.REQUEST)
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
			return await self.DirectSend(raw)

		if 'text/event-stream' in resp.headers['Content-Type']:
			log.debug(f"SSE Response {raw.seq_id} - {req.method} {resp.url} {resp.status}")
			asyncio.ensure_future(self.processSSE(raw, req, resp))
		else:
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
			await self.DirectSend(raw)
			await resp.release()

async def main(config: Configuration):
	client = Client(config)
	await client.MainLoop()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))
