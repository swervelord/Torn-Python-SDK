"""Targeted runtime coverage beyond the cross-SDK request scenarios."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from torn_sdk.errors import (
    CancelledError,
    DecodeError,
    TimeoutError,
    TornError,
    TornErrorCode,
    TransportError,
    ValidationError,
    is_retryable,
)
from torn_sdk.runtime import (
    AsyncRuntime,
    Hooks,
    MemoryCache,
    Request,
    RequestOptions,
    RequestTrace,
    Route,
    Runtime,
    RuntimeConfig,
)


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_cache_is_key_specific_expires_and_is_shared_by_clones() -> None:
    clock = VirtualClock()
    cache = MemoryCache(clock=clock.monotonic)
    calls: list[str] = []
    traces: list[RequestTrace] = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        return httpx.Response(200, json={"send": len(calls)})

    client = Runtime(
        "key-one-1234",
        cache=cache,
        clock=clock,
        hooks=Hooks(on_response=traces.append),
        transport=httpx.MockTransport(send),
    )
    clone = client.clone()
    request = Request("/v2/user", cache_ttl=10)
    try:
        assert client.request(request) == {"send": 1}
        client.close()
        assert clone.request(request) == {"send": 1}
        assert traces[-1].cached
        assert clone.key_states()[0].remaining == 99
        override = RequestOptions(api_key="key-two-4567")
        assert clone.request(request, options=override) == {"send": 2}
        assert clone.request(request, options=override) == {"send": 2}
        clock.sleep(10)
        assert clone.request(request) == {"send": 3}
        assert calls == ["ApiKey key-one-1234", "ApiKey key-two-4567", "ApiKey key-one-1234"]
    finally:
        client.close()
        clone.close()


def test_malformed_json_is_not_cached_and_csv_is_a_separate_cache_entry() -> None:
    sends = 0

    def send(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, content=b"id,value\n1,2\n")

    with Runtime("key", cache=MemoryCache(), transport=httpx.MockTransport(send)) as client:
        request = Request("/v2/market/history")
        assert client.request_text(request) == "id,value\n1,2\n"
        assert client.request_bytes(request) == b"id,value\n1,2\n"
        for _ in range(2):
            with pytest.raises(DecodeError) as failure:
                client.request(request)
            assert failure.value.raw == b"id,value\n1,2\n"
        assert sends == 3


def test_blocked_route_fails_over_with_same_key_and_quota() -> None:
    attempts: list[tuple[str, str]] = []

    def blocked(request: httpx.Request) -> httpx.Response:
        attempts.append(("first", request.headers["authorization"]))
        return httpx.Response(200, json={"error": {"code": 8, "error": "blocked"}})

    def available(request: httpx.Request) -> httpx.Response:
        attempts.append(("second", request.headers["authorization"]))
        return httpx.Response(200, json={"ok": True})

    with Runtime(
        keys=("key-a", "key-b"),
        routes=(
            Route("first", httpx.MockTransport(blocked)),
            Route("second", httpx.MockTransport(available)),
        ),
        config=RuntimeConfig(max_attempts=1),
    ) as client:
        assert client.request(Request("/v2/user")) == {"ok": True}
        assert client.request(Request("/v2/user")) == {"ok": True}
        assert attempts == [
            ("first", "ApiKey key-a"),
            ("second", "ApiKey key-a"),
            ("second", "ApiKey key-b"),
        ]


def test_deadline_interrupts_retry_wait_and_attempt_timeout_is_typed() -> None:
    clock = VirtualClock()

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("socket timed out", request=request)

    config = RuntimeConfig(jitter=False)
    with Runtime(
        "key", clock=clock, config=config, transport=httpx.MockTransport(timeout)
    ) as client:
        with pytest.raises(TimeoutError) as failure:
            client.request(Request("/v2/user"), options=RequestOptions(deadline=0.25))
        assert clock.now == 0.25
        assert not is_retryable(failure.value)
        with pytest.raises(TimeoutError) as failure:
            client.request(Request("/v2/user"), options=RequestOptions(max_attempts=1))
        assert is_retryable(failure.value)
        assert isinstance(failure.value.__cause__, RuntimeError)


def test_url_credentials_and_transport_errors_are_redacted() -> None:
    secret = "VERYSECRET1234567"
    traces: list[RequestTrace] = []
    failures: list[TornError] = []

    def send(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"ApiKey {secret}"
        assert "key=" not in str(request.url)
        raise httpx.ConnectError(f"upstream echoed {secret}", request=request)

    request = Request(f"/user/?key={secret}&comment={secret}", operation=f"raw {secret}")
    config = RuntimeConfig(max_attempts=1)
    with Runtime(
        config=config,
        hooks=Hooks(on_request=traces.append, on_error=lambda _, error: failures.append(error)),
        transport=httpx.MockTransport(send),
    ) as client:
        with pytest.raises(TransportError) as caught:
            client.request(request)
        assert caught.value.__context__ is None
    representations = (request, traces, failures, caught.value.__cause__)
    assert all(secret not in repr(value) for value in representations)


def test_env_precedence_and_split_version_base_urls() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        host = "legacy.example.test" if request.url.path == "/user" else "v2.example.test"
        assert request.url.host == host
        assert request.headers["authorization"] == "ApiKey second"
        return httpx.Response(200, json=True)

    env = {
        "TORN_API_KEY": "unused",
        "TORN_API_KEYS": "second",
        "TORN_API_BASE_URL_V1": "https://legacy.example.test",
        "TORN_API_BASE_URL_V2": "https://v2.example.test/api/v2",
    }
    with Runtime.from_env(environ=env, transport=httpx.MockTransport(send)) as client:
        assert client.request(Request("/user")) is True
        assert client.request(Request("/v2/user")) is True
    with Runtime.from_env(
        environ={**env, "TORN_API_KEYS": " ", "TORN_API_KEY": " second "},
        transport=httpx.MockTransport(send),
    ) as client:
        assert client.request(Request("/v2/user")) is True
    with pytest.raises(ValidationError):
        Runtime.from_env(environ={"TORN_API_KEYS": " , "})


def test_detected_owner_shares_window_and_detected_access_is_applied() -> None:
    clock = VirtualClock()
    seen: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/key/info"):
            access = (
                "Limited Access"
                if request.headers["authorization"].endswith("second")
                else "Public Only"
            )
            return httpx.Response(
                200, json={"info": {"access": {"type": access}, "user": {"id": 123}}}
            )
        assert clock.now == 60
        assert request.headers["authorization"] == "ApiKey second"
        return httpx.Response(200, json={"done": True})

    with Runtime(
        keys=("first", "second"),
        transport=httpx.MockTransport(send),
        config=RuntimeConfig(key_per_minute=2),
        clock=clock,
        detect_key_owners=True,
        check_access=True,
    ) as client:
        assert client.request(Request("/v2/user", access="limited")) == {"done": True}
        assert tuple(state.remaining for state in client.key_states()) == (1, 1)
        assert tuple(state.owner for state in client.key_states()) == (123, 123)
        assert seen == ["/v2/key/info", "/v2/key/info", "/v2/user"]


def test_async_can_cancel_in_flight_using_task_or_request_options() -> None:
    async def run() -> None:
        started = asyncio.Event()
        released = asyncio.Event()
        cancelled = asyncio.Event()

        async def send(request: httpx.Request) -> httpx.Response:
            started.set()
            try:
                await released.wait()
            finally:
                cancelled.set()
            return httpx.Response(200, json={})

        async with AsyncRuntime("async", transport=httpx.MockTransport(send)) as client:
            request = Request("/v2/user")
            task = asyncio.create_task(client.request(request))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
            user_stop = asyncio.Event()
            started.clear()
            cancelled.clear()
            task = asyncio.create_task(
                client.request(request, options=RequestOptions(cancelled=user_stop.is_set))
            )
            await started.wait()
            user_stop.set()
            with pytest.raises(CancelledError):
                await task
            assert cancelled.is_set()

    asyncio.run(run())


def test_rerouting_uses_explicit_fallback_and_unknown_errors_are_open() -> None:
    visited: list[str] = []

    def send(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        if request.url.path.startswith("/v2"):
            return httpx.Response(200, json={"error": {"code": 22, "error": "v1"}})
        return httpx.Response(200, json={"ok": True})

    request = Request("/v2/user", fallback=Request("/user"))
    with Runtime("api-key", transport=httpx.MockTransport(send)) as client:
        assert client.request(request) == {"ok": True}
        assert visited == ["/v2/user", "/user"]
        with pytest.raises(TornError) as failure:
            client.request(replace(request, fallback=None))
        assert failure.value.code == TornErrorCode.ONLY_AVAILABLE_IN_API_V1
    assert not TornErrorCode(999).is_known
    assert int(TornErrorCode(999)) == 999
