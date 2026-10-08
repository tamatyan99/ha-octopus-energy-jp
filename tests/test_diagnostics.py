"""Diagnostics tests: secrets redacted, title masked, arrays summarised."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASIC_CHARGE_PER_DAY,
    CONF_FUEL_ADJUSTMENT_PER_KWH,
    CONF_RENEWABLE_LEVY_PER_KWH,
    DOMAIN,
)
from custom_components.octopus_energy_jp.coordinator import OctopusEnergyJpCoordinator
from custom_components.octopus_energy_jp.diagnostics import (
    _json_safe,
    _mask_account_number,
    _mask_email,
    _summarize_list,
    async_get_config_entry_diagnostics,
)

ACCOUNT = "A-TEST1234"
EMAIL = "user@example.com"
PASSWORD = "secret"
COORD_TOKEN = "coord-token-abc-987"
ENTRY_DATA = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: PASSWORD,
    CONF_ACCOUNT_NUMBER: ACCOUNT,
}


def _new_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        title=f"Octopus Energy ({ACCOUNT})",
        data=ENTRY_DATA,
    )


def _api_client_patch():
    """Never construct a real API client during tests."""
    return patch(
        "custom_components.octopus_energy_jp.OctopusEnergyJpApiClient", MagicMock()
    )


def _offline_fetch_patch():
    """Keep the coordinator's first refresh offline."""
    return patch.object(
        OctopusEnergyJpCoordinator, "_async_update_data", AsyncMock(return_value={})
    )


