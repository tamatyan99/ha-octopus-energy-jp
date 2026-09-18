"""Config flow and options flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp.api import OctopusApiError, OctopusAuthError
from custom_components.octopus_energy_jp.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASIC_CHARGE_PER_DAY,
    CONF_FUEL_ADJUSTMENT_PER_KWH,
    CONF_RENEWABLE_LEVY_PER_KWH,
    DOMAIN,
)
from custom_components.octopus_energy_jp.sensor import SENSORS

ACCOUNT = "A-TEST1234"
CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"}
ENTRY_DATA = {**CREDENTIALS, CONF_ACCOUNT_NUMBER: ACCOUNT}


def _patched_client(account: str | None = None, error: Exception | None = None):
    """Patch target for the API client used by the config flow."""
    client = MagicMock()
    if error is not None:
        client.async_get_account_number = AsyncMock(side_effect=error)
    else:
        client.async_get_account_number = AsyncMock(return_value=account)
    return MagicMock(return_value=client)


async def test_user_flow_creates_entry(hass):
    """A valid account creates a config entry."""
    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(account=ACCOUNT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Octopus Energy ({ACCOUNT})"
    assert result["data"][CONF_ACCOUNT_NUMBER] == ACCOUNT


async def test_user_flow_invalid_auth(hass):
    """An auth failure surfaces invalid_auth on the form."""
    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(error=OctopusAuthError("nope")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    """A transport failure surfaces cannot_connect on the form."""
    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(error=OctopusApiError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_duplicate_account_aborts(hass):
    """The same account cannot be configured twice."""
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA, title="existing"
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(account=ACCOUNT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_updates_entry(hass):
    """Re-auth with the same account updates the entry."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(account=ACCOUNT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpw"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_EMAIL] == "new@example.com"


async def test_reauth_flow_account_mismatch(hass):
    """Different account during re-auth is rejected without touching the entry."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.octopus_energy_jp.config_flow.OctopusEnergyJpApiClient",
        _patched_client(account="A-OTHER999"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "account_mismatch"}


async def test_options_flow_stores_numeric_values(hass):
    """Options flow accepts numbers and persists them."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_BASIC_CHARGE_PER_DAY: 12.4,
            CONF_FUEL_ADJUSTMENT_PER_KWH: 3.8,
            CONF_RENEWABLE_LEVY_PER_KWH: 4.18,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BASIC_CHARGE_PER_DAY] == 12.4
    assert entry.options[CONF_RENEWABLE_LEVY_PER_KWH] == 4.18


async def test_options_flow_stores_only_provided_values(hass):
    """Omitted surcharges are not persisted."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BASIC_CHARGE_PER_DAY: 12.4}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_BASIC_CHARGE_PER_DAY: 12.4}


async def test_options_flow_rejects_non_numeric_input(hass):
    """A non-numeric value is rejected by the schema before our code runs."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_BASIC_CHARGE_PER_DAY: "not-a-number"}
        )


def test_sensor_keys_are_unique_and_legacy_usage_is_gone():
    """v0.3.0 removed the duplicate usage sensor; keys must stay unique."""
    keys = [description.key for description in SENSORS]
    assert "usage" not in keys
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("key", ["yesterday_kwh", "today_kwh", "billing_kwh"])
def test_expected_sensor_keys_present(key):
    """The main sensors remain available."""
    assert key in [description.key for description in SENSORS]
