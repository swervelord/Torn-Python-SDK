"""Synchronous and asynchronous Torn API HTTP runtimes."""

from __future__ import annotations

import asyncio
import builtins
import os
from collections.abc import AsyncIterator, Awaitable, Iterable, Iterator, Mapping
from copy import copy
from dataclasses import replace
from threading import BoundedSemaphore, Lock
from types import TracebackType
from typing import TypeVar

import httpx
from pydantic import JsonValue as JsonValue
from typing_extensions import Self, TypedDict, Unpack

from ._runtime_flow import (
    Executor,
    Send,
    Wait,
    WireResponse,
    key_access,
    page_items,
    page_link,
)
from ._runtime_flow import ExecutionContext as ExecutionContext
from ._runtime_state import Pool
from ._runtime_types import (
    SystemAsyncClock,
    SystemClock,
    TimeSource,
)
from ._runtime_types import AsyncClock as AsyncClock
from ._runtime_types import AsyncRoute as AsyncRoute
from ._runtime_types import Cache as Cache
from ._runtime_types import CacheEntry as CacheEntry
from ._runtime_types import Clock as Clock
from ._runtime_types import Hooks as Hooks
from ._runtime_types import Key as Key
from ._runtime_types import KeyAccess as KeyAccess
from ._runtime_types import KeyState as KeyState
from ._runtime_types import MemoryCache as MemoryCache
from ._runtime_types import Query as Query
from ._runtime_types import QueryScalar as QueryScalar
from ._runtime_types import QueryValue as QueryValue
from ._runtime_types import Request as Request
from ._runtime_types import RequestOptions as RequestOptions
from ._runtime_types import RequestTrace as RequestTrace
from ._runtime_types import Response as Response
from ._runtime_types import Route as Route
from ._runtime_types import RuntimeConfig as RuntimeConfig
from .errors import CancelledError, TimeoutError, TornError, TransportError, ValidationError

_DEFAULT_ORIGIN = "https://api.torn.com"
_KEY_INFO = Request("/v2/key/info", operation="getKeyInfo", cache_ttl=0)
_T = TypeVar("_T")


class _Init(TypedDict, total=False):
    config: RuntimeConfig | None
    hooks: Hooks | None
    cache: Cache | None
    base_url: str
    base_url_v1: str | None
    base_url_v2: str | None
    detect_key_owners: bool
    check_access: bool


class _SyncInit(_Init, total=False):
    clock: Clock | None
    routes: Iterable[Route]
    transport: httpx.BaseTransport | None


class _AsyncInit(_Init, total=False):
    clock: AsyncClock | None
    routes: Iterable[AsyncRoute]
    transport: httpx.AsyncBaseTransport | None


