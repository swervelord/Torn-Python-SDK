"""Transport-independent HTTP request execution and pagination."""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Literal, ParamSpec
from urllib.parse import SplitResult, parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit

from pydantic import JsonValue

from ._error_codes import HTTP_RETRY_STATUSES
from ._runtime_state import Pool, Reservation, Slot
from ._runtime_types import (
    Cache,
    CacheEntry,
    Hooks,
    Key,
    QueryValue,
    Request,
    RequestOptions,
    RequestTrace,
    Response,
    key_hint,
)
from .errors import (
    AccessLevel,
    ApiError,
    CancelledError,
    HttpError,
    TimeoutError,
    TornError,
    TransportError,
    ValidationError,
)

_V2 = re.compile(r"^/v2(/|$)")
_METHOD = re.compile(r"^[A-Z]+$")
_ACCESS_TYPES: dict[str, AccessLevel] = {
    "public": "public",
    "minimal": "minimal",
    "limited": "limited",
    "full": "full",
    "custom": "custom",
}
_HookArgs = ParamSpec("_HookArgs")


def _notify(
    hook: Callable[_HookArgs, None] | None, /, *args: _HookArgs.args, **kwargs: _HookArgs.kwargs
) -> None:
    if hook is not None:
        with suppress(Exception):
            hook(*args, **kwargs)


