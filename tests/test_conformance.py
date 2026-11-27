"""Run every shared SDK conformance scenario through the Python runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from torn_sdk._client_support import operation_request
from torn_sdk._decode import decode_response
from torn_sdk.errors import (
    ApiError,
    CancelledError,
    DecodeError,
    HttpError,
    NoUsableKeysError,
    TornError,
    TransportError,
    ValidationError,
)
from torn_sdk.operations import OPERATIONS
from torn_sdk.planner import execute_plan, plan_selections
from torn_sdk.runtime import Key, Request, Route, Runtime, RuntimeConfig

_FIXTURES = Path(__file__).resolve().parents[1] / "spec" / "shared" / "conformance"
_SCENARIOS = [
    scenario
    for fixture in sorted(_FIXTURES.glob("*.json"))
    for scenario in json.loads(fixture.read_text(encoding="utf-8"))["scenarios"]
]
_ERRORS = {
    "api": ApiError, "cancelled": CancelledError, "decode": DecodeError,
    "http": HttpError, "no_usable_keys": NoUsableKeysError,
    "transport": TransportError, "validation": ValidationError,
}


class _VirtualClock:
    def __init__(self, start_ms: int) -> None:
        self.now = start_ms / 1000

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _query_text(value: object) -> str:
    if isinstance(value, list):
        return ",".join(_query_text(item) for item in value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _dotted(value: Any, path: str) -> Any:
    for segment in path.split("."):
        value = value[segment] if isinstance(value, dict) else getattr(value, segment)
    return value


def test_shared_scenarios_are_present_and_unique() -> None:
    ids = [scenario["id"] for scenario in _SCENARIOS]
    assert len(ids) >= 32
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[case["id"] for case in _SCENARIOS])
def test_shared_conformance(scenario: dict[str, Any]) -> None:
    config = scenario["config"]
    expected = scenario["expect"]
    request = scenario["request"]
    clock = _VirtualClock(scenario.get("clock", {}).get("startMs", 0))
    start = clock.monotonic()
    sends = 0
    exchanges = scenario["exchanges"]

    def send(wire: httpx.Request) -> httpx.Response:
        nonlocal sends
        assert sends < len(exchanges), f"unexpected HTTP send to {wire.url.path}"
        exchange = exchanges[sends]
        sends += 1
        want = exchange["expect"]
        assert wire.method == want["method"]
        assert wire.url.host == "api.torn.com"
        assert wire.url.scheme == "https"
        assert wire.url.path == want["path"]
        assert wire.headers["Authorization"] == f"ApiKey {want['auth']}"
        assert clock.monotonic() * 1000 >= want.get("atMs", 0)
        for name, value in want.get("query", {}).items():
            assert ",".join(wire.url.params.get_list(name)) == _query_text(value)
        for name in want.get("absentQuery", ()):
            assert name not in wire.url.params
        response = exchange["respond"]
        if response.get("networkError"):
            raise httpx.ConnectError("scripted connection error", request=wire)
        body = response.get("body", "")
        payload = {"content": body} if isinstance(body, str) else {"json": body}
        return httpx.Response(response.get("status", 200), headers=response.get("headers", {}), **payload)

    mock = httpx.MockTransport(send)
    owners = {
        key: str(number) for number, members in enumerate(config.get("keyGroups", ()))
        for key in members
    }
    keys = [Key(key, owner=owners.get(key), access=config.get("keyAccess", {}).get(key)) for key in config["keys"]]
    runtime_config = RuntimeConfig(
        max_attempts=config.get("maxAttempts", RuntimeConfig().max_attempts),
        key_per_minute=config.get("perKeyPerMinute", RuntimeConfig().key_per_minute),
        jitter=config.get("jitter") != "none",
    )
    runtime = Runtime(
        keys=keys, config=runtime_config, transport=mock, clock=clock,
        routes=tuple(Route(name, transport=mock) for name in config.get("routes", ())),
        check_access=config.get("checkAccess", False),
    )
    for preload in scenario.get("preload", ()):
        runtime.preload_budget(preload["key"], [stamp / 1000 for stamp in preload["sendsAtMs"]])

    data: Any = None
    typed: Any = None
    decode_error: DecodeError | None = None
    failure: TornError | None = None
    report = None
    page_count = 0
    try:
        kind = request["kind"]
        params = request.get("params", {})
        if kind == "selections":
            names = request["selections"]
            use_types = expected.get("typedDecode") is not None
            plan = plan_selections(request["resource"], names, id=request.get("id"), query=params)
            batch = execute_plan(runtime, plan, typed=use_types)
            report, data = batch.report, batch.data
            if use_types:
                typed = batch.results[names[0]].value
                typed_failure = batch.results[names[0]].error
                if isinstance(typed_failure, DecodeError):
                    decode_error = typed_failure
                elif typed_failure is not None:
                    raise typed_failure
            else:
                batch.raise_for_errors()
        elif kind == "paginate":
            for page in runtime.pages(Request(f"/v2{request['path']}", params), max_pages=request.get("maxPages")):
                page_count += 1
                data = page
        elif kind == "operation":
            op = OPERATIONS[request["operationId"]]
            req = operation_request(op, op.params.model_validate(params), None)
            response = runtime.request_response(req)
            data = response.json()
            if expected.get("typedDecode") is not None:
                try:
                    typed = decode_response(op.response, data, operation=op.id, raw=response.body)
                except DecodeError as issue:
                    decode_error = issue
        elif kind == "raw":
            data = runtime.request(Request(request["path"], params))
        else:
            pytest.fail(f"unsupported conformance request kind {kind!r}")
    except TornError as error:
        failure = error
    finally:
        states = runtime.key_states()
        runtime.close()

    assert sends == len(exchanges) == expected["sends"]
    if expected["outcome"] == "ok":
        assert failure is None
    else:
        error_spec = expected["error"]
        assert isinstance(failure, _ERRORS[error_spec["kind"]])
        if "code" in error_spec:
            assert failure.code == error_spec["code"]
        if "requiredAccess" in error_spec:
            assert getattr(failure, "required", None) == error_spec["requiredAccess"]
    for path, value in expected.get("data", {}).items():
        assert _dotted(data, path) == value
    for key, state in expected.get("keyStates", {}).items():
        index = config["keys"].index(key)
        assert next(entry.state for entry in states if entry.index == index) == state
    elapsed = round((clock.monotonic() - start) * 1000, 6)
    assert expected.get("minElapsedMs", 0) <= elapsed <= expected.get("maxElapsedMs", float("inf"))
    if "pages" in expected:
        assert page_count == expected["pages"]
    if expected.get("typedDecode") == "ok":
        assert decode_error is None
        assert typed is not None
    elif expected.get("typedDecode") == "error":
        assert decode_error is not None
    if path := expected.get("unknownEnum"):
        decoded = _dotted(typed, path)
        assert decoded.is_known is False
        assert decoded.value == _dotted(data, path)
    if secret := expected.get("secretNotIn"):
        for value in (keys, states, runtime, runtime_config, report, failure):
            assert secret not in repr(value)
            assert secret not in str(value)
            assert secret not in format(value)
