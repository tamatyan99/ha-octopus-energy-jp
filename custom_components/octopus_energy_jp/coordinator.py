"""Data update coordinator for Octopus Energy Japan."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import utils
from .api import OctopusApiError, OctopusAuthError, OctopusEnergyJpApiClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    DOMAIN,
    MAX_DAILY_DAYS,
    STATIC_CACHE_TTL,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _tiered_cost(
    total_kwh: float, rates: list[dict[str, Any]] | list[utils.RateTier]
) -> float:
    """Legacy wrapper kept for backward compatibility; delegates to utils."""
    if not rates:
        return 0.0
    if isinstance(rates[0], dict):
        normalized = utils.normalize_rates(rates)  # type: ignore[arg-type]
    else:
        normalized = rates  # type: ignore[assignment]
    return utils.tiered_cost(total_kwh, normalized)


def _coerce_rates(raw_rates: Any) -> list[utils.RateTier]:
    """Accept normalized tuples or legacy dicts; return normalized tuples."""
    if not isinstance(raw_rates, list) or not raw_rates:
        raise ValueError("No usable consumption rates")
    if isinstance(raw_rates[0], dict):
        return utils.normalize_rates(raw_rates)
    clean: list[utils.RateTier] = []
    for item in raw_rates:
        if isinstance(item, (list, tuple)) and len(item) == 3:
            try:
                start = float(item[0])
                end = float(item[1]) if item[1] is not None else None
                price = float(item[2])
            except (TypeError, ValueError):
                continue
            if end is not None and end <= start:
                continue
            clean.append((start, end, price))
    if not clean:
        raise ValueError("No usable consumption rates")
    clean.sort(key=lambda tier: tier[0])
    return clean


class OctopusEnergyJpCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch readings and compute usage/cost aggregates."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: OctopusEnergyJpApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Octopus Energy Japan",
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.account_number: str = entry.data[CONF_ACCOUNT_NUMBER]
        self._static_fetched_at: datetime | None = None
        self._contract: dict[str, Any] | None = None
        self._rates: list[utils.RateTier] | None = None
        # API の保持期間（約1か月）を補う日次履歴の永続ストア
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_daily"
        )
        self._stored_days: dict[str, float] = {}

    async def async_load(self) -> None:
        """Restore the persisted daily history (corruption-tolerant)."""
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - store backend failure
            _LOGGER.warning("Failed to load daily history, starting fresh: %s", err)
            self._stored_days = {}
            return
        if not data:
            return
        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring corrupt daily history, starting fresh")
            self._stored_days = {}
            return
        raw_days = data.get("days")
        if not isinstance(raw_days, dict):
            _LOGGER.warning("Ignoring corrupt daily history, starting fresh")
            self._stored_days = {}
            return
        cleaned: dict[str, float] = {}
        corrupt = False
        for day, val in raw_days.items():
            try:
                num = float(val)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                corrupt = True
                continue
            if not math.isfinite(num):
                corrupt = True
                continue
            cleaned[str(day)] = num
        if corrupt:
            _LOGGER.warning("Ignoring corrupt daily entries, keeping valid ones")
        self._stored_days = cleaned

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._async_fetch()
        except OctopusAuthError as err:
            raise ConfigEntryAuthFailed from err
        except OctopusApiError as err:
            raise UpdateFailed(f"API error: {err}") from err
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as err:
            raise UpdateFailed(f"Data error: {err}") from err

    async def _async_fetch(self) -> dict[str, Any]:
        now = dt_util.now()

        if (
            self._static_fetched_at is None
            or now - self._static_fetched_at > STATIC_CACHE_TTL
        ):
            try:
                contract = await self.api.async_get_contract(self.account_number)
                rates_raw = await self.api.async_get_tariff_rates(
                    contract["grid_operator_code"],
                    contract["product_code"],
                    contract.get("capacity_unit", "")
                    if isinstance(contract, dict)
                    else "",
                )
                normalized_rates = _coerce_rates(rates_raw)
            except OctopusAuthError:
                raise
            except (
                OctopusApiError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
            ) as err:
                if self._contract is not None and self._rates is not None:
                    _LOGGER.warning(
                        "Static info refresh failed, using cached values: %s", err
                    )
                else:
                    raise
            else:
                self._contract = contract
                self._rates = normalized_rates
                self._static_fetched_at = now

        if self._contract is None or self._rates is None:
            raise OctopusApiError("Contract or tariff rates unavailable")
        rates = self._rates
        contract = self._contract

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 日別料金グラフと統計バックフィルのため当月を含む過去3か月分を取得
        back_year = month_start.year
        back_month = month_start.month - 2
        if back_month <= 0:
            back_month += 12
            back_year -= 1
        from_dt = month_start.replace(year=back_year, month=back_month)
        readings: list[dict[str, Any]] = []
        chunks = utils.chunk_date_range(from_dt, now, days=7)
        failed_chunks = 0
        for chunk_start, chunk_end in chunks:
            try:
                part = await self.api.async_get_readings(
                    self.account_number, chunk_start, chunk_end, limit=5000
                )
            except OctopusAuthError:
                raise
            except (
                OctopusApiError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
            ) as err:
                failed_chunks += 1
                _LOGGER.warning(
                    "Readings chunk %s-%s failed, skipping: %s",
                    chunk_start,
                    chunk_end,
                    err,
                )
                continue
            if isinstance(part, list):
                readings.extend(part)
        if chunks and failed_chunks >= len(chunks):
            raise OctopusApiError("All readings chunks failed")

        # 30分値をローカル日付・ローカル時間枠に集計
        daily_kwh: dict[str, float] = {}
        hourly_kwh: dict[datetime, float] = {}
        series_by_day: dict[str, list[dict[str, Any]]] = {}
        for r in readings:
            if not isinstance(r, dict):
                continue
            raw_start = r.get("startAt")
            raw_value = r.get("value")
            if raw_start is None or raw_value is None:
                continue
            if isinstance(raw_start, datetime):
                start = raw_start
            elif isinstance(raw_start, str):
                try:
                    start = dt_util.parse_datetime(raw_start)
                except Exception:  # noqa: BLE001 - defensive parse
                    continue
                if start is None:
                    continue
            else:
                continue
            try:
                value = float(raw_value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            try:
                start_local = dt_util.as_local(start)
            except Exception:  # noqa: BLE001 - defensive tz conversion
                continue
            day = start_local.strftime("%Y-%m-%d")
            daily_kwh[day] = daily_kwh.get(day, 0.0) + value
            hour_start = start_local.replace(minute=0, second=0, microsecond=0)
            hourly_kwh[hour_start] = hourly_kwh.get(hour_start, 0.0) + value
            series_by_day.setdefault(day, []).append(
                {"start": start_local.isoformat(), "kwh": value}
            )

        # API保持期間より古い日付はストアの値で補完し、最新値で更新して永続化
        self._stored_days.update(daily_kwh)
        self._stored_days = utils.prune_days(self._stored_days, MAX_DAILY_DAYS)
        daily_kwh = dict(self._stored_days)
        try:
            await self._store.async_save({"days": self._stored_days})
        except Exception as err:  # noqa: BLE001 - persistence must not fail update
            _LOGGER.warning("Failed to save daily history: %s", err)

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        day_before = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")

        yesterday_kwh = round(daily_kwh.get(yesterday, 0.0), 1)
        day_before_kwh = daily_kwh.get(day_before, 0.0)
        today_kwh = round(daily_kwh.get(today, 0.0), 1)
        month_kwh = round(
            sum(v for d, v in daily_kwh.items() if d >= month_start.strftime("%Y-%m-%d")),
            1,
        )

        diff_kwh = round(yesterday_kwh - day_before_kwh, 1)
        diff_pct = round(diff_kwh / day_before_kwh * 100) if day_before_kwh else None

        # 平均単価 = 段階制月額 ÷ 月次使用量。各日の料金 = 日次使用量 × その月の平均単価
        # 過去月の料金は現在の単価表による近似（単価改定は考慮しない）
        monthly_kwh: dict[str, float] = {}
        for d, kwh in daily_kwh.items():
            monthly_kwh[d[:7]] = monthly_kwh.get(d[:7], 0.0) + kwh
        avg_rate_by_month = {
            m: (utils.tiered_cost(total, rates) / total if total else 0.0)
            for m, total in monthly_kwh.items()
        }
        avg_rate = avg_rate_by_month.get(month_start.strftime("%Y-%m"), 0.0)
        cost_yesterday = round(yesterday_kwh * avg_rate_by_month.get(yesterday[:7], 0.0))
        cost_today = round(today_kwh * avg_rate_by_month.get(today[:7], 0.0))
        cost_month = round(month_kwh * avg_rate)

        # 前月集計（ストアに蓄積した完全な日次履歴から算出）
        prev_month_key = (month_start - timedelta(days=1)).strftime("%Y-%m")
        has_prev_month = prev_month_key in monthly_kwh
        prev_month_kwh = (
            round(monthly_kwh[prev_month_key], 1) if has_prev_month else None
        )
        prev_month_cost = (
            round(monthly_kwh[prev_month_key] * avg_rate_by_month.get(prev_month_key, 0.0))
            if has_prev_month
            else None
        )
        # 前月比較: 当月（昨日まで）と前月の同じ日数分を比較
        cur_through_yesterday = month_kwh - today_kwh
        prev_through_same = sum(
            v
            for d, v in daily_kwh.items()
            if d.startswith(prev_month_key) and int(d[8:10]) < now.day
        )
        month_diff_kwh = (
            round(cur_through_yesterday - prev_through_same, 1)
            if has_prev_month
            else None
        )
        month_diff_pct = (
            round(month_diff_kwh / prev_through_same * 100)
            if has_prev_month and prev_through_same
            else None
        )

        daily = [
            {
                "d": d,
                "kwh": round(kwh, 1),
                "cost": round(kwh * avg_rate_by_month[d[:7]]),
            }
            for d, kwh in sorted(daily_kwh.items())
        ]

        return {
            "yesterday_kwh": yesterday_kwh,
            "today_kwh": today_kwh,
            "month_kwh": month_kwh,
            "diff_kwh": diff_kwh,
            "diff_pct": diff_pct,
            "avg_rate": round(avg_rate, 2),
            "cost_yesterday": cost_yesterday,
            "cost_today": cost_today,
            "cost_month": cost_month,
            "prev_month_kwh": prev_month_kwh,
            "prev_month_cost": prev_month_cost,
            "month_diff_kwh": month_diff_kwh,
            "month_diff_pct": month_diff_pct,
            "daily": daily,
            "yesterday_series": series_by_day.get(yesterday, []),
            "today_series": series_by_day.get(today, []),
            "hourly": [
                {"start": start, "kwh": kwh}
                for start, kwh in sorted(hourly_kwh.items())
            ],
            "plan_name": contract.get("plan_name"),
            "last_update": now.isoformat(),
        }
