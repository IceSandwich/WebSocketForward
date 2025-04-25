import encryptor
import time_utils

from . import protocol_pb2 as pb
import uuid
import abc
import typing
import json

def GenerateSequenceId():
	return str(uuid.uuid4())

class TypeMapping:
	def __init__(self, mappings: typing.List[typing.Tuple[int, str, typing.Any]]):
		"""
		mappings: [ (value, name, protobuf) ]
		"""
		self.mappings = mappings
		self.valueToString = { x[0]: x[1] for x in mappings }
		self.valueToPB = { x[0]: x[2] for x in mappings }
		self.pbToValue = { x[2]: x[0] for x in mappings }

	def ValueToString(self, value: int) -> str:
		if value in self.valueToString:
			return self.valueToString[value]
		return f'(Unknown:{value})'
	
	def PBToValue(self, pb):
		if pb in self.pbToValue:
			return self.pbToValue[pb]
		return -1
	
	def ValueToPB(self, value: int):
		return self.valueToPB[value]

class Transport:
	CONTROL = 0
	REQUEST = 1
	RESPONSE = 2
	SUBPACKAGE = 3
	STREAM_DATA = 4
	UNKNOWN = -1

	Mappings = TypeMapping([
		[CONTROL, 'CONTROL', pb.TransportType.CONTROL],
		[REQUEST, 'REQUEST', pb.TransportType.REQUEST],
		[RESPONSE, 'RESPONSE', pb.TransportType.RESPONSE],
		[SUBPACKAGE, 'SUBPACKAGE', pb.TransportType.SUBPACKAGE],
		[STREAM_DATA, 'STREAM_DATA', pb.TransportType.STREAM_DATA],
	])

	def __init__(self, transportType: int):
		self.transportType = transportType
		self.sender = ""
		self.receiver = ""

		self.seq_id = GenerateSequenceId()
		self.cur_idx = 0
		self.total_cnt = 1

		self.data = b''

		self.timestamp = time_utils.GetTimestamp()

	def SetSenderReceiver(self, senderId: str, receiverId: str):
		self.sender = senderId
		self.receiver = receiverId

	def SetForResponseTransport(self, transport):
		self.sender = transport.receiver
		self.receiver = transport.sender
		self.seq_id = transport.seq_id

	def SetIndex(self, curIdx: int, totalIdx: int):
		self.cur_idx = curIdx
		self.total_cnt = totalIdx

	def Pack(self) -> bytes:
		pid = pb.PackageId()
		pid.cur_idx = self.cur_idx
		pid.total_cnt = self.total_cnt
		pid.seq_id = self.seq_id

		ret = pb.Transport()
		ret.type = self.Mappings.ValueToPB(self.transportType)
		ret.sender = self.sender
		ret.receiver = self.receiver
		ret.id.MergeFrom(pid)
		ret.data = self.data
		
		return ret.SerializeToString()
	
	@classmethod
	def Parse(cls, whole_data: bytes):
		pbt = pb.Transport()
		pbt.ParseFromString(whole_data)

		ret = Transport(Transport.Mappings.PBToValue(pbt.type))
		ret.receiver = pbt.receiver
		ret.sender = pbt.sender
		ret.seq_id = pbt.id.seq_id
		ret.cur_idx = pbt.id.cur_idx
		ret.total_cnt = pbt.id.total_cnt
		ret.data = pbt.data
		return ret

	def CopyFrom(self, transport):
		self.transportType: int = transport.transportType
		self.sender: str = transport.sender
		self.receiver: str = transport.receiver
		self.seq_id: str = transport.seq_id
		self.cur_idx: int = transport.cur_idx
		self.total_cnt: int = transport.total_cnt
		self.data: bytes = transport.data

