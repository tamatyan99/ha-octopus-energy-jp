"""Diagnostics for the Octopus Energy Japan integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {"password", "token", "email", "account_number"}


def _mask_account_number(value: Any) -> Any:
    """Mask an account number, e.g. 'A-****43A0'."""
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 6:
        return "**REDACTED**"
    return f"{value[:2]}****{value[-4:]}"


def _mask_email(value: Any) -> Any:
    """Mask an email address, keeping first char of local part + domain."""
    if not isinstance(value, str) or "@" not in value:
        return value
    local, _, domain = value.partition("@")
    if not local or not domain:
        return "**REDACTED**"
    return f"{local[:1]}***@{domain}"


def _summarize_list(value: Any) -> Any:
    """Replace a large list payload with its item count."""
    if isinstance(value, list):
        return {"count": len(value)}
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Large arrays (daily / series / hourly) are summarized to counts only.
    Secrets are never included; account number and email are masked.
    """
    coordinator = entry.runtime_data.get("coordinator") if entry.runtime_data else None
    data: dict[str, Any] = dict(coordinator.data) if coordinator and coordinator.data else {}

    summarized: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("daily", "yesterday_series", "today_series", "hourly"):
            summarized[key] = _summarize_list(value)
        else:
            summarized[key] = value

    entry_data = {
        "email": entry.data.get("email"),
        "account_number": entry.data.get("account_number"),
    }

    redacted_entry = async_redact_data(entry_data, TO_REDACT)
    # Keep account/email identifiable-but-safe for support: masked, not dropped.
    redacted_entry["email"] = _mask_email(entry_data["email"])
    redacted_entry["account_number"] = _mask_account_number(
        entry_data["account_number"]
    )

    diagnostics = {
        "entry": {
            "title": async_redact_data(entry.title, TO_REDACT),
            "data": async_redact_data(entry_data, TO_REDACT),
        },
        "coordinator_data": async_redact_data(summarized, TO_REDACT),
    }
    return diagnostics
