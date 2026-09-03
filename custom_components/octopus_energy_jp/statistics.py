"""External statistics importer for the Energy Dashboard."""
from __future__ import annotations

import logging
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

from .const import DOMAIN, STATISTIC_ID_CONSUMPTION, STATS_IMPORT_BUFFER, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class OctopusStatisticsImporter:
    """Import confirmed hourly consumption into recorder statistics.

    Readings arrive ~8 hours late, so only fully settled hours are imported.
    The cumulative sum and the imported range are persisted so restarts and
    re-imports stay idempotent (re-importing the same hour overwrites it).

    When the coordinator widens its fetch window into the past (e.g. a new
    multi-month history feature), a full re-import is triggered once so the
    older hours are backfilled with a consistent cumulative sum.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_statistics"
        )
        self._last_start: datetime | None = None
        self._earliest_start: datetime | None = None
        self._cumulative: float = 0.0

    async def async_load(self) -> None:
        """Restore persisted import state."""
        data = await self._store.async_load()
        if not data:
            return
        last_start = dt_util.parse_datetime(data.get("last_start", ""))
        if last_start is not None:
            self._last_start = last_start
        earliest_start = dt_util.parse_datetime(data.get("earliest_start", ""))
        if earliest_start is not None:
            self._earliest_start = earliest_start
        else:
            # 旧形式（earliest_start なし）: last_start と同じとみなす
            self._earliest_start = self._last_start
        self._cumulative = float(data.get("cumulative", 0.0))

    async def async_import(self, hourly: list[dict[str, Any]]) -> None:
        """Import newly settled hours from coordinator data."""
        now = dt_util.now()
        settled = [
            item
            for item in hourly
            if item["start"] + timedelta(hours=1) <= now - STATS_IMPORT_BUFFER
        ]
        if not settled:
            return

        if self._last_start is None or settled[0]["start"] < self._earliest_start:
            # 初回、または取得範囲が過去に拡大された場合は全件再投入（上書きで冪等）
            cumulative = 0.0
            targets = settled
            self._earliest_start = settled[0]["start"]
        else:
            cumulative = self._cumulative
            targets = [
                item for item in settled if item["start"] > self._last_start
            ]

        points: list[StatisticData] = []
        for item in targets:
            cumulative += item["kwh"]
            points.append({"start": item["start"], "sum": round(cumulative, 3)})

        if not points:
            return

        metadata: StatisticMetaData = {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": "Octopus Energy Japan consumption",
            "source": DOMAIN,
            "statistic_id": STATISTIC_ID_CONSUMPTION,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        }
        async_add_external_statistics(self._hass, metadata, points)
        self._last_start = targets[-1]["start"]
        self._cumulative = cumulative
        await self._store.async_save(
            {
                "last_start": self._last_start.isoformat(),
                "earliest_start": self._earliest_start.isoformat(),
                "cumulative": self._cumulative,
            }
        )
        _LOGGER.debug("Imported %d statistics points", len(points))
