"""Wire adapters for the polymorphic payloads returned by legacy selections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from ._model import WireModel


class CompatibilityModel(WireModel):
    """The compatibility contracts expose unmodeled properties in extra."""

    @property
    def extra(self) -> dict[str, JsonValue]:
        return cast("dict[str, JsonValue]", self.model_extra or {})


def stringish(value: object) -> object:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return value


def position_name(value: object) -> object:
    if isinstance(value, Mapping):
        value = value.get("name")
    return stringish(value)


def company_news(value: object) -> object:
    if value is None:
        return {}
    if isinstance(value, list):
        entries: dict[str, object] = {}
        for index, entry in enumerate(value):
            key = entry.get("id") if isinstance(entry, Mapping) else None
            if key is None and isinstance(entry, Mapping):
                key = entry.get("timestamp")
            key = str(index) if key is None else str(key)
            entries[key] = _news_entry(entry)
        return entries
    if isinstance(value, Mapping):
        return {key: _news_entry(entry) for key, entry in value.items()}
    return value


def _news_entry(value: object) -> object:
    if isinstance(value, str):
        return {"news": value}
    if isinstance(value, Mapping) and "id" in value:
        return {**value, "id": stringish(value["id"])}
    return value
