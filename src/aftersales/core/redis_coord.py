"""Redis lease ownership, rolling admission and per-user cost accounting."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC
from decimal import ROUND_CEILING, Decimal
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from aftersales.core.llm import PRICING, InferenceUnavailable, Usage
from aftersales.core.locks import SessionBusy
from aftersales.domain.clock import Clock

_RENEW = """if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2]) end return 0"""
_RELEASE = """if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1]) end return 0"""

_NOW = """local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
"""
_ADMIT = (
    _NOW
    + """
local window, ttl = tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 1 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[5]) then return 2 end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], window)
redis.call('ZADD', KEYS[2], now + ttl, ARGV[1])
redis.call('PEXPIRE', KEYS[2], ttl)
return 0
"""
)
_RENEW_SLOT = (
    _NOW
    + """
local old = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not old or tonumber(old) <= now then return 0 end
redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""
)
_CHARGE = """local total = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], 172800, 'NX')
return total"""


class CoordinationLost(RuntimeError):
    pass


class AdmissionDenied(ValueError):
    pass


class RedisCoordinator:
    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = "aftersales",
        lease_ms: int = 30_000,
        renew_seconds: float = 10,
        request_limit: int = 12,
        window_ms: int = 60_000,
    ) -> None:
        if not 0 < renew_seconds * 1000 < lease_ms:
            raise ValueError("Watchdog interval must be shorter than the lease")
        self.redis, self.namespace = redis, namespace
        self.lease_ms, self.renew_seconds = lease_ms, renew_seconds
        self.request_limit, self.window_ms = request_limit, window_ms
        self._lease: ContextVar[tuple[str, str] | None] = ContextVar("session_lease", default=None)

    def key(self, suffix: str) -> str:
        return f"{self.namespace}:{suffix}"

    @asynccontextmanager
    async def _watch(
        self, renew: Callable[[], Awaitable[int]], release: Callable[[], Awaitable[int]]
    ) -> AsyncIterator[None]:
        owner = asyncio.current_task()
        assert owner is not None
        lost = False

        async def watchdog() -> None:
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(self.renew_seconds)
                    if not await renew():
                        break
            except RedisError:
                pass
            lost = True
            owner.cancel()

        task = asyncio.create_task(watchdog())
        try:
            yield
        except asyncio.CancelledError as exc:
            if lost:
                owner.uncancel()
                raise CoordinationLost("会话协调已中断，请刷新后重试") from exc
            raise
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # An outage leaves only a bounded TTL and cannot release another owner.
            with suppress(RedisError):
                await release()

    @asynccontextmanager
    async def acquire(self, session_id: str, timeout: float = 0) -> AsyncIterator[None]:
        key, token = self.key(f"lock:session:{session_id}"), uuid4().hex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await self.redis.set(key, token, nx=True, px=self.lease_ms):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionBusy(session_id)
            await asyncio.sleep(min(0.05, remaining))
        async with self._watch(
            lambda: self.redis.eval(_RENEW, 1, key, token, self.lease_ms),
            lambda: self.redis.eval(_RELEASE, 1, key, token),
        ):
            handle = self._lease.set((key, token))
            try:
                yield
            finally:
                self._lease.reset(handle)

    async def require_current_lease(self, session_id: str) -> None:
        key = self.key(f"lock:session:{session_id}")
        lease = self._lease.get()
        if (
            lease is None
            or lease[0] != key
            or not await self.redis.eval(_RENEW, 1, key, lease[1], self.lease_ms)
        ):
            raise CoordinationLost("会话协调已中断，请刷新后重试")

    @asynccontextmanager
    async def admit(self, user_id: str) -> AsyncIterator[None]:
        # Both keys share a hash slot for the admission script.
        uid = self.key(f"uid:{{{user_id}}}")
        window, inflight, token = f"{uid}:window", f"{uid}:inflight", uuid4().hex
        denied = await self.redis.eval(
            _ADMIT, 2, window, inflight, token, self.window_ms, self.lease_ms, self.request_limit, 2
        )
        if denied:
            raise AdmissionDenied(
                "当前已有两条消息在处理，请稍后重试" if denied == 2 else "请求过于频繁，请稍后重试"
            )
        async with self._watch(
            lambda: self.redis.eval(_RENEW_SLOT, 1, inflight, token, self.lease_ms),
            lambda: self.redis.zrem(inflight, token),
        ):
            yield


class DailyBudget:
    def __init__(
        self,
        coord: RedisCoordinator,
        clock: Clock,
        user_id: str,
        limit: int,
        unknown_rates: tuple[Decimal, Decimal] | None = None,
    ) -> None:
        self.coord, self.clock, self.user_id, self.limit = coord, clock, user_id, limit
        self.unknown_rates = unknown_rates

    def key(self) -> str:
        day = self.clock.now().astimezone(UTC).date().isoformat()
        return self.coord.key(f"budget:uid:{self.user_id}:{day}")

    async def before(self, model: str) -> None:
        if model not in PRICING and self.unknown_rates is None:
            raise InferenceUnavailable("模型费率尚未配置，请联系人工客服。")
        await self.check_balance()

    async def check_balance(self) -> None:
        total = int(await self.coord.redis.get(self.key()) or 0)
        if total >= self.limit:
            raise InferenceUnavailable("已达到今日对话额度，请明天重试或联系人工客服。")

    async def record(self, usage: Usage, cost: Decimal | None) -> None:
        if cost is None:
            assert self.unknown_rates is not None
            inputs, outputs = self.unknown_rates
            cost = (inputs * usage.input_tokens + outputs * usage.output_tokens) / 1_000_000
        units = int((cost * 100_000_000).to_integral_value(rounding=ROUND_CEILING))
        await self.coord.redis.eval(_CHARGE, 1, self.key(), units)
