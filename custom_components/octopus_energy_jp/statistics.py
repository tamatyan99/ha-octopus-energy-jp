"""External statistics importer for the Energy Dashboard."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

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
from .utils import cost_statistic_id_for_account, statistic_id_for_account

_LOGGER = logging.getLogger(__name__)

# Seeded in _recover_baseline_from_recorder so widen-into-past never runs on recovery.
_RECOVERY_EARLIEST_UTC = datetime(1970, 1, 1, tzinfo=UTC)

SeriesKind = Literal["consumption", "cost"]


class OctopusStatisticsImporter:
    """Import confirmed hourly consumption into recorder statistics.

    Readings arrive ~8 hours late, so only fully settled hours are imported.
    The cumulative sum and the imported range are persisted so restarts and
    re-imports stay idempotent (re-importing the same hour overwrites it).

    When the coordinator widens its fetch window into the past (e.g. a new
    multi-month history feature), a full re-import is triggered once so the
    older hours are backfilled with a consistent cumulative sum.

    Hours still inside the fetch window are stored individually. A revised
    reading or a new tariff updates those hours and the running sum after
    them. Hours that have scrolled out of the window stay in ``_prefix_sum``
    and are not restarted at 0. Stores written before the hour map, and a
    baseline recovered from the recorder, keep the append-only path.

    正規ルートでは coordinator.account_number を account_number 引数に渡す
    こと。各 config entry が自分専用の statistic_id を持つため、複数契約の
    統計が互いを上書きしない。
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        account_number: str,
        *,
        series: SeriesKind = "consumption",
    ) -> None:
        self._hass = hass
        self._series = series
        if series == "cost":
            store_key = f"{DOMAIN}_{entry_id}_statistics_cost"
            self._statistic_id = cost_statistic_id_for_account(DOMAIN, account_number)
            self._statistic_name = f"Octopus Energy Japan cost ({account_number})"
            # HA Core cost external stats (opower, srp_energy) also omit unit_class/UoM.
            self._unit_class: str | None = None
            self._unit_of_measurement: str | None = None
            self._value_key = "cost"
            self._include_state = True
        else:
            store_key = f"{DOMAIN}_{entry_id}_statistics"
            self._statistic_id = statistic_id_for_account(DOMAIN, account_number)
            self._statistic_name = (
                f"Octopus Energy Japan consumption ({account_number})"
            )
            self._unit_class = "energy"
            self._unit_of_measurement = "kWh"
            self._value_key = "kwh"
            self._include_state = False
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, store_key)
        self._account_number = account_number
        self._last_start: datetime | None = None
        self._earliest_start: datetime | None = None
        self._cumulative: float = 0.0
        # None: per-hour values are unknown (legacy store or recorder recovery).
        # dict: hours still in the fetch window. Older hours live in _prefix_sum.
        self._prefix_sum: float = 0.0
        self._buckets: dict[datetime, float] | None = {}

    async def _recover_baseline_from_recorder(self) -> None:
        """Seed import state from the recorder when the local store is gone.

        A corrupt or missing store, a storage version change, or restoring from
        backup without the integration's storage would otherwise restart the
        cumulative sum from 0 and overwrite existing recorder rows with lower
        sums. Consult the recorder's last stored row instead and continue from
        there. Removing the config entry clears both the integration stores and
        its recorder statistics; re-adding starts fresh from the API only.
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
            start_s = float(raw_start)  # type: ignore[arg-type]
            # Recorder rows use start_ts epoch seconds (not WebSocket ms).
            start = datetime.fromtimestamp(start_s, tz=UTC)
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
        # Per-hour values are also unknown, so keep the legacy append-only path.
        self._earliest_start = dt_util.as_local(_RECOVERY_EARLIEST_UTC)
        self._cumulative = baseline
        self._prefix_sum = 0.0
        self._buckets = None
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
        self._buckets = self._restore_buckets(data)
        if self._buckets is None:
            self._prefix_sum = 0.0
        else:
            try:
                prefix_sum = float(data.get("prefix_sum", 0.0))
            except (TypeError, ValueError):
                prefix_sum = 0.0
            if not math.isfinite(prefix_sum):
                prefix_sum = 0.0
            bucket_total = prefix_sum + sum(self._buckets.values())
            if abs(bucket_total - self._cumulative) > 1e-3:
                _LOGGER.warning(
                    "Ignoring statistics buckets that do not match cumulative"
                )
                self._buckets = None
                self._prefix_sum = 0.0
            else:
                self._prefix_sum = prefix_sum

    def _restore_buckets(self, data: dict[str, Any]) -> dict[datetime, float] | None:
        """Return the persisted hour map, or None when this store predates it."""
        raw_buckets = data.get("buckets")
        if not isinstance(raw_buckets, list):
            return None
        restored: dict[datetime, float] = {}
        for item in raw_buckets:
            if not isinstance(item, dict):
                return None
            raw_start = item.get("start")
            start = (
                dt_util.parse_datetime(raw_start)
                if isinstance(raw_start, str)
                else None
            )
            if start is None:
                return None
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            restored[dt_util.as_local(start)] = value
        return restored

    def _collect_settled(self, hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return settled hour rows sorted by local start."""
        if not isinstance(hourly, list):
            return []
        value_key = self._value_key
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
                amount = float(item.get(value_key))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amount):
                continue
            if start_local + timedelta(hours=1) <= now - STATS_IMPORT_BUFFER:
                settled.append({"start": start_local, value_key: amount})
        settled.sort(key=lambda row: row["start"])
        return settled

    def _collapse_hours(self, settled: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the last value when one batch contains the same hour twice."""
        value_key = self._value_key
        values: dict[datetime, float] = {}
        order: list[datetime] = []
        for item in settled:
            start = item["start"]
            if start not in values:
                order.append(start)
            values[start] = item[value_key]
        return [{"start": start, value_key: values[start]} for start in order]

    def _points_for(
        self,
        buckets: dict[datetime, float],
        starts: list[datetime],
        base: float,
    ) -> list[StatisticData]:
        """Build statistic rows for ``starts``, continuing the sum from ``base``."""
        running = base
        points: list[StatisticData] = []
        for start in starts:
            delta = buckets[start]
            running += delta
            point: StatisticData = {
                "start": dt_util.as_utc(start),
                "sum": round(running, 3),
            }
            if self._include_state:
                point["state"] = round(delta, 3)
            points.append(point)
        return points

    async def _async_persist(self) -> None:
        """Save import cursors and, when known, the in-window hour values."""
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
        if self._buckets is not None:
            payload["prefix_sum"] = self._prefix_sum
            payload["buckets"] = [
                {"start": start.isoformat(), "value": value}
                for start, value in sorted(self._buckets.items())
            ]
        try:
            await self._store.async_save(payload)
        except Exception as err:  # noqa: BLE001 - persistence must not fail import
            _LOGGER.warning("Failed to save statistics state: %s", err)
            return
        _LOGGER.debug("Saved statistics state")

    def _metadata(self) -> StatisticMetaData:
        return {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": self._statistic_name,
            "source": DOMAIN,
            "statistic_id": self._statistic_id,
            "unit_class": self._unit_class,
            "unit_of_measurement": self._unit_of_measurement,
        }

    async def async_import(self, hourly: list[dict[str, Any]]) -> None:
        """Import newly settled hours from coordinator data."""
        settled = self._collect_settled(hourly)
        if not settled:
            return
        if self._buckets is None:
            await self._async_import_legacy(settled)
            return
        await self._async_import_bucketed(settled)

    async def _async_import_legacy(self, settled: list[dict[str, Any]]) -> None:
        """Append-only import used when per-hour values were not stored.

        A recovered recorder baseline has no hour map. Restarting its sum at 0
        would erase the prefix that is already in the dashboard.
        """
        value_key = self._value_key
        if (
            self._last_start is None
            or self._earliest_start is None
            or settled[0]["start"] < self._earliest_start
        ):
            cumulative = 0.0
            targets = settled
            self._earliest_start = settled[0]["start"]
        else:
            cumulative = self._cumulative
            overlap = [row for row in settled if row["start"] <= self._last_start]
            targets = [row for row in settled if row["start"] > self._last_start]
            if overlap and settled[0]["start"] == self._earliest_start:
                fresh_total = sum(row[value_key] for row in settled)
                expected = cumulative + sum(row[value_key] for row in targets)
                if abs(fresh_total - expected) > 1e-6:
                    _LOGGER.debug("Detected revised past readings; re-importing all")
                    cumulative = 0.0
                    targets = settled
        if not targets:
            return
        points: list[StatisticData] = []
        for item in targets:
            delta = item[value_key]
            cumulative += delta
            point: StatisticData = {
                "start": dt_util.as_utc(item["start"]),
                "sum": round(cumulative, 3),
            }
            if self._include_state:
                point["state"] = round(delta, 3)
            points.append(point)
        async_add_external_statistics(self._hass, self._metadata(), points)
        self._last_start = targets[-1]["start"]
        if self._earliest_start is None:
            self._earliest_start = targets[0]["start"]
        self._cumulative = cumulative
        await self._async_persist()
        _LOGGER.debug("Imported %d statistics points", len(points))

    async def _async_import_bucketed(self, settled: list[dict[str, Any]]) -> None:
        """Import hours, revising in-window values without dropping the prefix."""
        assert self._buckets is not None
        rows = self._collapse_hours(settled)
        value_key = self._value_key
        earliest = self._earliest_start
        if self._last_start is None or earliest is None or rows[0]["start"] < earliest:
            # First import, or the fetch window moved further into the past
            # than anything we have stored. There is no hidden prefix to keep.
            new_prefix = 0.0
            new_buckets = {row["start"]: row[value_key] for row in rows}
            earliest = rows[0]["start"]
            starts = [row["start"] for row in rows]
            base = 0.0
        else:
            new_buckets = dict(self._buckets)
            new_prefix = self._prefix_sum
            for start in [item for item in new_buckets if item < rows[0]["start"]]:
                new_prefix += new_buckets.pop(start)
            revised = False
            changed: list[datetime] = []
            for row in rows:
                start = row["start"]
                value = row[value_key]
                previous = new_buckets.get(start)
                if previous is None or abs(previous - value) > 1e-6:
                    if previous is not None:
                        revised = True
                    changed.append(start)
                    new_buckets[start] = value
            if not changed:
                return
            if revised:
                # Rewrite the visible window. Sums stay continuous because
                # hours that already scrolled off live in the prefix.
                starts = sorted(
                    start for start in new_buckets if start >= rows[0]["start"]
                )
                base = new_prefix
            else:
                starts = sorted(changed)
                base = new_prefix + sum(
                    value for start, value in new_buckets.items() if start < starts[0]
                )
        points = self._points_for(new_buckets, starts, base)
        if not points:
            return
        async_add_external_statistics(self._hass, self._metadata(), points)
        self._buckets = new_buckets
        self._prefix_sum = new_prefix
        self._earliest_start = earliest
        self._last_start = max(new_buckets)
        self._cumulative = new_prefix + sum(new_buckets.values())
        await self._async_persist()
        _LOGGER.debug("Imported %d statistics points", len(points))