class PackageId:
	def __init__(self, cur_idx: int = 0, total_cnt = 1, seq_id = None):
		self.seq_id = GenerateSequenceId() if seq_id is None else seq_id
		self.cur_idx = cur_idx
		self.total_cnt = total_cnt

	def Pack(self):
		"""
		Pack to pb format
		"""
		ret = pb.PackageId()
		ret.seq_id = self.seq_id
		ret.cur_idx = self.cur_idx
		ret.total_cnt= self.total_cnt
		return ret
	
	@classmethod
	def Unpack(cls, pkg):
		"""
		Unpack from pb format
		"""
		ret = PackageId()
		ret.seq_id = pkg.seq_id
		ret.cur_idx = pkg.cur_idx
		ret.total_cnt = pkg.total_cnt
		return ret
	
	def IsSamePackage(self, raw: Transport):
		return raw.seq_id == self.seq_id and raw.cur_idx == self.cur_idx and raw.total_cnt == self.total_cnt
	
	@classmethod
	def FromPackage(cls, raw: Transport):
		ret = PackageId()
		ret.seq_id = raw.seq_id
		ret.cur_idx = raw.cur_idx
		ret.total_cnt = raw.total_cnt
		return ret

class Request(Transport):
	def __init__(self, encrypt: typing.Optional[encryptor.Cipher] = None):
		super().__init__(Transport.REQUEST)
		self.url = ""
		self.method = ""
		self.headers: typing.Dict[str, str] = {}
		self.body = None
		self.encrypt = encrypt

	def Init(self, url: str, method: str, headers: typing.Dict[str, str]):
		self.url = url
		self.method = method
		self.headers = headers

	def SetBody(self, body: typing.Optional[bytes]):
		self.body = body

	def Prepack(self):
		req = pb.Request()
		req.url = self.url
		req.method = self.method
		req.headers = json.dumps(self.headers)
		if self.body is not None:
			req.body = self.body
		
		reqBytes: bytes = req.SerializeToString()
		if self.encrypt is not None:
			reqBytes = self.encrypt.Encrypt(reqBytes)
		
		self.data = reqBytes

	def Pack(self) -> bytes:
		self.Prepack()
		return super().Pack()
	
	def Unpack(self, transport: Transport):
		super().CopyFrom(transport)
		assert self.transportType == Transport.REQUEST, f"protocol.Request got package indicated as {Transport.Mappings.ValueToString(self.transportType)} which should be {Transport.Mappings.ValueToString(Transport.REQUEST)}."

		data = self.data
		if self.encrypt is not None:
			data = self.encrypt.Decrypt(data)

		pbt = pb.Request()
		pbt.ParseFromString(data)
		self.url = pbt.url
		self.method = pbt.method
		self.headers = json.loads(pbt.headers)
		self.body = pbt.body

class Response(Transport):
	def __init__(self, encrypt: typing.Optional[encryptor.Cipher] = None):
		super().__init__(Transport.RESPONSE)
		self.url = ""
		self.status = -1
		self.headers: typing.Dict[str, str] = {}
		self.body = b''
		self.encrypt = encrypt

	def Init(self, url: str, status: int, headers: typing.Dict[str, str], body: bytes = b''):
		self.url = url
		self.status = status
		self.headers = headers
		self.body = body

	def SetBody(self, body: typing.Optional[bytes]):
		self.body = body

	def Prepack(self):
		res = pb.Response()
		res.url = self.url
		res.status = self.status
		res.headers = json.dumps(self.headers)
		res.body = self.body

		resBytes: bytes = res.SerializeToString()
		if self.encrypt is not None:
			resBytes = self.encrypt.Encrypt(resBytes)

		self.data = resBytes

	def Pack(self) -> bytes:
		self.Prepack()
		return super().Pack()
	
	def Unpack(self, transport: Transport):
		super().CopyFrom(transport)
		assert self.transportType == Transport.RESPONSE, f"protocol.Response got package indicated as {Transport.Mappings.ValueToString(self.transportType)} which should be {Transport.Mappings.ValueToString(Transport.RESPONSE)}."

		data = self.data
		if self.encrypt is not None:
			data = self.encrypt.Decrypt(data)

		pbt = pb.Response()
		pbt.ParseFromString(data)
		self.url = pbt.url
		self.status = pbt.status
		self.headers = json.loads(pbt.headers)
		self.body = pbt.body

class Subpackage(Transport):
	def __init__(self, encrypt: typing.Optional[encryptor.Cipher] = None):
		super().__init__(Transport.SUBPACKAGE)
		self.encrypt = encrypt
		self.body = b''

	def SetBody(self, body: bytes):
		self.body = body
	
	def Pack(self) -> bytes:
		self.data = self.body
		if self.encrypt is not None:
			self.data = self.encrypt.Encrypt(self.data)

		return super().Pack()
	
	EXPECT_TYPE = Transport.SUBPACKAGE
	def Unpack(self, transport: Transport):
		super().CopyFrom(transport)
		assert self.transportType == self.EXPECT_TYPE, f"protocol.Subpackage got package {transport.seq_id} indicated as {Transport.Mappings.ValueToString(self.transportType)} which should be {Transport.Mappings.ValueToString(self.EXPECT_TYPE)}."

		self.body = self.data
		if self.encrypt is not None:
			self.body = self.encrypt.Decrypt(self.body)

