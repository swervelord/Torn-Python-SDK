"""Configuration, hooks and data passed through the Torn HTTP runtime."""

from __future__ import annotations

import asyncio
import json
import time as system_time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal, Protocol, cast

import httpx
from pydantic import JsonValue

from ._error_codes import (
    DEFAULT_BASE_DELAY_MS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_RETRY_AFTER_CAP_MS,
)
from .errors import AccessLevel, DecodeError, TornError, ValidationError

QueryScalar = str | int | float | bool
QueryValue = QueryScalar | Sequence[QueryScalar] | None
Query = Mapping[str, QueryValue] | Sequence[tuple[str, QueryValue]]
KeyStatus = Literal["healthy", "rate_limited", "disabled"]


def key_hint(value: str) -> str:
    return "***" + value[-4:] if len(value) >= 8 else "***"


class Key:
    """An API key with optional known access and a shared owner identifier."""

    __slots__ = ("__value", "access", "owner")

    def __init__(
        self, value: str, *, owner: str | int | None = None, access: AccessLevel | None = None
    ) -> None:
        value = value.strip()
        if (
            not value
            or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValidationError("Invalid Torn API key")
        self.__value = value
        self.owner = owner
        self.access = access

    def _reveal(self) -> str:
        return self.__value

    @property
    def hint(self) -> str:
        return key_hint(self.__value)

    def __repr__(self) -> str:
        return f"Key({self.hint!r})"


@dataclass(frozen=True)
class KeyState:
    index: int
    hint: str
    state: KeyStatus
    remaining: int
    access: AccessLevel | None = None
    owner: str | int | None = None
    until: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KeyAccess:
    hint: str
    owner: int | None = None
    access: AccessLevel | None = None
    error: TornError | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime policy. All times and durations in this module are seconds."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    key_per_minute: int = 100
    route_per_minute: int = 1000
    rate_window: float = 60.0
    base_delay: float = DEFAULT_BASE_DELAY_MS / 1000
    max_delay: float = DEFAULT_MAX_DELAY_MS / 1000
    retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP_MS / 1000
    max_concurrency: int = 8
    max_response_bytes: int = 64 * 1024 * 1024
    timeout: float = 30.0
    cache_ttl: float = 30.0
    jitter: bool = True
    user_agent: str = field(default="Torn-Python-SDK", repr=False)

    def __post_init__(self) -> None:
        from math import isfinite

        if any(
            not isfinite(limit) or limit <= 0
            for limit in (
                self.max_attempts,
                self.key_per_minute,
                self.route_per_minute,
                self.max_concurrency,
                self.max_response_bytes,
                self.rate_window,
                self.timeout,
            )
        ) or any(
            not isfinite(delay) or delay < 0
            for delay in (self.base_delay, self.max_delay, self.retry_after_cap, self.cache_ttl)
        ):
            raise ValidationError("Invalid Torn runtime limits")


@dataclass(frozen=True)
class Route:
    name: str
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    per_minute: int | None = None
    proxy: str | httpx.URL | httpx.Proxy | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AsyncRoute:
    name: str
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    per_minute: int | None = None
    proxy: str | httpx.URL | httpx.Proxy | None = field(default=None, repr=False)


class TimeSource(Protocol):
    def monotonic(self) -> float: ...

    def time(self) -> float: ...


class Clock(TimeSource, Protocol):
    def sleep(self, seconds: float) -> None: ...


class AsyncClock(TimeSource, Protocol):
    def sleep(self, seconds: float) -> Awaitable[None]: ...


class SystemClock:
    monotonic = staticmethod(system_time.monotonic)
    time = staticmethod(system_time.time)
    sleep = staticmethod(system_time.sleep)


class SystemAsyncClock:
    monotonic = staticmethod(system_time.monotonic)
    time = staticmethod(system_time.time)
    sleep = staticmethod(asyncio.sleep)


@dataclass(frozen=True)
class Request:
    path: str = field(repr=False)
    query: Query | None = field(default=None, repr=False)
    operation: str | None = field(default=None, repr=False)
    access: AccessLevel | None = None
    cache_ttl: float | None = None
    fallback: Request | None = field(default=None, repr=False)
    method: str = "GET"
    expect_json: bool = True


@dataclass(frozen=True)
class RequestOptions:
    api_key: str | None = field(default=None, repr=False)
    route: str | None = field(default=None, repr=False)
    timeout: float | None = None
    no_cache: bool = False
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = field(default=None, repr=False)
    max_attempts: int | None = None


@dataclass(frozen=True)
class RequestTrace:
    operation: str
    url: str
    attempt: int
    status: int | None
    duration: float
    key_hint: str
    route: str
    cached: bool
    version: Literal["v1", "v2"]


@dataclass(frozen=True)
class Hooks:
    on_request: Callable[[RequestTrace], None] | None = None
    on_response: Callable[[RequestTrace], None] | None = None
    on_retry: Callable[[RequestTrace, TornError, float], None] | None = None
    on_error: Callable[[RequestTrace, TornError], None] | None = None


@dataclass(frozen=True)
class Response:
    body: bytes = field(repr=False)
    trace: RequestTrace
    encoding: str = field(default="utf-8", repr=False)

    @property
    def status(self) -> int | None:
        return self.trace.status

    def json(self) -> JsonValue:
        """Decode raw JSON. Typed clients subsequently validate the response model."""
        try:
            return cast(JsonValue, json.loads(self.body))
        except (ValueError, UnicodeError):
            pass
        raise DecodeError(self.trace.operation, "$", "valid JSON", raw=self.body)

    def text(self) -> str:
        """Decode text (for example CSV) using the response charset when supplied."""
        try:
            return self.body.decode(self.encoding)
        except (LookupError, UnicodeError):
            pass
        raise DecodeError(self.trace.operation, "$", "valid response text", raw=self.body)


@dataclass(frozen=True)
class CacheEntry:
    body: bytes = field(repr=False)
    status: int
    encoding: str = field(repr=False)


class Cache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, entry: CacheEntry, ttl: float) -> None: ...


class MemoryCache:
    """Thread-safe bounded cache with per-entry expiration."""

    def __init__(
        self,
        max_entries: int = 1024,
        *,
        clock: Callable[[], float] = system_time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValidationError("Cache capacity must be positive")
        self._lock = Lock()
        self._items: dict[str, tuple[float, CacheEntry]] = {}
        self._max_entries = max_entries
        self._clock = clock

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            hit = self._items.pop(key, None)
            if hit is None or hit[0] <= self._clock():
                return None
            self._items[key] = hit
            return hit[1]

    def set(self, key: str, entry: CacheEntry, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = self._clock() + ttl, entry
            if len(self._items) > self._max_entries:
                del self._items[next(iter(self._items))]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
