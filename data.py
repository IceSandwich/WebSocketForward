import json
import protocol_pb2

class SerializableRequest:
	def __init__(self, url, method, headers):
		self.url = url
		self.method = method
		self.headers = headers

	def __repr__(self):
		return f"SerializableRequest(url={self.url}, method={self.method}, headers={self.headers})"

	def to_dict(self):
		return {
			'url': self.url,
			'method': self.method,
			'headers': self.headers
		}
	
	def to_json(self):
		return json.dumps(self.to_dict())
	
	def to_protobuf(self):
		request = protocol_pb2.Request()
		request.url = self.url
		request.method = self.method
		request.headers_json = json.dumps(self.headers)
		return request.SerializeToString()

	@staticmethod
	def from_dict(data):
		return SerializableRequest(data['url'], data['method'], data['headers'])
	
	@staticmethod
	def from_json(jsonString):
		data = json.loads(jsonString)
		return SerializableRequest(data['url'], data['method'], data['headers'])
	
	@staticmethod
	def from_protobuf(data):
		request = protocol_pb2.Request()
		request.ParseFromString(data)
		return SerializableRequest(request.url, request.method, json.loads(request.headers_json))
	
class SerializableResponse:
	def __init__(self, status_code, headers, body):
		self.status_code = status_code
		self.headers = headers
		self.body = body

	def __repr__(self):
		return f"SerializableResponse(status_code={self.status_code}, headers={self.headers}, body={self.body})"
	
	def to_dict(self):
		return {
			'statusCode': self.status_code,
			'headers': self.headers,
			'body': self.body
		}
	
	def to_json(self):
		return json.dumps(self.to_dict())
	
	def to_protobuf(self):
		response  = protocol_pb2.Response()
		response.status = self.status_code
		response.headers_json = json.dumps(self.headers)
		response.body = self.body
		return response.SerializeToString()
	
	@staticmethod
	def from_dict(data):
		return SerializableResponse(data['statusCode'], data['headers'], data['body'])
	
	@staticmethod
	def from_json(jsonString):
		ret = json.loads(jsonString)
		return SerializableResponse(ret['statusCode'], ret['headers'], ret['body'])
	
	@staticmethod
	def from_protobuf(data):
		response = protocol_pb2.Response()
		response.ParseFromString(data)
		return SerializableResponse(response.status, json.loads(response.headers_json), response.body)