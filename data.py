import json, typing
import protocol_pb2
from uuid import uuid4
import time_utils

class TransportDataType:
	CONTROL = 0
	REQUEST = 1
	RESPONSE = 2
	SUBPACKAGE = 3
	SSE_SUBPACKAGE = 4
	TCP_CONNECT = 5
	TCP_MESSAGE = 6

	@classmethod
	def ToString(cls, t: int):
		if t == cls.CONTROL:
			return 'CONTROL'
		elif t == cls.REQUEST:
			return 'REQUEST'
		elif t == cls.RESPONSE:
			return 'RESPONSE'
		elif t == cls.SUBPACKAGE:
			return 'SUBPACKAGE'
		elif t == cls.SSE_SUBPACKAGE:
			return 'SSE_SUBPACKAGE'
		elif t == cls.TCP_CONNECT:
			return 'TCP_COCNNECT'
		elif t == cls.TCP_MESSAGE:
			return 'TCP_MESSAGE'
		else:
			raise Exception(f'unknown data type: {t}')

class Transport:
	def __init__(self, data_type: TransportDataType, data: bytes, remote_id: int, client_id: int, seq_id: str = None):
		"""
		默认开启新的seq_id
		"""
		self.timestamp = time_utils.GetTimestamp()
		self.remote_id = remote_id
		self.client_id = client_id
		self.seq_id = str(uuid4()) if seq_id is None else seq_id
		self.data_type = data_type
		self.data = data
		self.cur_idx = 0
		self.total_cnt = 1

	def toTransportDataType(self):
		if self.data_type == TransportDataType.CONTROL:
			return protocol_pb2.TransportDataType.CONTROL
		elif self.data_type == TransportDataType.REQUEST:
			return protocol_pb2.TransportDataType.REQUEST
		elif self.data_type == TransportDataType.RESPONSE:
			return protocol_pb2.TransportDataType.RESPONSE
		elif self.data_type == TransportDataType.SUBPACKAGE:
			return protocol_pb2.TransportDataType.SUBPACKAGE
		elif self.data_type == TransportDataType.SSE_SUBPACKAGE:
			return protocol_pb2.TransportDataType.SSE_SUBPACKAGE
		elif self.data_type == TransportDataType.TCP_CONNECT:
			return protocol_pb2.TransportDataType.TCP_CONNECT
		elif self.data_type == TransportDataType.TCP_MESSAGE:
			return protocol_pb2.TransportDataType.TCP_MESSAGE
		else:
			raise Exception(f'Invalid data type: {self.data_type}')

	@classmethod
	def fromTransportDataType(cls, t):
		if t == protocol_pb2.TransportDataType.CONTROL:
			return TransportDataType.CONTROL
		elif t == protocol_pb2.TransportDataType.REQUEST:
			return TransportDataType.REQUEST
		elif t == protocol_pb2.TransportDataType.RESPONSE:
			return TransportDataType.RESPONSE
		elif t == protocol_pb2.TransportDataType.SUBPACKAGE:
			return TransportDataType.SUBPACKAGE
		elif t == protocol_pb2.TransportDataType.SSE_SUBPACKAGE:
			return TransportDataType.SSE_SUBPACKAGE
		elif t == protocol_pb2.TransportDataType.TCP_CONNECT:
			return TransportDataType.TCP_CONNECT
		elif t == protocol_pb2.TransportDataType.TCP_MESSAGE:
			return TransportDataType.TCP_MESSAGE
		else:
			raise Exception(f'Invalid data type: {t}')

	def ToProtobuf(self) -> bytes:
		pb = protocol_pb2.Transport()
		pb.timestamp = self.timestamp
		pb.remote_id = self.remote_id
		pb.client_id = self.client_id
		pb.seq_id = self.seq_id
		pb.cur_idx = self.cur_idx
		pb.total_cnt = self.total_cnt
		pb.data_type = self.toTransportDataType()
		pb.data = self.data
		return pb.SerializeToString()
	
	@classmethod
	def FromProtobuf(cls, buffer: bytes):
		pb = protocol_pb2.Transport()
		pb.ParseFromString(buffer)
		ret = Transport(cls.fromTransportDataType(pb.data_type), pb.data, pb.remote_id, pb.client_id)
		ret.timestamp = pb.timestamp
		ret.seq_id = pb.seq_id
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
	def __init__(self, url: str, status_code: int, headers: typing.Dict[str, str], body: bytes, sse_ticket = False):
		self.url = url
		self.status_code = status_code
		self.headers = headers
		self.body = body
		self.sse_ticket = sse_ticket

	def ToProtobuf(self) -> bytes:
		pb  = protocol_pb2.Response()
		pb.url = self.url
		pb.status = self.status_code
		pb.headers_json = json.dumps(self.headers)
		pb.body = self.body
		pb.sse_ticket = self.sse_ticket
		return pb.SerializeToString()
	
	@classmethod
	def FromProtobuf(cls, data: bytes):
		pb = protocol_pb2.Response()
		pb.ParseFromString(data)
		ret = Response(pb.url, pb.status, json.loads(pb.headers_json), pb.body)
		ret.sse_ticket = pb.sse_ticket
		return ret

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