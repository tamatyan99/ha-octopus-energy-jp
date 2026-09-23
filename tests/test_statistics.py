"""Tests for the external long-term-statistics importer.

Drives the real ``OctopusStatisticsImporter`` with a frozen clock and a
patched ``async_add_external_statistics`` sink (no recorder writes), using
the real ``helpers.storage.Store`` under the test config dir.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.octopus_energy_jp.const import (
    DOMAIN,
    STATS_IMPORT_BUFFER,
)
from custom_components.octopus_energy_jp.statistics import OctopusStatisticsImporter
from custom_components.octopus_energy_jp.utils import statistic_id_for_account

ACCOUNT = "A-TEST1234"
STATISTIC_ID = statistic_id_for_account(DOMAIN, ACCOUNT)


@contextmanager
def _frozen_jst() -> Iterator[datetime]:
    """Freeze time at 2026-07-15 12:00 JST and run HA under Asia/Tokyo."""
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    try:
        with freeze_time("2026-07-15 03:00:00"):
            yield dt_util.now()
    finally:
        dt_util.set_default_time_zone(previous)


def _jst(day: int, hour: int, minute: int = 0) -> datetime:
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    return datetime(2026, 7, day, hour, minute, tzinfo=tz)


def _hourly(start: datetime, kwh_values: list[float]) -> list[dict[str, Any]]:
    return [
        {"start": start + timedelta(hours=i), "kwh": kwh}
        for i, kwh in enumerate(kwh_values)
    ]


def _epoch_ms(value: datetime) -> float:
    return value.astimezone(UTC).timestamp() * 1000


def _new_importer(
    hass: HomeAssistant, entry_id: str = "stats-entry-1"
) -> OctopusStatisticsImporter:
    return OctopusStatisticsImporter(hass, entry_id, ACCOUNT)


def _recorder_patches(rows: Any):
    """Patch get_instance/get_last_statistics so recovery returns ``rows``."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=rows)
    return (
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=instance,
        ),
        patch("homeassistant.components.recorder.statistics.get_last_statistics"),
        instance,
    )


def _dict_rows(start_ms: float, total: Any) -> dict[str, Any]:
    return {STATISTIC_ID: [{"start": start_ms, "sum": total, "state": total}]}


# ---------------------------------------------------------------------------
# _recover_baseline_from_recorder
# ---------------------------------------------------------------------------


async def test_recover_baseline_dict_shape_seeds_state_and_warns(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    importer = _new_importer(hass)
    start_local = _jst(14, 2)
    get_patch, fn_patch, instance = _recorder_patches(
        _dict_rows(_epoch_ms(start_local), 12.5)
    )
    with _frozen_jst(), get_patch, fn_patch, caplog.at_level(logging.WARNING):
        await importer._recover_baseline_from_recorder()
    assert importer._cumulative == pytest.approx(12.5)
    assert importer._last_start == start_local
    assert importer._earliest_start == dt_util.as_local(
        datetime(1970, 1, 1, tzinfo=UTC)
    )
    assert any("recovered baseline" in record.getMessage() for record in caplog.records)
    # The executor is driven with the real recorder callable and type set.
    executor_args = instance.async_add_executor_job.call_args[0]
    assert executor_args[2] == 1
    assert executor_args[3] == STATISTIC_ID
    assert executor_args[4] is False
    assert executor_args[5] == {"sum", "state"}


async def test_recover_baseline_bare_list_shape(hass: HomeAssistant) -> None:
    importer = _new_importer(hass)
    start_local = _jst(14, 5)
    get_patch, fn_patch, _ = _recorder_patches(
        [{"start": _epoch_ms(start_local), "sum": 7.25}]
    )
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    assert importer._cumulative == pytest.approx(7.25)
    assert importer._last_start == start_local
    assert importer._earliest_start == dt_util.as_local(
        datetime(1970, 1, 1, tzinfo=UTC)
    )


@pytest.mark.parametrize("raw_sum", [None, float("inf"), float("nan"), "junk"])
async def test_recover_baseline_bad_sum_coerced_to_zero(
    hass: HomeAssistant, raw_sum: Any
) -> None:
    importer = _new_importer(hass)
    start_local = _jst(14, 2)
    get_patch, fn_patch, _ = _recorder_patches(
        _dict_rows(_epoch_ms(start_local), raw_sum)
    )
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    # The start is still usable, so the baseline seeds with a zero sum.
    assert importer._cumulative == 0.0
    assert importer._last_start == start_local


@pytest.mark.parametrize("raw_start", [None, "junk", {"epoch": 1}])
async def test_recover_baseline_unusable_start_leaves_fresh(
    hass: HomeAssistant, raw_start: Any
) -> None:
    importer = _new_importer(hass)
    get_patch, fn_patch, _ = _recorder_patches(_dict_rows(raw_start, 9.0))
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    assert importer._last_start is None
    assert importer._earliest_start is None
    assert importer._cumulative == 0.0


async def test_recover_baseline_huge_start_leaves_fresh(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass)
    get_patch, fn_patch, _ = _recorder_patches(_dict_rows(1e20, 9.0))
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    assert importer._last_start is None
    assert importer._earliest_start is None
    assert importer._cumulative == 0.0


@pytest.mark.parametrize(
    "rows",
    [
        {},
        {STATISTIC_ID: []},
        {STATISTIC_ID: "nope"},
        {STATISTIC_ID: [None]},
        {STATISTIC_ID: ["nope"]},
        [],
        ["nope"],
        {"other:id": [{"start": 1.0, "sum": 2.0}]},
    ],
)
async def test_recover_baseline_empty_rows_leave_fresh(
    hass: HomeAssistant, rows: Any
) -> None:
    importer = _new_importer(hass)
    get_patch, fn_patch, _ = _recorder_patches(rows)
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    assert importer._last_start is None
    assert importer._earliest_start is None
    assert importer._cumulative == 0.0


async def test_recover_baseline_recorder_error_leaves_fresh(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    importer = _new_importer(hass)
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        _frozen_jst(),
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=instance,
        ),
        patch("homeassistant.components.recorder.statistics.get_last_statistics"),
        caplog.at_level(logging.WARNING),
    ):
        await importer._recover_baseline_from_recorder()
    assert importer._last_start is None
    assert importer._earliest_start is None
    assert importer._cumulative == 0.0
    assert any(
        "baseline lookup failed" in record.getMessage() for record in caplog.records
    )


