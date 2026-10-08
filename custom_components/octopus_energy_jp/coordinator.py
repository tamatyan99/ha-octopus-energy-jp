"""Data update coordinator for Octopus Energy Japan."""

from __future__ import annotations

import logging
import math
import re
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
    CONF_BASIC_CHARGE_PER_DAY,
    CONF_FUEL_ADJUSTMENT_PER_KWH,
    CONF_RENEWABLE_LEVY_PER_KWH,
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


def _month_prior_daily_kwh(
    daily_kwh: dict[str, float], month_key: str, day_str: str
) -> float:
    """Sum daily_kwh for strict-earlier days in the same local calendar month."""
    return sum(v for d, v in daily_kwh.items() if d[:7] == month_key and d < day_str)


def _attach_hourly_slot_costs(
    hourly: list[dict[str, Any]],
    daily_kwh: dict[str, float],
    rates: list[utils.RateTier],
    fuel_per_kwh: float,
    levy_per_kwh: float,
) -> list[dict[str, Any]]:
    """Add per-slot marginal energy cost (JPY) for external cost statistics.

    Each calendar month is processed independently in local time. For each
    slot, cumulative energy before that slot is prior days in the month (from
    ``daily_kwh``, including store-backfilled days) plus earlier slots the same
    day. A slot's cost depends only on data at or before that slot, including
    store-backfilled daily totals — never on how far back the API currently
    returns data. Basic charge (CONF_BASIC_CHARGE_PER_DAY) is excluded — it
    cannot be prorated per hour without breaking stability.
    """
    surcharge_per_kwh = fuel_per_kwh + levy_per_kwh
    enriched: list[dict[str, Any]] = []
    same_day_kwh: dict[str, float] = {}
    for item in sorted(hourly, key=lambda row: row["start"]):
        start = item["start"]
        kwh = float(item["kwh"])
        start_local = dt_util.as_local(start)
        day_str = start_local.strftime("%Y-%m-%d")
        month_key = start_local.strftime("%Y-%m")
        cum_before = _month_prior_daily_kwh(daily_kwh, month_key, day_str) + (
            same_day_kwh.get(day_str, 0.0)
        )
        energy = utils.tiered_cost(cum_before + kwh, rates) - utils.tiered_cost(
            cum_before, rates
        )
        cost = round(energy + surcharge_per_kwh * kwh, 3)
        same_day_kwh[day_str] = same_day_kwh.get(day_str, 0.0) + kwh
        enriched.append({"start": start, "kwh": kwh, "cost": cost})
    return enriched


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
        # DataUpdateCoordinator.config_entry は HA 2024.11+ のため自前でも保持
        self._entry = entry
        self._static_fetched_at: datetime | None = None
        self._contract: dict[str, Any] | None = None
        self._rates: list[utils.RateTier] | None = None
        # 契約から取得した基本料金・燃料費調整額・再エネ賦課金 (オプション未設定時に使用)
        self._surcharges: dict[str, float | None] = {}
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
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                corrupt = True
                continue
            try:
                num = float(val)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                corrupt = True
                continue
            if not math.isfinite(num):
                corrupt = True
                continue
            cleaned[day] = num
        if corrupt:
            _LOGGER.warning("Ignoring corrupt daily entries, keeping valid ones")
        self._stored_days = cleaned

    def _resolve_surcharge(self, option_key: str, api_key: str) -> float:
        """Prefer an explicitly set option; otherwise fall back to the API value."""
        options = self._entry.options
        if options.get(option_key) is not None:
            return utils.coerce_option_float(options.get(option_key))
        return utils.coerce_option_float(self._surcharges.get(api_key))

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
                try:
                    self._surcharges = await self.api.async_get_surcharges(
                        self.account_number
                    )
                except OctopusAuthError:
                    raise
                except Exception as err:  # noqa: BLE001 - surcharges are optional
                    _LOGGER.warning(
                        "Surcharge fetch failed, keeping previous values: %s", err
                    )

        if self._contract is None or self._rates is None:
            raise OctopusApiError("Contract or tariff rates unavailable")
        rates = self._rates
        contract = self._contract

        # 日別料金グラフと統計バックフィルのため当月を含む過去3か月分を取得
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
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
        # Kraken は同一 startAt の改訂版(version違い)を返すことがあるため
        # 先に startAt で重複排除してから集計する(二重計上防止)
        daily_kwh: dict[str, float] = {}
        hourly_kwh: dict[datetime, float] = {}
        series_by_day: dict[str, list[dict[str, Any]]] = {}
        for r in utils.deduplicate_readings(readings):
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
        if daily_kwh:
            oldest_returned = min(daily_kwh)
            for day, kwh in daily_kwh.items():
                prev = self._stored_days.get(day)
                # The oldest day the API returns sits on the rolling-retention
                # boundary and can be a partial day; never let it shrink a day we
                # already stored completely. Other days still accept real
                # downward corrections from the API.
                if day == oldest_returned and prev is not None and kwh < prev:
                    continue
                self._stored_days[day] = kwh
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
            sum(
                v for d, v in daily_kwh.items() if d >= month_start.strftime("%Y-%m-%d")
            ),
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
        cost_yesterday = round(
            yesterday_kwh * avg_rate_by_month.get(yesterday[:7], 0.0)
        )
        cost_today = round(today_kwh * avg_rate_by_month.get(today[:7], 0.0))
        cost_month = round(month_kwh * avg_rate)

        # 前月集計（ストアに蓄積した完全な日次履歴から算出）
        prev_month_key = (month_start - timedelta(days=1)).strftime("%Y-%m")
        has_prev_month = prev_month_key in monthly_kwh
        prev_month_kwh = (
            round(monthly_kwh[prev_month_key], 1) if has_prev_month else None
        )
        prev_month_cost = (
            round(
                monthly_kwh[prev_month_key] * avg_rate_by_month.get(prev_month_key, 0.0)
            )
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

        # 請求期間の特定: 直近請求書の fromDate/toDate をそのまま使う。
        # 取得失敗・空・構造異常時は請求期間機能だけ無効化し更新は継続する。
        latest_bill: dict[str, Any] | None = None
        try:
            latest_bill = await self.api.async_get_latest_bill(self.account_number)
        except OctopusAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - bills failure must not fail update
            _LOGGER.warning(
                "Latest bill fetch failed, skipping billing period: %s", err
            )
            latest_bill = None

        basic_per_day = self._resolve_surcharge(
            CONF_BASIC_CHARGE_PER_DAY, "standing_charge_per_day"
        )
        fuel_per_kwh = self._resolve_surcharge(
            CONF_FUEL_ADJUSTMENT_PER_KWH, "fuel_per_kwh"
        )
        levy_per_kwh = self._resolve_surcharge(
            CONF_RENEWABLE_LEVY_PER_KWH, "levy_per_kwh"
        )

        billing_period: dict[str, Any] | None = None
        billing: dict[str, Any] | None = None
        if latest_bill is not None:
            from_day = utils.parse_day(latest_bill.get("from_date"))
            to_day = utils.parse_day(latest_bill.get("to_date"))
            if from_day is not None and to_day is not None and from_day <= to_day:
                billing_period = {
                    "from": from_day,
                    "to": to_day,
                    "bill_type": latest_bill.get("bill_type"),
                    "source": "bill",
                }
                billing = utils.compute_billing(
                    daily_kwh,
                    rates,
                    from_day,
                    to_day,
                    "bill",
                    basic_per_day,
                    fuel_per_kwh,
                    levy_per_kwh,
                )

        hourly_base = [
            {"start": start, "kwh": kwh} for start, kwh in sorted(hourly_kwh.items())
        ]
        hourly_with_cost = _attach_hourly_slot_costs(
            hourly_base, daily_kwh, rates, fuel_per_kwh, levy_per_kwh
        )
        month_start_str = month_start.strftime("%Y-%m-%d")
        has_current_month_days = any(day >= month_start_str for day in daily_kwh)
        current_rate_kwh: float | None = None
        current_rate_tier_kwh: float | None = None
        current_rate_next_tier_kwh: float | None = None
        current_rate_month_kwh: float | None = None
        if has_current_month_days:
            marginal = utils.marginal_rate_kwh(month_kwh, rates)
            if marginal is not None:
                current_rate_month_kwh = month_kwh
                current_rate_tier_kwh = round(marginal, 2)
                next_tier = utils.next_tier_rate_kwh(month_kwh, rates)
                current_rate_next_tier_kwh = (
                    round(next_tier, 2) if next_tier is not None else None
                )
                current_rate_kwh = round(marginal + fuel_per_kwh + levy_per_kwh, 2)

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
            "hourly": hourly_with_cost,
            "plan_name": contract.get("plan_name"),
            "current_rate_kwh": current_rate_kwh,
            "current_rate_tier_kwh": current_rate_tier_kwh,
            "current_rate_fuel_per_kwh": fuel_per_kwh,
            "current_rate_levy_per_kwh": levy_per_kwh,
            "current_rate_next_tier_kwh": current_rate_next_tier_kwh,
            "current_rate_month_kwh": current_rate_month_kwh,
            "billing_period": billing_period,
            "billing": billing,
            "last_update": now.isoformat(),
            "today_start": now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(),
            "month_start": month_start.isoformat(),
        }