class StreamData(Subpackage):
	EXPECT_TYPE = Transport.STREAM_DATA
	def __init__(self, encrypt: typing.Optional[encryptor.Cipher] = None):
		super().__init__(encrypt)
		self.transportType = Transport.STREAM_DATA

	def __lt__(self, other):
		return self.cur_idx < other.cur_idx

class ClientInfo:
	CLIENT = 0
	REMOTE = 1
	UNKNOWN = -1

	def __init__(self, clientId: str, clientType: int):
		self.id = clientId
		self.type = clientType

	def Pack(self):
		"""
		Pack to protobuf type
		"""
		pbt = pb.ClientInfo()
		pbt.id = self.id
		pbt.type = self.type
		return pbt
	
	@classmethod
	def Unpack(cls, pb):
		return ClientInfo(pb.id, pb.type)

class HelloClientControl:
	"""
	Client/Remote send HelloClientControl package.
	Server receive HelloClientControl package.
	"""
	def __init__(self, info: ClientInfo):
		self.info = info
		self.pkgs: typing.List[PackageId] = []

	def Append(self, pkg: PackageId):
		self.pkgs.append(pkg)

	def Pack(self):
		pbt = pb.HelloClientControl()
		pbt.info.id = self.info.id
		pbt.info.type = self.info.type
		pbt.pkgs.extend([ x.Pack() for x in self.pkgs ])
		return pbt
	
	@classmethod
	def Unpack(cls, data: bytes):
		pbt = pb.HelloClientControl()
		pbt.ParseFromString(data)
		
		ret = HelloClientControl(ClientInfo(pbt.info.id, pbt.info.type))
		ret.pkgs.extend([ PackageId.Unpack(x) for x in pbt.pkgs ])
		return ret

class HelloServerControl:
	"""
	Client/Remote receive HelloServerControl package.
	Server send HelloServerControl package.
	"""
	def __init__(self):
		self.clients: typing.List[ClientInfo] = []
		self.pkgs: typing.List[PackageId] = []

	def AppendClient(self, client: ClientInfo):
		self.clients.append(client)
	
	def AppendPkg(self, pkg: PackageId):
		self.pkgs.append(pkg)

	def IsEmpty(self):
		return len(self.clients) == 0 and len(self.pkgs) == 0

	def Pack(self):
		pbt = pb.HelloServerControl()
		pbt.clients.extend([ x.Pack() for x in self.clients ])
		pbt.pkgs.extend([ x.Pack() for x in self.pkgs ])
		return pbt
	
	@classmethod
	def Unpack(cls, data: bytes):
		pbt  = pb.HelloServerControl()
		pbt.ParseFromString(data)

		ret = HelloServerControl()
		ret.clients = [ ClientInfo.Unpack(x) for x in pbt.clients ]
		ret.pkgs = [ PackageId.Unpack(x) for x in pbt.pkgs  ]
		return ret
	
class QueryClientsControl:
	def __init__(self):
		self.connected: typing.List[ClientInfo] = []

	def Append(self, client: ClientInfo):
		self.connected.append(client)

	def __len__(self):
		return len(self.connected)

	def Pack(self):
		pbt = pb.QueryClientsControl()
		pbt.connected.extend([ x.Pack() for x in self.connected ])
		return pbt
	
	def __iter__(self):
		return iter(self.connected)
	
	@classmethod
	def Unpack(cls, data: bytes):
		pbt   = pb.QueryClientsControl()
		pbt.ParseFromString(data)

		ret = QueryClientsControl()
		ret.connected = [ ClientInfo.Unpack(x) for x in pbt.connected   ]
		return ret
	
