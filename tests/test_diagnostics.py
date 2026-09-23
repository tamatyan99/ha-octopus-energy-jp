"""Diagnostics tests: secrets redacted, title masked, arrays summarised."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp.const import CONF_ACCOUNT_NUMBER, DOMAIN
from custom_components.octopus_energy_jp.coordinator import OctopusEnergyJpCoordinator
from custom_components.octopus_energy_jp.diagnostics import (
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
        "token": COORD_TOKEN,
        "debug": {"email": EMAIL, "account_number": ACCOUNT},
    }


async def _setup_entry_with_data(hass, coordinator_data: dict[str, Any]):
    """Set up a real config entry, then inject coordinator data."""
    entry = _new_entry()
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

    # Top-level structure with the coordinator section present.
    assert set(result) == {"entry", "coordinator_data"}
    assert set(result["entry"]) == {"title", "data"}
    assert result["coordinator_data"]

    # No raw secret anywhere in the nested payload.
    strings = _collect_strings(result)
    for secret in (EMAIL, PASSWORD, ACCOUNT, COORD_TOKEN):
        assert all(secret not in text for text in strings), secret

    # Password is not carried at all; email/account stay identifiable-but-safe.
    assert "password" not in result["entry"]["data"]
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

    assert set(result) == {"entry", "coordinator_data"}
    assert result["coordinator_data"] == {}
    assert ACCOUNT not in result["entry"]["title"]
    strings = _collect_strings(result)
    for secret in (EMAIL, PASSWORD, ACCOUNT):
        assert all(secret not in text for text in strings), secret
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
