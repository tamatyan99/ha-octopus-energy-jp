"""External statistics importer for the Energy Dashboard."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
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
    統計が互いを上書きしない。
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, account_number: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_statistics"
        )
        self._account_number = account_number
        self._statistic_id = statistic_id_for_account(DOMAIN, account_number)
        self._statistic_name = f"Octopus Energy Japan consumption ({account_number})"
        self._last_start: datetime | None = None
        self._earliest_start: datetime | None = None
        self._cumulative: float = 0.0

    async def _recover_baseline_from_recorder(self) -> None:
        """Seed import state from the recorder when the local store is gone.

        Deleting and re-adding the config entry (new entry_id, same
        statistic_id), a corrupt/missing store, or a storage version change
        would otherwise restart the cumulative sum from 0 and overwrite
        existing recorder rows with lower sums. Consult the recorder's last
        stored row instead and continue from there.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                get_last_statistics,
            )

            # get_last_statistics() は同期関数で、recorder の DB セッションを
            # 直接開く。イベントループを塞がないよう executor 経由で呼ぶ。
            # types は必須引数で、内部で in-place に削られるため毎回新しい
            # set を渡す。convert_units=False は保存済みの生の sum を得るため
            # (単位変換されると累積の連続性が崩れる)。
            rows = await get_instance(self._hass).async_add_executor_job(
                get_last_statistics,
                self._hass,
                1,
                self._statistic_id,
                False,
                {"sum", "state"},
            )
        except Exception as err:  # noqa: BLE001 - recorder unavailable
            _LOGGER.warning("Recorder baseline lookup failed, starting fresh: %s", err)
            return
        last_row: dict[str, Any] | None = None
        if isinstance(rows, dict):
            candidates = rows.get(self._statistic_id)
            if isinstance(candidates, list) and candidates:
                last_row = candidates[-1] if isinstance(candidates[-1], dict) else None
        elif isinstance(rows, list) and rows:
            last_row = rows[-1] if isinstance(rows[-1], dict) else None
        if last_row is None:
            return
        try:
            raw_start = last_row.get("start")
            start_ms = float(raw_start)  # type: ignore[arg-type]
            start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            _LOGGER.debug("Ignoring recorder baseline with unusable start: %r", rows)
            return
        raw_sum = last_row.get("sum")
        try:
            baseline = 0.0 if raw_sum is None else float(raw_sum)
        except (TypeError, ValueError):
            baseline = 0.0
        if not math.isfinite(baseline):
            baseline = 0.0
        self._last_start = dt_util.as_local(start)
        # NOTE: the true earliest imported hour is unknown (only the last row
        # was queried), so seed a sentinel old enough that the "fetch window
        # widened into the past" full re-import branch in async_import never
        # fires on the recovered path. A from-0 full re-import over the ~1
        # month API window would overwrite recorder rows that continue an
        # older cumulative sum; only strictly newer buckets are imported.
        self._earliest_start = dt_util.as_local(datetime(1970, 1, 1, tzinfo=UTC))
        self._cumulative = baseline
        _LOGGER.warning(
            "Local statistics state was missing; recovered baseline from "
            "recorder (statistic_id=%s, sum=%s)",
            self._statistic_id,
            baseline,
        )

    async def async_load(self) -> None:
        """Restore persisted import state (corruption-tolerant)."""
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - store backend failure
            _LOGGER.warning("Failed to load statistics state, starting fresh: %s", err)
            await self._recover_baseline_from_recorder()
            return
        if not data:
            await self._recover_baseline_from_recorder()
            return
        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring corrupt statistics state, starting fresh")
            await self._recover_baseline_from_recorder()
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
