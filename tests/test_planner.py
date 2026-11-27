"""Routing, compatibility, and typed partial selection results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from torn_sdk.errors import ApiError, DecodeError, ValidationError
from torn_sdk.models import UserBasicResponse
from torn_sdk.operations import OPERATIONS
from torn_sdk.planner import batch, batch_async, execute_plan, plan_selections
from torn_sdk.runtime import AsyncRuntime, Runtime
from torn_sdk.selections import SELECTIONS, user
from torn_sdk.testing import MockTransport, json_response, torn_error

_SPEC = Path(__file__).resolve().parents[1] / "spec"
_BASIC = {
    "profile": {
        "id": 1, "name": "Test", "level": 10, "gender": "Male",
        "status": {"description": "Okay", "details": None, "state": "Okay", "color": "green", "until": None},
    },
}
_KEY_A = "planner-test-key-0001"
_KEY_B = "planner-test-key-0002"


def test_plan_groups_generic_and_separates_csv_and_v1() -> None:
    plan = plan_selections(
        " FACTION ", [" Timestamp ", "basic", "basic", "snapshot", "armorynews"],
        query=[("comment", "batch check"), ("future", "one"), ("future", "two")],
    )
    assert [call.url_path for call in plan.calls] == ["/v2/faction", "/v2/faction/snapshot", "/faction/"]
    combined, csv, v1 = plan.calls
    assert combined.selections == ("timestamp", "basic")
    assert [call.selections for call in combined.split] == [("timestamp",), ("basic",)]
    assert csv.media == "csv"
    assert v1.selections == ("armorynews",)
    for call in plan.calls:
        assert tuple(call.query)[:3] == (("comment", "batch check"), ("future", "one"), ("future", "two"))


def test_plan_path_domains_and_required_direct_queries() -> None:
    plan = plan_selections("company", ["companies", "timestamp"], path_args={"typeId": 5})
    assert [call.url_path for call in plan.calls] == ["/v2/company", "/v2/company/5/companies"]
    assert "id" not in dict(plan.calls[0].query)
    with pytest.raises(ValidationError, match="incompatible target ID domains"):
        plan_selections("user", ["basic", "crimes"], id=92)
    with pytest.raises(ValidationError, match="requires target"):
        plan_selections("user", ["crimes"])
    with pytest.raises(ValidationError, match="requires query parameter 'log'"):
        plan_selections("torn", ["attacklog"])
    call = plan_selections("torn", ["attacklog"], query={"log": "a" * 32}).calls[0]
    assert call.url_path == "/v2/torn/attacklog"
    assert dict(call.query) == {"log": "a" * 32}
    call = plan_selections("torn", ["items"], path_args={"itemIds": [1, 2]}, force_direct=True).calls[0]
    assert call.url_path == "/v2/torn/1,2/items"
    call = plan_selections("torn", ["territory"], path_args={"territoryIds": ["AAB", "AAC"]}, force_direct=True).calls[0]
    assert call.url_path == "/v2/torn/territory"
    assert dict(call.query)["ids"] == "AAB,AAC"


def test_full_explicit_v1_catalog_can_be_planned() -> None:
    manifest = json.loads((_SPEC / "routing-manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["v1_catalog"]) == 184
    for entry in manifest["v1_catalog"]:
        resource, name = entry["resource"], entry["name"]
        call, = plan_selections(resource, [name], id=12, version="v1").calls
        assert call.url_path == f"/{resource}/12"
        assert call.selections == (name,)


def test_every_declared_selection_has_a_plan() -> None:
    for (resource, _), selection in SELECTIONS.items():
        endpoint = min(selection.endpoints, key=lambda item: len(item.required), default=None)
        path_args = {name: "123" for name in endpoint.required} if endpoint else {}
        query = {
            parameter.name: parameter.choices[0] if parameter.choices else "123"
            for parameter in OPERATIONS[endpoint.id].parameters
            if parameter.required and parameter.location == "query"
        } if endpoint else {}
        call, = plan_selections(resource, [selection], path_args=path_args, query=query).calls
        assert call.resource == resource
        assert call.selections == (selection.name,)


def test_unknown_selection_and_query_are_forwarded_as_sent() -> None:
    plan = plan_selections("user", ["futureselection"], query=[("future", "a"), ("future", "b")])
    mock = MockTransport([json_response({"futureselection": {"answer": 42}})])
    runtime = Runtime(_KEY_A, transport=mock)
    try:
        result = execute_plan(runtime, plan).raise_for_errors()
    finally:
        runtime.close()
    assert result.data == {"futureselection": {"answer": 42}}
    assert mock.requests[0].query.count(("future", "a")) == 1
    assert mock.requests[0].query.count(("future", "b")) == 1


def test_wrong_fields_individual_split_uses_same_key_and_preserves_success() -> None:
    mock = MockTransport([torn_error(4), torn_error(6), json_response(_BASIC)])
    runtime = Runtime(keys=[_KEY_A, _KEY_B], transport=mock)
    try:
        result = batch(runtime, user.bars, user.basic)
    finally:
        runtime.close()
    assert [entry.key_hint for entry in mock.requests] == ["***0001", "***0001", "***0001"]
    assert [call.selections for call in result.report.calls] == [("bars", "basic"), ("bars",), ("basic",)]
    assert isinstance(result.get(user.basic), UserBasicResponse)
    assert result.get(user.basic).profile.name == "Test"
    with pytest.raises(ApiError) as caught:
        result.get(user.bars)
    assert int(caught.value.code) == 6


def test_combined_typed_errors_have_original_body_and_do_not_drop_other_models() -> None:
    payload = {**_BASIC, "bars": "not a bars object"}
    mock = MockTransport([json_response(payload)])
    runtime = Runtime(_KEY_A, transport=mock)
    try:
        result = batch(runtime, user.basic, user.bars, strict=True)
    finally:
        runtime.close()
    assert result.get(user.basic).profile.id == 1
    assert result.data == payload
    assert "bars" not in (result.get(user.basic).model_extra or {})
    with pytest.raises(DecodeError) as caught:
        result.get(user.bars)
    assert caught.value.operation == "user.bars"
    assert caught.value.path == "$.bars"
    assert json.loads(caught.value.raw) == payload


def test_legacy_query_key_moves_to_header_and_cannot_escape_through_decode_errors() -> None:
    def send(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"ApiKey {_KEY_B}"
        assert "Key" not in request.url.params and "key" not in request.url.params
        return json_response({"profile": _KEY_B})

    mock = MockTransport([send])
    runtime = Runtime(_KEY_A, transport=mock)
    plan = plan_selections("user", [user.basic], query={"Key": _KEY_B})
    try:
        result = execute_plan(runtime, plan, typed=True)
    finally:
        runtime.close()
    error = result.errors["basic"]
    assert isinstance(error, DecodeError)
    assert error.path == "$.profile"
    assert _KEY_B in error.raw.decode()
    assert "***0002" in str(error)
    for value in (plan, asdict(plan), result, error, result.report, asdict(result.report)):
        assert _KEY_B not in repr(value)


def test_version_migration_only_runs_once_in_either_direction() -> None:
    mock = MockTransport([torn_error(23), torn_error(22)])
    runtime = Runtime(_KEY_A, transport=mock)
    try:
        plan = plan_selections("faction", ["armorynews"])
        result = execute_plan(runtime, plan)
    finally:
        runtime.close()
    assert [entry.path for entry in mock.requests] == ["/faction/", "/v2/faction"]
    assert result.errors["armorynews"].code == 22


def test_csv_and_json_selection_can_share_one_batch() -> None:
    async def run() -> None:
        mock = MockTransport([json_response(_BASIC), httpx.Response(200, text="id,name\n1,Test\n")])
        runtime = AsyncRuntime(_KEY_A, transport=mock)
        try:
            result = await batch_async(runtime, user.basic, user.snapshot)
        finally:
            await runtime.aclose()
        assert result.get(user.basic).profile.name == "Test"
        assert result.get(user.snapshot) == "id,name\n1,Test\n"
        assert result.results["snapshot"].raw == "id,name\n1,Test\n"
        assert result.data == {**_BASIC, "snapshot": "id,name\n1,Test\n"}
        assert result.errors == {}
        assert [entry.path for entry in mock.requests] == ["/v2/user", "/v2/user/snapshot"]

    asyncio.run(run())


def test_future_selection_with_non_object_json_is_preserved() -> None:
    mock = MockTransport([json_response([1, "future", None])])
    runtime = Runtime(_KEY_A, transport=mock)
    try:
        plan = plan_selections("user", ["futureselection"])
        result = execute_plan(runtime, plan).raise_for_errors()
    finally:
        runtime.close()
    assert result.results["futureselection"].raw == [1, "future", None]
    assert result.data == {"futureselection": [1, "future", None]}