async def test_recover_baseline_get_instance_error_leaves_fresh(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass)
    with (
        _frozen_jst(),
        patch(
            "homeassistant.components.recorder.get_instance",
            side_effect=RuntimeError("no recorder"),
        ),
        patch("homeassistant.components.recorder.statistics.get_last_statistics"),
    ):
        await importer._recover_baseline_from_recorder()
    assert importer._last_start is None
    assert importer._cumulative == 0.0


# ---------------------------------------------------------------------------
# async_load
# ---------------------------------------------------------------------------


async def test_async_load_valid_store_restores_state(hass: HomeAssistant) -> None:
    importer = _new_importer(hass, "load-valid")
    last = _jst(14, 5)
    earliest = _jst(13, 22)
    await importer._store.async_save(
        {
            "last_start": last.isoformat(),
            "earliest_start": earliest.isoformat(),
            "cumulative": 5.25,
        }
    )
    importer._last_start = None
    importer._earliest_start = None
    importer._cumulative = 0.0
    await importer.async_load()
    assert importer._last_start == last
    assert importer._earliest_start == earliest
    assert importer._cumulative == pytest.approx(5.25)


async def test_async_load_legacy_missing_earliest_falls_back(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "load-legacy")
    last = _jst(14, 5)
    await importer._store.async_save(
        {"last_start": last.isoformat(), "cumulative": 3.0}
    )
    await importer.async_load()
    assert importer._last_start == last
    assert importer._earliest_start == last
    assert importer._cumulative == pytest.approx(3.0)


async def test_async_load_missing_store_invokes_recovery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _new_importer(hass, "load-missing")
    recover = AsyncMock()
    monkeypatch.setattr(importer, "_recover_baseline_from_recorder", recover)
    await importer.async_load()
    assert recover.await_count == 1
    assert importer._last_start is None
    assert importer._cumulative == 0.0


@pytest.mark.parametrize("bad_payload", [["not", "a", "dict"]])
async def test_async_load_corrupt_non_dict_invokes_recovery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, bad_payload: Any
) -> None:
    importer = _new_importer(hass, "load-corrupt")
    await importer._store.async_save(bad_payload)
    recover = AsyncMock()
    monkeypatch.setattr(importer, "_recover_baseline_from_recorder", recover)
    await importer.async_load()
    assert recover.await_count == 1


