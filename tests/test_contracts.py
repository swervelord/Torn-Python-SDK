from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from torn_sdk import AsyncTornClient, SPEC_VERSION, TornClient, compatibility, models, operations
from torn_sdk._model import decode_payload


SPEC = Path(__file__).resolve().parents[1] / "spec"


def test_exact_operation_surface_matches_the_pinned_spec() -> None:
    wire = json.loads((SPEC / f"openapi-{SPEC_VERSION}.json").read_bytes())
    assert set(models.__all__) == wire["components"]["schemas"].keys()
    assert set(operations.OPERATIONS) == {
        methods["get"]["operationId"] for methods in wire["paths"].values()
    }
    for path, methods in wire["paths"].items():
        operation_id = methods["get"]["operationId"]
        endpoint = operations.OPERATIONS[operation_id]
        assert endpoint.path == path
        assert endpoint.stability == methods["get"].get("x-stability", "Unknown")
        assert endpoint.deprecated == methods["get"].get("deprecated", False)
        assert all(parameter.name != "key" for parameter in endpoint.parameters)
    assert operations.get_my_inventory.cache_ttl == 900
    assert operations.get_torn_timestamp.cache_ttl is None
    for name, endpoint in vars(operations).items():
        if getattr(endpoint, "id", None) not in operations.OPERATIONS:
            continue
        assert callable(getattr(TornClient, name))
        assert callable(getattr(AsyncTornClient, name))
        if endpoint.paginated:
            assert endpoint.item_type is not None
            assert callable(getattr(TornClient, f"{name}_pages"))
            assert callable(getattr(TornClient, f"{name}_items"))
            assert callable(getattr(AsyncTornClient, f"{name}_pages"))
            assert callable(getattr(AsyncTornClient, f"{name}_items"))


def test_all_frozen_openapi_snapshots_have_separate_models() -> None:
    provenance = json.loads((SPEC / "provenance.json").read_bytes())
    for snapshot in provenance["snapshots"][1:]:
        name = snapshot["version"].replace(".", "_")
        frozen = import_module(f"torn_sdk.historical.v{name}")
        assert len(frozen.__all__) == snapshot["schemas"]
        assert frozen.TimestampResponse is not models.TimestampResponse
        assert frozen.TimestampResponse(timestamp=123).timestamp == 123


def test_open_enums_retain_wire_value_and_strict_mode_reports_unknown_values() -> None:
    enum = models.ApiKeyAccessTypeEnum
    known = decode_payload(enum, "Full Access")
    assert known.is_known
    assert "Full Access" in enum.known_values()
    value = decode_payload(enum, "Future access % ✓")
    assert not value.is_known
    assert value.value == "Future access % ✓"
    assert json.loads(TypeAdapter(enum).dump_json(value)) == value.value
    with pytest.raises(ValidationError) as captured:
        decode_payload(enum, value.value, strict=True)
    assert captured.value.errors()[0]["type"] == "unknown_enum"


def test_strict_mode_reinstates_upstream_required_fields() -> None:
    payload = {
        "id": 1,
        "name": "Upgrade",
        "description": "Description",
        "flavor": "Flavor",
        "cost": 10,
        "requirement": 0,
    }
    assert decode_payload(models.TornHalloweenUpgrade, payload).type is None
    with pytest.raises(ValidationError) as captured:
        decode_payload(models.TornHalloweenUpgrade, payload, strict=True)
    assert captured.value.errors()[0]["loc"] == ("type",)
    assert decode_payload(models.TornHalloweenUpgrade, {**payload, "type": "future"}).type == "future"


@pytest.mark.parametrize("wire_name", ["itemstats", "itemdetails"])
def test_item_stats_uses_live_wire_name_and_accepts_the_openapi_spelling(wire_name: str) -> None:
    item = {
        "id": 247,
        "uid": 123456,
        "name": "Katana",
        "type": "Weapon",
        "sub_type": "Slashing",
        "stats": [{"id": 1, "title": "Damage", "value": 60}],
    }
    payload = decode_payload(models.TornItemStatsResponse, {wire_name: [item]}, strict=True)
    assert payload.itemstats[0].stats[0].value == 60
    assert payload.itemdetails is payload.itemstats
    assert payload.model_dump(mode="json", by_alias=True) == {"itemstats": [item]}


def test_compatibility_models_normalize_documented_legacy_shapes() -> None:
    employee = compatibility.CompanyEmployee.model_validate({
        "position": {"name": "Engineer"}, "future": 42,
    })
    assert employee.position == "Engineer"
    assert employee.extra == {"future": 42}
    upgrades = compatibility.CompanyUpgrades.model_validate({"storage_capacity": 100})
    assert upgrades.storage_capacity == "100"
    news = compatibility.CompanyNewsBundle.model_validate({"news": [{"id": 4, "news": "hello"}]})
    assert news.news["4"].news == "hello"
