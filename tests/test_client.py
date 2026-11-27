from __future__ import annotations

import asyncio
from importlib.metadata import version

import httpx
import pytest

from torn_sdk import SDK_VERSION, DecodeError, ValidationError, selections
from torn_sdk.operations import GetMarketItemHistoryParams, GetTornItemsSpecificParams
from torn_sdk.testing import TEST_API_KEY, json_response, mock_async_sdk, mock_sdk


def test_package_version_matches_sdk_version() -> None:
    assert version("torn-python-sdk") == SDK_VERSION


def test_exact_comma_separated_path_ids_and_query_passthrough() -> None:
    sdk, mock = mock_sdk([json_response({"items": []})])
    with sdk:
        result = sdk.get_torn_items_specific(
            GetTornItemsSpecificParams(ids=[206, 207], comment="local test"),
            query=[("future", "a"), ("future", "b")],
        )
    assert result.items == []
    mock.expect_request(
        0, "/v2/torn/206,207/items", query=[("comment", "local test"), ("future", "a"), ("future", "b")],
    )
    mock.assert_done()


def test_csv_and_relaxed_and_strict_response_diagnostics() -> None:
    response = {"timestamp": 1, "future": {"foo": TEST_API_KEY}}
    sdk, mock = mock_sdk([
        httpx.Response(200, text="id,name\n1,Torn\n", headers={"Content-Type": "text/csv"}),
        json_response(response),
        json_response(response),
    ])
    with sdk:
        assert sdk.get_users_snapshot().splitlines()[1] == "1,Torn"
        assert sdk.get_torn_timestamp().model_extra == {"future": {"foo": TEST_API_KEY}}
        with pytest.raises(DecodeError) as captured:
            sdk.get_torn_timestamp(strict=True)
    error = captured.value
    assert error.operation == "getTornTimestamp"
    assert error.path == "$.future"
    assert TEST_API_KEY not in str(error)
    assert TEST_API_KEY.encode() in error.raw
    mock.assert_done()


def test_typed_pages_follow_links_without_credential_and_keep_item_type() -> None:
    next_link = "https://api.torn.com/v2/market/206/itemhistory?offset=1&key=ignored"
    sdk, mock = mock_sdk([
        json_response({
            "itemhistory": [{"timestamp": 1, "price": None, "circulation": 10}],
            "_metadata": {"links": {"next": next_link, "prev": None}},
        }),
        json_response({
            "itemhistory": [{"timestamp": 2, "price": 55, "circulation": None}],
            "_metadata": {"links": {"next": None, "prev": None}},
        }),
    ])
    with sdk:
        rows = list(sdk.get_market_item_history_items(GetMarketItemHistoryParams(id=206), limit=2))
    assert [row.price for row in rows] == [None, 55]
    mock.expect_request(1, "/v2/market/206/itemhistory", query={"offset": "1"}, key_hint="***0001")
    mock.assert_done()


def test_unknown_selections_and_repeated_filters() -> None:
    sdk, mock = mock_sdk([json_response({"future": [1]})])
    with sdk:
        assert sdk.user(321).raw_selection(
            "future", query=[("future_filter", "a"), ("future_filter", "b"), ("comment", "sdk")],
        ) == {"future": [1]}
        with pytest.raises(ValidationError):
            sdk.user().batch(selections.faction.timestamp)
    mock.expect_request(
        0, "/v2/user",
        query=[
            ("id", "321"), ("selections", "future"),
            ("future_filter", "a"), ("future_filter", "b"), ("comment", "sdk"),
        ],
    )


def test_async_facade_selection() -> None:
    async def run() -> None:
        sdk, mock = mock_async_sdk([json_response({"timestamp": 675})])
        async with sdk:
            assert (await sdk.torn().timestamp()).timestamp == 675
        mock.assert_done()

    asyncio.run(run())
