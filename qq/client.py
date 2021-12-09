from __future__ import annotations

__all__ = ('Client',)

import asyncio
import datetime
import logging
import signal
import sys
import traceback
from typing import Optional, Any, Dict, Callable, List, Tuple, Coroutine, TypeVar

import aiohttp

from .backoff import ExponentialBackoff
from .error import HTTPException, GatewayNotFound, ConnectionClosed
from .state import ConnectionState
from .gateway import QQWebSocket, ReconnectWebSocket
from .guild import Guild
from .http import HTTPClient
from .iterators import GuildIterator
from .user import ClientUser, User

URL = r'https://api.sgroup.qq.com'
_log = logging.getLogger(__name__)
Coro = TypeVar('Coro', bound=Callable[..., Coroutine[Any, Any, Any]])


def _cancel_tasks(loop: asyncio.AbstractEventLoop) -> None:
    tasks = {t for t in asyncio.all_tasks(loop=loop) if not t.done()}

    if not tasks:
        return

    _log.info('在 %d 个任务后清理。', len(tasks))
    for task in tasks:
        task.cancel()

    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    _log.info('所有任务都取消了。')

    for task in tasks:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler({
                'message': 'Client.run 关闭期间未处理的异常。',
                'exception': task.exception(),
                'task': task
            })


def _cleanup_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        _cancel_tasks(loop)
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        _log.info('关闭事件循环。')
        loop.close()


