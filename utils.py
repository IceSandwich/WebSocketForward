import logging, os, typing, collections, asyncio, sys, io
from datetime import datetime
from PIL import Image
import data
import uuid
import aiohttp.web as web
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
class Chunk:
	def __init__(self, total_cnt: int, start_idx: int):
		self.start_idx = start_idx
		self.count = total_cnt - start_idx
		self.total_cnt = total_cnt
		self.data: typing.List[bytes] = [None] * self.count
		self.cur_idx = 0
		self.template: data.Transport = None
		self.lock = asyncio.Lock()

	def IsFinish(self):
		return self.cur_idx >= self.count

	async def Put(self, raw: data.Transport):
		if self.IsFinish():
			raise Exception(f"already full(total_cnt={self.total_cnt}).")
		
		async with self.lock:
			self.data[raw.cur_idx - self.start_idx] = raw.body
			if raw.data_type != data.TransportDataType.SUBPACKAGE:
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

MIMETYPE_WEBP = 'image/webp'
CONTENTTYPE_UTF8HTML = 'text/html; charset=utf-8'

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
		print(f"Cannot compress image, unknown target mimetype: {target_mimetype}. Use WebP as default.")
		target_mimetype = MIMETYPE_WEBP

	image = DecodeImageFromBytes(raw)
	img_bytes = io.BytesIO()
	image.save(img_bytes, format=mimetypeToFormat[target_mimetype], quality=quality)
	return target_mimetype, img_bytes.getvalue()

def ComputeCompressRatio(source: int, target: int):
	return 1.0 - (target / source)

def NewSeqId():
	return str(uuid.uuid4())

def SetWSFCompress(req: typing.Union[data.Request, typing.Dict[str, str]],image_type: str = "image/webp", quality: int = 75):
	"""
	quality设置-1表示这是一个通知信息
	"""
	value = f'{image_type}' if quality == -1 else f'{image_type},{quality}'
	if type(req) == data.Request:
		req.headers['WSF-Compress'] = value
	else:
		req['WSF-Compress'] = value

def HasWSFCompress(req: data.Request):
	return 'WSF-Compress' in req.headers

def GetWSFCompress(req: data.Request):
	"""
	解析Compress头，返回MIMETYPE和图片质量。
	注意，调用前请使用HasWSFCompress()确保包含Compress头。
	"""
	compress = req.headers['WSF-Compress']
	sp = compress.split(',')
	assert(len(sp) == 2)
	return (sp[0], int(sp[1]))