import logging, os
from datetime import datetime


# 配置 logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def SetupLogging(log: logging.Logger, id: str):
	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

	# chandler = logging.StreamHandler()
	# chandler.setLevel(logging.INFO)
	# chandler.setFormatter(formatter)
	# log.addHandler(chandler)

	log_dir = "logs"
	os.makedirs(log_dir, exist_ok=True)

	timestr = datetime.now().strftime("%Y%m%d_%H%M%S")
	log_file = os.path.join(log_dir, f"{id}-{timestr}.log")
	fhandler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
	fhandler.setFormatter(formatter)
	log.addHandler(fhandler)