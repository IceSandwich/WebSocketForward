import json, typing
import protocol_pb2

class SerializableRequest:
	def __init__(self, url: str, method: str, headers: typing.Dict[str, str], body: bytes = None):
		self.url = url
		self.method = method
		self.headers = headers
		self.body = body

	def __repr__(self):
		return f"SerializableRequest(url={self.url}, method={self.method}, headers={self.headers}), body={'None' if self.body is None else len(self.body)}"
	
	def to_protobuf(self):
		request = protocol_pb2.Request()
		request.url = self.url
		request.method = self.method
		request.headers_json = json.dumps(self.headers)
		if self.body is not None:
			request.body = self.body
		return request.SerializeToString()
	
	@staticmethod
	def from_protobuf(data: bytes):
		request = protocol_pb2.Request()
		request.ParseFromString(data)
		return SerializableRequest(request.url, request.method, json.loads(request.headers_json), request.body)
	
class SerializableResponse:
	def __init__(self, url: str, status_code: int, headers: typing.Dict[str, str], body: bytes, sse_ticket: bool = False, stream_end: bool = False):
		self.url = url
		self.status_code = status_code
		self.headers = headers
		self.body = body
		self.sse_ticket = sse_ticket
		self.stream_end = stream_end

	def __repr__(self):
		return f"SerializableResponse(url={self.url}, status_code={self.status_code}, headers={self.headers}, body={len(self.body)}, sse_ticket={self.sse_ticket}, stream_end={self.stream_end})"
	
	def to_protobuf(self):
		response  = protocol_pb2.Response()
		response.url = self.url
		response.status = self.status_code
		response.headers_json = json.dumps(self.headers)
		response.body = self.body
		response.sse_ticket = self.sse_ticket
		response.stream_end = self.stream_end
		return response.SerializeToString()
	
	@staticmethod
	def from_protobuf(data):
		response = protocol_pb2.Response()
		response.ParseFromString(data)
		return SerializableResponse(response.url, response.status, json.loads(response.headers_json), response.body, sse_ticket=response.sse_ticket, stream_end=response.stream_end)