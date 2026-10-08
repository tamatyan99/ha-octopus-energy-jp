"""Config flow for Octopus Energy Japan."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
        return OctopusEnergyJpOptionsFlow()

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
        except Exception:
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

    async def _async_try_update_credentials(
        self,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any],
        abort_reason: str,
    ) -> tuple[dict[str, str], config_entries.ConfigFlowResult | None]:
        """Validate credentials and update the entry when the account matches."""
        errors, account = await self._async_validate(
            user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
        )
        if errors:
            return errors, None
        if account == entry.unique_id:
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    CONF_EMAIL: user_input[CONF_EMAIL],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_ACCOUNT_NUMBER: account,
                },
            )
            return {}, self.async_abort(reason=abort_reason)
        return {"base": "account_mismatch"}, None

    async def async_step_reauth(
        self, _user_input: Mapping[str, Any] | None = None
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
            errors, result = await self._async_try_update_credentials(
                entry, user_input, "reauth_successful"
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Reconfigure credentials for an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, result = await self._async_try_update_credentials(
                entry, user_input, "reconfigure_successful"
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="reconfigure", data_schema=DATA_SCHEMA, errors=errors
        )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the options schema, pre-filling current values.

    未設定の項目に default=None を渡すと、その項目を送信しなくても
    HA のスキーマ検証が失敗する（= 一部の項目だけ設定できない）。
    そのため値がある項目にだけ default を付ける。
    """

    def _suggest(key: str) -> float | None:
        value = current.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    fields: dict[Any, Any] = {}
    for key, config in (
        (
            CONF_BASIC_CHARGE_PER_DAY,
            selector.NumberSelectorConfig(
                min=0, step=0.01, mode=selector.NumberSelectorMode.BOX
            ),
        ),
        (
            CONF_FUEL_ADJUSTMENT_PER_KWH,
            selector.NumberSelectorConfig(
                step=0.01, mode=selector.NumberSelectorMode.BOX
            ),
        ),
        (
            CONF_RENEWABLE_LEVY_PER_KWH,
            selector.NumberSelectorConfig(
                min=0, step=0.01, mode=selector.NumberSelectorMode.BOX
            ),
        ),
    ):
        suggested = _suggest(key)
        marker = (
            vol.Optional(key)
            if suggested is None
            else vol.Optional(key, default=suggested)
        )
        fields[marker] = selector.NumberSelector(config)
    return vol.Schema(fields)


class OctopusEnergyJpOptionsFlow(config_entries.OptionsFlow):
    """Handle options for billing-period surcharges (all optional)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the surcharge options form."""
        if user_input is not None:
            cleaned: dict[str, float] = {}
            for key in (
                CONF_BASIC_CHARGE_PER_DAY,
                CONF_FUEL_ADJUSTMENT_PER_KWH,
                CONF_RENEWABLE_LEVY_PER_KWH,
            ):
                raw = user_input.get(key)
                if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                    continue
                try:
                    cleaned[key] = float(raw)
                except (TypeError, ValueError):
                    continue
            return self.async_create_entry(title="", data=cleaned)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(dict(self.config_entry.options)),
        )
