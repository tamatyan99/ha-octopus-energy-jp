"""Unit tests for the HA-independent utils module."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_UTILS_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "octopus_energy_jp"
    / "utils.py"
)

_spec = importlib.util.spec_from_file_location("oejp_utils", _UTILS_PATH)
assert _spec is not None and _spec.loader is not None
utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(utils)


def test_slugify_account() -> None:
    assert utils.slugify_account("A-B00C43A0") == "a_b00c43a0"
    assert utils.slugify_account("  XYZ-123 ") == "xyz_123"
    assert utils.slugify_account("!!!") == "unknown"


def test_statistic_id_for_account() -> None:
    assert (
        utils.statistic_id_for_account("octopus_energy_jp", "A-B00C43A0")
        == "octopus_energy_jp:a_b00c43a0_consumption"
    )
    assert utils.statistic_id_for_account(
        "octopus_energy_jp", "A-1"
    ) != utils.statistic_id_for_account("octopus_energy_jp", "B-2")


def test_normalize_rates_sorts_and_coerces() -> None:
    raw = [
        {"stepStart": "120", "stepEnd": None, "pricePerUnitIncTax": "35.5"},
        {"stepStart": 0, "stepEnd": "120", "pricePerUnitIncTax": 30},
    ]
    assert utils.normalize_rates(raw) == [(0.0, 120.0, 30.0), (120.0, None, 35.5)]


def test_normalize_rates_skips_garbage() -> None:
    raw = [
        {"stepStart": "NaN-broken", "stepEnd": None, "pricePerUnitIncTax": 1},
        {"stepStart": 50, "stepEnd": 40, "pricePerUnitIncTax": 1},  # inverted
        "not-a-dict",
        {"stepStart": 0, "stepEnd": None, "pricePerUnitIncTax": 20},
    ]
    assert utils.normalize_rates(raw) == [(0.0, None, 20.0)]


def test_normalize_rates_empty_raises() -> None:
    with pytest.raises(ValueError):
        utils.normalize_rates([])
    with pytest.raises(ValueError):
        utils.normalize_rates([{"stepStart": "bad"}])


def test_tiered_cost() -> None:
    rates = [(0.0, 120.0, 30.0), (120.0, 300.0, 35.0), (300.0, None, 40.0)]
    assert utils.tiered_cost(0, rates) == 0.0
    assert utils.tiered_cost(100, rates) == pytest.approx(3000.0)
    assert utils.tiered_cost(120, rates) == pytest.approx(3600.0)
    assert utils.tiered_cost(200, rates) == pytest.approx(3600.0 + 80 * 35.0)
    assert utils.tiered_cost(400, rates) == pytest.approx(
        3600.0 + 180 * 35.0 + 100 * 40.0
    )


def test_chunk_date_range() -> None:
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 20)
    chunks = utils.chunk_date_range(start, end, days=7)
    assert [c[0] for c in chunks] == [
        datetime(2026, 6, 1),
        datetime(2026, 6, 8),
        datetime(2026, 6, 15),
    ]
    assert chunks[-1][1] == end
    # contiguous, no gaps or overlaps
    for ( _s1, e1), (s2, _e2) in zip(chunks, chunks[1:]):
        assert e1 == s2
    assert utils.chunk_date_range(start, start) == []
    assert len(utils.chunk_date_range(start, start + timedelta(hours=1))) == 1


def test_prune_days() -> None:
    days = {f"2026-01-{d:02d}": float(d) for d in range(1, 32)}
    pruned = utils.prune_days(days, 10)
    assert len(pruned) == 10
    assert min(pruned) == "2026-01-22"
    assert utils.prune_days({"a": 1.0}, 10) == {"a": 1.0}


def test_chunk_date_range_invalid_days() -> None:
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 20)
    with pytest.raises(ValueError):
        utils.chunk_date_range(start, end, days=0)
    with pytest.raises(ValueError):
        utils.chunk_date_range(start, end, days=-3)


def test_chunk_date_range_start_after_end() -> None:
    start = datetime(2026, 6, 20)
    end = datetime(2026, 6, 1)
    assert utils.chunk_date_range(start, end) == []


def test_prune_days_keep_zero() -> None:
    assert utils.prune_days({"a": 1.0, "b": 2.0}, 0) == {}
    assert utils.prune_days({"a": 1.0}, -1) == {}


def test_normalize_rates_skips_nonfinite() -> None:
    raw = [
        {"stepStart": 0, "stepEnd": 120, "pricePerUnitIncTax": float("nan")},
        {"stepStart": float("inf"), "stepEnd": None, "pricePerUnitIncTax": 10},
        {"stepStart": 0, "stepEnd": float("inf"), "pricePerUnitIncTax": 10},
        {"stepStart": 0, "stepEnd": "", "pricePerUnitIncTax": 10},
        {"stepStart": 0, "stepEnd": None, "pricePerUnitIncTax": 20},
    ]
    assert utils.normalize_rates(raw) == [(0.0, None, 20.0)]


def test_slugify_account_none() -> None:
    assert utils.slugify_account(None) == "unknown"
    assert utils.slugify_account(123) == "123"


def test_tiered_cost_empty_rates() -> None:
    assert utils.tiered_cost(100, []) == 0.0
