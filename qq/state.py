import asyncio
import inspect
import logging
import os
from collections import deque
from typing import Callable, TYPE_CHECKING, Dict, Any, Optional, List, Union, Deque, Tuple

from qq import Client, utils, Guild
from qq.gateway import QQWebSocket
from qq.http import HTTPClient


class ChunkRequest:
    def __init__(
        self,
        guild_id: int,
        loop: asyncio.AbstractEventLoop,
        resolver: Callable[[int], Any],
        *,
        cache: bool = True,
    ) -> None:
        self.guild_id: int = guild_id
        self.resolver: Callable[[int], Any] = resolver
        self.loop: asyncio.AbstractEventLoop = loop
        self.cache: bool = cache
        self.nonce: str = os.urandom(16).hex()
        self.buffer: List[Member] = []
        self.waiters: List[asyncio.Future[List[Member]]] = []

    def add_members(self, members: List[Member]) -> None:
        self.buffer.extend(members)
        if self.cache:
            guild = self.resolver(self.guild_id)
            if guild is None:
                return

            for member in members:
                existing = guild.get_member(member.id)
                if existing is None or existing.joined_at is None:
                    guild._add_member(member)

    async def wait(self) -> List[Member]:
        future = self.loop.create_future()
        self.waiters.append(future)
        try:
            return await future
        finally:
            self.waiters.remove(future)

    def get_future(self) -> asyncio.Future[List[Member]]:
        future = self.loop.create_future()
        self.waiters.append(future)
        return future

    def done(self) -> None:
        for future in self.waiters:
            if not future.done():
                future.set_result(self.buffer)


_log = logging.getLogger(__name__)


class ConnectionState:
    if TYPE_CHECKING:
        _get_websocket: Callable[..., QQWebSocket]
        _get_client: Callable[..., Client]
        _parsers: Dict[str, Callable[[Dict[str, Any]], None]]

    def __init__(
        self,
        *,
        dispatch: Callable,
        handlers: Dict[str, Callable],
        hooks: Dict[str, Callable],
        http: HTTPClient,
        loop: asyncio.AbstractEventLoop,
        **options: Any,
    ) -> None:
        self.loop: asyncio.AbstractEventLoop = loop
        self.http: HTTPClient = http
        self.max_messages: Optional[int] = options.get('max_messages', 1000)
        if self.max_messages is not None and self.max_messages <= 0:
            self.max_messages = 1000

        self.dispatch: Callable = dispatch
        self.handlers: Dict[str, Callable] = handlers
        self.hooks: Dict[str, Callable] = hooks
        self.shard_count: Optional[int] = None
        self._ready_task: Optional[asyncio.Task] = None
        self.application_id: Optional[int] = options.get('application_id')
        self.heartbeat_timeout: float = options.get('heartbeat_timeout', 60.0)
        self.guild_ready_timeout: float = options.get('guild_ready_timeout', 2.0)
        if self.guild_ready_timeout < 0:
            raise ValueError('guild_ready_timeout cannot be negative')

        self._chunk_requests: Dict[Union[int, str], ChunkRequest] = {}

        self.parsers = parsers = {}
        for attr, func in inspect.getmembers(self):
            if attr.startswith('parse_'):
                parsers[attr[6:].upper()] = func

        self.clear()
