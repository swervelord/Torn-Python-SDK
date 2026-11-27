"""Scripted HTTP transports and clocks for deterministic consumer tests."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

import httpx

from .runtime import JsonValue, RuntimeConfig

if TYPE_CHECKING:
    from .client import AsyncTornClient, TornClient

TEST_API_KEY = "torn-test-key-0001"
ResponseFactory: TypeAlias = Callable[[httpx.Request], httpx.Response]
ScriptStep: TypeAlias = httpx.Response | ResponseFactory


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    key_hint: str | None


class MockTransport(httpx.MockTransport):
    """Serve a response script with HTTPX in synchronous or asynchronous tests."""

    def __init__(self, script: Iterable[ScriptStep] = ()) -> None:
        self._script = deque(script)
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        super().__init__(self._respond)

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        with self._lock:
            return tuple(self._requests)

    def enqueue(self, *steps: ScriptStep) -> None:
        with self._lock:
            self._script.extend(steps)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        key = auth.removeprefix("ApiKey ") if auth.startswith("ApiKey ") else ""

        def redact(value: str) -> str:
            return value.replace(key, "***") if key else value

        query = tuple(
            (redact(name), "***" if name.lower() == "key" else redact(value))
            for name, value in request.url.params.multi_items()
        )
        with self._lock:
            self._requests.append(
                RecordedRequest(
                    request.method,
                    redact(request.url.path),
                    query,
                    ("***" + key[-4:] if len(key) > 4 else "***") if key else None,
                )
            )
            if not self._script:
                raise AssertionError("Unexpected HTTP request after the mock script was exhausted")
            step = self._script.popleft()
        return step(request) if callable(step) else step

    def expect_request(
        self,
        index: int,
        path: str,
        *,
        query: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        method: str = "GET",
        key_hint: str | None = None,
    ) -> RecordedRequest:
        request = self.requests[index]
        assert request.method == method
        assert request.path == path
        if query is not None:
            expected = query.items() if isinstance(query, Mapping) else query
            assert sorted(request.query) == sorted(expected)
        if key_hint is not None:
            assert request.key_hint == key_hint
        return request

    def assert_done(self) -> None:
        with self._lock:
            assert not self._script, f"{len(self._script)} mock response(s) were never used"


class VirtualClock:
    """A clock whose sleeps advance time immediately."""

    def __init__(self, *, epoch: float = 1_700_000_000) -> None:
        self._epoch = epoch
        self._elapsed = 0.0
        self._sleeps: list[float] = []
        self._lock = threading.Lock()

    @property
    def sleeps(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._sleeps)

    def monotonic(self) -> float:
        with self._lock:
            return self._elapsed

    def time(self) -> float:
        return self._epoch + self.monotonic()

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._sleeps.append(seconds)
            self._elapsed += seconds


class AsyncVirtualClock:
    def __init__(self, *, epoch: float = 1_700_000_000) -> None:
        self._clock = VirtualClock(epoch=epoch)

    @property
    def sleeps(self) -> tuple[float, ...]:
        return self._clock.sleeps

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def time(self) -> float:
        return self._clock.time()

    async def sleep(self, seconds: float) -> None:
        self._clock.sleep(seconds)


def json_response(
    payload: JsonValue, *, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def torn_error(code: int, message: str = "Torn API error") -> httpx.Response:
    return json_response({"error": {"code": code, "error": message}})


def network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Scripted connection failure", request=request)


def load_fixture(path: str | Path) -> JsonValue:
    import json
    from typing import cast

    return cast(JsonValue, json.loads(Path(path).read_text(encoding="utf-8")))


def mock_sdk(
    script: Iterable[ScriptStep],
    *,
    api_key: str = TEST_API_KEY,
    clock: VirtualClock | None = None,
) -> tuple[TornClient, MockTransport]:
    from .client import TornClient

    transport = MockTransport(script)
    client = TornClient(
        api_key, transport=transport, config=RuntimeConfig(jitter=False),
        clock=clock or VirtualClock(),
    )
    return client, transport


def mock_async_sdk(
    script: Iterable[ScriptStep],
    *,
    api_key: str = TEST_API_KEY,
    clock: AsyncVirtualClock | None = None,
) -> tuple[AsyncTornClient, MockTransport]:
    from .client import AsyncTornClient

    transport = MockTransport(script)
    client = AsyncTornClient(
        api_key, transport=transport, config=RuntimeConfig(jitter=False),
        clock=clock or AsyncVirtualClock(),
    )
    return client, transport
