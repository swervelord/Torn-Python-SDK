"""Exercise installed sync and async SDK types without a Torn account."""

import asyncio

from typing_extensions import assert_type

from torn_sdk import AsyncTornClient, TornClient, selections
from torn_sdk.models import MarketHistoryEntry, TimestampResponse, UserJobResponse
from torn_sdk.operations import GetMarketItemHistoryParams
from torn_sdk.testing import json_response, mock_async_sdk, mock_sdk


def sync_example() -> None:
    client, transport = mock_sdk([
        json_response({"timestamp": 42, "job": None}),
        json_response({
            "itemhistory": [{"timestamp": 42, "price": 713, "circulation": None}],
            "_metadata": {"links": {"next": None, "prev": None}},
        }),
    ])
    with client as sdk:
        assert_type(sdk, TornClient)
        batch = sdk.user().batch(selections.user.timestamp, selections.user.job)
        stamp = assert_type(batch.get(selections.user.timestamp), TimestampResponse)
        job = assert_type(batch.get(selections.user.job), UserJobResponse)
        assert stamp.timestamp == 42 and job.job is None
        items = sdk.get_market_item_history_items(GetMarketItemHistoryParams(id=206))
        first = assert_type(next(items), MarketHistoryEntry)
        assert first.price == 713
    transport.expect_request(0, "/v2/user", query={"selections": "timestamp,job"})
    transport.expect_request(1, "/v2/market/206/itemhistory")
    transport.assert_done()


async def async_example() -> None:
    client, transport = mock_async_sdk([json_response({"timestamp": 42})])
    async with client as sdk:
        assert_type(sdk, AsyncTornClient)
        response = assert_type(await sdk.get_torn_timestamp(), TimestampResponse)
        assert response.timestamp == 42
    transport.expect_request(0, "/v2/torn/timestamp")
    transport.assert_done()


if __name__ == "__main__":
    sync_example()
    asyncio.run(async_example())
