import abc
import zlib

class Cipher(abc.ABC):
	def __init__(self):
		pass

	@abc.abstractmethod
	def GetName(self) -> str:
		raise NotImplementedError()
	
	@abc.abstractmethod
	def Encrypt(self, data: bytes) -> bytes:
		raise NotImplementedError()
	
	@abc.abstractmethod
	def Decrypt(self, data: bytes) -> bytes:
		raise NotImplementedError()
	
	def compress(self, compress_method: str, data: bytes) -> bytes:
		if compress_method == 'zlib':
			return zlib.compress(data)
		elif compress_method == 'none':
			return data
		raise Exception(f'compress] compress method {compress_method} not supported')
	
	def decompress(self, compress_method: str, data: bytes) -> bytes:
		if compress_method == 'zlib':
			return zlib.decompress(data)
		elif compress_method == 'none':
			return data
		raise Exception(f'decompress] compress method {compress_method} not supported')
	
class XorCipher(Cipher):
	def __init__(self, key: str, compress_method: str):
		super().__init__()
		self.key = key.encode(encoding='utf-8')
		self.compress_method = compress_method

	def GetName(self) -> str:
		return "XorCipher"
	
	def xor(self, data: bytes) -> bytes:
		# 使用异或操作加密或解密数据
		return bytes([data[i] ^ self.key[i % len(self.key)] for i in range(len(data))])
		
	def Encrypt(self, data: bytes) -> bytes:
		raw = self.xor(data)
		ret = self.compress(self.compress_method, raw)
		print(f"Compress from {len(raw)} to {len(ret)}, ratio: {(len(raw) - len(ret)) / len(raw) * 100} %")
		return ret
		# return self.compress(self.compress_method, self.xor(data))
	
	def Decrypt(self, data: bytes) -> bytes:
		return self.xor(self.decompress(self.compress_method, data))
	
class PlainCipher(Cipher):
	def __init__(self):
		super().__init__()

	def GetName(self) -> str:
		return "PlainCipher"
	
	def Encrypt(self, data: bytes) -> bytes:
		return data
	
	def Decrypt(self, data: bytes) -> bytes:
		return data
	
def NewCipher(method: str, key: str, compress_method: str = "none") -> Cipher:
	method = method.lower()

	if method == 'xor':
		return XorCipher(key, compress_method)
	elif method == 'plain':
		return PlainCipher()

	raise ValueError(f'Invalid cipher method: {method}')

def GetAvailableCipherMethods():
	return ['xor', 'plain']

def GetAvailableCompressMethods():
	return ['none', 'zlib']