class Control(Transport):
	HELLO = 0
	QUERY_CLIENTS = 1
	QUIT_SIGNAL = 2
	PRINT = 3
	RETRIEVE_PKG = 4
	UNKNOWN = -1

	Mapping = TypeMapping([
		[HELLO, 'HELLO', pb.ControlType.HELLO],
		[QUERY_CLIENTS, 'QUERY_CLIENTS', pb.ControlType.QUERY_CLIENTS],
		[QUIT_SIGNAL, 'QUIT_SIGNAL', pb.ControlType.QUIT_SIGNAL],
		[PRINT, 'PRINT', pb.ControlType.PRINT],
	])

	def __init__(self):
		super().__init__(Transport.CONTROL)
		self.controlType = self.UNKNOWN
		self.body = b''
	
	def GetControlType(self) -> int:
		return self.controlType
	
	def InitHelloClientControl(self, data: HelloClientControl):
		self.controlType = Control.HELLO
		self.body = data.Pack().SerializeToString()

	def ToHelloClientControl(self):
		assert self.controlType == Control.HELLO, f'ToHelloClientControl() require controlType == {Control.HELLO} but got {self.controlType}'
		return HelloClientControl.Unpack(self.body)

	def InitHelloServerControl(self, data: HelloServerControl):
		self.controlType = Control.HELLO
		self.body = data.Pack().SerializeToString()

	def ToHelloServerControl(self):
		assert self.controlType == Control.HELLO, f'ToHelloServerControl() require controlType == {Control.HELLO} but got {self.controlType}'
		return HelloServerControl.Unpack(self.body)
	
	def InitQueryClientsControl(self, data: QueryClientsControl):
		self.controlType = Control.QUERY_CLIENTS
		self.body = data.Pack().SerializeToString()

	def ToQueryClientsControl(self):
		assert self.controlType == Control.QUERY_CLIENTS, f'ToQueryClientsControl() require controlType == {Control.QUERY_CLIENTS} but got {self.controlType}'
		return QueryClientsControl.Unpack(self.body)
	
	def InitPrintControl(self, msg: str):
		self.controlType = Control.PRINT
		self.body = msg.encode('utf-8')

	def DecodePrintMsg(self):
		assert self.controlType == Control.PRINT, f'DecodePrintMsg() require controlType == {Control.PRINT} but got {self.controlType}'
		return self.body.decode('utf-8')
	
	def InitRetrievePkg(self, pkg: PackageId):
		self.controlType = Control.RETRIEVE_PKG
		self.body = pkg.Pack().SerializeToString()

	def DecodeRetrievePkg(self):
		assert self.controlType == Control.RETRIEVE_PKG, f'DecodeRetrievePkg() require controlType == {Control.RETRIEVE_PKG} but got {self.controlType}'
		ret = pb.PackageId()
		ret.ParseFromString(self.body)
		return PackageId.Unpack(ret)
	
	def InitQuitSignal(self):
		self.controlType = Control.QUIT_SIGNAL
		
	def Pack(self) -> bytes:
		pbt = pb.Control()
		pbt.type = self.Mapping.ValueToPB(self.controlType)
		pbt.data = self.body

		self.data = pbt.SerializeToString()
		return super().Pack()
	
	def Unpack(self, transport: Transport):
		super().CopyFrom(transport)
		assert self.transportType == Transport.CONTROL, f'protocol.Control got package indicated as {Transport.Mappings.ValueToString(self.transportType)} which should be {Transport.Mappings.ValueToString(Transport.CONTROL)}'

		pbt = pb.Control()
		pbt.ParseFromString(self.data)
		self.controlType = pbt.type
		self.body = pbt.data


def Parse(data: bytes, encrypt: typing.Optional[encryptor.Cipher] = None) -> typing.Union[Request, Response, Subpackage, StreamData, Control]:
	raw = Transport.Parse(data)

	if raw.transportType == Transport.REQUEST:
		ret = Request(encrypt = encrypt)
	elif raw.transportType == Transport.RESPONSE:
		ret = Response(encrypt  = encrypt)
	elif raw.transportType == Transport.SUBPACKAGE:
		ret = Subpackage(encrypt= encrypt)
	elif raw.transportType == Transport.STREAM_DATA:
		ret = StreamData(encrypt  = encrypt)
	elif raw.transportType == Transport.CONTROL:
		ret = Control()
	else:
		raise Exception(f"unknown transport type {raw.transportType}: {Transport.Mappings.ValueToString(raw.transportType)}")
	ret.Unpack(raw)

	return ret