def _collect_strings(obj: Any) -> list[str]:
    """Flatten every string in a nested diagnostics payload."""
    found: list[str] = []
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_collect_strings(key))
            found.extend(_collect_strings(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(_collect_strings(item))
    return found


def _sample_coordinator_data() -> dict[str, Any]:
    """Realistic coordinator payload with large arrays and nested secrets."""
    return {
        "yesterday_kwh": 12.3,
        "today_kwh": 4.5,
        "daily": [
            {"d": f"2026-07-{(i % 28) + 1:02d}", "kwh": 10.0, "cost": 300}
            for i in range(90)
        ],
        "yesterday_series": [
            {"start": f"2026-09-21T{h:02d}:00:00+09:00", "kwh": 0.4} for h in range(48)
        ],
        "today_series": [
            {"start": f"2026-09-22T{h:02d}:00:00+09:00", "kwh": 0.5} for h in range(48)
        ],
        # datetime objects are NOT JSON serialisable: only summarising
        # hourly to a count keeps the diagnostics payload serialisable.
        "hourly": [
            {"start": datetime(2026, 9, 20, h % 24, 0, 0), "kwh": 0.8}
            for h in range(72)
        ],
        "billing": {"total": 1234, "days": 5},
        "billing_period": {"from": "2026-08-01", "to": "2026-08-31"},
        "plan_name": "Standard Plan",
        "last_update": datetime(2026, 9, 22, 12, 0, 0),
        "avg_rate": 31.25,
        "token": COORD_TOKEN,
        "debug": {"email": EMAIL, "account_number": ACCOUNT},
    }


async def _setup_entry_with_data(hass, coordinator_data: dict[str, Any]):
    """Set up a real config entry, then inject coordinator data."""
    options = {
        CONF_BASIC_CHARGE_PER_DAY: 12.4,
        CONF_FUEL_ADJUSTMENT_PER_KWH: 3.8,
        CONF_RENEWABLE_LEVY_PER_KWH: 4.18,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        title=f"Octopus Energy ({ACCOUNT})",
        data=ENTRY_DATA,
        options=options,
    )
    entry.add_to_hass(hass)
    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data["coordinator"].data = coordinator_data
    return entry


async def test_diagnostics_redacts_secrets_masks_title_and_summarizes(hass):
    """Secrets never leak, title is masked, arrays are counts, JSON works."""
    payload = _sample_coordinator_data()
    entry = await _setup_entry_with_data(hass, payload)

    result = await async_get_config_entry_diagnostics(hass, entry)

    # Top-level structure with the coordinator sections present.
    assert set(result) == {"entry", "coordinator_data", "coordinator_summary"}
    assert set(result["entry"]) == {
        "title",
        "data",
        "version",
        "unique_id",
        "options",
    }
    assert result["coordinator_data"]

    strings = _collect_strings(result)
    for secret in (EMAIL, PASSWORD, COORD_TOKEN):
        assert all(secret not in text for text in strings), secret
    assert all(ACCOUNT not in text for text in strings)

    # Password is not carried at all; email/account stay identifiable-but-safe.
    assert "password" not in result["entry"]["data"]
    assert PASSWORD not in json.dumps(result)
    assert result["entry"]["version"] == entry.version
    assert result["entry"]["unique_id"] == "A-****1234"
    assert result["entry"]["options"] == entry.options
    assert result["entry"]["data"]["email"] == "u***@example.com"
    assert result["entry"]["data"]["account_number"] == "A-****1234"

    # Title embeds the account number in production; it must be masked.
    assert ACCOUNT in entry.title
    assert ACCOUNT not in result["entry"]["title"]

    # Large arrays are summarised to counts, small dicts pass through.
    assert result["coordinator_data"]["daily"] == {"count": 90}
    assert result["coordinator_data"]["yesterday_series"] == {"count": 48}
    assert result["coordinator_data"]["today_series"] == {"count": 48}
    assert result["coordinator_data"]["hourly"] == {"count": 72}
    assert result["coordinator_data"]["billing"] == {"total": 1234, "days": 5}

    assert result["coordinator_summary"] == {
        "plan_name": "Standard Plan",
        "last_update": "2026-09-22T12:00:00",
        "avg_rate": 31.25,
        "billing_period": {"from": "2026-08-01", "to": "2026-08-31"},
        "billing": {"total": 1234, "days": 5},
    }

    # HA serialises diagnostics as JSON; raw datetimes would break that.
    json.dumps(result)


async def test_diagnostics_empty_coordinator_data(hass):
    """Structure holds and secrets stay hidden when there is no data."""
    entry = _new_entry()
    entry.add_to_hass(hass)
    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert set(result) == {"entry", "coordinator_data", "coordinator_summary"}
    assert result["coordinator_data"] == {}
    assert result["coordinator_summary"] == {}
    assert ACCOUNT not in result["entry"]["title"]
    strings = _collect_strings(result)
    for secret in (EMAIL, PASSWORD):
        assert all(secret not in text for text in strings), secret
    assert all(ACCOUNT not in text for text in strings)
    json.dumps(result)


def test_mask_and_summarize_helpers_edge_cases():
    """Unit-cover every branch of the masking/summarising helpers."""
    assert _mask_account_number(None) is None
    assert _mask_account_number("") == ""
    assert _mask_account_number("ABC") == "**REDACTED**"
    assert _mask_account_number("ABCDEF") == "**REDACTED**"
    assert _mask_account_number(ACCOUNT) == "A-****1234"

    assert _mask_email(None) is None
    assert _mask_email(123) == 123
    assert _mask_email("no-at-sign") == "no-at-sign"
    assert _mask_email("@example.com") == "**REDACTED**"
    assert _mask_email("user@") == "**REDACTED**"
    assert _mask_email(EMAIL) == "u***@example.com"

    assert _summarize_list([1, 2, 3]) == {"count": 3}
    assert _summarize_list([]) == {"count": 0}
    assert _summarize_list("x") == "x"
    assert _summarize_list(None) is None
    assert _summarize_list({"a": 1}) == {"a": 1}

    dt = datetime(2026, 1, 2, 3, 4, 5)
    assert _json_safe(dt) == "2026-01-02T03:04:05"
    assert _json_safe(date(2026, 1, 2)) == "2026-01-02"
    assert _json_safe({"nested": [dt]}) == {"nested": ["2026-01-02T03:04:05"]}
    assert _json_safe(42) == 42


async def test_diagnostics_password_never_in_serialised_output(hass):
    """The entry password must not appear anywhere in diagnostics JSON."""
    entry = await _setup_entry_with_data(hass, {})
    result = await async_get_config_entry_diagnostics(hass, entry)
    serialised = json.dumps(result)
    assert PASSWORD not in serialised
    assert CONF_PASSWORD not in result["entry"]
    assert CONF_PASSWORD not in result["entry"]["data"]
