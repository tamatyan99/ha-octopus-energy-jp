"""Pure helper functions for the Octopus Energy Japan integration.

This module has no Home Assistant imports so it can be unit-tested
with plain pytest.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

# Normalized tariff tier: (step_start_kwh, step_end_kwh_or_None, price_per_kwh)
RateTier = tuple[float, float | None, float]


def slugify_account(account: Any) -> str:
    """Return a filesystem/statistic-safe slug for an account number.

    Non-string inputs (including None) are coerced via
    ``str(account or "unknown")``; an empty result falls back to "unknown".
    """
    slug = re.sub(r"[^a-z0-9]+", "_", str(account or "unknown").lower()).strip("_")
    return slug or "unknown"


def statistic_id_for_account(domain: str, account: str) -> str:
    """Return a per-account external statistics ID.

    Each config entry gets its own ID so multiple accounts never
    overwrite each other's Energy Dashboard statistics.
    """
    return f"{domain}:{slugify_account(account)}_consumption"


def normalize_rates(raw_rates: list[dict[str, Any]]) -> list[RateTier]:
    """Coerce tariff tiers to sorted (start, end, price) tuples.

    Missing prices are tolerated as 0.0. Non-finite values (NaN/Inf),
    empty-string stepEnd values, and inverted tiers (end <= start) are
    skipped. Duplicate starts are kept; the result is only sorted.
    Raises ValueError when no usable tier remains.
    """
    clean: list[RateTier] = []
    for rate in raw_rates or []:
        if not isinstance(rate, dict):
            continue
        try:
            start = float(rate.get("stepStart") or 0.0)
            raw_end = rate.get("stepEnd")
            if isinstance(raw_end, str) and raw_end.strip() == "":
                continue
            end = float(raw_end) if raw_end is not None else None
            price = float(rate.get("pricePerUnitIncTax") or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(price):
            continue
        if end is not None:
            if not math.isfinite(end) or end <= start:
                continue
        clean.append((start, end, price))
    if not clean:
        raise ValueError("No usable consumption rates")
    clean.sort(key=lambda tier: tier[0])
    return clean


def tiered_cost(total_kwh: float, rates: list[RateTier]) -> float:
    """Compute the tiered monthly energy charge for a total consumption.

    An empty rates list yields 0.0.
    """
    cost = 0.0
    for start, end, price in rates:
        if total_kwh <= start:
            break
        upper = total_kwh if end is None else min(total_kwh, end)
        cost += (upper - start) * price
    return cost


def chunk_date_range(
    start: datetime, end: datetime, days: int = 7
) -> list[tuple[datetime, datetime]]:
    """Split a date range into contiguous chunks.

    The Kraken API may silently truncate large half-hourly windows,
    so long histories are fetched in small adjacent chunks instead.

    Raises ValueError when days <= 0; returns [] when start >= end.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    if start >= end:
        return []
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        nxt = min(cursor + step, end)
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks


def prune_days(days: dict[str, float], keep: int) -> dict[str, float]:
    """Keep only the most recent `keep` daily entries.

    keep <= 0 yields an empty dict; when keep >= len(days) a copy is returned.
    """
    if keep <= 0:
        return {}
    if len(days) <= keep:
        return dict(days)
    return dict(sorted(days.items())[-keep:])


def deduplicate_readings(readings: list[dict]) -> list[dict]:
    """Deduplicate half-hourly readings by ``startAt``.

    The Kraken API may return multiple revisions of the same slot
    (same ``startAt``, different ``version``). Each element is expected
    to look like ``{"startAt": str, "value": Any, "version": Any}``.
    When duplicates share a ``startAt``, the entry with the larger
    ``version`` wins when versions are comparable; otherwise the later
    occurrence wins. ``None``/non-dict elements (and dicts without a
    ``startAt``) are skipped. The result is sorted by ``startAt``
    ascending.
    """
    best: dict[Any, dict] = {}
    for entry in readings or []:
        if not isinstance(entry, dict):
            continue
        start = entry.get("startAt")
        if start is None:
            continue
        prev = best.get(start)
        if prev is None:
            best[start] = entry
            continue
        prev_version = prev.get("version")
        cur_version = entry.get("version")
        if prev_version is None or cur_version is None:
            # version 無しは後勝ち
            best[start] = entry
            continue
        try:
            if cur_version >= prev_version:
                best[start] = entry
        except TypeError:
            # 比較不能な型同士は後勝ち
            best[start] = entry
    return [best[key] for key in sorted(best.keys(), key=str)]


