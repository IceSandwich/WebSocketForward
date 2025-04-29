import logging, os, collections, asyncio, sys, io, uuid, datetime, heapq, abc
from PIL import Image
import aiohttp.web as web
import protocol, time_utils

import typing
T = typing.TypeVar('T')

import mimetypes
mimetypes.add_type("text/plain; charset=utf-8", ".woff2")

MIMETYPE_WEBP = 'image/webp'
CONTENTTYPE_UTF8HTML = 'text/html; charset=utf-8'

# 在日志中打印数据内容，这里设置打印前n个字节
LOG_BYTES_LEN = 20

log: logging.Logger = None

# 配置 logging
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def SetupLogging(log: logging.Logger, id: str, terminalLevel: int = logging.INFO, saveInFile: bool = True):
	formatter = logging.Formatter('%(asctime)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s')
	log.setLevel(logging.DEBUG)

	chandler = logging.StreamHandler(sys.stdout)
	chandler.setLevel(terminalLevel)
	chandler.setFormatter(formatter)
	log.addHandler(chandler)

	if saveInFile:
		log_dir = "logs"
		os.makedirs(log_dir, exist_ok=True)

		timestr = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
		log_file = os.path.join(log_dir, f"{id}-{timestr}.log")
		fhandler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
		fhandler.setFormatter(formatter)
		fhandler.setLevel(logging.DEBUG)
		log.addHandler(fhandler)

def GuessMimetype(filename: str):
	return mimetypes.guess_type(filename)[0] or "application/octet-stream"

class BoundedQueue(typing.Generic[T]):
	"""
	固定容量的Queue，当超过容量时。线程不安全，请自行加锁。
	"""
	def __init__(self, capacity: int):
		# 初始化容量和队列
		self.capacity = capacity
		self.queue: typing.Deque[T] = collections.deque()
	
	def Add(self, item: T):
		# 如果队列已满，删除最旧的元素
		if len(self.queue) >= self.capacity:
			self.queue.popleft()
		# 添加新元素到队列末尾
		self.queue.append(item)
	
	def __len__(self):
		# 获取当前队列的大小
		return len(self.queue)

	def IsEmpty(self):
		return len(self.queue) == 0

	def __iter__(self):
		return self.queue.__iter__()
	
	def Clear(self):
		self.queue.clear()

class Chunk:
	def __init__(self, total_cnt: int, start_idx: int):
		self.start_idx = start_idx
		self.count = total_cnt - start_idx
		self.total_cnt = total_cnt
		self.data: typing.List[bytes] = [None] * self.count
		self.cur_idx = 0
		self.template: typing.Union[protocol.Request, protocol.Response] = None
		self.lock = asyncio.Lock()

	def IsFinish(self):
		return self.cur_idx >= self.count

	async def Put(self, raw: typing.Union[protocol.Request, protocol.Response, protocol.Subpackage]):
		if self.IsFinish():
			raise Exception(f"Chunk] {raw.seq_id} already full(total_cnt={self.total_cnt}, cur_idx={self.cur_idx}).")
		
		async with self.lock:
			assert raw.body is not None, f"{protocol.Transport.Mappings.ValueToString(raw.transportType)} Package {raw.seq_id}:{raw.cur_idx}/{raw.total_cnt} the body is None which should not happend."
			self.data[raw.cur_idx - self.start_idx] = raw.body
			if raw.transportType != protocol.Transport.SUBPACKAGE:
				self.template = raw
			
			self.cur_idx = self.cur_idx + 1
	
	def Combine(self):
		raw = b''.join(self.data)
		self.template.body = raw
		self.template.cur_idx = 0
		self.template.total_cnt = 1
		return self.template

def DecodeImageFromBytes(raw: bytes):
	return Image.open(io.BytesIO(raw))


def ReponseText(msg: str, status_code: int):
	return web.Response(
		body=msg.encode('utf-8'),
		headers={
			'Content-Type': CONTENTTYPE_UTF8HTML
		},
		status=status_code
	)

def CompressImage(raw: bytes, target_mimetype: str, quality: int = 75):
	"""
	根据Mimetype压缩图片，如果不支持这个Mimetype，返回最终使用的Mimetype和字节流。
	"""
	mimetypeToFormat = {
		MIMETYPE_WEBP: 'WebP'
	}
	if target_mimetype not in mimetypeToFormat:
		if log is not None:
			log.error(f"Cannot compress image, unknown target mimetype: {target_mimetype}. Use WebP as default.")
		target_mimetype = MIMETYPE_WEBP

	image = DecodeImageFromBytes(raw)
	img_bytes = io.BytesIO()
	image.save(img_bytes, format=mimetypeToFormat[target_mimetype], quality=quality)
	return target_mimetype, img_bytes.getvalue()

def ComputeCompressRatio(source: int, target: int):
	return 1.0 - (target / source)

def NewId():
	return str(uuid.uuid4())

def SetWSFCompress(req: typing.Union[protocol.Request, typing.Dict[str, str]],image_type: str = "image/webp", quality: int = 75):
	"""
	quality设置-1表示这是一个通知信息
	"""
	value = f'{image_type}' if quality == -1 else f'{image_type},{quality}'
	if type(req) == protocol.Request:
		req.headers['WSF-Compress'] = value
	else:
		req['WSF-Compress'] = value

def HasWSFCompress(req: protocol.Request):
	return 'WSF-Compress' in req.headers

def GetWSFCompress(req: protocol.Request):
	"""
	解析Compress头，返回MIMETYPE和图片质量。
	注意，调用前请使用HasWSFCompress()确保包含Compress头。
	"""
	compress = req.headers['WSF-Compress']
	sp = compress.split(',')
	assert(len(sp) == 2)
	return (sp[0], int(sp[1]))

class TimerTask(abc.ABC):
	def __init__(self, time: float):
		self.timestamp = time_utils.GetTimestamp()
		self.time = time

	def __le__(self, other):
		return self.time < other.time
	
	def GetTimestamp(self):
		return self.timestamp
	
	@abc.abstractmethod
	async def Run(self):
		raise NotImplementedError()
	
class Timer:
	"""
	Not an accurate timer.
	"""
	def __init__(self):
		self.tasks: typing.List[TimerTask] = []
		self.condition = asyncio.Condition()
		self.runningSignal = False

	def Start(self):
		"""
		清空任务并启动协程
		"""
		self.runningSignal = True
		self.tasks = []
		self.instance = asyncio.ensure_future(self.mainloop())
	
	async def Stop(self):
		self.runningSignal = False
		async with self.condition:
			self.condition.notify_all()
		await self.instance

	async def AddTask(self, task: TimerTask):
		if self.runningSignal == False:
			if log is not None:
				log.error(f"Warning: Timer doesn't start at this moment but AddTask() has been called.")

		async with self.condition:
			heapq.heappush(self.tasks, task)
			self.condition.notify_all()

	async def mainloop(self):
		while True:
			async with self.condition:
				await self.condition.wait_for(lambda: len(self.tasks) > 0 or self.runningSignal == False)
				if self.runningSignal == False:
					return
				item = heapq.heappop(self.tasks)
			if item.time <= 0:
				continue
			await asyncio.sleep(item.time)
			if log is not None:
				log.debug(f"Timer] After {item.time}, invoke run()")
			await item.Run()
			async with self.condition: # this will change the new packages added during sleep time. So utils.Timer is not a accurate timer.
				for task in self.tasks:
					task.time -= item.time