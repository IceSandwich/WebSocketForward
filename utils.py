import aiohttp
import typing
import requests

class ProxyRequest:
	def __init__(self):
		# self.session = aiohttp.ClientSession()
		pass

	def Do(self, method: str, url: str, headers: typing.Dict[str, str], body: bytes = None):
		# 根据请求方法转发 HTTP 请求
		if method.upper() == "GET":
			response = requests.get(url, headers=headers, stream=True)
		elif method.upper() == "POST":
			response = requests.post(url, headers=headers, data=body, stream=True)
		elif method.upper() == "PUT":
			response = requests.put(url, headers=headers, data=body, stream=True)
		elif method.upper() == "DELETE":
			response = requests.delete(url, headers=headers, stream=True)
		else:
			raise ValueError(f"Unsupported HTTP method: {method}")
		
		return response