# 請求書タイプの優先対象 (STATEMENT/INVOICE を優先、それ以外も利用可)
_PREFERRED_BILL_TYPES = frozenset({"STATEMENT", "INVOICE"})


def select_latest_bill(bills: Any) -> dict[str, Any] | None:
    """Pick the most recent bill period from a GraphQL ``bills`` payload.

    Accepts either ``{"edges": [{"node": {...}}, ...]}`` or a bare list of
    nodes (defensive: the orderBy enum name may differ per schema, so the
    issuedDate sort is redone locally). STATEMENT/INVOICE bills are
    preferred; the latest one by issuedDate wins. Returns None for
    empty/malformed payloads without raising (except on non-dict access
    errors, which the caller guards).
    """
    if isinstance(bills, dict):
        edges = bills.get("edges")
    elif isinstance(bills, list):
        edges = bills
    else:
        return None
    if not isinstance(edges, list) or not edges:
        return None
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for edge in edges:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            node = edge["node"]
        elif isinstance(edge, dict):
            node = edge
        else:
            continue
        from_date = node.get("fromDate")
        to_date = node.get("toDate")
        if not isinstance(from_date, str) or not from_date:
            continue
        if not isinstance(to_date, str) or not to_date:
            continue
        bill_type = node.get("billType")
        issued = node.get("issuedDate")
        preferred = 0 if bill_type in _PREFERRED_BILL_TYPES else 1
        candidates.append(
            (
                preferred,
                issued if isinstance(issued, str) else "",
                {
                    "bill_type": bill_type if isinstance(bill_type, str) else None,
                    "from_date": from_date,
                    "to_date": to_date,
                    "issued_date": issued if isinstance(issued, str) else None,
                },
            )
        )
    if not candidates:
        return None
    # issuedDate 降順 → STATEMENT/INVOICE 優先 (stable sort の順序で適用)
    candidates.sort(key=lambda c: c[1], reverse=True)
    candidates.sort(key=lambda c: c[0])
    return candidates[0][2]


def parse_day(value: Any) -> str | None:
    """Extract a ``YYYY-MM-DD`` day string from a bill date value.

    Accepts full ISO datetimes as well as plain dates; returns None for
    unparseable values. Only the date part is significant for aggregation.
    """
    if not isinstance(value, str) or not value:
        return None
    day = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


def coerce_option_float(value: Any) -> float:
    """Coerce an options value to float; garbage (incl. NaN/Inf) → 0.0."""
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return num


def compute_billing(
    daily_kwh: dict[str, float],
    rates: list[RateTier],
    from_day: str,
    to_day: str,
    source: str,
    basic_per_day: float,
    fuel_per_kwh: float,
    levy_per_kwh: float,
) -> dict[str, Any] | None:
    """Aggregate billing-period usage and cost.

    Sums daily_kwh inside [from_day, to_day] (inclusive, both ends).
    Returns None when no daily data falls inside the period.
    Zero-valued surcharges are excluded from the breakdown (and from
    the total), so default options yield energy-only billing.
    """
    in_period = [day for day in daily_kwh if from_day <= day <= to_day]
    if not in_period:
        return None
    total_kwh = sum(daily_kwh[day] for day in in_period)
    try:
        span = (
            datetime.strptime(to_day, "%Y-%m-%d").date()
            - datetime.strptime(from_day, "%Y-%m-%d").date()
        ).days + 1
        period_days = span if span > 0 else len(in_period)
    except (ValueError, TypeError):
        period_days = len(in_period)
    energy_cost = tiered_cost(total_kwh, rates)
    basic_charge = basic_per_day * period_days if basic_per_day else 0.0
    fuel_adjustment = fuel_per_kwh * total_kwh if fuel_per_kwh else 0.0
    renewable_levy = levy_per_kwh * total_kwh if levy_per_kwh else 0.0
    billing: dict[str, Any] = {
        "kwh": round(total_kwh, 1),
        "days": period_days,
        "energy_cost": round(energy_cost),
        "from": from_day,
        "to": to_day,
        "source": source,
    }
    if basic_charge:
        billing["basic_charge"] = round(basic_charge)
    if fuel_adjustment:
        billing["fuel_adjustment"] = round(fuel_adjustment)
    if renewable_levy:
        billing["renewable_levy"] = round(renewable_levy)
    billing["total"] = round(
        energy_cost + basic_charge + fuel_adjustment + renewable_levy
    )
    return billing
