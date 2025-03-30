import aiohttp
import wsutils
import typing
import abc
import asyncio

class Tunnel(abc.ABC):
    @abc.abstractmethod
    def SendBytes(self, data: bytes):
        raise Exception("unimplement")
    
class Callbacks:
	def __init__(self, name: str = "Websocket"):
		self.name = name
		self.handler: typing.Union[Tunnel, None] = None

	def SetHandler(self, handler: Tunnel):
		self.handler = handler
		
	def OnConnected(self):
		print(f"{self.name} connected.")
		
	def OnDisconnected(self):
		print(f"{self.name} disconnected.")
		
	def OnError(self, ex: typing.Union[None, BaseException]):
		print(f"{self.name} error: {ex}")
		
	async def OnProcess(self, data: bytes):
		pass

    
class WebSocketTunnelServer(Tunnel):
    def __init__(self):
        pass
    
class WebSocketTunnelClient(Tunnel):
    Headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
    
    def __init__(self, url:str, callbacks: Callbacks, maxTries = 3):
        self.url = url
        self.callbacks = callbacks
        self.handler = typing.Union[None, aiohttp.ClientWebSocketResponse] = None
        self.maxTries = maxTries
        self.send_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.isConnected = False
        self.session = aiohttp.ClientSession()
        
    async def resend(self):
        while self.isConnected and not self.send_queue.empty():
            item = await self.send_queue.get()
            try:
                await self.handler.send_bytes(item)
            except aiohttp.ClientConnectionResetError:
                self.send_queue.put(item)
        
    async def connect(self):
        try:
            self.handler: aiohttp.ClientWebSocketResponse = self.session.ws_connect(self.url, headers=WebSocketTunnelClient.Headers, max_msg_size=wsutils.WS_MAX_MSG_SIZE)
            self.isConnected = True
            self.callbacks.OnConnected()
            if not self.send_queue.empty():
                print(f"Resend {len(self.send_queue)} requests.")
                asyncio.create_task(self.resend())
            async for msg in self.handler:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    self.callbacks.OnError(self.ws.exception())
                elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
                    await self.callbacks.OnProcess(msg)
        except:
            return False
        finally:
            self.isConnected = False
            self.callbacks.OnDisconnected()
        return True
        
    async def SendBytes(self, data: bytes):
        if self.isConnected:
            try:
                await self.handler.send_bytes(data)
            except aiohttp.ClientConnectionResetError:
                # send failed, caching data
                self.send_queue.put(data)
        else:
            # not connected, cacheing data
            self.send_queue.put(data)
        
    async def MainLoop(self):
        self.callbacks.SetHandler(self)
        curTries = 1
        while curTries < self.maxTries:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(self.url, headers=WebSocketTunnelClient.Headers, max_msg_size=wsutils.WS_MAX_MSG_SIZE) as ws:
                    self.handler = ws
                    self.callbacks.OnConnected()
                    try:
                        async for msg in self.handler:
                            if msg.type == aiohttp.WSMsgType.ERROR:
                                self.callbacks.OnError(self.ws.exception())
                            elif msg.type == aiohttp.WSMsgType.TEXT or msg.type == aiohttp.WSMsgType.BINARY:
                                await self.callbacks.OnProcess(msg)
                    except KeyboardInterrupt:
                        print("Closing...")
                        break
                    finally:
                        self.callbacks.OnDisconnected()
                    curTries += 1
                    print(f"Reconnecting...")
        return self.handler