import json, typing
import protocol_pb2
from uuid import uuid4
import time_utils

class TransportDataType:
	CONTROL = 0
	REQUEST = 1
	RESPONSE = 2
	SUBPACKAGE = 3
	STREAM_SUBPACKAGE = 4 # 传输SSE、TCP包
	TCP_CONNECT = 5
	# TCP_MESSAGE = 6

	Mappings: typing.List[typing.Tuple[int, str, typing.Any]] = [
		[CONTROL, 'CONTROL', protocol_pb2.TransportDataType.CONTROL],
		[REQUEST, 'REQUEST', protocol_pb2.TransportDataType.REQUEST],
		[RESPONSE, 'RESPONSE', protocol_pb2.TransportDataType.RESPONSE],
		[SUBPACKAGE, 'SUBPACKAGE', protocol_pb2.TransportDataType.SUBPACKAGE],
		[STREAM_SUBPACKAGE, 'STREAM_SUBPACKAGE', protocol_pb2.TransportDataType.STREAM_SUBPACKAGE],
		[TCP_CONNECT, 'TCP_CONNECT', protocol_pb2.TransportDataType.TCP_CONNECT],
		# [TCP_MESSAGE, 'TCP_MESSAGE', protocol_pb2.TransportDataType.TCP_MESSAGE]
	]

	TypeToPB = { x[0] : x[2] for x in Mappings }
	TypeToName = { x[0]: x[1] for x in Mappings }
	PBToType = { x[2] : x[0] for x in Mappings }

	@classmethod
	def ToString(cls, t: int):
		if t in cls.PBToType:
			return cls.TypeToName[cls.PBToType[t]]
		else:
			raise Exception(f'unknown data type: {t}')
		
	@classmethod
	def ToPB(cls, t: int):
		if t in cls.TypeToPB:
			return cls.TypeToPB[t]
		else:
			raise Exception(f'unknown data type: {t}')

	@classmethod
	def FromPB(cls, t: typing.Any):
		if t in cls.PBToType:
			return cls.PBToType[t]
		else:
			raise Exception(f'unknown data type: {t}')

class Transport:
	END_PACKAGE = -1
	SINGLE_PACKAGE = 1

	def __init__(self, data_type: TransportDataType, data: bytes, remote_id: int, client_id: int, seq_id: str = None):
		"""
		默认开启新的seq_id
		"""
		self.timestamp = time_utils.GetTimestamp()
		self.remote_id = remote_id
		self.client_id = client_id
		self.seq_id = str(uuid4()) if seq_id is None else seq_id
		self.cur_idx = 0
		self.total_cnt = 1
		self.data_type = data_type
		self.data = data
	
	def IsStreamPackage(self):
		return self.data_type == TransportDataType.STREAM_SUBPACKAGE
	
	def IsSinglePackage(self):
		assert(self.cur_idx == 0)
		return self.total_cnt == self.SINGLE_PACKAGE
	
	def IsEndPackage(self):
		return self.total_cnt == self.END_PACKAGE
	
	def MarkAsSinglePackage(self):
		self.cur_idx = 0
		self.total_cnt = self.SINGLE_PACKAGE

	def SetPackages(self, total: int, cur: int = 1):
		self.total_cnt = total
		self.cur_idx = cur

	def ResetData(self, data: bytes, data_type: TransportDataType = None):
		self.data = data
		if data_type is not None:
			self.data_type = data_type

	def CloneWithSameSeqID(self):
		"""
		保留seq_id和data_type，更新时间戳
		"""
		ret = Transport(self.data_type, None, self.remote_id, self.client_id, seq_id=self.seq_id)
		return ret

	def ToProtobuf(self) -> bytes:
		pb = protocol_pb2.Transport()
		pb.timestamp = self.timestamp
		pb.remote_id = self.remote_id
		pb.client_id = self.client_id
		pb.seq_id = self.seq_id
		pb.cur_idx = self.cur_idx
		pb.total_cnt = self.total_cnt
		pb.data_type = TransportDataType.ToPB(self.data_type)
		pb.data = self.data
		return pb.SerializeToString()
	
	@classmethod
	def FromProtobuf(cls, buffer: bytes):
		pb = protocol_pb2.Transport()
		pb.ParseFromString(buffer)
		ret = Transport(TransportDataType.FromPB(pb.data_type), pb.data, pb.remote_id, pb.client_id, seq_id=pb.seq_id)
		ret.timestamp = pb.timestamp
		ret.cur_idx = pb.cur_idx
		ret.total_cnt = pb.total_cnt
		return ret
	
	def RenewTimestamp(self):
		self.timestamp = time_utils.GetTimestamp()

	def __lt__(self, other):
		return self.cur_idx < other.cur_idx

class Request:
	def __init__(self, url: str, method: str, headers: typing.Dict[str, str], body: bytes = None):
		self.url = url
		self.method = method
		self.headers = headers
		self.body = body

	def __repr__(self):
		return f"SerializableRequest(url={self.url}, method={self.method}, headers={self.headers}), body={'None' if self.body is None else len(self.body)}"
	
	def ToProtobuf(self) -> bytes:
		pb = protocol_pb2.Request()
		pb.url = self.url
		pb.method = self.method
		pb.headers_json = json.dumps(self.headers)
		if self.body is not None:
			pb.body = self.body
		return pb.SerializeToString()
	
	@classmethod
	def FromProtobuf(cls, buffers: bytes):
		pb = protocol_pb2.Request()
		pb.ParseFromString(buffers)
		ret = Request(pb.url, pb.method, json.loads(pb.headers_json), pb.body)
		return ret

class Response:
	def __init__(self, url: str, status_code: int, headers: typing.Dict[str, str], body: bytes):
		self.url = url
		self.status_code = status_code
		self.headers = headers
		self.body = body

	def ToProtobuf(self) -> bytes:
		pb  = protocol_pb2.Response()
		pb.url = self.url
		pb.status = self.status_code
		pb.headers_json = json.dumps(self.headers)
		pb.body = self.body
		return pb.SerializeToString()
	
	@classmethod
	def FromProtobuf(cls, data: bytes):
		pb = protocol_pb2.Response()
		pb.ParseFromString(data)
		ret = Response(pb.url, pb.status, json.loads(pb.headers_json), pb.body)
		return ret
	
	def IsSSEResponse(self):
		return 'Content-Type' in self.headers and 'text/event-stream' in self.headers['Content-Type']

class TCPConnect:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        
    def ToProtobuf(self) -> bytes:
        pb = protocol_pb2.TCPConnect()
        pb.host = self.host
        pb.port = self.port
        return pb.SerializeToString()
    
    @classmethod
    def FromProtobuf(cls, data: bytes):
        pb = protocol_pb2.TCPConnect()
        pb.ParseFromString(data)
        ret = TCPConnect(pb.host, pb.port)
        return ret