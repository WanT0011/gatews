# !/usr/bin/env python
# coding: utf-8
import abc
import asyncio
import hashlib
import hmac
import json
import logging
import time
import typing

import websockets

logger = logging.getLogger(__name__)


class GateWebsocketError(Exception):

    def __init__(self, code, message):
        self.code = code
        self.message = message

    def __str__(self):
        return 'code: %d, message: %s' % (self.code, self.message)


class Configuration(object):

    def __init__(self,
                 app: str = 'spot',
                 settle: str = 'usdt',
                 test_net: bool = False,
                 host: str = None,
                 api_key: str = '',
                 api_secret: str = '',
                 executor_pool=None,
                 default_callback=None,
                 ping_interval: int = 5,
                 max_retry: int = 10):
        self.app = app
        self.api_key = api_key
        self.api_secret = api_secret
        default_host = 'wss://api.gateio.ws/ws/v4/'
        if app == 'futures':
            default_host = 'wss://fx-ws.gateio.ws/v4/ws/%s' % settle
            if test_net:
                default_host = 'wss://fx-ws-testnet.gateio.ws/v4/ws/%s' % settle
        self.host = host or default_host
        self.pool = executor_pool
        self.default_callback = default_callback
        self.ping_interval = ping_interval
        self.max_retry = max_retry


class WebSocketResponse(object):

    def __init__(self, body: str):
        self.body = body
        msg = json.loads(body)
        self.channel = msg.get('channel')
        if not self.channel:
            raise ValueError("no channel found from response message")

        self.timestamp = msg.get('time')
        self.event = msg.get('event')
        self.result = msg.get('result')
        self.error = None
        if msg.get('error'):
            self.error = GateWebsocketError(msg['error'].get('code'), msg['error'].get('message'))


Callback = typing.NewType("WebSocketCallback",
                          typing.Union[typing.Callable[[WebSocketResponse], None],
                                       typing.Awaitable])


class Connection(object):

    def __init__(self, cfg: Configuration):
        self.cfg = cfg
        self.channels: typing.Dict[str, Callback] = dict()
        self.sending_queue = asyncio.Queue()
        self.sending_history = list()
        self.main_loop = None

    def register(self, channel, callback: Callback = None):
        if callback:
            self.channels[channel] = callback

    def unregister(self, channel):
        self.channels.pop(channel, None)

    def send(self, msg):
        self.sending_queue.put_nowait(msg)

    async def _active_ping(self, conn: websockets.WebSocketClientProtocol):
        while True:
            data = json.dumps({'time': int(time.time()), 'channel': '%s.ping' % self.cfg.app})
            await conn.send(data)
            await asyncio.sleep(self.cfg.ping_interval)

    async def _write(self, conn: websockets.WebSocketClientProtocol):
        if self.sending_history:
            for msg in self.sending_history:
                await conn.send(msg)
        while True:
            msg = await self.sending_queue.get()
            self.sending_history.append(msg)
            await conn.send(msg)

    async def _read(self, conn: websockets.WebSocketClientProtocol):
        async for msg in conn:
            response = WebSocketResponse(msg)
            callback = self.channels.get(response.channel, self.cfg.default_callback)
            if callback is not None:
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback(self, response))
                else:
                    asyncio.get_event_loop().run_in_executor(self.cfg.pool, callback, self, response)

    def close(self):
        if self.main_loop:
            self.main_loop.cancel()

    async def run(self):
        stopped = False
        retried = 0
        while not stopped:
            try:
                conn = await websockets.connect(self.cfg.host)
                if retried > 0:
                    logger.warning("reconnect succeeded after retrying %d times", retried + 1)
                    retried = 0
            # DNS might be resolved to multiple address, which cause multiple ConnectionRefusedError
            # being combined to one OSError
            except (ConnectionRefusedError, OSError):
                logger.warning("failed to connect to server for the %d time, try again later", retried + 1)
                retried += 1
                if 0 < self.cfg.max_retry < retried:
                    logger.error("max reconnect time %d reached, give it up", self.cfg.max_retry)
                    stopped = True
                    continue
                await asyncio.sleep(0.5 * retried)
            else:
                tasks: typing.List[asyncio.Task] = list()
                try:
                    tasks.append(asyncio.create_task(self._write(conn), name='write'))
                    tasks.append(asyncio.create_task(self._read(conn), name='read'))
                    tasks.append(asyncio.create_task(self._active_ping(conn), name='ping'))
                    self.main_loop = asyncio.gather(*tasks)
                    await self.main_loop
                except websockets.ConnectionClosed:
                    logger.warning("websocket connection lost, retry to reconnect")
                except asyncio.CancelledError:
                    await conn.close()
                    stopped = True
                finally:
                    # user callback tasks are not our concern
                    for task in tasks:
                        task.cancel()


class BaseChannel(abc.ABC):
    name = ''
    require_auth = False

    def __init__(self, conn: Connection, callback: Callback = None):
        self.conn = conn
        self.callback = callback
        self.cfg = self.conn.cfg
        self.conn.register(self.name, callback)

    def _request(self, event, payload):
        request = {
            'time': int(time.time()),
            'channel': self.name,
            'event': event,
            'payload': payload,
        }
        if self.require_auth:
            if not (self.cfg.api_key and self.cfg.api_secret):
                raise ValueError("configuration does not provide api key or secret")
            message = "channel=%s&event=%s&time=%d" % (self.name, event, request['time'])
            request['auth'] = {
                "method": "api_key",
                "KEY": self.cfg.api_key,
                "SIGN": hmac.new(self.cfg.api_secret.encode("utf8"), message.encode("utf8"),
                                 hashlib.sha512).hexdigest()
            }
        self.conn.send(json.dumps(request))

    def subscribe(self, payload):
        self._request('subscribe', payload)

    def unsubscribe(self, payload):
        self._request('unsubscribe', payload)