class Client:
    r"""代表了客户端与 QQ 之间的连接
    此类用于与 QQ WebSocket 和 API 进行交互。
    许多选项可以传递给:class:`Client`。

    Parameters
    -----------
    max_messages: Optional[:class:`int`]
        要存储在内部消息缓存中的最大消息数。
        这默认为 ``1000`` 。传入 ``None`` 会禁用消息缓存。
   loop: Optional[:class:`asyncio.AbstractEventLoop`]
        用于异步操作的 :class:`asyncio.AbstractEventLoop` 。
        默认为 ``None`` ，在这种情况下，默认事件循环通过 :func:`asyncio.get_event_loop()` 使用。
    connector: Optional[:class:`aiohttp.BaseConnector`]
        用于连接池的连接器。
    proxy: Optional[:class:`str`]
        代理网址。
    proxy_auth: Optional[:class:`aiohttp.BasicAuth`]
        代表代理 HTTP 基本授权的对象。
    shard_id: Optional[:class:`int`]
        从 0 开始并且小于 :attr:`.shard_count` 的整数。
    shard_count: Optional[:class:`int`]
        分片总数。
    app_id: :class:`int`
        客户端的 App ID。
    intents: :class:`Intents`
        您要为会话启用的意图。 这是一种禁用和启用某些网关事件触发和发送的方法。
        如果未给出，则默认为默认的 Intents 类。
    heartbeat_timeout: :class:`float`
        在未收到 HEARTBEAT_ACK 的情况下超时和重新启动 WebSocket 之前的最大秒数。
        如果处理初始数据包花费的时间太长而导致您断开连接，则很有用。默认超时为 59 秒。
    guild_ready_timeout: :class:`float`
        在准备成员缓存和触发 READY 之前等待 GUILD_CREATE 流结束的最大秒数。默认超时为 2 秒。

    Attributes
    -----------
    ws
        客户端当前连接到的 websocket 网关。可能是 ``None`` 。
    loop: :class:`asyncio.AbstractEventLoop`
        客户端用于异步操作的事件循环。
    """

    def __init__(
            self,
            *,
            loop: Optional[asyncio.AbstractEventLoop] = None,
            **options: Any,
    ):
        self.ws: QQWebSocket = None  # type: ignore
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop() if loop is None else loop
        self._listeners: Dict[str, List[Tuple[asyncio.Future, Callable[..., bool]]]] = {}
        self.token = f"{options.pop('app_id', None)}.{options.pop('token', None)}"
        self.shard_id: Optional[int] = options.get('shard_id')
        self.shard_count: Optional[int] = options.get('shard_count')
        self._enable_debug_events: bool = options.pop('enable_debug_events', False)

        self._handlers: Dict[str, Callable] = {
            'ready': self._handle_ready
        }

        self._hooks: Dict[str, Callable] = {
            'before_identify': self._call_before_identify_hook
        }

        connector: Optional[aiohttp.BaseConnector] = options.pop('connector', None)
        proxy: Optional[str] = options.pop('proxy', None)
        proxy_auth: Optional[aiohttp.BasicAuth] = options.pop('proxy_auth', None)
        unsync_clock: bool = options.pop('assume_unsync_clock', True)
        self.http: HTTPClient = HTTPClient(connector, proxy=proxy, proxy_auth=proxy_auth, unsync_clock=unsync_clock,
                                           loop=self.loop)

        self._connection: ConnectionState = self._get_state(**options)
        self._connection.shard_count = self.shard_count
        self._closed: bool = False
        self._ready: asyncio.Event = asyncio.Event()

    def _get_websocket(self, guild_id: Optional[int] = None, *, shard_id: Optional[int] = None) -> QQWebSocket:
        return self.ws

    def _get_state(self, **options: Any) -> ConnectionState:
        return ConnectionState(dispatch=self.dispatch, handlers=self._handlers,
                               hooks=self._hooks, http=self.http, loop=self.loop, **options)

    def _handle_ready(self) -> None:
        self._ready.set()

    async def _call_before_identify_hook(self, shard_id: Optional[int], *, initial: bool = False) -> None:
        # This hook is an internal hook that actually calls the public one.
        # It allows the library to have its own hook without stepping on the
        # toes of those who need to override their own hook.
        await self.before_identify_hook(shard_id, initial=initial)

    async def before_identify_hook(self, shard_id: Optional[int], *, initial: bool = False) -> None:
        if not initial:
            await asyncio.sleep(5.0)

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        """|coro|
        客户端提供的默认错误处理程序。
        默认情况下，这会打印到 :data:`sys.stderr` 但是它可以被覆盖以使用不同的实现。
        看看 :func:`~qq.on_error` 以获取更多详细信息。
        """

        print(f'忽略 {event_method} 中的异常', file=sys.stderr)
        traceback.print_exc()

    async def _run_event(self, coro: Callable[..., Coroutine[Any, Any, Any]], event_name: str, *args: Any,
                         **kwargs: Any) -> None:
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    def _schedule_event(self, coro: Callable[..., Coroutine[Any, Any, Any]], event_name: str, *args: Any,
                        **kwargs: Any) -> asyncio.Task:
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        # Schedules the task
        return asyncio.create_task(wrapped, name=f'qq.py: {event_name}')

    def dispatch(self, event: str, *args: Any, **kwargs: Any) -> None:
        _log.debug('分派事件 %s', event)
        method = 'on_' + event

        listeners = self._listeners.get(event)
        if listeners:
            removed = []
            for i, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    removed.append(i)
                    continue

                try:
                    result = condition(*args)
                except Exception as exc:
                    future.set_exception(exc)
                    removed.append(i)
                else:
                    if result:
                        if len(args) == 0:
                            future.set_result(None)
                        elif len(args) == 1:
                            future.set_result(args[0])
                        else:
                            future.set_result(args)
                        removed.append(i)

            if len(removed) == len(listeners):
                self._listeners.pop(event)
            else:
                for idx in reversed(removed):
                    del listeners[idx]

        try:
            coro = getattr(self, method)
        except AttributeError:
            pass
        else:
            self._schedule_event(coro, method, *args, **kwargs)

    async def login(self, token: str) -> None:
        """|coro|
        使用指定的凭据登录客户端。

        Parameters
        -----------
        token: :class:`str`
            身份验证令牌。不要在这个令牌前面加上任何东西，因为库会为你做这件事。

        Raises
        ------
        :exc:`.LoginFailure`
            传递了错误的凭据。
        :exc:`.HTTPException`
            发生未知的 HTTP 相关错误，通常是当它不是 200 或已知的错误。
        """
        _log.info('使用静态令牌登录')

        data = await self.http.static_login(token.strip())
        self._connection.user = ClientUser(state=self._connection, data=data)

    @property
    def latency(self) -> float:
        """:class:`float`: 以秒为单位测量 HEARTBEAT 和 HEARTBEAT_ACK 之间的延迟。这可以称为 QQ WebSocket 协议延迟。
        """
        ws = self.ws
        return float('nan') if not ws else ws.latency

    def is_ready(self) -> bool:
        """:class:`bool`: 指定客户端的内部缓存是否可以使用。"""
        return self._ready.is_set()

    @property
    def user(self) -> Optional[ClientUser]:
        """Optional[:class:`.ClientUser`]: 代表连接的客户端。如果未登录，则为 ``None`` 。"""
        return self._connection.user

    @property
    def guilds(self) -> List[Guild]:
        """List[:class:`.Guild`]: 连接的客户端所属的频道。"""
        return self._connection.guilds

    def get_guild(self, id: int, /) -> Optional[Guild]:
        """返回具有给定 ID 的公会。

        Parameters
        -----------
        id: :class:`int`
            要搜索的 ID。

        Returns
        --------
        Optional[:class:`.Guild`]
            如果未找到公会则 ``None`` 。
        """
        return self._connection._get_guild(id)

    def get_user(self, id: int, /) -> Optional[User]:
        """返回具有给定 ID 的用户。

        Parameters
        -----------
        id: :class:`int`
            要搜索的 ID。

        Returns
        --------
        Optional[:class:`~discord.User`]
            如果未找到，则为 ``None`` 。
        """
        return self._connection.get_user(id)

    async def fetch_guild(self, guild_id: int) -> Optional[Guild]:
        data = await self.http.get_guild(guild_id)
        return Guild(data=data, state=self._connection)

    async def fetch_guilds(
            self,
            *,
            limit: Optional[int] = 100,
            before: datetime.datetime = None,
            after: datetime.datetime = None
    ):
        return GuildIterator(self, limit=limit, before=before, after=after)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """一个阻塞调用，它从你那里抽象出事件循环初始化。
        如果您想对事件循环进行更多控制，则不应使用此函数。使用 :meth:`start` 协程或 :meth:`connect` + :meth:`login`。

        大致相当于： ::

            try:
                loop.run_until_complete(start(*args, **kwargs))
            except KeyboardInterrupt:
                loop.run_until_complete(close())
                # cancel all tasks lingering
            finally:
                loop.close()

        .. warning::

            由于它是阻塞的，因此该函数必须是最后一个调用的函数。
            这意味着在此函数调用之后注册的事件或任何被调用的东西在它返回之前不会执行。

        """
        loop = self.loop

        try:
            loop.add_signal_handler(signal.SIGINT, lambda: loop.stop())
            loop.add_signal_handler(signal.SIGTERM, lambda: loop.stop())
        except NotImplementedError:
            pass

        async def runner():
            try:
                await self.start(**kwargs)
            finally:
                if not self.is_closed():
                    await self.close()

        def stop_loop_on_completion(f):
            loop.stop()

        future = asyncio.ensure_future(runner(), loop=loop)
        future.add_done_callback(stop_loop_on_completion)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            _log.info('接收到终止机器人和事件循环的信号。')
        finally:
            future.remove_done_callback(stop_loop_on_completion)
            _log.info('清理任务。')
            _cleanup_loop(loop)

        if not future.cancelled():
            try:
                return future.result()
            except KeyboardInterrupt:
                # I am unsure why this gets raised here but suppress it anyway
                return None

    async def start(self, reconnect: bool = True) -> None:
        """|coro|
        :meth:`login` + :meth:`connect` 的协程。

        Raises
        -------
        TypeError
            收到意外的关键字参数。
        """
        await self.login(self.token)
        await self.connect(reconnect=reconnect)

    def clear(self) -> None:
        """清除机器人的内部状态。在此之后，机器人可以被视为 ``重新连接`` ，
        即 :meth:`is_closed` 和 :meth:`is_ready` 都返回 ``False`` 以及清除机器人的内部缓存。
        """
        self._closed = False
        self._ready.clear()
        self._connection.clear()
        self.http.recreate()

    def is_closed(self) -> bool:
        return self._closed

    async def connect(self, *, reconnect: bool = True) -> None:
        """|coro|
        创建一个 websocket 连接并让 websocket 监听来自 QQ 的消息。这运行整个事件系统和库的其他方面的循环。在 WebSocket 连接终止之前，不会恢复控制。

        Parameters
        -----------
        reconnect: :class:`bool`
            我们应不应该尝试重新连接，无论是由于互联网故障还是 QQ 的特定故障。
            某些导致错误状态的断开连接将不会得到处理（例如无效的分片或错误的令牌）。

        Raises
        -------
        :exc:`.GatewayNotFound`
            如果找不到连接到 QQ 的网关。通常，如果抛出此问题，则会导致 QQ API 中断。
        :exc:`.ConnectionClosed`
            websocket 连接已终止。
        """
        backoff = ExponentialBackoff()
        ws_params = {
            'initial': True,
            'shard_id': self.shard_id,
        }
        while not self.is_closed():
            try:
                coro = QQWebSocket.from_client(self, **ws_params)
                self.ws = await asyncio.wait_for(coro, timeout=60.0)
                ws_params['initial'] = False
                while True:
                    await self.ws.poll_event()
            except ReconnectWebSocket as e:
                _log.info('收到了 %s websocket 的请求。', e.op)
                self.dispatch('disconnect')
                ws_params.update(sequence=self.ws.sequence, resume=e.resume, session=self.ws.session_id)
                continue
            except (OSError,
                    HTTPException,
                    GatewayNotFound,
                    ConnectionClosed,
                    aiohttp.ClientError,
                    asyncio.TimeoutError) as exc:

                self.dispatch('disconnect')
                if not reconnect:
                    await self.close()
                    if isinstance(exc, ConnectionClosed) and exc.code == 1000:
                        # clean close, don't re-raise this
                        return
                    raise

                if self.is_closed():
                    return

                # If we get connection reset by peer then try to RESUME
                if isinstance(exc, OSError) and exc.errno in (54, 10054):
                    ws_params.update(sequence=self.ws.sequence, initial=False, resume=True, session=self.ws.session_id)
                    continue

                # We should only get this when an unhandled close code happens,
                # such as a clean disconnect (1000) or a bad state (bad token, no sharding, etc)
                # sometimes, discord sends us 1000 for unknown reasons, so we should reconnect
                # regardless and rely on is_closed instead
                if isinstance(exc, ConnectionClosed):
                    if exc.code != 1000:
                        await self.close()
                        raise

                retry = backoff.delay()
                _log.exception("尝试在 %.2fs 中重新连接", retry)
                await asyncio.sleep(retry)
                # Always try to RESUME the connection
                # If the connection is not RESUME-able then the gateway will invalidate the session.
                # This is apparently what the official Discord client does.
                ws_params.update(sequence=self.ws.sequence, resume=True, session=self.ws.session_id)

    async def close(self) -> None:
        """|coro|
        关闭与 Discord 的连接。
        """
        if self._closed:
            return

        self._closed = True

        if self.ws is not None and self.ws.open:
            await self.ws.close(code=1000)

        await self.http.close()
        self._ready.clear()

    def event(self, coro: Coro) -> Coro:
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('注册的事件必须是协程函数')

        setattr(self, coro.__name__, coro)
        _log.debug('%s 已成功注册为事件', coro.__name__)
        return coro
