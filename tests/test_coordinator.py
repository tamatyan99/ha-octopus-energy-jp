"""Hermetic tests for the Octopus Energy Japan data coordinator.

Every test drives the real ``OctopusEnergyJpCoordinator`` with a fake API
client (no network) under a frozen clock, asserting on ``coordinator.data``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp import utils as oejp_utils
from custom_components.octopus_energy_jp.api import OctopusApiError, OctopusAuthError
from custom_components.octopus_energy_jp.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASIC_CHARGE_PER_DAY,
    CONF_FUEL_ADJUSTMENT_PER_KWH,
    CONF_RENEWABLE_LEVY_PER_KWH,
    DOMAIN,
    MAX_DAILY_DAYS,
)
from custom_components.octopus_energy_jp.coordinator import (
    OctopusEnergyJpCoordinator,
    _attach_hourly_slot_costs,
    _coerce_rates,
    _month_prior_daily_kwh,
    _tiered_cost,
)

ACCOUNT = "A-TEST1234"
ENTRY_DATA = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_ACCOUNT_NUMBER: ACCOUNT,
}

# Realistic three-tier tariff payload (dict form, as the API returns it).
TARIFF_DICTS: list[dict[str, Any]] = [
    {"stepStart": 0, "stepEnd": 120, "pricePerUnitIncTax": 30.0},
    {"stepStart": 120, "stepEnd": 300, "pricePerUnitIncTax": 36.0},
    {"stepStart": 300, "stepEnd": None, "pricePerUnitIncTax": 40.0},
]

SURCHARGE_OPTIONS = {
    CONF_BASIC_CHARGE_PER_DAY: 40.0,
    CONF_FUEL_ADJUSTMENT_PER_KWH: 2.0,
    CONF_RENEWABLE_LEVY_PER_KWH: 3.0,
}


@contextmanager
def _frozen_jst() -> Iterator[datetime]:
    """Freeze time at 2026-07-15 12:00 JST and run HA under Asia/Tokyo."""
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    try:
        with freeze_time("2026-07-15 03:00:00"):
            yield dt_util.now()
    finally:
        dt_util.set_default_time_zone(previous)


def _contract() -> dict[str, Any]:
    return {
        "plan_name": "Octopus Plan",
        "product_code": "TEST-PRODUCT",
        "grid_operator_code": "22",
        "capacity_unit": "",
    }


def _fixed_days() -> dict[str, float]:
    """July 1..15 holds 1..15 kWh (month total 120); June holds 5 kWh/day."""
    days = {f"2026-07-{d:02d}": float(d) for d in range(1, 16)}
    days.update({f"2026-06-{d:02d}": 5.0 for d in range(1, 31)})
    return days


def _half_hourly(days: dict[str, float], *, version: int = 1) -> list[dict[str, Any]]:
    """Expand day -> kWh into 48 half-hourly readings per day (JST slots)."""
    out: list[dict[str, Any]] = []
    for day, kwh in sorted(days.items()):
        per_slot = kwh / 48
        for slot in range(48):
            hour, minute = divmod(slot * 30, 60)
            out.append(
                {
                    "startAt": f"{day}T{hour:02d}:{minute:02d}:00+09:00",
                    "value": per_slot,
                    "version": version,
                }
            )
    return out


def _new_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data=dict(ENTRY_DATA),
        options=dict(options or {}),
    )


def _bill(from_date: str = "2026-07-01", to_date: str = "2026-07-10") -> dict[str, Any]:
    return {
        "bill_type": "STATEMENT",
        "from_date": from_date,
        "to_date": to_date,
        "issued_date": "2026-07-11",
    }


def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    readings: list[dict[str, Any]],
    *,
    tariff: Any = None,
    contract: dict[str, Any] | None = None,
    bill: dict[str, Any] | None = None,
    bill_error: Exception | None = None,
    surcharges: dict[str, float | None] | None = None,
) -> OctopusEnergyJpCoordinator:
    """Build a coordinator backed by a fake API client (no network)."""
    api = MagicMock()
    api.async_get_contract = AsyncMock(
        return_value=dict(_contract()) if contract is None else contract
    )
    api.async_get_tariff_rates = AsyncMock(
        return_value=[dict(t) for t in TARIFF_DICTS] if tariff is None else tariff
    )
    api.async_get_surcharges = AsyncMock(return_value=dict(surcharges or {}))
    payload = [dict(r) if isinstance(r, dict) else r for r in readings]

    async def _readings(
        account_number: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        assert account_number == ACCOUNT
        return [dict(r) if isinstance(r, dict) else r for r in payload]

    api.async_get_readings = AsyncMock(side_effect=_readings)
    if bill_error is not None:
        api.async_get_latest_bill = AsyncMock(side_effect=bill_error)
    else:
        api.async_get_latest_bill = AsyncMock(return_value=bill)
    return OctopusEnergyJpCoordinator(hass, entry, api)


# ---------------------------------------------------------------------------
# Error mapping in _async_update_data
# ---------------------------------------------------------------------------


async def test_contract_auth_error_becomes_config_entry_auth_failed(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    coord.api.async_get_contract = AsyncMock(
        side_effect=OctopusAuthError("token expired")
    )
    with _frozen_jst(), pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_contract_api_error_becomes_update_failed(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    coord.api.async_get_contract = AsyncMock(side_effect=OctopusApiError("HTTP 500"))
    with _frozen_jst(), pytest.raises(UpdateFailed, match="API error"):
        await coord._async_update_data()


async def test_contract_data_error_becomes_update_failed(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass, _new_entry(), [], contract={})
    with _frozen_jst(), pytest.raises(UpdateFailed, match="Data error"):
        await coord._async_update_data()


async def test_tariff_without_rates_becomes_update_failed(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass, _new_entry(), [], tariff=[])
    with _frozen_jst(), pytest.raises(UpdateFailed, match="Data error"):
        await coord._async_update_data()


async def test_all_readings_chunks_failing_becomes_update_failed(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    coord.api.async_get_readings = AsyncMock(side_effect=OctopusApiError("down"))
    with _frozen_jst(), pytest.raises(UpdateFailed, match="All readings"):
        await coord._async_update_data()


async def test_partial_chunk_failure_is_tolerated(hass: HomeAssistant) -> None:
    payload = _half_hourly({"2026-07-14": 14.0, "2026-07-15": 15.0})
    coord = _make_coordinator(hass, _new_entry(), payload)
    calls = 0

    async def _flaky(
        account_number: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OctopusApiError("chunk boom")
        return [dict(r) for r in payload]

    coord.api.async_get_readings = AsyncMock(side_effect=_flaky)
    with _frozen_jst():
        data = await coord._async_update_data()
    # Every chunk was attempted (the failure did not abort the update) ...
    assert calls > 1
    # ... and the surviving chunks still produced correct aggregates.
    assert data["yesterday_kwh"] == 14.0
    assert data["today_kwh"] == 15.0


async def test_readings_auth_error_propagates_as_auth_failed(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    coord.api.async_get_readings = AsyncMock(
        side_effect=OctopusAuthError("token expired")
    )
    with _frozen_jst(), pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_bill_fetch_failure_is_tolerated(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(),
        _half_hourly({"2026-07-14": 14.0, "2026-07-15": 15.0}),
        bill_error=OctopusApiError("bills unavailable"),
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["yesterday_kwh"] == 14.0
    assert data["billing"] is None
    assert data["billing_period"] is None


async def test_bill_auth_error_propagates_as_auth_failed(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(),
        _half_hourly({"2026-07-14": 14.0}),
        bill_error=OctopusAuthError("token expired"),
    )
    with _frozen_jst(), pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


# ---------------------------------------------------------------------------
# Aggregation correctness on a fixed input
# ---------------------------------------------------------------------------


async def test_fixed_input_aggregation(hass: HomeAssistant) -> None:
    coord = _make_coordinator(hass, _new_entry(), _half_hourly(_fixed_days()))
    with _frozen_jst():
        data = await coord._async_update_data()

    assert data["yesterday_kwh"] == 14.0
    assert data["today_kwh"] == 15.0
    assert data["month_kwh"] == 120.0
    assert data["diff_kwh"] == 1.0
    assert data["diff_pct"] == 8
    assert data["avg_rate"] == 30.0
    assert data["cost_yesterday"] == 420
    assert data["cost_today"] == 450
    assert data["cost_month"] == 3600
    assert data["prev_month_kwh"] == 150.0
    assert data["prev_month_cost"] == 4680
    assert data["month_diff_kwh"] == 35.0
    assert data["month_diff_pct"] == 50
    assert data["plan_name"] == "Octopus Plan"
    assert data["billing"] is None
    assert data["billing_period"] is None

    assert data["today_start"] == "2026-07-15T00:00:00+09:00"
    assert data["month_start"] == "2026-07-01T00:00:00+09:00"
    assert data["last_update"] == "2026-07-15T12:00:00+09:00"
    for key in ("today_start", "month_start", "last_update"):
        parsed = dt_util.parse_datetime(data[key])
        assert parsed is not None and parsed.tzinfo is not None

    assert len(data["daily"]) == 45
    assert data["daily"][0] == {"d": "2026-06-01", "kwh": 5.0, "cost": 156}
    assert data["daily"][-1] == {"d": "2026-07-15", "kwh": 15.0, "cost": 450}
    assert len(data["yesterday_series"]) == 48
    assert data["yesterday_series"][0]["start"] == "2026-07-14T00:00:00+09:00"
    assert len(data["today_series"]) == 48
    assert round(sum(point["kwh"] for point in data["hourly"]), 1) == 270.0


async def test_half_hourly_dedup_and_junk_rows(hass: HomeAssistant) -> None:
    readings: list[dict[str, Any]] = [
        {"startAt": "2026-07-14T00:00:00+09:00", "value": 0.2, "version": 1},
        # Revised version of the same slot wins: no double counting.
        {"startAt": "2026-07-14T00:00:00+09:00", "value": 0.7, "version": 2},
        {"startAt": "2026-07-14T00:30:00+09:00", "value": 0.3, "version": 1},
        # Naive datetime start is interpreted in the local time zone.
        {"startAt": datetime(2026, 7, 14, 1, 0, 0), "value": 0.5},
        # Junk rows below must be skipped without failing the update.
        {"value": 9.0},
        {"startAt": "2026-07-14T02:00:00+09:00"},
        {"startAt": "not-a-date", "value": 1.0},
        {"startAt": 12345, "value": 1.0},
        {"startAt": "2026-07-14T03:00:00+09:00", "value": "junk"},
        {"startAt": "2026-07-14T04:00:00+09:00", "value": float("inf")},
        "not-a-dict",
        None,
    ]
    coord = _make_coordinator(hass, _new_entry(), readings)
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["yesterday_kwh"] == 1.5
    assert data["today_kwh"] == 0.0
    assert len(data["yesterday_series"]) == 3


async def test_minimal_history_has_no_prev_month(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass, _new_entry(), _half_hourly({"2026-07-14": 4.0, "2026-07-15": 2.0})
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["yesterday_kwh"] == 4.0
    assert data["today_kwh"] == 2.0
    assert data["month_kwh"] == 6.0
    assert data["diff_kwh"] == 4.0
    assert data["diff_pct"] is None
    assert data["prev_month_kwh"] is None
    assert data["prev_month_cost"] is None
    assert data["month_diff_kwh"] is None
    assert data["month_diff_pct"] is None


# ---------------------------------------------------------------------------
# Billing-period aggregation
# ---------------------------------------------------------------------------


async def test_billing_with_surcharges(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(dict(SURCHARGE_OPTIONS)),
        _half_hourly(_fixed_days()),
        bill=_bill(),
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["billing_period"] == {
        "from": "2026-07-01",
        "to": "2026-07-10",
        "bill_type": "STATEMENT",
        "source": "bill",
    }
    # July 1..10 holds 1+2+...+10 = 55 kWh over 10 calendar days.
    assert data["billing"] == {
        "kwh": 55.0,
        "days": 10,
        "energy_cost": 1650,
        "from": "2026-07-01",
        "to": "2026-07-10",
        "source": "bill",
        "basic_charge": 400,
        "fuel_adjustment": 110,
        "renewable_levy": 165,
        "total": 2325,
    }


async def test_billing_without_options_is_energy_only(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass, _new_entry(), _half_hourly(_fixed_days()), bill=_bill()
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["billing"] is not None
    assert data["billing"]["kwh"] == 55.0
    assert data["billing"]["days"] == 10
    assert data["billing"]["energy_cost"] == 1650
    assert data["billing"]["total"] == 1650
    assert "basic_charge" not in data["billing"]
    assert "fuel_adjustment" not in data["billing"]
    assert "renewable_levy" not in data["billing"]


API_SURCHARGES = {
    "standing_charge_per_day": 40.0,
    "fuel_per_kwh": 2.0,
    "levy_per_kwh": 3.0,
}


async def test_billing_uses_api_surcharges_when_options_unset(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(),
        _half_hourly(_fixed_days()),
        bill=_bill(),
        surcharges=dict(API_SURCHARGES),
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["billing"]["basic_charge"] == 400
    assert data["billing"]["fuel_adjustment"] == 110
    assert data["billing"]["renewable_levy"] == 165
    assert data["billing"]["total"] == 2325
    assert data["current_rate_fuel_per_kwh"] == 2.0
    assert data["current_rate_levy_per_kwh"] == 3.0
    assert data["current_rate_kwh"] == 41.0


async def test_options_override_api_surcharges(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry({CONF_FUEL_ADJUSTMENT_PER_KWH: 0.0}),
        _half_hourly(_fixed_days()),
        bill=_bill(),
        surcharges=dict(API_SURCHARGES),
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    # Explicit 0.0 option wins over the API's 2.0; the others fall back to API.
    assert data["current_rate_fuel_per_kwh"] == 0.0
    assert data["current_rate_levy_per_kwh"] == 3.0
    assert data["billing"]["basic_charge"] == 400
    assert data["billing"]["renewable_levy"] == 165
    assert "fuel_adjustment" not in data["billing"]


async def test_api_surcharges_match_equivalent_options_for_hourly_cost(
    hass: HomeAssistant,
) -> None:
    readings = _half_hourly(_fixed_days())
    coord_api = _make_coordinator(
        hass, _new_entry(), readings, surcharges=dict(API_SURCHARGES)
    )
    coord_opts = _make_coordinator(hass, _new_entry(dict(SURCHARGE_OPTIONS)), readings)
    with _frozen_jst():
        from_api = await coord_api._async_update_data()
        from_opts = await coord_opts._async_update_data()
    assert from_api["hourly"] == from_opts["hourly"]


async def test_surcharge_fetch_failure_keeps_previous_values(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(
        hass, _new_entry(), _half_hourly(_fixed_days()), surcharges=dict(API_SURCHARGES)
    )
    with _frozen_jst():
        await coord._async_update_data()
    coord.api.async_get_surcharges = AsyncMock(side_effect=OctopusApiError("boom"))
    coord._static_fetched_at = None
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["current_rate_fuel_per_kwh"] == 2.0
    assert data["current_rate_levy_per_kwh"] == 3.0


async def test_surcharge_auth_error_becomes_config_entry_auth_failed(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    coord.api.async_get_surcharges = AsyncMock(
        side_effect=OctopusAuthError("token expired")
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_billing_with_iso_datetime_bill_dates(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(),
        _half_hourly(_fixed_days()),
        bill={
            "bill_type": "INVOICE",
            "from_date": "2026-07-01T00:00:00+09:00",
            "to_date": "2026-07-10T23:59:59+09:00",
            "issued_date": "2026-07-11T00:00:00+09:00",
        },
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["billing"] is not None
    assert data["billing"]["kwh"] == 55.0
    assert data["billing"]["from"] == "2026-07-01"
    assert data["billing"]["to"] == "2026-07-10"
    assert data["billing_period"] is not None
    assert data["billing_period"]["bill_type"] == "INVOICE"


async def test_billing_invalid_period_is_ignored(hass: HomeAssistant) -> None:
    for bill in (
        _bill(from_date="not-a-date", to_date="also-bad"),
        _bill(from_date="2026-07-10", to_date="2026-07-01"),
        {"bill_type": None, "from_date": None, "to_date": None},
    ):
        coord = _make_coordinator(
            hass, _new_entry(), _half_hourly(_fixed_days()), bill=bill
        )
        with _frozen_jst():
            data = await coord._async_update_data()
        assert data["month_kwh"] == 120.0
        assert data["billing"] is None
        assert data["billing_period"] is None


# ---------------------------------------------------------------------------
# Persisted daily history store
# ---------------------------------------------------------------------------


async def test_async_load_restores_valid_and_drops_garbage(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    await coord._store.async_save(
        {
            "days": {
                "2026-06-01": 3.5,
                "2026-06-02": "2.5",
                "oops": 1.0,
                "2026-06-03": "junk",
                "2026-06-04": None,
                "2026-6-5": 1.0,
            }
        }
    )
    await coord.async_load()
    assert coord._stored_days == {"2026-06-01": 3.5, "2026-06-02": 2.5}


async def test_async_load_tolerates_missing_and_corrupt_store(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord = _make_coordinator(hass, _new_entry(), [])
    # Nothing persisted yet: stays empty instead of raising.
    await coord.async_load()
    assert coord._stored_days == {}

    for bad_payload in (["not", "a", "dict"], {"days": ["nope"]}, {"other": {}}):
        coord._stored_days = {"2026-01-01": 9.0}
        await coord._store.async_save(bad_payload)
        await coord.async_load()
        assert coord._stored_days == {}

    coord._stored_days = {"2026-01-01": 9.0}
    monkeypatch.setattr(
        coord._store, "async_load", AsyncMock(side_effect=OSError("disk gone"))
    )
    await coord.async_load()
    assert coord._stored_days == {}


async def test_rolling_boundary_day_not_overwritten_downward(
    hass: HomeAssistant,
) -> None:
    readings = _half_hourly({"2026-07-13": 4.0, "2026-07-14": 3.0})

    # The oldest day returned by the API sits on the rolling-retention
    # boundary and may be partial: it must never shrink a stored day,
    # while other days still accept downward corrections.
    coord = _make_coordinator(hass, _new_entry(), readings)
    coord._stored_days = {"2026-07-13": 10.0, "2026-07-14": 5.0}
    with _frozen_jst():
        data = await coord._async_update_data()
    assert coord._stored_days["2026-07-13"] == 10.0
    assert coord._stored_days["2026-07-14"] == pytest.approx(3.0)
    assert data["yesterday_kwh"] == 3.0

    # Upward revisions are accepted everywhere, including the boundary.
    coord2 = _make_coordinator(hass, _new_entry(), readings)
    coord2._stored_days = {"2026-07-13": 2.0, "2026-07-14": 5.0}
    with _frozen_jst():
        await coord2._async_update_data()
    assert coord2._stored_days["2026-07-13"] == pytest.approx(4.0)
    assert coord2._stored_days["2026-07-14"] == pytest.approx(3.0)


async def test_prune_days_keeps_newest(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(),
        _half_hourly({"2026-07-14": 2.0, "2026-07-15": 1.0}),
    )
    coord._stored_days = {
        (date(2025, 1, 1) + timedelta(days=i)).isoformat(): 1.0 for i in range(405)
    }
    with _frozen_jst():
        await coord._async_update_data()
    assert len(coord._stored_days) == MAX_DAILY_DAYS
    assert "2025-01-01" not in coord._stored_days
    assert coord._stored_days["2026-07-14"] == pytest.approx(2.0)
    assert coord._stored_days["2026-07-15"] == pytest.approx(1.0)


async def test_store_write_survives_across_instances(hass: HomeAssistant) -> None:
    entry = _new_entry()
    coord = _make_coordinator(
        hass, entry, _half_hourly({"2026-07-14": 4.0, "2026-07-15": 2.0})
    )
    with _frozen_jst():
        await coord._async_update_data()
    assert coord._stored_days

    reloaded = OctopusEnergyJpCoordinator(hass, entry, MagicMock())
    await reloaded.async_load()
    assert reloaded._stored_days == coord._stored_days


async def test_store_save_failure_does_not_fail_update(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord = _make_coordinator(
        hass, _new_entry(), _half_hourly({"2026-07-14": 4.0, "2026-07-15": 2.0})
    )
    monkeypatch.setattr(
        coord._store, "async_save", AsyncMock(side_effect=RuntimeError("disk gone"))
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["yesterday_kwh"] == 4.0
    assert coord._stored_days["2026-07-14"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Static info caching
# ---------------------------------------------------------------------------


async def test_static_info_cached_and_reused_after_failure(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(
        hass, _new_entry(), _half_hourly({"2026-07-14": 4.0, "2026-07-15": 2.0})
    )
    with _frozen_jst():
        first = await coord._async_update_data()
        assert coord.api.async_get_contract.await_count == 1

        # Within the TTL the contract/tariff are not refetched.
        second = await coord._async_update_data()
        assert coord.api.async_get_contract.await_count == 1
        assert second["yesterday_kwh"] == first["yesterday_kwh"]

        # After the TTL a failed refresh falls back to the cached values.
        coord._static_fetched_at = dt_util.now() - timedelta(days=2)
        coord.api.async_get_contract = AsyncMock(side_effect=OctopusApiError("stale"))
        third = await coord._async_update_data()
    assert third["plan_name"] == "Octopus Plan"
    assert third["yesterday_kwh"] == 4.0


# ---------------------------------------------------------------------------
# Tiered cost boundaries
# ---------------------------------------------------------------------------


async def test_tiered_cost_at_exact_boundaries(hass: HomeAssistant) -> None:
    cases = [
        # (july daily map, expected month kwh/cost/avg rate)
        ({f"2026-07-{d:02d}": float(d) for d in range(1, 16)}, 120.0, 3600, 30.0),
        ({f"2026-07-{d:02d}": 20.0 for d in range(1, 16)}, 300.0, 10080, 33.6),
        (
            {f"2026-07-{d:02d}": 20.0 for d in range(1, 15)} | {"2026-07-15": 70.0},
            350.0,
            12080,
            34.51,
        ),
    ]
    for july_days, month_kwh, cost_month, avg_rate in cases:
        coord = _make_coordinator(hass, _new_entry(), _half_hourly(july_days))
        with _frozen_jst():
            data = await coord._async_update_data()
        assert data["month_kwh"] == month_kwh
        assert data["cost_month"] == cost_month
        assert data["avg_rate"] == avg_rate


def test_tiered_cost_legacy_wrapper() -> None:
    assert _tiered_cost(150.0, TARIFF_DICTS) == pytest.approx(4680.0)
    assert _tiered_cost(
        150.0,
        [(0.0, 120.0, 30.0), (120.0, 300.0, 36.0), (300.0, None, 40.0)],
    ) == pytest.approx(4680.0)
    assert _tiered_cost(10.0, []) == 0.0


def test_coerce_rates_branches() -> None:
    assert _coerce_rates(TARIFF_DICTS) == [
        (0.0, 120.0, 30.0),
        (120.0, 300.0, 36.0),
        (300.0, None, 40.0),
    ]
    # Unsorted tuple input is sorted; malformed tuples are skipped.
    assert _coerce_rates(
        [
            (120.0, None, 36.0),
            (0.0, 120.0, 30.0),
            (1, 2),
            (5, 5, 1.0),
            ("a", "b", "c"),
            "nope",
        ]
    ) == [(0.0, 120.0, 30.0), (120.0, None, 36.0)]
    for bad in ([], "nope", None, [(1, 2)], [(5, 5, 1.0)]):
        with pytest.raises(ValueError):
            _coerce_rates(bad)


# ---------------------------------------------------------------------------
# Per-slot marginal hourly costs and current-rate sensors
# ---------------------------------------------------------------------------

TIERED_RATES: list[tuple[float, float | None, float]] = [
    (0.0, 120.0, 30.0),
    (120.0, 300.0, 36.0),
    (300.0, None, 40.0),
]


def _july_hourly_slots(
    day: str, total_kwh: float, slots: int = 4
) -> list[dict[str, Any]]:
    per = total_kwh / slots
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    out: list[dict[str, Any]] = []
    for h in range(slots):
        start = datetime.strptime(f"{day} {h:02d}:00:00", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=tz
        )
        out.append({"start": start, "kwh": per})
    return out


def test_attach_hourly_slot_costs_sum_matches_tiered_plus_surcharge() -> None:
    fuel, levy = 2.0, 3.0
    july_days = {f"2026-07-{d:02d}": float(d) for d in range(1, 16)}
    hourly: list[dict[str, Any]] = []
    for day, kwh in sorted(july_days.items()):
        hourly.extend(_july_hourly_slots(day, kwh))
    enriched = _attach_hourly_slot_costs(hourly, july_days, TIERED_RATES, fuel, levy)
    month_total = sum(july_days.values())
    expected_energy = oejp_utils.tiered_cost(month_total, TIERED_RATES)
    expected = expected_energy + (fuel + levy) * month_total
    assert sum(row["cost"] for row in enriched) == pytest.approx(expected, rel=1e-6)
    for row in enriched:
        assert set(row) == {"start", "kwh", "cost"}


def test_attach_hourly_slot_costs_uses_daily_kwh_for_prior_month_days() -> None:
    """Mid-month hourly window with store-backfilled dailies prices higher tiers."""
    fuel = levy = 0.0
    daily = {f"2026-07-{d:02d}": 10.0 for d in range(1, 15)}  # 140 kWh before day 15
    day15_only = _july_hourly_slots("2026-07-15", 15.0, slots=3)
    enriched = _attach_hourly_slot_costs(day15_only, daily, TIERED_RATES, fuel, levy)
    # First slot on the 15th: cumulative before = 140 kWh → tier-2 marginal (36)
    first_cost = enriched[0]["cost"]
    k0 = day15_only[0]["kwh"]
    energy = oejp_utils.tiered_cost(140.0 + k0, TIERED_RATES) - oejp_utils.tiered_cost(
        140.0, TIERED_RATES
    )
    assert first_cost == pytest.approx(energy, rel=1e-6)

    full_hourly: list[dict[str, Any]] = []
    for d in range(1, 15):
        full_hourly.extend(_july_hourly_slots(f"2026-07-{d:02d}", 10.0, slots=1))
    full_hourly.extend(day15_only)
    enriched_full = _attach_hourly_slot_costs(
        full_hourly, daily, TIERED_RATES, fuel, levy
    )
    target_start = day15_only[0]["start"]
    by_start = {row["start"]: row["cost"] for row in enriched_full}
    assert by_start[target_start] == pytest.approx(first_cost, rel=1e-6)


def test_attach_hourly_slot_costs_tolerates_missing_daily_and_empty_daily() -> None:
    slots = _july_hourly_slots("2026-07-15", 6.0, slots=2)
    daily_missing_prior = {"2026-07-14": 5.0}  # gap: no 2026-07-01..13
    out = _attach_hourly_slot_costs(slots, daily_missing_prior, TIERED_RATES, 1.0, 1.0)
    assert len(out) == 2
    out_empty = _attach_hourly_slot_costs(slots, {}, TIERED_RATES, 0.0, 0.0)
    assert len(out_empty) == 2


def test_month_prior_daily_kwh_sums_strictly_earlier_days() -> None:
    daily = {
        "2026-06-30": 9.0,
        "2026-07-01": 1.0,
        "2026-07-14": 14.0,
        "2026-07-15": 15.0,
    }
    assert _month_prior_daily_kwh(daily, "2026-07", "2026-07-15") == pytest.approx(15.0)
    assert _month_prior_daily_kwh(daily, "2026-06", "2026-06-30") == 0.0


async def test_hourly_cost_excludes_basic_charge(hass: HomeAssistant) -> None:
    july = {f"2026-07-{d:02d}": 5.0 for d in range(1, 16)}
    readings = _half_hourly(july)
    fuel_levy = {
        CONF_FUEL_ADJUSTMENT_PER_KWH: 2.0,
        CONF_RENEWABLE_LEVY_PER_KWH: 3.0,
    }
    coord_no_basic = _make_coordinator(hass, _new_entry(dict(fuel_levy)), readings)
    coord_with_basic = _make_coordinator(
        hass,
        _new_entry({**fuel_levy, CONF_BASIC_CHARGE_PER_DAY: 99.0}),
        readings,
    )
    with _frozen_jst():
        no_basic = await coord_no_basic._async_update_data()
        with_basic = await coord_with_basic._async_update_data()
    assert no_basic["hourly"] == with_basic["hourly"]


async def test_current_rate_keys_when_month_has_usage(hass: HomeAssistant) -> None:
    coord = _make_coordinator(
        hass,
        _new_entry(
            {
                CONF_FUEL_ADJUSTMENT_PER_KWH: 2.0,
                CONF_RENEWABLE_LEVY_PER_KWH: 3.0,
            }
        ),
        _half_hourly(_fixed_days()),
    )
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["month_kwh"] == 120.0
    assert data["current_rate_month_kwh"] == 120.0
    assert data["current_rate_tier_kwh"] == 36.0
    assert data["current_rate_next_tier_kwh"] == 40.0
    assert data["current_rate_fuel_per_kwh"] == 2.0
    assert data["current_rate_levy_per_kwh"] == 3.0
    assert data["current_rate_kwh"] == 41.0


async def test_current_rate_keys_none_without_current_month_days(
    hass: HomeAssistant,
) -> None:
    june_only = {f"2026-06-{d:02d}": 4.0 for d in range(1, 31)}
    coord = _make_coordinator(hass, _new_entry(), _half_hourly(june_only))
    coord._stored_days = dict(june_only)
    with _frozen_jst():
        data = await coord._async_update_data()
    assert data["month_kwh"] == 0.0
    assert data["current_rate_kwh"] is None
    assert data["current_rate_tier_kwh"] is None
    assert data["current_rate_next_tier_kwh"] is None
    assert data["current_rate_month_kwh"] is None
    assert data["current_rate_fuel_per_kwh"] == 0.0
    assert data["current_rate_levy_per_kwh"] == 0.0
