import logging, os, typing, collections, asyncio, sys
from datetime import datetime
T = typing.TypeVar('T')

# 配置 logging
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def SetupLogging(log: logging.Logger, id: str, terminalLevel: int = logging.INFO, saveInFile: bool = True):
	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
	log.setLevel(logging.DEBUG)

	chandler = logging.StreamHandler(sys.stdout)
	chandler.setLevel(terminalLevel)
	chandler.setFormatter(formatter)
	log.addHandler(chandler)

	if saveInFile:
		log_dir = "logs"
		os.makedirs(log_dir, exist_ok=True)

		timestr = datetime.now().strftime("%Y%m%d_%H%M%S")
		log_file = os.path.join(log_dir, f"{id}-{timestr}.log")
		fhandler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
		fhandler.setFormatter(formatter)
		fhandler.setLevel(logging.DEBUG)
		log.addHandler(fhandler)

class BoundedQueue(typing.Generic[T]):
	"""
	固定容量的Queue，当超过容量时
	"""
	def __init__(self, capacity: int):
		# 初始化容量和队列
		self.capacity = capacity
		self.queue: typing.Deque[T] = collections.deque()
		self.lock = asyncio.Lock()
	
	async def Add(self, item: T):
		async with self.lock:
			# 如果队列已满，删除最旧的元素
			if len(self.queue) >= self.capacity:
				self.queue.popleft()
			# 添加新元素到队列末尾
			self.queue.append(item)
	
	async def PopAllElements(self) -> typing.List[T]:
		async with self.lock:
			# 获取队列中的所有元素
			ret = list(self.queue)
			self.queue.clear()
			return ret
	
	def GetSize(self):
		# 获取当前队列的大小
		return len(self.queue)

	def IsEmpty(self):
		return len(self.queue) == 0
