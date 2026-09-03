"""Octopus Energy Japan integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OctopusEnergyJpApiClient
from .const import DOMAIN
from .coordinator import OctopusEnergyJpCoordinator
from .statistics import OctopusStatisticsImporter

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


def _hourly_signature(hourly: list) -> tuple:
    """hourly の簡易シグネチャ（件数 + 最終start）を返す。"""
    try:
        last = hourly[-1] if hourly else None
        if isinstance(last, dict):
            last_start = last.get("start")
        else:
            last_start = getattr(last, "start", last)
        return (len(hourly), str(last_start))
    except Exception:  # noqa: BLE001 - シグネチャ計算の失敗ではimportを止めない
        try:
            return (len(hourly), "")
        except Exception:  # noqa: BLE001
            return (0, "")


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

    try:
        # 新シグネチャ (hass, entry_id, account_number) に対応
        importer = OctopusStatisticsImporter(
            hass, entry.entry_id, coordinator.account_number
        )
    except (TypeError, AttributeError):
        # 旧シグネチャ (hass, entry_id) の importer でも動作させる
        importer = OctopusStatisticsImporter(hass, entry.entry_id)
    await importer.async_load()
    last_signature: tuple | None = None
    hourly = _get_hourly(coordinator.data)
    if hourly is not None:
        await importer.async_import(hourly)
        last_signature = _hourly_signature(hourly)

    async def _safe_import(hourly_data: list) -> None:
        """例外を握り潰さずログに残す import ラッパー。"""
        try:
            await importer.async_import(hourly_data)
        except Exception:  # noqa: BLE001 - バックグラウンドimportの失敗を記録
            _LOGGER.exception("統計のインポートに失敗しました")

    def _log_task_done(task) -> None:
        """fire-and-forget タスクの例外をログに出す。"""
        try:
            exc = task.exception()
        except Exception:  # noqa: BLE001 - キャンセル時等の取得失敗
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
        task = hass.async_create_task(_safe_import(hourly_data))
        task.add_done_callback(_log_task_done)

    entry.async_on_unload(coordinator.async_add_listener(_import_on_update))

    entry.runtime_data = {"coordinator": coordinator, "importer": importer}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
