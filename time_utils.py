import time

def GetTimestamp():
	return int(time.time())

def Seconds(seconds: int):
	return float(seconds)

def Minutes(minutes: int):
	return float(minutes * 60)

def WithInDuration(oldTime: int, newTime: int, duration: int):
	if duration == -1:
		return True
	return newTime - oldTime < duration