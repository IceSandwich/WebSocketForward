import protocol_pb2 as pb
import uuid
import abc
import typing

def GenerateSequenceId():
	return str(uuid.uuid4())

class PackageId:
	def __init__(self, cur_idx: int = 0, total_idx = 1, seq_id = None):
		self.seq_id = GenerateSequenceId() if seq_id is None else seq_id
		self.cur_idx = cur_idx
		self.total_idx = total_idx

	def ToProtobuf(self):
		ret = pb.PackageId()
		ret.seq_id = self.seq_id
		ret.cur_idx = self.cur_idx
		ret.total_idx = self.total_idx
		return ret
	
	@classmethod
	def FromProtobuf(cls, pkg):
		ret = PackageId()
		ret.seq_id = pkg.seq_id
		ret.cur_idx = pkg.cur_idx
		ret.total_idx = pkg.total_idx
		return ret
	
class ClientInfo:
	CLIENT = 0
	REMOTE = 1

	def __init__(self, clientId: str, clientType: int):
		self.id = clientId
		self.clientType = clientType

	def ToProtobuf(self):
		ret = pb.ClientInfo()
		ret.id = self.id
		ret.type = self.clientType
		return ret
	
	@classmethod
	def FromProtobuf(cls, pkg):
		ret = ClientInfo(pkg.id, pkg.type)
		return ret
	
class Control:
	HELLO = 0
	QUERY_CLIENTS = 1
	QUIT_SIGNAL = 2

	def __init__(self, controlType: int):
		self.controlType = controlType

	@abc.abstractmethod
	def ToData(self) -> bytes:
		raise NotImplementedError()
	
class HelloClientControl(Control):
	def __init__(self, client: ClientInfo, packages: typing.List[PackageId]):
		super().__init__(Control.HELLO)
		self.client = client
		self.packages = packages

	def ToData(self) -> bytes:
		ret = pb.HelloClientControl()
		ret.pkgs = [p.ToProtobuf() for p in self.packages]

		container = pb.Control()
		container.type = Control.HELLO
		container.data = ret.SerializeToString()
		return container.SerializeToString()

	@classmethod
	def FromData(cls, data):

class Transport:
	CONTROL = 0
	REQUEST = 1
	RESPONSE = 2
	SUBPACKAGE = 3
	STREAM_DATA = 4
	UNKNOWN = -1

	def __init__(self, sender: str, receiver: str):
		self.type = self.UNKNOWN
		self.sender = sender
		self.receiver = receiver
		self.data = b''
		self.packageId = PackageId()

	@classmethod
	def NewResponse(cls, lastTransport):
		ret = Transport(lastTransport.receiver, lastTransport.sender)
		ret.packageId.seq_id = lastTransport.packageId.seq_id
		return ret

	def SetControl(self, control: Control):
		self.data = control.ToData()

	