import typing, requests, logging, sys, os
from datetime import datetime

WS_MAX_MSG_SIZE = 12 * 1024 * 1024

# 配置 logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def SetupLogging(log: logging.Logger, id: str):
	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

	chandler = logging.StreamHandler()
	chandler.setLevel(logging.INFO)
	chandler.setFormatter(formatter)
	log.addHandler(chandler)

	log_dir = "logs"
	os.makedirs(log_dir, exist_ok=True)

	timestr = datetime.now().strftime("%Y%m%d_%H%M%S")
	log_file = os.path.join(log_dir, f"{id}-{timestr}.log")
	fhandler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
	fhandler.setFormatter(formatter)
	log.addHandler(fhandler)

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