class ExecutionContext:
    """Captures redacted traces and can pin subsequent requests to the last key."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._traces: list[RequestTrace] = []
        self._last_key: Key | None = None
        self._pinned_key: Key | None = None

    @property
    def traces(self) -> tuple[RequestTrace, ...]:
        return tuple(self._traces)

    def pin_key(self) -> ExecutionContext:
        pinned = ExecutionContext(self._pool)
        pinned._traces = self._traces
        pinned._last_key = pinned._pinned_key = self._last_key
        return pinned


def _safe_split(url: str) -> SplitResult:
    try:
        result = urlsplit(url)
        result.port
    except ValueError:
        pass
    else:
        no_credentials = result.username is None and result.password is None
        if (
            no_credentials
            and not result.fragment
            and all(ord(char) >= 32 and char != chr(92) for char in url)
        ):
            return result
    raise ValidationError("Invalid Torn request URL")


def _origin(url: SplitResult) -> tuple[str, str | None, int]:
    return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)


def _query_value(value: QueryValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    return ",".join(str(v).lower() if isinstance(v, bool) else str(v) for v in value)


def _request_query(request: Request, split: SplitResult) -> list[tuple[str, str]]:
    query = parse_qsl(split.query, keep_blank_values=True)
    if request.query is not None:
        entries = request.query.items() if isinstance(request.query, Mapping) else request.query
        additions = [(name, _query_value(value)) for name, value in entries]
        names = {name for name, _ in additions}
        query = [(name, value) for name, value in query if name not in names]
        query.extend((name, value) for name, value in additions if value is not None)
    return query


def _query_key(query: Iterable[tuple[str, str]]) -> str | None:
    return next((v for k, v in query if k.casefold() == "key"), None)


def page_link(payload: JsonValue, backwards: bool) -> str | None:
    metadata = payload.get("_metadata") if isinstance(payload, dict) else None
    links = metadata.get("links") if isinstance(metadata, dict) else None
    link = links.get("prev" if backwards else "next") if isinstance(links, dict) else None
    return link if isinstance(link, str) and link else None


def page_items(payload: JsonValue, field_name: str | None) -> list[JsonValue]:
    if isinstance(payload, list) and field_name is None:
        return payload
    if not isinstance(payload, dict):
        return []
    if field_name is not None:
        value = payload.get(field_name)
        return value if isinstance(value, list) else []
    members = [value for name, value in payload.items() if name != "_metadata"]
    arrays = [value for value in members if isinstance(value, list)]
    if len(arrays) == 1:
        return arrays[0]
    if len(members) == 1 and isinstance(members[0], dict):
        items = members[0].get("items")
        if isinstance(items, list):
            return items
    return []


def key_access(payload: JsonValue) -> tuple[int | None, AccessLevel | None]:
    info = payload.get("info") if isinstance(payload, dict) else None
    user = info.get("user") if isinstance(info, dict) else None
    access = info.get("access") if isinstance(info, dict) else None
    owner = user.get("id") if isinstance(user, dict) else None
    label = access.get("type") if isinstance(access, dict) else None
    first = label.strip().split(maxsplit=1) if isinstance(label, str) else []
    return (
        owner if isinstance(owner, int) and not isinstance(owner, bool) else None,
        _ACCESS_TYPES.get(first[0].lower()) if first else None,
    )


@dataclass(frozen=True)
class Prepared:
    request: Request = field(repr=False)
    url: str = field(repr=False)
    operation: str
    version: Literal["v1", "v2"]


@dataclass(frozen=True)
class Send:
    prepared: Prepared
    key: Key
    route: str
    timeout: float


@dataclass(frozen=True)
class WireResponse:
    status: int
    body: bytes = field(repr=False)
    encoding: str = field(default="utf-8", repr=False)
    retry_after: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Wait:
    delay: float


Flow = Generator[Send | Wait, WireResponse | TornError | None, Response]


class Executor:
    def __init__(
        self,
        pool: Pool,
        base_url: str,
        hooks: Hooks,
        cache: Cache | None,
        *,
        base_url_v1: str | None = None,
        base_url_v2: str | None = None,
    ) -> None:
        self.pool = pool
        self.config = pool.config
        self.clock = pool.clock
        self.hooks = hooks
        self.cache = cache
        self.base_url = base_url.rstrip("/")
        self.base_urls = {
            "v1": (base_url_v1 or self.base_url).rstrip("/"),
            "v2": (base_url_v2 or f"{self.base_url}/v2").rstrip("/"),
        }
        for url in self.base_urls.values():
            base = _safe_split(url)
            if base.scheme not in {"http", "https"} or not base.hostname or base.query:
                raise ValidationError("Invalid Torn API base URL")
        self.pagination_base = _safe_split(self.base_urls["v2"])

    def prepare(self, request: Request) -> tuple[Prepared, str | None]:
        parts = _safe_split(request.path)
        path = "/" + parts.path.lstrip("/")
        if parts.netloc or parts.scheme or request.path.startswith("//"):
            raise ValidationError("Torn request paths must be relative to the API origin")
        if not _METHOD.fullmatch(request.method):
            raise ValidationError("Invalid Torn request method")
        query = _request_query(request, parts)
        auth = _query_key(query)
        clean_query = urlencode([(k, v) for k, v in query if k.casefold() != "key"])
        version: Literal["v1", "v2"] = "v2" if _V2.match(path) else "v1"
        suffix = path[3:] if version == "v2" else path
        wire_url = self.base_urls[version] + suffix + (f"?{clean_query}" if clean_query else "")
        return Prepared(
            request, wire_url, request.operation or f"{request.method} {path}", version
        ), auth

    def next_request(self, first: Request, link: str) -> Request:
        parts = _safe_split(link)
        prefix = self.pagination_base.path.rstrip("/")
        if not parts.netloc or not parts.scheme or _origin(parts) != _origin(self.pagination_base):
            raise ValidationError("Torn pagination link points outside the API origin")
        if prefix and parts.path != prefix and not parts.path.startswith(prefix + "/"):
            raise ValidationError("Torn pagination link points outside the API base path")
        path = "/v2" + parts.path[len(prefix) :]
        if any(segment in {".", ".."} for segment in unquote(path).split("/")):
            raise ValidationError("Torn pagination link points outside the v2 API path")
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name.casefold() != "key"
        ]
        return replace(first, path=path, query=query, fallback=None)

    def redact(
        self,
        text: str,
        options: RequestOptions | None = None,
        request: Request | None = None,
        extra: Key | None = None,
    ) -> str:
        values = [key._reveal() for key in self.pool.keys]
        if options is not None and options.api_key:
            values.append(options.api_key.strip())
        if request is not None:
            try:
                supplied = _query_key(_request_query(request, _safe_split(request.path)))
            except ValidationError:
                supplied = None
            if supplied:
                values.append(supplied)
        if extra is not None:
            values.append(extra._reveal())
        replacements: dict[str, str] = {}
        for value in values:
            if value:
                encoded = quote(value, safe="")
                replacements.update(
                    (candidate, key_hint(value))
                    for candidate in (value, encoded, quote_plus(value, safe=""))
                )
        if not replacements:
            return text
        pattern = "|".join(
            re.escape(value) for value in sorted(replacements, key=len, reverse=True)
        )
        return re.sub(pattern, lambda match: replacements[match[0]], text)

    def check(self, options: RequestOptions) -> None:
        if options.cancelled is not None and options.cancelled():
            raise CancelledError("Torn request cancelled")
        if options.deadline is not None and options.deadline <= self.clock.monotonic():
            raise TimeoutError("Torn request deadline elapsed")

    def timeout(self, options: RequestOptions) -> float:
        self.check(options)
        attempt_timeout = options.timeout if options.timeout is not None else self.config.timeout
        if options.deadline is not None:
            attempt_timeout = min(attempt_timeout, options.deadline - self.clock.monotonic())
        return max(attempt_timeout, 0.000001)

    def wait_length(self, delay: float, options: RequestOptions) -> float:
        self.check(options)
        if options.deadline is not None:
            delay = min(delay, options.deadline - self.clock.monotonic())
        if options.cancelled is not None:
            delay = min(delay, 0.05)
        return max(0.0, delay)

    def _retry_after(self, value: str | None) -> float | None:
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - self.clock.time()
            except (ValueError, OverflowError, OSError):
                return None
        return min(max(0.0, delay), self.config.retry_after_cap) if math.isfinite(delay) else None

    def _failure(
        self,
        wire: WireResponse,
        trace: RequestTrace,
        options: RequestOptions,
        prepared: Prepared,
        key: Key,
    ) -> TornError | None:
        if wire.body.lstrip().startswith(b"{"):
            try:
                envelope = json.loads(wire.body)
            except (ValueError, UnicodeError):
                envelope = None
            api_error = envelope.get("error") if isinstance(envelope, dict) else None
            if isinstance(api_error, dict):
                api_code = api_error.get("code")
                if isinstance(api_code, int) and not isinstance(api_code, bool):
                    message = api_error.get("error", api_error.get("message", ""))
                    return ApiError(
                        api_code,
                        self.redact(str(message), options, prepared.request, key),
                        key_hint=trace.key_hint,
                        operation=trace.operation,
                    )
        if wire.status < 200 or wire.status >= 300:
            return HttpError(wire.status, retry_after=self._retry_after(wire.retry_after))
        return None

    def _cache_key(self, prepared: Prepared, slot: Slot, route: str | None) -> str:
        media = "json" if prepared.request.expect_json else "bytes"
        parts = (prepared.request.method, prepared.url, slot.id, route or "", media)
        return sha256("\0".join(parts).encode()).hexdigest()

    def _cache_ttl(self, request: Request, options: RequestOptions) -> float:
        if options.no_cache or self.cache is None or request.method != "GET":
            return 0
        return request.cache_ttl if request.cache_ttl is not None else self.config.cache_ttl

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        ceiling = min(self.config.base_delay * 2 ** min(attempt - 1, 1023), self.config.max_delay)
        backoff = random.uniform(0, ceiling) if self.config.jitter else ceiling
        return max(backoff, retry_after or 0)

    def flow(self, request: Request, options: RequestOptions, context: ExecutionContext) -> Flow:
        if context._pool is not self.pool:
            raise ValidationError("Torn execution context belongs to another client")
        if options.route is not None and options.route not in self.pool.route_windows:
            raise ValidationError("Unknown Torn egress route")
        if options.timeout is not None and (
            not math.isfinite(options.timeout) or options.timeout <= 0
        ):
            raise ValidationError("Torn request timeout must be positive")
        if options.deadline is not None and not math.isfinite(options.deadline):
            raise ValidationError("Torn request deadline must be finite")
        max_attempts = (
            options.max_attempts if options.max_attempts is not None else self.config.max_attempts
        )
        if max_attempts < 1:
            raise ValidationError("Torn request attempts must be positive")

        prepared, query_key = self.prepare(request)
        explicit_value = options.api_key if options.api_key is not None else query_key
        pinned = context._pinned_key
        explicit_key = Key(explicit_value) if explicit_value is not None else pinned
        explicit = self.pool.explicit_slot(explicit_key) if explicit_key is not None else None
        preferred: Slot | None = None
        skipped: set[str] = set()
        skipped_routes: set[str] = set()
        attempts: dict[str, int] = {}
        rate_limited: set[str] = set()
        last: ApiError | None = None
        trace: RequestTrace | None = None
        rerouted = False
        sends = 0

        while True:
            self.check(options)
            operation = self.redact(prepared.operation, options, prepared.request, explicit_key)
            safe_url = self.redact(prepared.url, options, prepared.request, explicit_key)
            ttl = self._cache_ttl(prepared.request, options)
            try:
                if ttl > 0 and self.cache is not None:
                    index, slot = self.pool.peek_key(
                        explicit,
                        preferred,
                        skipped,
                        operation,
                        prepared.request.access,
                        last,
                    )
                    cached = self.cache.get(self._cache_key(prepared, slot, options.route))
                    if cached is not None:
                        chosen = explicit_key if explicit_key is not None else self.pool.keys[index]
                        context._last_key = chosen
                        trace = RequestTrace(
                            operation,
                            safe_url,
                            sends,
                            cached.status,
                            0,
                            chosen.hint,
                            self.redact(options.route or "", options, prepared.request, chosen),
                            True,
                            prepared.version,
                        )
                        context._traces.append(trace)
                        _notify(self.hooks.on_response, trace)
                        return Response(cached.body, trace, cached.encoding)
                reservation = self.pool.reserve(
                    explicit=explicit,
                    preferred=preferred,
                    skipped=skipped,
                    pinned_route=options.route,
                    skipped_routes=skipped_routes,
                    operation=operation,
                    access=prepared.request.access,
                    last=last,
                )
            except TornError as error:
                if trace is not None:
                    _notify(self.hooks.on_error, trace, error)
                raise
            if isinstance(reservation, float):
                yield Wait(reservation)
                continue
            key = explicit_key if explicit_key is not None else self.pool.keys[reservation.index]
            context._last_key = key
            slot = reservation.slot
            sends += 1
            started = self.clock.monotonic()
            trace = RequestTrace(
                operation,
                safe_url,
                sends,
                None,
                0,
                key.hint,
                self.redact(reservation.route, options, prepared.request, key),
                False,
                prepared.version,
            )
            _notify(self.hooks.on_request, trace)
            wire = yield Send(prepared, key, reservation.route, self.timeout(options))
            if wire is None:
                raise RuntimeError("Torn executor resumed without a transport result")
            trace = replace(
                trace,
                duration=self.clock.monotonic() - started,
                status=wire.status if isinstance(wire, WireResponse) else None,
            )
            context._traces.append(trace)
            if isinstance(wire, WireResponse):
                _notify(self.hooks.on_response, trace)
                failure = self._failure(wire, trace, options, prepared, key)
                if failure is None:
                    response = Response(wire.body, trace, wire.encoding)
                    if prepared.request.expect_json:
                        try:
                            response.json()
                        except TornError as error:
                            _notify(self.hooks.on_error, trace, error)
                            raise
                    if ttl > 0 and self.cache is not None:
                        cache_key = self._cache_key(prepared, slot, options.route)
                        self.cache.set(
                            cache_key, CacheEntry(wire.body, wire.status, wire.encoding), ttl
                        )
                    return response
            else:
                failure = wire
            delay = self._recovery_delay(
                failure,
                wire,
                reservation,
                attempts,
                max_attempts,
            )
            if delay is not None:
                preferred = slot
            if isinstance(failure, ApiError):
                last = failure
                action = failure.code.action
                if action == "rotate_key":
                    skipped.add(slot.id)
                    preferred = None
                    delay = 0.0
                elif action == "wait_rate_window":
                    preferred = None
                    delay = 0.0
                    if slot.id in rate_limited:
                        attempt = attempts.get(slot.id, 0) + 1
                        attempts[slot.id] = attempt
                        if attempt >= max_attempts:
                            delay = None
                    rate_limited.add(slot.id)
                elif action == "rotate_route":
                    skipped_routes.add(reservation.route)
                    preferred = slot
                    delay = 0.0
                elif action == "reroute_version" and not rerouted and prepared.request.fallback:
                    target, _ = self.prepare(prepared.request.fallback)
                    if target.version == ("v1" if int(failure.code) == 22 else "v2"):
                        prepared = target
                        preferred = slot
                        rerouted = True
                        delay = 0.0
            if delay is None:
                _notify(self.hooks.on_error, trace, failure)
                raise failure
            _notify(self.hooks.on_retry, trace, failure, delay)
            if delay > 0:
                yield Wait(delay)

    def _recovery_delay(
        self,
        failure: TornError,
        wire: WireResponse | TornError,
        reservation: Reservation,
        attempts: dict[str, int],
        max_attempts: int,
    ) -> float | None:
        retry_after = None
        if isinstance(failure, ApiError):
            self.pool.reject(reservation, failure)
            if failure.code.action != "retry_backoff":
                return None
            if isinstance(wire, WireResponse):
                retry_after = self._retry_after(wire.retry_after)
        elif isinstance(failure, HttpError) and failure.status in HTTP_RETRY_STATUSES:
            retry_after = failure.retry_after
        elif not (
            isinstance(failure, TransportError)
            or isinstance(failure, TimeoutError)
            and failure.retryable
        ):
            return None
        slot_id = reservation.slot.id
        attempt = attempts.get(slot_id, 0) + 1
        attempts[slot_id] = attempt
        return self._delay(attempt, retry_after) if attempt < max_attempts else None
