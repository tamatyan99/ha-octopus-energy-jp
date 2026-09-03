"""Config flow for Octopus Energy Japan."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OctopusApiError, OctopusAuthError, OctopusEnergyJpApiClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BASIC_CHARGE_PER_DAY,
    CONF_FUEL_ADJUSTMENT_PER_KWH,
    CONF_RENEWABLE_LEVY_PER_KWH,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


class OctopusEnergyJpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return OctopusEnergyJpOptionsFlow(config_entry)

    async def _async_validate(
        self, email: str, password: str
    ) -> tuple[dict[str, str], str | None]:
        """Validate credentials, returning (errors, account_number)."""
        api = OctopusEnergyJpApiClient(
            async_get_clientsession(self.hass), email, password
        )
        try:
            account = await api.async_get_account_number()
        except OctopusAuthError:
            return {"base": "invalid_auth"}, None
        except OctopusApiError:
            return {"base": "cannot_connect"}, None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error during validation")
            return {"base": "unknown"}, None
        return {}, account

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for credentials and create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, account = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if account is not None:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Octopus Energy ({account})",
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ACCOUNT_NUMBER: account,
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication when credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask for new credentials and update the existing entry."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, account = await self._async_validate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if account is not None and account == entry.unique_id:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ACCOUNT_NUMBER: account,
                    },
                )
            if account is not None:
                errors["base"] = "account_mismatch"
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=DATA_SCHEMA, errors=errors
        )


def _optional_number(value: Any) -> float | None:
    """Accept empty input as None, otherwise coerce to float."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise vol.Invalid("expected a number") from None


def _optional_non_negative(value: Any) -> float | None:
    """Accept empty input as None, otherwise require a non-negative float."""
    num = _optional_number(value)
    if num is not None and num < 0:
        raise vol.Invalid("must be non-negative")
    return num


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the options schema, pre-filling current values."""

    def _suggest(key: str) -> Any:
        value = current.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return vol.Schema(
        {
            vol.Optional(
                CONF_BASIC_CHARGE_PER_DAY, default=_suggest(CONF_BASIC_CHARGE_PER_DAY)
            ): _optional_non_negative,
            vol.Optional(
                CONF_FUEL_ADJUSTMENT_PER_KWH,
                default=_suggest(CONF_FUEL_ADJUSTMENT_PER_KWH),
            ): _optional_number,
            vol.Optional(
                CONF_RENEWABLE_LEVY_PER_KWH,
                default=_suggest(CONF_RENEWABLE_LEVY_PER_KWH),
            ): _optional_non_negative,
        }
    )


class OctopusEnergyJpOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """Handle options for billing-period surcharges (all optional)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Store the entry explicitly (base class keeps it private)."""
        super().__init__(config_entry)
        self._options_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the surcharge options form."""
        if user_input is not None:
            cleaned = {
                key: float(user_input[key])
                for key in (
                    CONF_BASIC_CHARGE_PER_DAY,
                    CONF_FUEL_ADJUSTMENT_PER_KWH,
                    CONF_RENEWABLE_LEVY_PER_KWH,
                )
                if user_input.get(key) is not None
            }
            return self.async_create_entry(title="", data=cleaned)
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self._options_entry.options))
        )
