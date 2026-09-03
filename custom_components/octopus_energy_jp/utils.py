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
