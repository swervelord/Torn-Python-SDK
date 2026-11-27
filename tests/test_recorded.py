"""Public Torn-Zig-SDK fixtures from commit f5f9233b5003ef2858b8101082b92213df528a1e."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from torn_sdk import ValidationError, models, operations
from torn_sdk._model import decode_payload
from torn_sdk.historical import v6_15_7
from torn_sdk.testing import json_response, mock_sdk

FIXTURES = Path(__file__).parent / "fixtures" / "zig"

HISTORICAL = {
    "faction_reports_stockanalysis": v6_15_7.ReportsResponse,
    "racing_races_available": v6_15_7.RacingRacesResponse,
    "torn_attacklog": v6_15_7.AttackLogResponse,
    "torn_calendar": v6_15_7.TornCalendarResponse,
    "user_gym": v6_15_7.UserGymResponse,
    "user_log": v6_15_7.UserLogsResponse,
}
CURRENT = {
    "faction_headlines": operations.get_my_faction_headlines,
    "faction_ochistory": operations.get_my_faction_organized_crime_history,
    "faction_rankedwars": operations.get_my_faction_ranked_wars_history,
    "key_info": operations.get_key_info,
    "market_auctionhouse_ongoing": operations.get_market_auction_house,
    "market_itemhistory": operations.get_market_item_history,
    "market_pointshistory": operations.get_market_points_history,
    "torn_headlines": operations.get_torn_headlines,
    "user_auctionhouse": operations.get_my_auction_house_listings,
    "user_bazaar": operations.get_my_bazaar,
    "user_bazaar_empty": operations.get_my_bazaar,
    "user_criminalrecord_v1": operations.get_my_criminal_record,
    "user_criminalrecord_v2": operations.get_my_criminal_record,
    "user_display": operations.get_my_display_case,
    "user_honors_basic": operations.get_my_honors,
    "user_honors_detailed": operations.get_my_honors,
    "user_medals_detailed": operations.get_my_medals,
}


def fixture(version: str, name: str) -> object:
    return json.loads((FIXTURES / f"api-{version}" / f"{name}.json").read_bytes())


@pytest.mark.parametrize("name", HISTORICAL)
def test_frozen_responses_preserve_recorded_payloads(name: str) -> None:
    payload = fixture("6.15.7", name)
    model = decode_payload(HISTORICAL[name], payload)
    assert model.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload


@pytest.mark.parametrize("name", CURRENT)
def test_exact_responses_preserve_recorded_payloads(name: str) -> None:
    payload = fixture("6.21.0", name)
    model = decode_payload(CURRENT[name].response, payload)
    assert model.model_dump(mode="json", by_alias=True, exclude_unset=True) == payload


def test_award_helpers_select_the_requested_contract_for_empty_and_nonempty_payloads() -> None:
    basic = fixture("6.21.0", "user_honors_basic")
    detailed = fixture("6.21.0", "user_honors_detailed")
    client, transport = mock_sdk([
        json_response(basic),
        json_response(detailed),
        json_response({"medals": []}),
    ])
    with client:
        assert isinstance(client.user().honors_basic(), models.UserHonorsResponse)
        assert isinstance(client.user().honors_detailed("crimes"), models.UserHonorsDetailedResponse)
        assert client.user().medals_detailed("latest").medals == []
        with pytest.raises(ValidationError):
            client.user().honors_detailed(models.UserHonorsCategory.ALL)
    transport.expect_request(0, "/v2/user/honors", query={"cat": "all"})
    transport.expect_request(1, "/v2/user/honors", query={"cat": "crimes"})
    transport.expect_request(2, "/v2/user/medals", query={"cat": "latest"})
    transport.assert_done()


def test_named_raw_wrappers_return_json_and_csv_without_decoding_models() -> None:
    client, transport = mock_sdk([
        json_response({"name": "raw-only"}),
        httpx.Response(200, text="id,name\n1,Torn\n", headers={"Content-Type": "text/csv"}),
    ])
    with client:
        assert client.user().profile_raw() == {"name": "raw-only"}
        assert client.user().snapshot_raw() == "id,name\n1,Torn\n"
    transport.expect_request(1, "/v2/user/snapshot")
    transport.assert_done()