class _Lifetime:
    """Refcounts a runtime and its clones, which share transports and owner detection."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.sync_detect = Lock()
        self.async_detect = asyncio.Lock()
        self.references = 1
        self.detected = False


def _env_keys(env: Mapping[str, str]) -> tuple[str, ...]:
    keys = tuple(key for value in env.get("TORN_API_KEYS", "").split(",") if (key := value.strip()))
    if keys:
        return keys
    single = env.get("TORN_API_KEY", "").strip()
    return (single,) if single else ()


def _env_config(env: Mapping[str, str]) -> RuntimeConfig:
    defaults = RuntimeConfig()
    try:
        return replace(
            defaults,
            max_attempts=int(
                env.get("TORN_MAX_ATTEMPTS", env.get("TORN_ATTEMPTS", defaults.max_attempts))
            ),
            max_concurrency=int(
                env.get("TORN_MAX_IN_FLIGHT", env.get("TORN_CONCURRENCY", defaults.max_concurrency))
            ),
            key_per_minute=int(env.get("TORN_RATE_LIMIT_PER_KEY_PER_MIN", defaults.key_per_minute)),
            route_per_minute=int(
                env.get("TORN_RATE_LIMIT_PER_IP_PER_MIN", defaults.route_per_minute)
            ),
            timeout=float(env.get("TORN_HTTP_TIMEOUT_SECS", defaults.timeout)),
            base_delay=float(env.get("TORN_NETWORK_RETRY_BACKOFF_MS", defaults.base_delay * 1000))
            / 1000,
            user_agent=env.get("TORN_USER_AGENT", "") or defaults.user_agent,
        )
    except ValueError:
        pass
    raise ValidationError("Invalid Torn runtime environment configuration")


def _env_urls(env: Mapping[str, str], options: _Init) -> None:
    if "base_url" not in options and "base_url_v2" not in options:
        v2 = env.get("TORN_API_BASE_URL_V2", "") or env.get("TORN_BASE_URL", "")
        if v2:
            options["base_url_v2"] = v2
    if "base_url" not in options and "base_url_v1" not in options:
        v1 = env.get("TORN_API_BASE_URL_V1", "")
        if v1:
            options["base_url_v1"] = v1


def _route_limits(routes: Iterable[Route | AsyncRoute], config: RuntimeConfig) -> dict[str, int]:
    limits: dict[str, int] = {}
    for route in routes:
        if not route.name or route.name in limits:
            raise ValidationError("Torn egress route names must be nonempty and unique")
        if route.per_minute is not None and route.per_minute < 1:
            raise ValidationError("Torn egress route budget must be positive")
        if route.proxy is not None and route.transport is not None:
            raise ValidationError("Torn egress routes accept either a proxy or a custom transport")
        limits[route.name] = route.per_minute or config.route_per_minute
    return limits


def _wire_response(response: httpx.Response, body: bytes) -> WireResponse:
    return WireResponse(
        response.status_code,
        body,
        response.encoding or "utf-8",
        response.headers.get("Retry-After"),
    )


def _body_limit() -> ValidationError:
    return ValidationError("Torn response exceeded the configured maximum body size")


class _RuntimeBase:
    def _setup(
        self,
        api_key: str | None,
        keys: Iterable[str | Key],
        config: RuntimeConfig,
        clock: TimeSource,
        routes: Iterable[Route | AsyncRoute],
        hooks: Hooks | None,
        cache: Cache | None,
        base_url: str,
        base_url_v1: str | None,
        base_url_v2: str | None,
        detect_key_owners: bool,
        check_access: bool,
    ) -> None:
        supplied = [Key(api_key)] if api_key is not None else []
        supplied.extend(key if isinstance(key, Key) else Key(key) for key in keys)
        self._pool = Pool(
            supplied,
            _route_limits(routes, config),
            config,
            clock,
            check_access=check_access,
            detect_key_owners=detect_key_owners,
        )
        self._executor = Executor(
            self._pool,
            base_url,
            hooks or Hooks(),
            cache,
            base_url_v1=base_url_v1,
            base_url_v2=base_url_v2,
        )
        self._lifetime = _Lifetime()
        self._closed = False

    @property
    def config(self) -> RuntimeConfig:
        return self._executor.config

    @property
    def base_url(self) -> str:
        return self._executor.base_url

    @property
    def base_url_v1(self) -> str:
        return self._executor.base_urls["v1"]

    @property
    def base_url_v2(self) -> str:
        return self._executor.base_urls["v2"]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(keys={len(self._pool.keys)})"

    def _check_open(self) -> None:
        if self._closed:
            raise ValidationError("Torn client is closed")

    def _release(self) -> bool:
        with self._lifetime.lock:
            if self._closed:
                return False
            self._closed = True
            self._lifetime.references -= 1
            return self._lifetime.references == 0

    def clone(self) -> Self:
        """Return another handle sharing key budgets, caches, and HTTP connections."""
        with self._lifetime.lock:
            self._check_open()
            clone = copy(self)
            self._lifetime.references += 1
            return clone

    def new_context(self) -> ExecutionContext:
        return ExecutionContext(self._pool)

    def key_states(self) -> tuple[KeyState, ...]:
        return self._pool.states()

    def preload_budget(self, key: str, sends_at: Iterable[float]) -> None:
        """Seed the sliding window with absolute monotonic timestamps for a configured key."""
        self._pool.preload(key, sends_at)

    def redact(
        self,
        text: str,
        options: RequestOptions | None = None,
        request: Request | None = None,
    ) -> str:
        return self._executor.redact(text, options, request)

    def _transport_error(
        self,
        cause: Exception,
        send: Send,
        options: RequestOptions,
    ) -> TornError:
        detail = self._executor.redact(str(cause), options, send.prepared.request, send.key)
        clean_cause = RuntimeError(f"{type(cause).__name__}: {detail}")
        if isinstance(cause, (httpx.TimeoutException, builtins.TimeoutError, asyncio.TimeoutError)):
            return TimeoutError("Torn request attempt timed out", retryable=True, cause=clean_cause)
        return TransportError(clean_cause)

    def _key_info_observation(
        self,
        request: Request,
        context: ExecutionContext,
        response: Response,
    ) -> KeyAccess | None:
        key = context._last_key
        if key is None or request.path.split("?", 1)[0].strip("/") != "v2/key/info":
            return None
        owner, access = key_access(response.json())
        self._pool.update_key(key, owner, access)
        return KeyAccess(key.hint, owner, access)

    def _should_detect(self, request: Request, options: RequestOptions) -> bool:
        return (
            self._pool.detect_key_owners
            and not self._lifetime.detected
            and options.api_key is None
            and request.path.split("?", 1)[0].strip("/") != "v2/key/info"
            and self._executor.prepare(request)[1] is None
        )

    def _pagination_options(
        self, request: Request, options: RequestOptions | None
    ) -> RequestOptions:
        opts = options or RequestOptions()
        if opts.api_key is not None:
            return opts
        query_key = self._executor.prepare(request)[1]
        return replace(opts, api_key=query_key) if query_key is not None else opts

    def _headers(self, send: Send) -> dict[str, str]:
        return {
            "Authorization": f"ApiKey {send.key._reveal()}",
            "User-Agent": self.config.user_agent,
        }


class Runtime(_RuntimeBase):
    """Blocking HTTP runtime with a thread-safe key and egress pool."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        keys: Iterable[str | Key] = (),
        transport: httpx.BaseTransport | None = None,
        config: RuntimeConfig | None = None,
        clock: Clock | None = None,
        routes: Iterable[Route] = (),
        hooks: Hooks | None = None,
        cache: Cache | None = None,
        base_url: str = _DEFAULT_ORIGIN,
        base_url_v1: str | None = None,
        base_url_v2: str | None = None,
        detect_key_owners: bool = False,
        check_access: bool = False,
    ) -> None:
        resolved = config or RuntimeConfig()
        egress = tuple(routes) or (Route("default", transport),)
        self._clock = clock or SystemClock()
        self._setup(
            api_key,
            keys,
            resolved,
            self._clock,
            egress,
            hooks,
            cache,
            base_url,
            base_url_v1,
            base_url_v2,
            detect_key_owners,
            check_access,
        )
        self._permit = BoundedSemaphore(resolved.max_concurrency)
        self._clients = {
            route.name: httpx.Client(
                transport=route.transport or (transport if route.proxy is None else None),
                proxy=route.proxy,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=resolved.max_concurrency),
            )
            for route in egress
        }

    @classmethod
    def from_env(
        cls, *, environ: Mapping[str, str] | None = None, **kwargs: Unpack[_SyncInit]
    ) -> Self:
        env: Mapping[str, str] = os.environ if environ is None else environ
        keys = _env_keys(env)
        if not keys:
            raise ValidationError(
                "Set TORN_API_KEYS or TORN_API_KEY to configure Torn authentication"
            )
        if kwargs.get("config") is None:
            kwargs["config"] = _env_config(env)
        _env_urls(env, kwargs)
        return cls(keys=keys, **kwargs)

    def __enter__(self) -> Self:
        self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._release():
            for client in self._clients.values():
                client.close()

    def _wait(self, delay: float, options: RequestOptions) -> None:
        end = self._clock.monotonic() + delay
        while (remaining := end - self._clock.monotonic()) > 0:
            self._clock.sleep(self._executor.wait_length(remaining, options))
        self._executor.check(options)

    def _send(self, send: Send, options: RequestOptions) -> WireResponse | TornError:
        while not self._permit.acquire(timeout=self._executor.wait_length(0.05, options)):
            continue
        try:
            with self._clients[send.route].stream(
                send.prepared.request.method,
                send.prepared.url,
                headers=self._headers(send),
                timeout=min(send.timeout, self._executor.timeout(options)),
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    self._executor.check(options)
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        return _body_limit()
                    body.extend(chunk)
                return _wire_response(response, bytes(body))
        except (httpx.RequestError, OSError) as cause:
            self._executor.check(options)
            return self._transport_error(cause, send, options)
        except httpx.InvalidURL:
            return ValidationError("Invalid Torn request URL")
        finally:
            self._permit.release()

    def _execute(
        self,
        request: Request,
        options: RequestOptions,
        context: ExecutionContext,
    ) -> Response:
        flow = self._executor.flow(request, options, context)
        try:
            result: WireResponse | TornError | None = None
            while True:
                step = flow.send(result)
                if isinstance(step, Wait):
                    self._wait(step.delay, options)
                    result = None
                else:
                    result = self._send(step, options)
                    self._executor.check(options)
        except StopIteration as done:
            response: Response = done.value
            self._key_info_observation(request, context, response)
            return response
        finally:
            flow.close()

    def detect_key_access(self, *, options: RequestOptions | None = None) -> tuple[KeyAccess, ...]:
        """Look up access levels and, when enabled, merge budgets by detected Torn owner."""
        self._check_open()
        with self._lifetime.sync_detect:
            return self._detect(options or RequestOptions())

    def _detect(self, options: RequestOptions) -> tuple[KeyAccess, ...]:
        entries: list[KeyAccess] = []
        for key in self._pool.keys:
            context = self.new_context()
            opts = replace(options, api_key=key._reveal(), no_cache=True)
            try:
                response = self._execute(_KEY_INFO, opts, context)
            except TornError as error:
                if (
                    isinstance(error, CancelledError)
                    or isinstance(error, TimeoutError)
                    and not error.retryable
                ):
                    raise
                entries.append(KeyAccess(key.hint, error=error))
            else:
                owner, access = key_access(response.json())
                entries.append(KeyAccess(key.hint, owner, access))
        self._lifetime.detected = True
        return tuple(entries)

    def request_response(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> Response:
        self._check_open()
        opts = options or RequestOptions()
        if self._should_detect(request, opts):
            with self._lifetime.sync_detect:
                if not self._lifetime.detected:
                    self._detect(opts)
        return self._execute(request, opts, context or self.new_context())

    def request(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> JsonValue:
        return self.request_response(request, options=options, context=context).json()

    def request_bytes(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> bytes:
        return self.request_response(
            replace(request, expect_json=False), options=options, context=context
        ).body

    def request_text(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> str:
        return self.request_response(
            replace(request, expect_json=False), options=options, context=context
        ).text()

    def page_responses(
        self,
        request: Request,
        *,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[Response]:
        """Iterate raw responses at a validated v2 page cursor."""
        page = 0
        opts = self._pagination_options(request, options)
        while max_pages is None or page < max_pages:
            response = self.request_response(request, options=opts)
            yield response
            page += 1
            if max_pages is not None and page >= max_pages:
                return
            link = page_link(response.json(), backwards)
            if link is None:
                return
            request = self._executor.next_request(request, link)

    def pages(
        self,
        request: Request,
        *,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[JsonValue]:
        for response in self.page_responses(
            request,
            backwards=backwards,
            max_pages=max_pages,
            options=options,
        ):
            yield response.json()

    def items(
        self,
        request: Request,
        *,
        field: str | None = None,
        limit: int | None = None,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> Iterator[JsonValue]:
        if limit is not None and limit <= 0:
            return
        for page in self.pages(request, backwards=backwards, max_pages=max_pages, options=options):
            for item in page_items(page, field):
                yield item
                if limit is not None:
                    limit -= 1
                    if limit == 0:
                        return


class AsyncRuntime(_RuntimeBase):
    """Asyncio HTTP runtime with shared async transport and rate limits."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        keys: Iterable[str | Key] = (),
        transport: httpx.AsyncBaseTransport | None = None,
        config: RuntimeConfig | None = None,
        clock: AsyncClock | None = None,
        routes: Iterable[AsyncRoute] = (),
        hooks: Hooks | None = None,
        cache: Cache | None = None,
        base_url: str = _DEFAULT_ORIGIN,
        base_url_v1: str | None = None,
        base_url_v2: str | None = None,
        detect_key_owners: bool = False,
        check_access: bool = False,
    ) -> None:
        resolved = config or RuntimeConfig()
        egress = tuple(routes) or (AsyncRoute("default", transport),)
        self._clock = clock or SystemAsyncClock()
        self._setup(
            api_key,
            keys,
            resolved,
            self._clock,
            egress,
            hooks,
            cache,
            base_url,
            base_url_v1,
            base_url_v2,
            detect_key_owners,
            check_access,
        )
        self._permit = asyncio.Semaphore(resolved.max_concurrency)
        self._clients = {
            route.name: httpx.AsyncClient(
                transport=route.transport or (transport if route.proxy is None else None),
                proxy=route.proxy,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=resolved.max_concurrency),
            )
            for route in egress
        }

    @classmethod
    def from_env(
        cls, *, environ: Mapping[str, str] | None = None, **kwargs: Unpack[_AsyncInit]
    ) -> Self:
        env: Mapping[str, str] = os.environ if environ is None else environ
        keys = _env_keys(env)
        if not keys:
            raise ValidationError(
                "Set TORN_API_KEYS or TORN_API_KEY to configure Torn authentication"
            )
        if kwargs.get("config") is None:
            kwargs["config"] = _env_config(env)
        _env_urls(env, kwargs)
        return cls(keys=keys, **kwargs)

    async def __aenter__(self) -> Self:
        self._check_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._release():
            for client in self._clients.values():
                await client.aclose()

    async def _await(
        self,
        awaitable: Awaitable[_T],
        options: RequestOptions,
        timeout: float | None = None,
    ) -> _T:
        if options.cancelled is None:
            return await asyncio.wait_for(awaitable, timeout)
        task = asyncio.ensure_future(awaitable)
        try:
            end = asyncio.get_running_loop().time() + timeout if timeout is not None else None
            while not task.done():
                self._executor.check(options)
                wait = 0.05 if end is None else min(0.05, end - asyncio.get_running_loop().time())
                if wait <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait({task}, timeout=wait)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _wait(self, delay: float, options: RequestOptions) -> None:
        end = self._clock.monotonic() + delay
        while (remaining := end - self._clock.monotonic()) > 0:
            await self._clock.sleep(self._executor.wait_length(remaining, options))
        self._executor.check(options)

    async def _send(self, send: Send, options: RequestOptions) -> WireResponse | TornError:
        async def read() -> WireResponse | TornError:
            async with (
                self._permit,
                self._clients[send.route].stream(
                    send.prepared.request.method,
                    send.prepared.url,
                    headers=self._headers(send),
                    timeout=min(send.timeout, self._executor.timeout(options)),
                ) as response,
            ):
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    self._executor.check(options)
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        return _body_limit()
                    body.extend(chunk)
                return _wire_response(response, bytes(body))

        try:
            return await self._await(read(), options, send.timeout)
        except (httpx.RequestError, OSError, builtins.TimeoutError, asyncio.TimeoutError) as cause:
            self._executor.check(options)
            return self._transport_error(cause, send, options)
        except httpx.InvalidURL:
            return ValidationError("Invalid Torn request URL")

    async def _execute(
        self,
        request: Request,
        options: RequestOptions,
        context: ExecutionContext,
    ) -> Response:
        flow = self._executor.flow(request, options, context)
        try:
            result: WireResponse | TornError | None = None
            while True:
                step = flow.send(result)
                if isinstance(step, Wait):
                    await self._wait(step.delay, options)
                    result = None
                else:
                    result = await self._send(step, options)
                    self._executor.check(options)
        except StopIteration as done:
            response: Response = done.value
            self._key_info_observation(request, context, response)
            return response
        finally:
            flow.close()

    async def detect_key_access(
        self,
        *,
        options: RequestOptions | None = None,
    ) -> tuple[KeyAccess, ...]:
        """Look up access levels and, when enabled, merge budgets by detected Torn owner."""
        self._check_open()
        async with self._lifetime.async_detect:
            return await self._detect(options or RequestOptions())

    async def _detect(self, options: RequestOptions) -> tuple[KeyAccess, ...]:
        entries: list[KeyAccess] = []
        for key in self._pool.keys:
            context = self.new_context()
            opts = replace(options, api_key=key._reveal(), no_cache=True)
            try:
                response = await self._execute(_KEY_INFO, opts, context)
            except TornError as error:
                if (
                    isinstance(error, CancelledError)
                    or isinstance(error, TimeoutError)
                    and not error.retryable
                ):
                    raise
                entries.append(KeyAccess(key.hint, error=error))
            else:
                owner, access = key_access(response.json())
                entries.append(KeyAccess(key.hint, owner, access))
        self._lifetime.detected = True
        return tuple(entries)

    async def request_response(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> Response:
        self._check_open()
        opts = options or RequestOptions()
        if self._should_detect(request, opts):
            async with self._lifetime.async_detect:
                if not self._lifetime.detected:
                    await self._detect(opts)
        return await self._execute(request, opts, context or self.new_context())

    async def request(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> JsonValue:
        return (await self.request_response(request, options=options, context=context)).json()

    async def request_bytes(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> bytes:
        return (
            await self.request_response(
                replace(request, expect_json=False), options=options, context=context
            )
        ).body

    async def request_text(
        self,
        request: Request,
        *,
        options: RequestOptions | None = None,
        context: ExecutionContext | None = None,
    ) -> str:
        return (
            await self.request_response(
                replace(request, expect_json=False), options=options, context=context
            )
        ).text()

    async def page_responses(
        self,
        request: Request,
        *,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> AsyncIterator[Response]:
        """Iterate raw responses at a validated v2 page cursor."""
        page = 0
        opts = self._pagination_options(request, options)
        while max_pages is None or page < max_pages:
            response = await self.request_response(request, options=opts)
            yield response
            page += 1
            if max_pages is not None and page >= max_pages:
                return
            link = page_link(response.json(), backwards)
            if link is None:
                return
            request = self._executor.next_request(request, link)

    async def pages(
        self,
        request: Request,
        *,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> AsyncIterator[JsonValue]:
        async for response in self.page_responses(
            request,
            backwards=backwards,
            max_pages=max_pages,
            options=options,
        ):
            yield response.json()

    async def items(
        self,
        request: Request,
        *,
        field: str | None = None,
        limit: int | None = None,
        backwards: bool = False,
        max_pages: int | None = None,
        options: RequestOptions | None = None,
    ) -> AsyncIterator[JsonValue]:
        if limit is not None and limit <= 0:
            return
        async for page in self.pages(
            request,
            backwards=backwards,
            max_pages=max_pages,
            options=options,
        ):
            for item in page_items(page, field):
                yield item
                if limit is not None:
                    limit -= 1
                    if limit == 0:
                        return
