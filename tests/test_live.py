"""Opt-in smoke tests and a full live contract sweep against the pinned API."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping

import pytest
from pydantic import BaseModel

from torn_sdk import OPERATIONS, ApiError, Operation, RuntimeConfig, TornClient, TornError
from torn_sdk._model import RequestParams

pytestmark = pytest.mark.live

SKIP_CODES = frozenset({3, 4, 6, 7, 9, 16, 19, 20, 21, 22, 23, 25, 26, 27, 28, 29, 30})
SEEDS: dict[str, int] = {
    "company.typeId": 1,
    "forum.categoryIds": 2,
    "market.id": 206,
    "market.propertyTypeId": 13,
    "racing.trackId": 6,
    "torn.crimeId": 1,
    "torn.logCategoryId": 1,
    "torn.shopId": 1,
    "torn.stockId": 1,
    "torn.typeId": 1,
    "user.crimeId": 1,
}
ARRAYS: dict[str, tuple[str, int | None]] = {
    "/torn/{ids}/honors": ("ids", 1),
    "/torn/{ids}/medals": ("ids", 1),
    "/torn/{ids}/items": ("ids", 206),
    "/torn/{ids}/itemstats": ("ids", 206),
    "/torn/{ids}/itemdetails": ("ids", None),
}
HARVEST: dict[str, tuple[tuple[str, str], ...]] = {
    "/key/info": (
        ("info.user.id", "user.id"),
        ("info.user.faction_id", "faction.id"),
        ("info.user.company_id", "company.id"),
    ),
    "/user/faction": (("faction.id", "faction.id"),),
    "/user/job": (("job.id", "company.id"),),
    "/faction/crimes": (("crimes.0.id", "faction.crimeId"),),
    "/faction/chains": (("chains.0.id", "faction.chainId"),),
    "/faction/raids": (("raids.0.id", "faction.raidWarId"),),
    "/faction/rankedwars": (("rankedwars.0.id", "faction.rankedWarId"),),
    "/faction/territorywars": (("territorywars.0.id", "faction.territoryWarId"),),
    "/forum/categories": (("categories.0.id", "forum.categoryIds"),),
    "/forum/{categoryIds}/threads": (("threads.0.id", "forum.threadId"),),
    "/user/properties": (("properties.0.id", "property.id"),),
    "/racing/races": (("races.0.id", "racing.raceId"),),
    "/racing/tracks": (("tracks.0.id", "racing.trackId"),),
    "/torn/elimination": (("elimination.0.id", "torn.id"),),
    "/user/trades": (("trades.0.id", "user.tradeId"),),
    "/user/attacks": (("attacks.0.code", "torn.log"),),
}


@pytest.fixture
def live_client() -> Iterator[TornClient]:
    key = (os.getenv("TORN_API_KEY") or os.getenv("TORN_LIVE_TEST_API_KEY") or "").strip()
    if not key:
        pytest.skip("TORN_API_KEY is required for live tests")
    with TornClient(key, config=RuntimeConfig(max_attempts=2)) as client:
        yield client


def _lookup(value: object, path: str) -> object:
    for part in path.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(value, list) and part.isdecimal() and len(value) > int(part):
            value = value[int(part)]
        else:
            return None
    return value


def _find_item_uid(value: object) -> int | None:
    nested: Iterator[object]
    if isinstance(value, Mapping):
        if type(uid := value.get("uid")) is int and uid > 0:
            return uid
        nested = iter(value.values())
    elif isinstance(value, list):
        nested = iter(value)
    else:
        return None
    for child in nested:
        if found := _find_item_uid(child):
            return found
    return None


def _inputs(
    operation: Operation[RequestParams, object], pool: Mapping[str, int | str]
) -> tuple[dict[str, object], str | None]:
    resource = operation.path.split("/")[1]
    values: dict[str, object] = {}
    for parameter in operation.parameters:
        if not parameter.required:
            continue
        name = parameter.name
        if name == ARRAYS.get(operation.path, (None, None))[0]:
            chosen = ARRAYS[operation.path][1] or pool.get("torn.itemUid")
            value: object = [chosen] if chosen else None
        elif name == "categoryIds":
            value = [pool["forum.categoryIds"]]
        elif parameter.choices:
            value = parameter.choices[0]
        else:
            value = pool.get(f"{resource}.{name}")
        if value is None:
            return values, name
        values[name] = value
    return values, None


def _harvest(operation: Operation[RequestParams, object], result: object, pool: dict[str, int | str]) -> None:
    if not isinstance(result, BaseModel):
        return
    payload = result.model_dump(mode="json", by_alias=True)
    for source, target in HARVEST.get(operation.path, ()):
        value = _lookup(payload, source)
        if (type(value) is int and value > 0) or (isinstance(value, str) and value):
            pool.setdefault(target, value)
    if "torn.itemUid" not in pool and (uid := _find_item_uid(payload)):
        pool["torn.itemUid"] = uid


def test_live_smoke(live_client: TornClient) -> None:
    info = live_client.get_key_info()
    assert info.info.user.id > 0
    basic = live_client.get_my_basic_information()
    assert basic.profile.id == info.info.user.id
    assert live_client.torn().timestamp().timestamp > 0


def test_live_sweep(live_client: TornClient) -> None:
    pool: dict[str, int | str] = dict(SEEDS)
    remaining = sorted(
        OPERATIONS.values(),
        key=lambda operation: (
            operation.path != "/key/info",
            "{" in operation.path,
            operation.path,
        ),
    )
    passed: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    deferred: list[Operation[RequestParams, object]] = []
    for pass_index in range(2):
        for operation in remaining:
            values, missing = _inputs(operation, pool)
            if missing:
                if pass_index == 0:
                    deferred.append(operation)
                else:
                    skipped.append(f"{operation.id} needs {missing}")
                continue
            time.sleep(0.7)
            try:
                result = live_client.exact(operation, operation.params.model_validate(values))
            except ApiError as error:
                if error.code == 21 and "cat" not in values:
                    category = next((p.choices for p in operation.parameters if p.name == "cat"), ())
                    if category:
                        try:
                            params = operation.params.model_validate({**values, "cat": category[0]})
                            time.sleep(0.7)
                            result = live_client.exact(operation, params)
                        except TornError as retry_error:
                            error_code = retry_error.code
                            if error_code is not None and error_code in SKIP_CODES:
                                skipped.append(f"{operation.id} returned Torn API code {int(error_code)}")
                            else:
                                failures.append(f"{operation.id} {retry_error}")
                        else:
                            passed.append(operation.id)
                            _harvest(operation, result, pool)
                        continue
                if error.code in SKIP_CODES and operation.path != "/key/info":
                    skipped.append(f"{operation.id} returned Torn API code {int(error.code)}")
                else:
                    failures.append(f"{operation.id} {error}")
            except TornError as error:
                failures.append(f"{operation.id} {error}")
            else:
                passed.append(operation.id)
                _harvest(operation, result, pool)
        remaining, deferred = deferred, []
    print(f"Live sweep: {len(passed)} decoded, {len(skipped)} skipped, {len(failures)} failed")
    for entry in skipped:
        print(f"  skipped {entry}")
    assert len(passed) + len(skipped) + len(failures) == len(OPERATIONS)
    assert passed and not failures, "\n".join(failures)
