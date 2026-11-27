# Torn Python SDK

Typed synchronous and asynchronous clients for the official [Torn API](https://www.torn.com/swagger.php), with planner-aware selection batching, v1 routing, and an HTTPX transport.

The pinned Torn API **6.21.0** contract includes **248 exact operations**, **772 component schemas**, **3 CSV endpoints**, and typed page and item iterators across **64 operations**. The SDK includes 211 modeled selections, 19 additional raw legacy selections, and two selection aliases. Its [shared parity contract](spec/shared/PARITY_SPEC.md) and vendored inputs come from the [Torn Zig SDK snapshot](https://github.com/swervelord/Torn-Zig-SDK/tree/f5f9233/spec).

## Install

Python **3.10+**.

```sh
git clone https://github.com/swervelord/Torn-Python-SDK.git
cd Torn-Python-SDK
python -m pip install .
```

Import the package as `torn_sdk`.

## Quickstart

Set `TORN_API_KEY` in your environment.

```python
from torn_sdk import TornClient

with TornClient.from_env() as torn:
    me = torn.get_my_basic_information()
    print(me.profile.name, me.profile.level)
```

```python
from torn_sdk import AsyncTornClient

async def headlines() -> list[str]:
    async with AsyncTornClient.from_env() as torn:
        news = await torn.torn().headlines()
        return [item.headline for item in news.headlines]
```

## Authentication & keys

Use `TornClient(api_key)` for one key or `TornClient(keys=[...])` for a pool. `from_env()` reads `TORN_API_KEYS` (comma-separated) first and falls back to `TORN_API_KEY`. Both API versions authenticate via the `Authorization: ApiKey …` header. A legacy `key` query argument is moved to that header. Request traces and `key_states()` expose masked key hints.

The client rotates across healthy keys. Torn error policy determines whether to skip a key for the request, until UTC midnight, or for the client's lifetime. Exhausting eligible keys raises `NoUsableKeysError` with the last Torn error.

## Typed calls

Exact endpoint methods use the OpenAPI operation IDs in snake case. Typed parameter objects live in `torn_sdk.operations` and response models in `torn_sdk.models`. You can omit the parameter object when the operation has no required parameters. The nine resource facades take typed filters and an optional bound ID.

```python
from torn_sdk import TornClient
from torn_sdk.operations import GetUserProfileParams

with TornClient.from_env() as torn:
    other = torn.get_user_profile(GetUserProfileParams(id=123))
    own = torn.user().profile()
    history = torn.market(206).itemhistory(limit=25)
    csv_text = torn.get_factions_snapshot()
```

Wire names that are Python keywords use a suffix, as in `from_`. Models serialize wire names with `model_dump(mode="json", by_alias=True)`. Use `model_fields_set` to distinguish omitted fields from explicit null. Historical models are under `torn_sdk.historical`, and shared compatibility models are in `torn_sdk.compatibility`.

## Batching

```python
from torn_sdk import TornClient, selections

with TornClient.from_env() as torn:
    result = torn.user(123).batch(selections.user.profile, selections.user.job)
    print(result.get(selections.user.profile).profile.name)
    if "job" not in result.errors:
        print(result.get(selections.user.job).job)
```

The planner groups compatible requests and isolates dedicated, standalone, legacy, and incompatible selections. When Torn rejects a combined call with code 4, it retries each affected selection with the same key. `result.get(token)` returns the token's typed value or raises its error. `result.errors`, `result.data`, and `result.report` expose per-selection failures, merged raw fields, and request traces. Use `resource.plan(...)` or `plan_selections(...)` to inspect routing offline. Pass `version="v1"`, `"v2"`, or `"legacy"` to select a different routing policy.

## Pagination

```python
from torn_sdk import TornClient
from torn_sdk.operations import GetMarketItemHistoryParams

with TornClient.from_env() as torn:
    params = GetMarketItemHistoryParams(id=206, limit=100)
    for row in torn.get_market_item_history_items(params, max_pages=3, limit=200):
        print(row.timestamp, row.price)
```

`*_pages` yields typed response envelopes and `*_items` yields their typed rows. Async variants support `async for`. Pass `backwards=True` to follow previous links. Raw equivalents are `raw_pages` and `raw_items`. Pagination checks the configured API origin and attaches authentication through the client.

## Errors

Catch `TornError` or a specific subclass such as `ApiError`, `HttpError`, `TransportError`, `DecodeError`, `AccessError`, or `NoUsableKeysError`. `TornErrorCode` exposes the documented error policy and preserves unrecognized numeric codes. The package also exports `code()`, `is_retryable()`, `is_key_error()`, `is_rate_limit()`, `is_access_error()`, and `is_transient()`.

`DecodeError` reports the operation, JSON path, expected shape, and a redacted excerpt. Its explicit `raw` property exposes the response bytes for diagnostics.

## Rate limits & retries

The default budget is **100 requests per rolling 60 seconds per key**. Pass `Key(key, owner=...)` to share a budget across keys for the same Torn user, or enable `detect_key_owners` for discovery through `key/info`. `Route` and `AsyncRoute` support named egress routes, proxies, and per-route budgets. A Torn IP block spills over to another configured route when one is available.

Retries follow the shared error policy, with capped exponential backoff, full jitter, and bounded `Retry-After`. Key rotations have their own pool bound. Configure the runtime with `RuntimeConfig` and individual calls with `RequestOptions` for deadline, timeout, cancellation checks, and cache bypass. Async tasks can also use asyncio's task cancellation.

## Access levels

Inspect `operation.access` and `selection.access` through the public `OPERATIONS` and `SELECTIONS` mappings. `get_key_info()` returns the typed key metadata. Pass `check_access=True` to enforce known access levels before sending a request. Declare levels with `Key(key, access="limited")` or discover them with `detect_key_access()`.

## Forward compatibility

Responses preserve additional properties and unknown enum values. Check an enum's `is_known` flag or call `known_values()` on its type. Pass `strict=True` to a typed call to report unknown enum values, unexpected properties, and missing fields relaxed by the shared OpenAPI overrides. Set `client.strict = True` to use strict validation throughout a client.

Unknown selections pass through the v2 generic endpoint. Exact calls and facades accept extra query parameters through `query=`, including repeated values supplied as ordered pairs.

## Caching

Caching is opt-in. Pass `cache=MemoryCache(max_entries=500)` to either client or implement the `Cache` protocol. Successful requests use the operation's upstream cache duration where documented and the configured fallback TTL otherwise. Keys are fingerprinted in cache identifiers.

## Hooks

Pass `hooks=Hooks(...)` with any of `on_request`, `on_response`, `on_retry`, and `on_error`. The callbacks receive a credential-redacted `RequestTrace` with the operation, URL, attempt, status, duration, route, and key hint. Retry and error callbacks also receive the typed error.

## Testing

```python
from torn_sdk.testing import json_response, mock_sdk

sdk, mock = mock_sdk([json_response({"timestamp": 42})])
with sdk:
    assert sdk.get_torn_timestamp().timestamp == 42
mock.expect_request(0, "/v2/torn/timestamp")
mock.assert_done()
```

`torn_sdk.testing` also exports an async factory, virtual clocks, a scripted mock transport, fixture loading, and Torn and network error response helpers.

```sh
python -m pip install -e ".[dev]"
python -m pytest -m "not live"
python -m mypy src/torn_sdk examples/typed_consumer.py
python scripts/generate.py --check
python scripts/generate_error_codes.py --check
```

For live verification, set `TORN_API_KEY` (or `TORN_LIVE_TEST_API_KEY`) and run `python -m pytest -m live -s`. The sweep paces requests, discovers the key owner's IDs, and validates all reachable exact operations. It reports missing IDs and Torn access errors separately from SDK or decoding failures.

## Raw access

Use `raw_get("/user", {"selections": "profile"})` for raw JSON. Set `version="v1"` for legacy paths. `raw_typed(path, Model, query)` decodes into a generated or custom model. Resource facades expose named raw methods such as `user().profile_raw()`, alongside `raw_selection` and `raw_selections` for arbitrary names. Both clients accept an injected HTTPX transport.

## Helpers

`torn_sdk.helpers` builds Torn links for profiles, attacks, factions, companies, bazaars, the item market, trades, forum threads, properties, messages, and name searches. It converts Unix seconds to timezone-aware datetimes, formats Torn City Time (UTC), and computes the next TCT midnight.

## Versioning

The package exports `SDK_VERSION`, `SPEC_VERSION`, and `SPEC_SHA256`. Generated endpoint metadata, models, and routing are shipped in the package. See [CHANGELOG.md](CHANGELOG.md) for contract and behavior changes.
