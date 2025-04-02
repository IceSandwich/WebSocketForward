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

class Client(tunnel.WebSocketTunnelClient):
	def __init__(self, config: Configuration):
		super().__init__(config.server, log=log)
		self.config = config
		self.forward_session: aiohttp.ClientSession = None

	async def OnConnected(self):
		self.forward_session = aiohttp.ClientSession(timeout=self.timeout)
		await super().OnConnected()

	async def OnDisconnected(self):
		await self.forward_session.close()
		await super().OnDisconnected()

	async def processSSE(self, raw: data.Transport, req: data.Request, resp: aiohttp.ClientResponse):
		isFirst = True
		cur = 0
		async for chunk in resp.content.iter_any():
			if not chunk: continue
			# 将每一行事件发送给客户端
			if isFirst: # 第一个是response包
				isFirst = False
				respRaw = raw.CloneWithSameSeqID()
				respRaw.ResetData(
					data.Response(
						url=req.url,
						status_code=resp.status,
						headers = dict(resp.headers),
						body=chunk
					).ToProtobuf(),
					data_type=data.TransportDataType.RESPONSE
				)
				respRaw.SetPackages(0, cur)
				respRawData = respRaw.data if self.config.cipher is None else self.config.cipher.Encrypt(respRaw.data)
				log.debug(f"SSE >>>Begin {raw.seq_id} - {resp.url} {len(respRawData)} bytes")

				# log SSE Package at the first time
				log.debug(f"SSE Package <<< {repr(respRaw.data[:50])} ...>>> to <<< {repr(respRawData[:50])} ...>>>")
				respRaw.data = respRawData

				await self.QueueToSend(respRaw)
			else:
				respRaw = raw.CloneWithSameSeqID()
				respRaw.ResetData(
					chunk if self.config.cipher is None else self.config.cipher.Encrypt(chunk),
					data.TransportDataType.STREAM_SUBPACKAGE
				)
				respRaw.SetPackages(0, cur)
				log.debug(f"SSE Stream {raw.seq_id} - {len(respRaw.data)} bytes")
				await self.QueueToSend(respRaw)
			cur = cur + 1

		respRaw = raw.CloneWithSameSeqID()
		respRaw.ResetData(b'', data.TransportDataType.STREAM_SUBPACKAGE)
		respRaw.SetPackages(data.Transport.END_PACKAGE, cur)
		log.debug(f"SSE <<<End {respRaw.seq_id} - {resp.url} {len(respRaw.data)} bytes")
		await self.QueueToSend(respRaw)
		await resp.release()

	async def OnRecvStreamPackage(self, raw: data.Transport):
		await self.putStream(raw)

	async def OnRecvPackage(self, raw: data.Transport):
		if self.checkTransportDataType(raw, [data.TransportDataType.REQUEST]) == False: return
		
		rawData = raw.data if self.config.cipher is None else self.config.cipher.Decrypt(raw.data)
		req = data.Request.FromProtobuf(rawData)
		log.debug(f"Request {raw.seq_id} - {req.method} {req.url}")

		try:
			resp = await self.forward_session.request(req.method, req.url, headers=req.headers, data=req.body)
		except aiohttp.ClientConnectorError as e:
			log.error(f'Failed to forward request {req.url}: {e}')
			respRaw = raw.CloneWithSameSeqID()
			respRaw.ResetData(
				data.Response(
					req.url,
					502,
					{
						'Content-Type': 'text/plain; charset=utf-8',
					},
					f'=== Proxy server cannot request: {e}'.encode('utf8'),
				).ToProtobuf(),
				data.TransportDataType.RESPONSE
			)
			respRaw.data = respRaw.data if self.config.cipher is None else self.config.cipher.Encrypt(respRaw.data)
			await self.QueueSendSmartSegments(respRaw)
			return

		if 'text/event-stream' in resp.headers['Content-Type']:
			log.debug(f"SSE Response {raw.seq_id} - {req.method} {resp.url} {resp.status}")
			asyncio.ensure_future(self.processSSE(raw, req, resp))
		else:
			log.debug(f"Response {raw.seq_id} - {req.method} {resp.url} {resp.status}")
			respData = await resp.content.read()
			respRaw = raw.CloneWithSameSeqID()
			respRaw.ResetData(
				data.Response(
					req.url,
					resp.status,
					dict(resp.headers),
					respData
				).ToProtobuf(),
				data.TransportDataType.RESPONSE
			)
			respRaw.data = respRaw.data if self.config.cipher is None else self.config.cipher.Encrypt(respRaw.data)
			await self.QueueSendSmartSegments(respRaw)
			await resp.release()

	# async def Session(self, raw: data.Transport):
	# 	raise SyntaxError("This client doesn't support session method.")
	
	# async def Stream(self, seq_id: str):
	# 	raise SyntaxError("DONOT call stream method directly. It have been used internally.")

async def main(config: Configuration):
	client = Client(config)
	await client.MainLoop()

if __name__ == "__main__":
	argparse = argp.ArgumentParser()
	argparse = Configuration.SetupParser(argparse)
	args = argparse.parse_args()
	conf = Configuration(args)

	asyncio.run(main(conf))
