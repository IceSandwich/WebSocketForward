import abc

class Cipher(abc.ABC):
	def __init__(self):
		pass

	@abc.abstractmethod
	def GetName(self) -> str:
		raise NotImplementedError()
	
	@abc.abstractmethod
	def Encrypt(self, data: bytes) -> bytes:
		raise NotImplementedError()
		
class XorCipher(Cipher):
	def __init__(self, key: str):
		super().__init__()
		self.key = key.encode(encoding='utf-8')

	def GetName(self) -> str:
		return "XorCipher"
		
	def Encrypt(self, data: bytes) -> bytes:
		# 使用异或操作加密或解密数据
		return bytes([data[i] ^ self.key[i % len(self.key)] for i in range(len(data))])
	
	def Decrypt(self, data: bytes) -> bytes:
		return self.Encrypt(data)

def NewCipher(method: str, key: str):
	method = method.lower()

	if method == 'xor':
		return XorCipher(key)

	raise ValueError(f'Invalid cipher method: {method}')

def GetAvailableCipherMethods():
	return ['xor']