"""Torn website links and Torn City Time (UTC)."""

from datetime import datetime, timedelta, timezone
from math import floor
from urllib.parse import quote_plus

TORN_WEB_BASE = "https://www.torn.com"


def _id(value: int | str) -> str:
    result = str(value).strip()
    if not result.isascii() or not result.isdecimal():
        raise ValueError("Expected an unsigned numeric Torn ID")
    return result


def profile_url(user_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/profiles.php?XID={_id(user_id)}"


def attack_url(user_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/loader.php?sid=attack&user2ID={_id(user_id)}"


def faction_url(faction_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/factions.php?step=profile&ID={_id(faction_id)}"


def company_url(company_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/joblist.php#/p=corpinfo&ID={_id(company_id)}"


def bazaar_url(user_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/bazaar.php?userId={_id(user_id)}"


def item_market_url(item_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/page.php?sid=ItemMarket#/market/view=search&itemID={_id(item_id)}"


def trade_url(trade_id: int | str | None = None) -> str:
    url = f"{TORN_WEB_BASE}/trade.php"
    return url if trade_id is None else f"{url}#step=view&ID={_id(trade_id)}"


def forum_thread_url(forum_id: int | str, thread_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/forums.php#/p=threads&f={_id(forum_id)}&t={_id(thread_id)}"


def property_url(property_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/properties.php#/p=propertyinfo&ID={_id(property_id)}"


def send_message_url(user_id: int | str) -> str:
    return f"{TORN_WEB_BASE}/messages.php?p=compose&XID={_id(user_id)}"


def search_url(name: str) -> str:
    return f"{TORN_WEB_BASE}/page.php?sid=UserList&searchname={quote_plus(name)}"


def from_torn_timestamp(seconds: int | float) -> datetime:
    """Convert Unix seconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def torn_now() -> datetime:
    return datetime.now(timezone.utc)


def tct(value: datetime | int | float | None = None) -> datetime:
    """Convert to UTC. Naïve datetimes are interpreted as UTC."""
    if value is None:
        return torn_now()
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return from_torn_timestamp(value)


def to_torn_timestamp(value: datetime) -> int:
    return floor(tct(value).timestamp())


def format_tct(value: datetime | int | float | None = None) -> str:
    return tct(value).strftime("%H:%M:%S")


def format_tct_datetime(value: datetime | int | float | None = None) -> str:
    return tct(value).strftime("%H:%M:%S - %d/%m/%y")


def tct_start_of_day(value: datetime | int | float | None = None) -> datetime:
    return tct(value).replace(hour=0, minute=0, second=0, microsecond=0)


def next_tct_midnight(value: datetime | int | float | None = None) -> datetime:
    return tct_start_of_day(value) + timedelta(days=1)


def ms_until_tct_midnight(value: datetime | int | float | None = None) -> int:
    instant = tct(value)
    return floor((next_tct_midnight(instant) - instant) / timedelta(milliseconds=1))
