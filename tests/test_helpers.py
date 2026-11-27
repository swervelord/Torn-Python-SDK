from datetime import datetime, timedelta, timezone

import pytest

from torn_sdk import helpers


def test_links() -> None:
    assert helpers.profile_url(" 123 ") == "https://www.torn.com/profiles.php?XID=123"
    assert helpers.attack_url(123).endswith("loader.php?sid=attack&user2ID=123")
    assert helpers.faction_url(123).endswith("factions.php?step=profile&ID=123")
    assert helpers.company_url(123).endswith("joblist.php#/p=corpinfo&ID=123")
    assert helpers.bazaar_url(123).endswith("bazaar.php?userId=123")
    assert helpers.item_market_url(123).endswith("/market/view=search&itemID=123")
    assert helpers.trade_url().endswith("trade.php")
    assert helpers.trade_url(123).endswith("#step=view&ID=123")
    assert helpers.forum_thread_url(1, 23).endswith("#/p=threads&f=1&t=23")
    assert helpers.property_url(123).endswith("#/p=propertyinfo&ID=123")
    assert helpers.send_message_url(123).endswith("messages.php?p=compose&XID=123")
    assert helpers.search_url("a & b").endswith("searchname=a+%26+b")
    with pytest.raises(ValueError):
        helpers.profile_url("1&other=42")


def test_tct_is_utc() -> None:
    local = datetime(2026, 12, 31, 18, tzinfo=timezone(-timedelta(hours=6)))
    utc = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert helpers.tct(local) == utc
    assert helpers.tct(utc.replace(tzinfo=None)) == utc
    assert helpers.from_torn_timestamp(helpers.to_torn_timestamp(local)) == utc
    assert helpers.format_tct_datetime(local) == "00:00:00 - 01/01/27"
    assert helpers.format_tct(local) == "00:00:00"
    assert helpers.tct_start_of_day(local) == utc
    assert helpers.next_tct_midnight(local) == utc + timedelta(days=1)
    assert helpers.ms_until_tct_midnight(utc - timedelta(milliseconds=10)) == 10
