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

	@classmethod
	def SetupParser(cls, parser: argp.ArgumentParser):
		parser.add_argument("--server", type=str, default="http://127.0.0.1:8030/client_ws")
		parser.add_argument("--cipher", type=str, default="xor")
		parser.add_argument("--key", type=str, default="websocket forward")
		return parser
class ReqChunk:
	def __init__(self, total_cnt: int):
		self.total_cnt = total_cnt
		self.data: typing.List[bytes] = [None] * total_cnt
		self.cur_idx = 0
		self.req = None

	def IsFinish(self):
		return self.cur_idx >= self.total_cnt

	def Put(self, raw: typing.Union[data.Transport]):
		if raw.data_type not in [data.TransportDataType.SUBPACKAGE, data.TransportDataType.REQUEST]:
			raise Exception("unknown type")
		if self.IsFinish():
			raise Exception("already full.")
		
		self.data[raw.cur_idx] = raw.body
		if raw.data_type == data.TransportDataType.REQUEST:
			self.req = raw
		
		self.cur_idx = self.cur_idx + 1
	
	def Combine(self):
		raw = b''.join(self.data)
		self.req.body = raw
		self.req.cur_idx = 0
		self.req.total_cnt = 1
		return self.req

class Client(tunnel.WebSocketTunnelClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server, maxRetries=1)
		self.config = config
		self.forward_session = aiohttp.ClientSession()
		self.subpackages: typing.Dict[str, ReqChunk] = {}

	async def OnDisconnected(self):
		await self.forward_session.close()
		return await super().OnDisconnected()
	
	async def processSSE(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		raw.cur_idx = 0
		raw.total_cnt = 0
		# send data.Response for the first time and send data.Subpackage for the remain sequences, set total_cnt = -1 to end
		isFirst = True
		# 对于 SSE 流，持续读取并转发数据
		async for chunk in resp.content.iter_any():
			if not chunk: continue
			# 将每一行事件发送给客户端
			if isFirst:
				payload = data.Response(
					req.url,
					resp.status,
					dict(resp.headers),
					chunk,
					sse_ticket=True
				)
				raw.data_type = data.TransportDataType.RESPONSE
				raw.data = payload.ToProtobuf()
				if self.config.cipher is not None:
					raw.data = self.config.cipher.Encrypt(raw.data)
				raw.RenewTimestamp()
				await self.QueueToSend(raw)
				isFirst = False
			else:
				raw.data_type = data.TransportDataType.SSE_SUBPACKAGE
				raw.data = chunk if self.config.cipher is None else self.config.cipher.Encrypt(chunk)
				raw.RenewTimestamp()
				await self.QueueToSend(raw)
			# 第一个response的idx为0，第一个sse_subpackage的idx为1
			raw.cur_idx = raw.cur_idx + 1
		
		raw.total_cnt = -1 # -1 表示结束
		raw.data_type = data.TransportDataType.SSE_SUBPACKAGE
		raw.data = b''
		await self.QueueToSend(raw)

		log.info(f"SSE {payload.url} <<< Close")
		await resp.release()

	async def OnProcess(self, raw: data.Transport):
		if raw.data_type not in [data.TransportDataType.REQUEST, data.TransportDataType.SUBPACKAGE]: return

		if raw.data_type == data.TransportDataType.SUBPACKAGE:
			isGotPackage, package = await self.ReadSegments(raw)
			if isGotPackage == False: return
			raw = package

		rawData = raw.data if self.config.cipher is None else self.config.cipher.Decrypt(raw.data)
		req = data.Request.FromProtobuf(rawData)

		resp = await self.forward_session.request(req.method, req.url, headers=req.headers, data=req.body)
	
		if 'text/event-stream' in resp.headers['Content-Type']:
			log.info(f"SSE {req.method} {req.url} >>> Open")
			asyncio.ensure_future(self.processSSE(raw, req, resp))
		else:
			log.info(f"Proxy {req.method} {req.url} {resp.status}")
			respData = await resp.content.read()
			template = data.Response(
				req.url,
				resp.status,
				dict(resp.headers),
				None
			)
			await self.QueueSendSegments(raw, template, respData, cipher=self.config.cipher)
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
