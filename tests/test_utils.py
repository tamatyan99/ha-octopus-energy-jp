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
    for (_s1, e1), (s2, _e2) in zip(chunks, chunks[1:], strict=False):
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


def test_deduplicate_readings_prefers_larger_version() -> None:
    readings = [
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1.0, "version": 1},
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 2.0, "version": 2},
        {"startAt": "2026-01-01T00:30:00+09:00", "value": 3.0, "version": 1},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 2
    assert result[0]["value"] == 2.0
    assert result[0]["version"] == 2


def test_deduplicate_readings_no_version_later_wins() -> None:
    readings = [
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1.0},
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 5.0},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 5.0


def test_deduplicate_readings_skips_none_and_non_dict() -> None:
    readings = [
        None,
        "not-a-dict",
        {"value": 9.0},
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1.0, "version": 1},
    ]
    result = utils.deduplicate_readings(readings)
    assert result == [
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1.0, "version": 1}
    ]


def test_deduplicate_readings_empty() -> None:
    assert utils.deduplicate_readings([]) == []


def _bill_edges(*nodes: dict) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def test_select_latest_bill_prefers_statement() -> None:
    bills = _bill_edges(
        {
            "billType": "OTHER",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-10",
        },
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-01",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["bill_type"] == "STATEMENT"
    assert result["from_date"] == "2026-07-01"


def test_select_latest_bill_picks_newest_issued() -> None:
    bills = _bill_edges(
        {
            "billType": "STATEMENT",
            "fromDate": "2026-06-01",
            "toDate": "2026-06-30",
            "issuedDate": "2026-07-05",
        },
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-05",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["from_date"] == "2026-07-01"


def test_select_latest_bill_bare_list_and_malformed() -> None:
    bare = [
        {
            "billType": "INVOICE",
            "fromDate": "2026-07-01T00:00:00+09:00",
            "toDate": "2026-07-31T00:00:00+09:00",
            "issuedDate": "2026-08-05",
        }
    ]
    result = utils.select_latest_bill(bare)
    assert result is not None
    assert result["to_date"] == "2026-07-31T00:00:00+09:00"
    # empty / malformed → None, no exception
    assert utils.select_latest_bill({"edges": []}) is None
    assert utils.select_latest_bill(None) is None
    assert utils.select_latest_bill({"edges": [{"node": {"billType": "X"}}]}) is None
    assert utils.select_latest_bill("garbage") is None


def test_parse_day() -> None:
    assert utils.parse_day("2026-07-01") == "2026-07-01"
    assert utils.parse_day("2026-07-01T00:00:00+09:00") == "2026-07-01"
    assert utils.parse_day("not-a-date") is None
    assert utils.parse_day("2026-13-99") is None
    assert utils.parse_day(None) is None
    assert utils.parse_day("") is None


def test_coerce_option_float() -> None:
    assert utils.coerce_option_float(None) == 0.0
    assert utils.coerce_option_float("") == 0.0
    assert utils.coerce_option_float("bad") == 0.0
    assert utils.coerce_option_float(float("nan")) == 0.0
    assert utils.coerce_option_float(float("inf")) == 0.0
    assert utils.coerce_option_float("12.5") == 12.5
    assert utils.coerce_option_float(-3.0) == -3.0


def test_compute_billing_energy_only() -> None:
    daily = {f"2026-07-{d:02d}": 10.0 for d in range(1, 32)}
    rates = [(0.0, 120.0, 30.0), (120.0, None, 35.0)]
    billing = utils.compute_billing(
        daily, rates, "2026-07-01", "2026-07-31", "bill", 0.0, 0.0, 0.0
    )
    assert billing is not None
    assert billing["kwh"] == 310.0
    assert billing["days"] == 31
    assert billing["energy_cost"] == round(120 * 30.0 + 190 * 35.0)
    assert billing["total"] == billing["energy_cost"]
    # ゼロ項目は内訳に出さない
    assert "basic_charge" not in billing
    assert "fuel_adjustment" not in billing
    assert "renewable_levy" not in billing


def test_compute_billing_with_surcharges() -> None:
    daily = {"2026-07-01": 10.0, "2026-07-02": 20.0, "2026-08-01": 99.0}
    rates = [(0.0, None, 30.0)]
    billing = utils.compute_billing(
        daily, rates, "2026-07-01", "2026-07-31", "bill", 40.0, 2.0, 3.0
    )
    assert billing is not None
    assert billing["kwh"] == 30.0
    assert billing["days"] == 31  # 期間日数は from〜to の暦日数
    assert billing["energy_cost"] == 900
    assert billing["basic_charge"] == 40 * 31
    assert billing["fuel_adjustment"] == 60
    assert billing["renewable_levy"] == 90
    assert billing["total"] == 900 + 40 * 31 + 60 + 90


def test_compute_billing_no_overlap_returns_none() -> None:
    daily = {"2026-08-01": 5.0}
    rates = [(0.0, None, 30.0)]
    assert (
        utils.compute_billing(
            daily, rates, "2026-07-01", "2026-07-31", "bill", 0.0, 0.0, 0.0
        )
        is None
    )


def test_deduplicate_readings_smaller_version_later_keeps_first() -> None:
    readings = [
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 9.0, "version": 5},
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1.0, "version": 2},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 9.0
    assert result[0]["version"] == 5


def test_deduplicate_readings_incomparable_versions_later_wins() -> None:
    readings = [
        {"startAt": "t", "value": 1, "version": 1},
        {"startAt": "t", "value": 2, "version": "v2"},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 2
    # reverse direction is also later-wins
    readings_rev = [
        {"startAt": "t", "value": 1, "version": "v1"},
        {"startAt": "t", "value": 2, "version": 2},
    ]
    result_rev = utils.deduplicate_readings(readings_rev)
    assert len(result_rev) == 1
    assert result_rev[0]["value"] == 2


def test_deduplicate_readings_equal_version_later_wins() -> None:
    readings = [
        {"startAt": "t", "value": 1, "version": 2},
        {"startAt": "t", "value": 2, "version": 2},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 2


def test_deduplicate_readings_one_sided_missing_version_later_wins() -> None:
    first_missing = [
        {"startAt": "t", "value": 1},
        {"startAt": "t", "value": 2, "version": 3},
    ]
    assert utils.deduplicate_readings(first_missing)[0]["value"] == 2
    second_missing = [
        {"startAt": "t", "value": 1, "version": 3},
        {"startAt": "t", "value": 2},
    ]
    assert utils.deduplicate_readings(second_missing)[0]["value"] == 2


def test_deduplicate_readings_sorts_unsorted_input() -> None:
    readings = [
        {"startAt": "2026-01-01T01:00:00+09:00", "value": 2},
        {"startAt": "2026-01-01T00:00:00+09:00", "value": 1},
        {"startAt": "2026-01-01T00:30:00+09:00", "value": 1.5},
    ]
    result = utils.deduplicate_readings(readings)
    assert [r["startAt"] for r in result] == [
        "2026-01-01T00:00:00+09:00",
        "2026-01-01T00:30:00+09:00",
        "2026-01-01T01:00:00+09:00",
    ]


def test_deduplicate_readings_three_duplicates_fold_to_largest() -> None:
    readings = [
        {"startAt": "t", "value": 1, "version": 1},
        {"startAt": "t", "value": 2, "version": 2},
        {"startAt": "t", "value": 3, "version": 3},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 3
    assert result[0]["version"] == 3


def test_compute_billing_invalid_to_falls_back_to_matched_count() -> None:
    daily = {"2026-07-01": 10.0, "2026-07-02": 20.0}
    rates = [(0.0, None, 30.0)]
    billing = utils.compute_billing(
        daily, rates, "2026-07-01", "not-a-date", "bill", 40.0, 2.0, 3.0
    )
    assert billing is not None
    assert billing["kwh"] == 30.0
    assert billing["days"] == 2
    assert billing["from"] == "2026-07-01"
    assert billing["to"] == "not-a-date"
    assert billing["energy_cost"] == 900
    assert billing["total"] == 900 + 80 + 60 + 90


def test_compute_billing_invalid_from_yields_none_when_no_overlap() -> None:
    daily = {"2026-07-01": 10.0, "2026-07-02": 20.0}
    rates = [(0.0, None, 30.0)]
    assert (
        utils.compute_billing(
            daily, rates, "not-a-date", "2026-07-31", "bill", 0.0, 0.0, 0.0
        )
        is None
    )
    assert (
        utils.compute_billing(daily, rates, "bad", "also-bad", "bill", 0.0, 0.0, 0.0)
        is None
    )


def test_compute_billing_empty_from_falls_back_to_matched_count() -> None:
    daily = {"2026-07-01": 10.0, "2026-07-02": 20.0}
    rates = [(0.0, None, 30.0)]
    billing = utils.compute_billing(
        daily, rates, "", "2026-07-31", "bill", 0.0, 0.0, 0.0
    )
    assert billing is not None
    assert billing["days"] == 2
    assert billing["kwh"] == 30.0
    # empty `to` sorts before any daily key, so nothing overlaps
    assert (
        utils.compute_billing(daily, rates, "2026-07-01", "", "bill", 0.0, 0.0, 0.0)
        is None
    )


def test_select_latest_bill_skips_missing_to_date() -> None:
    bills = _bill_edges(
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "issuedDate": "2026-08-01",
        },
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-02",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["to_date"] == "2026-07-31"
    # all nodes missing toDate -> None
    assert (
        utils.select_latest_bill(
            _bill_edges({"billType": "STATEMENT", "fromDate": "2026-07-01"})
        )
        is None
    )


def test_select_latest_bill_prefers_invoice_over_other() -> None:
    bills = _bill_edges(
        {
            "billType": "OTHER",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-10",
        },
        {
            "billType": "INVOICE",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-01",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["bill_type"] == "INVOICE"


def test_select_latest_bill_other_only_picks_newest_issued() -> None:
    bills = _bill_edges(
        {
            "billType": "OTHER",
            "fromDate": "2026-06-01",
            "toDate": "2026-06-30",
            "issuedDate": "2026-07-05",
        },
        {
            "billType": "OTHER",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-05",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["from_date"] == "2026-07-01"


def test_select_latest_bill_missing_issued_date() -> None:
    bills = _bill_edges(
        {
            "billType": "STATEMENT",
            "fromDate": "2026-06-01",
            "toDate": "2026-06-30",
        },
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
            "issuedDate": "2026-08-05",
        },
    )
    result = utils.select_latest_bill(bills)
    assert result is not None
    assert result["from_date"] == "2026-07-01"
    # both missing issuedDate -> first node wins, issued_date is None
    both_missing = _bill_edges(
        {
            "billType": "STATEMENT",
            "fromDate": "2026-06-01",
            "toDate": "2026-06-30",
        },
        {
            "billType": "STATEMENT",
            "fromDate": "2026-07-01",
            "toDate": "2026-07-31",
        },
    )
    result_both = utils.select_latest_bill(both_missing)
    assert result_both is not None
    assert result_both["from_date"] == "2026-06-01"
    assert result_both["issued_date"] is None


def test_select_latest_bill_edges_not_list_returns_none() -> None:
    assert utils.select_latest_bill({"edges": "x"}) is None
    assert utils.select_latest_bill({"edges": {"a": 1}}) is None
    assert utils.select_latest_bill({"edges": None}) is None


def test_parse_day_non_str_returns_none() -> None:
    assert utils.parse_day(123) is None
    assert utils.parse_day(20260701) is None
    assert utils.parse_day(["2026-07-01"]) is None
    assert utils.parse_day({"d": "2026-07-01"}) is None
    assert utils.parse_day(("2026-07-01",)) is None
    assert utils.parse_day(0.0) is None


def test_coerce_option_float_int_blank_and_containers() -> None:
    assert utils.coerce_option_float(5) == 5.0
    assert utils.coerce_option_float(0) == 0.0
    assert utils.coerce_option_float(-3) == -3.0
    assert utils.coerce_option_float("   ") == 0.0
    assert utils.coerce_option_float(" 12.5 ") == 12.5
    assert utils.coerce_option_float([1]) == 0.0
    assert utils.coerce_option_float({"a": 1}) == 0.0


def test_normalize_rates_collapses_exact_duplicates() -> None:
    raw = [
        {"stepStart": 0, "stepEnd": 120, "pricePerUnitIncTax": 30},
        {"stepStart": 0, "stepEnd": 120, "pricePerUnitIncTax": 30},
        {"stepStart": 120, "stepEnd": None, "pricePerUnitIncTax": 35.5},
    ]
    assert utils.normalize_rates(raw) == [(0.0, 120.0, 30.0), (120.0, None, 35.5)]


def test_normalize_rates_conflicting_start_keeps_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = [
        {"stepStart": 0, "stepEnd": None, "pricePerUnitIncTax": 10},
        {"stepStart": 0, "stepEnd": 120, "pricePerUnitIncTax": 20},
    ]
    with caplog.at_level("WARNING", logger="oejp_utils"):
        normalized = utils.normalize_rates(raw)
    assert normalized == [(0.0, None, 10.0)]
    assert any("0.0" in record.message for record in caplog.records)
    # no longer double-charges: was 3900.0 (150*10 + 120*20) before the fix
    assert utils.tiered_cost(150, normalized) == pytest.approx(1500.0)


def test_tiered_cost_normal_multi_tier_ladder() -> None:
    rates = [(0.0, 120.0, 30.0), (120.0, 300.0, 35.0), (300.0, None, 40.0)]
    assert utils.tiered_cost(150, rates) == pytest.approx(3600.0 + 30 * 35.0)
    assert utils.tiered_cost(350, rates) == pytest.approx(
        3600.0 + 180 * 35.0 + 50 * 40.0
    )


def test_deduplicate_readings_collapses_equivalent_instants() -> None:
    readings = [
        {"startAt": "2026-07-01T00:30:00+09:00", "value": 1.0, "version": 1},
        {"startAt": "2026-06-30T15:30:00Z", "value": 2.0, "version": 2},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 1
    assert result[0]["value"] == 2.0


def test_deduplicate_readings_unparseable_start_behaves_as_before() -> None:
    readings = [
        {"startAt": "not-a-timestamp", "value": 1, "version": 1},
        {"startAt": "not-a-timestamp", "value": 2, "version": 2},
        {"startAt": "also-bad", "value": 3, "version": 1},
    ]
    result = utils.deduplicate_readings(readings)
    assert len(result) == 2
    by_start = {r["startAt"]: r for r in result}
    assert by_start["not-a-timestamp"]["value"] == 2
    assert by_start["also-bad"]["value"] == 3


def test_deduplicate_readings_numeric_string_versions_compare_numerically() -> None:
    assert (
        utils.deduplicate_readings(
            [
                {"startAt": "t", "value": 1, "version": "9"},
                {"startAt": "t", "value": 2, "version": "10"},
            ]
        )[0]["value"]
        == 2
    )
    assert (
        utils.deduplicate_readings(
            [
                {"startAt": "t", "value": 1, "version": "1.9"},
                {"startAt": "t", "value": 2, "version": "1.10"},
            ]
        )[0]["value"]
        == 2
    )
    # equal versions keep later-wins tie-breaking
    assert (
        utils.deduplicate_readings(
            [
                {"startAt": "t", "value": 1, "version": "1.10"},
                {"startAt": "t", "value": 2, "version": "1.10"},
            ]
        )[0]["value"]
        == 2
    )