async def test_async_load_unexpected_dict_shape_stays_safe(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dict without statistics keys must not raise (no days key here)."""
    importer = _new_importer(hass, "load-shape")
    await importer._store.async_save({"days": ["nope"]})
    recover = AsyncMock()
    monkeypatch.setattr(importer, "_recover_baseline_from_recorder", recover)
    await importer.async_load()
    assert importer._cumulative == 0.0
    assert importer._last_start is None


async def test_async_load_store_exception_invokes_recovery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _new_importer(hass, "load-error")
    monkeypatch.setattr(
        importer._store, "async_load", AsyncMock(side_effect=OSError("disk gone"))
    )
    recover = AsyncMock()
    monkeypatch.setattr(importer, "_recover_baseline_from_recorder", recover)
    await importer.async_load()
    assert recover.await_count == 1


@pytest.mark.parametrize("bad_cumulative", ["junk", float("inf"), float("nan"), None])
async def test_async_load_corrupt_cumulative_coerced(
    hass: HomeAssistant, bad_cumulative: Any
) -> None:
    importer = _new_importer(hass, f"load-cum-{str(bad_cumulative)}")
    last = _jst(14, 5)
    await importer._store.async_save(
        {
            "last_start": last.isoformat(),
            "earliest_start": last.isoformat(),
            "cumulative": bad_cumulative,
        }
    )
    await importer.async_load()
    assert importer._last_start == last
    assert importer._cumulative == 0.0


# ---------------------------------------------------------------------------
# async_import core loop
# ---------------------------------------------------------------------------


async def test_async_import_first_import_emits_all_settled(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-first")
    hourly = _hourly(_jst(14, 0), [0.4, 0.5, 0.6, 0.12345])
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(hourly)
    assert mock_add.call_count == 1
    metadata = mock_add.call_args[0][1]
    points = mock_add.call_args[0][2]
    assert metadata["source"] == DOMAIN
    assert metadata["statistic_id"] == STATISTIC_ID
    assert metadata["has_sum"] is True
    assert len(points) == 4
    expected_sums = [0.4, 0.9, 1.5, round(1.5 + 0.12345, 3)]
    for point, local_start, expected in zip(
        points, [item["start"] for item in hourly], expected_sums, strict=True
    ):
        assert point["start"] == dt_util.as_utc(local_start)
        assert point["start"].tzinfo is not None
        assert point["sum"] == pytest.approx(expected)
    assert importer._last_start == hourly[-1]["start"]
    assert importer._earliest_start == hourly[0]["start"]
    # Internal cumulative keeps full precision; only emitted rows are rounded.
    assert importer._cumulative == pytest.approx(sum(item["kwh"] for item in hourly))


async def test_async_import_incremental_emits_only_newer(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-incr")
    first = _hourly(_jst(14, 0), [0.5, 0.6, 0.7, 0.8])
    second = _hourly(_jst(14, 0), [0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(first)
        assert mock_add.call_count == 1
        mock_add.reset_mock()
        await importer.async_import(second)
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    assert len(points) == 2
    assert points[0]["start"] == dt_util.as_utc(_jst(14, 4))
    assert points[1]["start"] == dt_util.as_utc(_jst(14, 5))
    assert points[0]["sum"] == pytest.approx(2.6 + 0.9)
    assert points[1]["sum"] == pytest.approx(2.6 + 0.9 + 1.0)
    assert importer._cumulative == pytest.approx(4.5)


async def test_async_import_window_widened_reimports_from_zero(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-widen")
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(_hourly(_jst(14, 0), [0.5, 0.6, 0.7]))
        assert importer._earliest_start == _jst(14, 0)
        mock_add.reset_mock()
        widened = _hourly(_jst(13, 22), [0.3, 0.4, 0.5, 0.6, 0.7])
        await importer.async_import(widened)
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    assert len(points) == len(widened)
    running = 0.0
    for point, item in zip(points, widened, strict=True):
        running += item["kwh"]
        assert point["sum"] == pytest.approx(round(running, 3))
    assert importer._earliest_start == _jst(13, 22)
    assert importer._cumulative == pytest.approx(sum(item["kwh"] for item in widened))


async def test_async_import_revision_detection_reimports_from_zero(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-rev")
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(_hourly(_jst(14, 0), [0.5, 0.6, 0.7]))
        assert importer._cumulative == pytest.approx(1.8)
        mock_add.reset_mock()
        # Middle bucket revised upward; the settled window still covers the
        # known range exactly, so the whole window is re-imported from 0.
        await importer.async_import(_hourly(_jst(14, 0), [0.5, 0.9, 0.7]))
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    assert [point["sum"] for point in points] == pytest.approx([0.5, 1.4, 2.1])
    assert importer._cumulative == pytest.approx(2.1)


async def test_async_import_matching_totals_appends_only(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-match")
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(_hourly(_jst(14, 0), [0.5, 0.6, 0.7]))
        mock_add.reset_mock()
        await importer.async_import(_hourly(_jst(14, 0), [0.5, 0.6, 0.7, 0.8]))
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    assert len(points) == 1
    assert points[0]["start"] == dt_util.as_utc(_jst(14, 3))
    assert points[0]["sum"] == pytest.approx(2.6)


async def test_async_import_skips_invalid_and_unsettled(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-skip")
    with _frozen_jst() as now:
        settled_one = _jst(14, 0)
        settled_two = _jst(14, 1)
        # An hour starting an hour ago is still inside the 8h import buffer.
        fresh = now - STATS_IMPORT_BUFFER + timedelta(hours=1)
        fresh = dt_util.as_local(fresh.replace(minute=0, second=0, microsecond=0))
        hourly: list[Any] = [
            {"start": settled_one, "kwh": 0.5},
            "not-a-dict",
            None,
            {"start": "2026-07-14T01:00:00+09:00", "kwh": 1.0},
            {"start": settled_two, "kwh": 0.6},
            {"start": settled_two, "kwh": float("inf")},
            {"start": settled_two, "kwh": float("nan")},
            {"start": settled_two},
            {"start": settled_two, "kwh": "junk"},
            {"start": settled_one, "kwh": 0.25},
            {"start": fresh, "kwh": 9.0},
        ]
        with patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add:
            await importer.async_import(hourly)
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    # settled_one (0.5, then duplicate-hour 0.25 sorts adjacent), settled_two.
    assert len(points) == 3
    assert points[0]["sum"] == pytest.approx(0.5)
    assert points[1]["sum"] == pytest.approx(0.75)
    assert points[2]["sum"] == pytest.approx(1.35)
    assert all(
        point["start"] <= dt_util.as_utc(now - STATS_IMPORT_BUFFER) for point in points
    )


async def test_async_import_no_settled_buckets_no_emit_no_save(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _new_importer(hass, "import-nosettled")
    save = AsyncMock()
    monkeypatch.setattr(importer._store, "async_save", save)
    with (
        _frozen_jst() as now,
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        fresh_start = dt_util.as_local(now - timedelta(hours=1))
        await importer.async_import([{"start": fresh_start, "kwh": 1.0}])
        assert mock_add.call_count == 0
        assert save.await_count == 0
        # Empty input behaves the same way.
        await importer.async_import([])
        assert mock_add.call_count == 0
        assert save.await_count == 0


@pytest.mark.parametrize("payload", ["nope", None, {"start": "x"}])
async def test_async_import_non_list_ignored(hass: HomeAssistant, payload: Any) -> None:
    importer = _new_importer(hass, "import-nonlist")
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(payload)
    assert mock_add.call_count == 0


# ---------------------------------------------------------------------------
# Persisted save path
# ---------------------------------------------------------------------------


async def test_async_import_persists_state_and_reloads(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-persist")
    hourly = _hourly(_jst(14, 0), [0.5, 0.6, 0.7])
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ),
    ):
        await importer.async_import(hourly)
    stored = await importer._store.async_load()
    assert stored is not None
    assert dt_util.parse_datetime(stored["last_start"]) == hourly[-1]["start"]
    assert dt_util.parse_datetime(stored["earliest_start"]) == hourly[0]["start"]
    assert stored["cumulative"] == pytest.approx(1.8)

    reloaded = OctopusStatisticsImporter(hass, "import-persist", ACCOUNT)
    await reloaded.async_load()
    assert reloaded._last_start == importer._last_start
    assert reloaded._earliest_start == importer._earliest_start
    assert reloaded._cumulative == pytest.approx(1.8)


async def test_async_import_store_save_failure_tolerated(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = _new_importer(hass, "import-savefail")
    monkeypatch.setattr(
        importer._store, "async_save", AsyncMock(side_effect=RuntimeError("disk gone"))
    )
    hourly = _hourly(_jst(14, 0), [0.5, 0.6])
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(hourly)
    assert mock_add.call_count == 1
    assert importer._last_start == hourly[-1]["start"]
    assert importer._cumulative == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# Recovered-baseline end to end
# ---------------------------------------------------------------------------


async def test_recovered_baseline_end_to_end_continues_cumulative(
    hass: HomeAssistant,
) -> None:
    importer = _new_importer(hass, "import-recovered")
    recovered_start = _jst(14, 2)
    get_patch, fn_patch, _ = _recorder_patches(
        _dict_rows(_epoch_ms(recovered_start), 10.0)
    )
    with _frozen_jst(), get_patch, fn_patch:
        await importer._recover_baseline_from_recorder()
    assert importer._cumulative == pytest.approx(10.0)

    hourly = _hourly(_jst(14, 0), [0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    with (
        _frozen_jst(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        await importer.async_import(hourly)
    assert mock_add.call_count == 1
    points = mock_add.call_args[0][2]
    # Buckets at or before the recovered start are not re-imported; the
    # running sum continues from the recovered 10 kWh baseline.
    assert len(points) == 3
    assert points[0]["start"] == dt_util.as_utc(_jst(14, 3))
    assert points[0]["sum"] == pytest.approx(10.8)
    assert points[1]["sum"] == pytest.approx(11.7)
    assert points[2]["sum"] == pytest.approx(12.7)
    assert importer._cumulative == pytest.approx(12.7)
    assert importer._last_start == _jst(14, 5)
