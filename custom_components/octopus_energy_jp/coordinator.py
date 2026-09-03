"""Data update coordinator for Octopus Energy Japan."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OctopusApiError, OctopusAuthError, OctopusEnergyJpApiClient
from .const import (
    CONF_ACCOUNT_NUMBER,
    DOMAIN,
    STATIC_CACHE_TTL,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _tiered_cost(total_kwh: float, rates: list[dict[str, Any]]) -> float:
    """Compute the tiered monthly energy charge for a total consumption."""
    cost = 0.0
    for rate in rates:
        # API は数値を文字列で返すことがあるため明示的に float 化する
        step_start = float(rate.get("stepStart") or 0.0)
        raw_end = rate.get("stepEnd")
        step_end = float(raw_end) if raw_end is not None else None
        price = float(rate.get("pricePerUnitIncTax") or 0.0)
        if total_kwh <= step_start:
            break
        upper = total_kwh if step_end is None else min(total_kwh, step_end)
        cost += (upper - step_start) * price
    return cost


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
        self._rates: list[dict[str, Any]] | None = None
        # API の保持期間（約1か月）を補う日次履歴の永続ストア
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_daily"
        )
        self._stored_days: dict[str, float] = {}

    async def async_load(self) -> None:
        """Restore the persisted daily history."""
        data = await self._store.async_load()
        if data:
            self._stored_days = {
                d: float(v) for d, v in data.get("days", {}).items()
            }

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self._async_fetch()
        except OctopusAuthError as err:
            raise ConfigEntryAuthFailed from err
        except OctopusApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

    async def _async_fetch(self) -> dict[str, Any]:
        now = dt_util.now()

        if (
            self._static_fetched_at is None
            or now - self._static_fetched_at > STATIC_CACHE_TTL
        ):
            self._contract = await self.api.async_get_contract(self.account_number)
            self._rates = await self.api.async_get_tariff_rates(
                self._contract["grid_operator_code"],
                self._contract["product_code"],
                self._contract["capacity_unit"],
            )
            self._static_fetched_at = now

        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 日別料金グラフと統計バックフィルのため当月を含む過去3か月分を取得
        back_year = month_start.year
        back_month = month_start.month - 2
        if back_month <= 0:
            back_month += 12
            back_year -= 1
        from_dt = month_start.replace(year=back_year, month=back_month)
        readings = await self.api.async_get_readings(
            self.account_number, from_dt, now
        )

        # 30分値をローカル日付・ローカル時間枠に集計
        daily_kwh: dict[str, float] = {}
        hourly_kwh: dict[datetime, float] = {}
        series_by_day: dict[str, list[dict[str, Any]]] = {}
        for r in readings:
            start = dt_util.parse_datetime(r["startAt"])
            if start is None:
                continue
            start_local = dt_util.as_local(start)
            value = float(r["value"])
            day = start_local.strftime("%Y-%m-%d")
            daily_kwh[day] = daily_kwh.get(day, 0.0) + value
            hour_start = start_local.replace(minute=0, second=0, microsecond=0)
            hourly_kwh[hour_start] = hourly_kwh.get(hour_start, 0.0) + value
            series_by_day.setdefault(day, []).append(
                {"start": start_local.isoformat(), "kwh": value}
            )

        # API保持期間より古い日付はストアの値で補完し、最新値で更新して永続化
        self._stored_days.update(daily_kwh)
        daily_kwh = dict(self._stored_days)
        await self._store.async_save({"days": self._stored_days})

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

        # 按比例: 平均単価 = 段階制月額 ÷ 月次使用量。各日料金 = 日次使用量 × その月の平均単価
        # 過去月の料金は現在の単価表による近似（単価改定は考慮しない）
        monthly_kwh: dict[str, float] = {}
        for d, kwh in daily_kwh.items():
            monthly_kwh[d[:7]] = monthly_kwh.get(d[:7], 0.0) + kwh
        avg_rate_by_month = {
            m: (_tiered_cost(total, self._rates) / total if total else 0.0)
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
            "plan_name": self._contract["plan_name"],
            "last_update": now.isoformat(),
        }
