"""Octopus Energy Japan integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import OctopusEnergyJpApiClient
from .const import CONF_ACCOUNT_NUMBER, DOMAIN, STORAGE_VERSION
from .coordinator import OctopusEnergyJpCoordinator
from .statistics import OctopusStatisticsImporter
from .utils import cost_statistic_id_for_account, statistic_id_for_account

PLATFORMS = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


def _get_hourly(data: Any) -> list | None:
    """coordinator.data から hourly リストを安全に取り出す。"""
    if not data or not isinstance(data, dict):
        return None
    hourly = data.get("hourly")
    if not hourly:
        return None
    return hourly


def _hourly_metric(row: Any, key: str) -> float:
    """hourly 行から数値を安全に取り出す（シグネチャ用、I/O なし）。"""
    try:
        if isinstance(row, dict):
            raw = row.get(key)
        else:
            raw = getattr(row, key, None)
        if raw is None:
            return 0.0
        return float(raw)
    except Exception:  # noqa: BLE001
        return 0.0


def _hourly_signature(hourly: list) -> tuple:
    """hourly の簡易シグネチャ（件数 + 最終start + kWh/料金ダイジェスト）を返す。"""
    try:
        last = hourly[-1] if hourly else None
        if isinstance(last, dict):
            last_start = last.get("start")
        else:
            last_start = getattr(last, "start", last)
        total_kwh = round(sum(_hourly_metric(row, "kwh") for row in hourly), 4)
        last_kwh = round(_hourly_metric(last, "kwh") if last is not None else 0.0, 4)
        total_cost = round(sum(_hourly_metric(row, "cost") for row in hourly), 4)
        last_cost = round(_hourly_metric(last, "cost") if last is not None else 0.0, 4)
        return (
            len(hourly),
            str(last_start),
            total_kwh,
            last_kwh,
            total_cost,
            last_cost,
        )
    except Exception:  # noqa: BLE001 - シグネチャ計算の失敗ではimportを止めない
        try:
            return (len(hourly), "", 0.0, 0.0, 0.0, 0.0)
        except Exception:  # noqa: BLE001
            return (0, "", 0.0, 0.0, 0.0, 0.0)


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    api = OctopusEnergyJpApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
    coordinator = OctopusEnergyJpCoordinator(hass, entry, api)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    # v0.3.0 で削除した legacy `usage` センサーの残存レジストリエントリを掃除する。
    try:
        registry = er.async_get(hass)
        legacy_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{coordinator.account_number}_usage"
        )
        if legacy_entity_id is not None:
            registry.async_remove(legacy_entity_id)
            _LOGGER.info("Removed legacy duplicate sensor entity %s", legacy_entity_id)
    except Exception as err:  # noqa: BLE001 - cleanup must not fail setup
        _LOGGER.debug("Failed to remove legacy usage sensor entity: %s", err)

    # options 変更時はエントリをリロード（coordinator が最新 options を参照する）
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    importer = OctopusStatisticsImporter(
        hass, entry.entry_id, coordinator.account_number
    )
    cost_importer = OctopusStatisticsImporter(
        hass,
        entry.entry_id,
        coordinator.account_number,
        series="cost",
    )
    await importer.async_load()
    await cost_importer.async_load()
    last_signature: tuple | None = None
    hourly = _get_hourly(coordinator.data)
    if hourly is not None:
        await importer.async_import(hourly)
        await cost_importer.async_import(hourly)
        last_signature = _hourly_signature(hourly)

    async def _safe_import(hourly_data: list) -> None:
        """例外を握り潰さずログに残す import ラッパー。"""
        try:
            await importer.async_import(hourly_data)
            await cost_importer.async_import(hourly_data)
        except Exception:
            _LOGGER.exception("統計のインポートに失敗しました")

    def _log_task_done(task) -> None:
        """fire-and-forget タスクの例外をログに出す。"""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:
            _LOGGER.exception("インポートタスクの状態取得に失敗しました")
            return
        if exc is not None:
            _LOGGER.exception("統計のインポートタスクが失敗しました", exc_info=exc)

    def _import_on_update() -> None:
        nonlocal last_signature
        hourly_data = _get_hourly(coordinator.data)
        if hourly_data is None:
            return
        # データ変化がない場合は無駄なimport/store書き込みを避ける
        signature = _hourly_signature(hourly_data)
        if signature == last_signature:
            return
        last_signature = signature
        task = entry.async_create_background_task(
            hass, _safe_import(hourly_data), name="octopus_energy_jp import"
        )
        task.add_done_callback(_log_task_done)

    entry.async_on_unload(coordinator.async_add_listener(_import_on_update))

    entry.runtime_data = {
        "coordinator": coordinator,
        "importer": importer,
        "cost_importer": cost_importer,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove persistent stores when the config entry is deleted."""
    for key in (
        f"{DOMAIN}_{entry.entry_id}_daily",
        f"{DOMAIN}_{entry.entry_id}_statistics",
        f"{DOMAIN}_{entry.entry_id}_statistics_cost",
    ):
        try:
            await Store(hass, STORAGE_VERSION, key).async_remove()
        except Exception as err:  # noqa: BLE001 - missing store must not fail
            _LOGGER.debug("Failed to remove store %s: %s", key, err)

    account = entry.unique_id or entry.data.get(CONF_ACCOUNT_NUMBER)
    if account:
        statistic_ids = [
            statistic_id_for_account(DOMAIN, account),
            cost_statistic_id_for_account(DOMAIN, account),
        ]
        try:
            get_instance(hass).async_clear_statistics(statistic_ids)
        except Exception as err:  # noqa: BLE001 - recorder absent/disabled must not fail
            _LOGGER.debug("Failed to clear recorder statistics: %s", err)
