"""External statistics importer for the Energy Dashboard."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STATS_IMPORT_BUFFER, STORAGE_VERSION
from .utils import statistic_id_for_account

_LOGGER = logging.getLogger(__name__)

# 旧単一アカウント時代の statistic_id（account_number 未指定時のフォールバック）
LEGACY_STATISTIC_ID = f"{DOMAIN}:consumption"


class OctopusStatisticsImporter:
    """Import confirmed hourly consumption into recorder statistics.

    Readings arrive ~8 hours late, so only fully settled hours are imported.
    The cumulative sum and the imported range are persisted so restarts and
    re-imports stay idempotent (re-importing the same hour overwrites it).

    When the coordinator widens its fetch window into the past (e.g. a new
    multi-month history feature), a full re-import is triggered once so the
    older hours are backfilled with a consistent cumulative sum.

    正規ルートでは coordinator.account_number を account_number 引数に渡す
    こと。各 config entry が自分専用の statistic_id を持つため、複数契約の
    統計が互いを上書きしない。account_number 省略時は旧 ID にフォールバック
    する後方互換モードとなる。
    """

    def __init__(
        self, hass: HomeAssistant, entry_id: str, account_number: str | None = None
    ) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_statistics"
        )
        self._account_number = account_number
        if account_number:
            self._statistic_id = statistic_id_for_account(DOMAIN, account_number)
            self._statistic_name = (
                f"Octopus Energy Japan consumption ({account_number})"
            )
        else:
            _LOGGER.warning(
                "OctopusStatisticsImporter without account_number; "
                "falling back to legacy statistic id"
            )
            self._statistic_id = LEGACY_STATISTIC_ID
            self._statistic_name = "Octopus Energy Japan consumption"
        self._last_start: datetime | None = None
        self._earliest_start: datetime | None = None
        self._cumulative: float = 0.0

    async def async_load(self) -> None:
        """Restore persisted import state (corruption-tolerant)."""
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - store backend failure
            _LOGGER.warning("Failed to load statistics state, starting fresh: %s", err)
            return
        if not data:
            return
        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring corrupt statistics state, starting fresh")
            return

        raw_last = data.get("last_start", "")
        last_start = (
            dt_util.parse_datetime(raw_last) if isinstance(raw_last, str) else None
        )
        if last_start is not None:
            self._last_start = last_start
        raw_earliest = data.get("earliest_start", "")
        earliest_start = (
            dt_util.parse_datetime(raw_earliest)
            if isinstance(raw_earliest, str)
            else None
        )
        if earliest_start is not None:
            self._earliest_start = earliest_start
        else:
            # 旧形式（earliest_start なし）: last_start と同じとみなす
            self._earliest_start = self._last_start
        try:
            cumulative = float(data.get("cumulative", 0.0))
        except (TypeError, ValueError):
            cumulative = 0.0
        if not math.isfinite(cumulative):
            cumulative = 0.0
        self._cumulative = cumulative

    async def async_import(self, hourly: list[dict[str, Any]]) -> None:
        """Import newly settled hours from coordinator data."""
        if not isinstance(hourly, list):
            return
        now = dt_util.now()
        settled: list[dict[str, Any]] = []
        for item in hourly:
            if not isinstance(item, dict):
                continue
            raw_start = item.get("start")
            if not isinstance(raw_start, datetime):
                continue
            try:
                start_local = dt_util.as_local(raw_start)
            except Exception:  # noqa: BLE001 - defensive tz conversion
                continue
            try:
                kwh = float(item.get("kwh"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(kwh):
                continue
            if start_local + timedelta(hours=1) <= now - STATS_IMPORT_BUFFER:
                settled.append({"start": start_local, "kwh": kwh})
        if not settled:
            return
        settled.sort(key=lambda s: s["start"])

        if (
            self._last_start is None
            or self._earliest_start is None
            or settled[0]["start"] < self._earliest_start
        ):
            # 初回、または取得範囲が過去に拡大された場合は全件再投入（上書きで冪等）
            cumulative = 0.0
            targets = settled
            self._earliest_start = settled[0]["start"]
        else:
            cumulative = self._cumulative
            overlap = [s for s in settled if s["start"] <= self._last_start]
            targets = [s for s in settled if s["start"] > self._last_start]
            if overlap and settled[0]["start"] == self._earliest_start:
                # 訂正追従の簡易策: settled が既知範囲全体を覆う場合、
                # 既存累積と settled 合計の不整合は過去値の訂正とみなして
                # settled 全体を再投入（上書き冪等）する
                fresh_total = sum(s["kwh"] for s in settled)
                expected = cumulative + sum(s["kwh"] for s in targets)
                if abs(fresh_total - expected) > 1e-6:
                    _LOGGER.debug("Detected revised past readings; re-importing all")
                    cumulative = 0.0
                    targets = settled

        if not targets:
            return

        points: list[StatisticData] = []
        for item in targets:
            cumulative += item["kwh"]
            points.append(
                {
                    "start": dt_util.as_utc(item["start"]),
                    "sum": round(cumulative, 3),
                }
            )

        if not points:
            return

        metadata: StatisticMetaData = {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": self._statistic_name,
            "source": DOMAIN,
            "statistic_id": self._statistic_id,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        async_add_external_statistics(self._hass, metadata, points)
        self._last_start = targets[-1]["start"]
        if self._earliest_start is None:
            self._earliest_start = targets[0]["start"]
        self._cumulative = cumulative
        if self._last_start is None:
            return
        earliest = self._earliest_start
        payload: dict[str, Any] = {
            "last_start": self._last_start.isoformat(),
            "earliest_start": (
                earliest.isoformat()
                if earliest is not None
                else self._last_start.isoformat()
            ),
            "cumulative": self._cumulative,
        }
        try:
            await self._store.async_save(payload)
        except Exception as err:  # noqa: BLE001 - persistence must not fail import
            _LOGGER.warning("Failed to save statistics state: %s", err)
            return
        _LOGGER.debug("Imported %d statistics points", len(points))
