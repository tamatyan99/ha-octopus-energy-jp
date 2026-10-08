"""Setup, unload and cleanup tests for the integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun import freeze_time
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_energy_jp import (
    async_remove_entry,
    async_unload_entry,
)
from custom_components.octopus_energy_jp.__init__ import (
    _async_reload_on_update,
    _get_hourly,
    _hourly_signature,
)
from custom_components.octopus_energy_jp.const import CONF_ACCOUNT_NUMBER, DOMAIN
from custom_components.octopus_energy_jp.coordinator import OctopusEnergyJpCoordinator
from custom_components.octopus_energy_jp.sensor import SENSORS
from custom_components.octopus_energy_jp.statistics import OctopusStatisticsImporter

ACCOUNT = "A-TEST1234"
ENTRY_DATA = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_ACCOUNT_NUMBER: ACCOUNT,
}


def _new_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data=ENTRY_DATA)


def _api_client_patch():
    """Never construct a real API client during tests."""
    return patch(
        "custom_components.octopus_energy_jp.OctopusEnergyJpApiClient", MagicMock()
    )


def _offline_fetch_patch():
    """Keep the coordinator's first refresh offline."""
    return patch.object(
        OctopusEnergyJpCoordinator, "_async_update_data", AsyncMock(return_value={})
    )


def _fetch_patch(payload: dict):
    return patch.object(
        OctopusEnergyJpCoordinator,
        "_async_update_data",
        AsyncMock(return_value=payload),
    )


def _jst_hour(day: int, hour: int) -> datetime:
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    return datetime(2026, 7, day, hour, 0, tzinfo=tz)


def _settled_hourly_payload() -> dict:
    """Coordinator payload with one settled hour (frozen clock in tests)."""
    start = _jst_hour(14, 0)
    return {
        "hourly": [
            {"start": start, "kwh": 0.5, "cost": 12.5},
            {"start": start + timedelta(hours=1), "kwh": 0.6, "cost": 15.0},
        ]
    }


def test_get_hourly_and_signature_helpers() -> None:
    assert _get_hourly(None) is None
    assert _get_hourly({}) is None
    assert _get_hourly({"hourly": []}) is None
    hourly = [{"start": "2026-07-14T01:00:00+09:00", "kwh": 1.0}]
    assert _get_hourly({"hourly": hourly}) == hourly
    assert _hourly_signature(hourly) == (1, "2026-07-14T01:00:00+09:00", 1.0, 1.0)
    assert _hourly_signature([{"start": 42}]) == (1, "42", 0.0, 0.0)

    class _Row:
        start = "object-start"

    assert _hourly_signature([_Row()]) == (1, "object-start", 0.0, 0.0)
    assert _hourly_signature(["bad"]) == (1, "bad", 0.0, 0.0)

    class _UnstrableStart:
        def __str__(self) -> str:
            raise ValueError("no str")

    assert _hourly_signature([{"start": _UnstrableStart()}]) == (1, "", 0.0, 0.0)

    class _LenAlwaysFails:
        def __getitem__(self, _idx: int) -> dict[str, str]:
            return {"start": "x"}

        def __len__(self) -> int:
            raise TypeError("no len")

    assert _hourly_signature(_LenAlwaysFails()) == (0, "", 0.0, 0.0)


async def test_async_reload_on_update_requests_entry_reload(hass) -> None:
    entry = _new_entry()
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as mock_reload:
        await _async_reload_on_update(hass, entry)
    mock_reload.assert_awaited_once_with(entry.entry_id)


async def test_setup_creates_all_sensors_and_unloads(hass):
    """Setup registers every sensor description and unload tears them down."""
    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert len(hass.states.async_entity_ids("sensor")) == len(SENSORS)
        assert entry.runtime_data is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_creates_both_importers_and_imports_at_setup(hass):
    """Setup wires consumption and cost importers and imports settled hourly once."""
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ) as mock_add,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    finally:
        dt_util.set_default_time_zone(previous)

    runtime = entry.runtime_data
    assert isinstance(runtime["importer"], OctopusStatisticsImporter)
    assert isinstance(runtime["cost_importer"], OctopusStatisticsImporter)
    assert mock_add.call_count == 2
    statistic_ids = {call.args[1]["statistic_id"] for call in mock_add.call_args_list}
    assert len(statistic_ids) == 2


async def test_import_task_done_logs_task_exception(hass, caplog) -> None:
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    caplog.set_level(logging.ERROR)
    orig_create_bg = hass.async_create_background_task

    def wrap_create_background_task(coro, name, eager_start=True):
        inner = orig_create_bg(coro, name, eager_start)
        orig_add_done = inner.add_done_callback

        def add_done_callback(callback):
            report = MagicMock()
            report.cancelled.return_value = False
            report.exception.return_value = RuntimeError("background import failed")
            orig_add_done(lambda _task: callback(report))

        inner.add_done_callback = add_done_callback
        return inner

    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ),
            patch.object(
                hass,
                "async_create_background_task",
                side_effect=wrap_create_background_task,
            ),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            extended = {
                "hourly": payload["hourly"]
                + [
                    {
                        "start": payload["hourly"][-1]["start"] + timedelta(hours=4),
                        "kwh": 0.2,
                        "cost": 3.0,
                    }
                ]
            }
            entry.runtime_data["coordinator"].data = extended
            entry.runtime_data["coordinator"].async_update_listeners()
            await hass.async_block_till_done()
    finally:
        dt_util.set_default_time_zone(previous)

    assert any(
        "統計のインポートタスクが失敗しました" in record.getMessage()
        for record in caplog.records
    )


async def test_import_task_done_logs_when_exception_lookup_fails(hass, caplog) -> None:
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    caplog.set_level(logging.ERROR)
    orig_create_bg = hass.async_create_background_task

    def wrap_create_background_task(coro, name, eager_start=True):
        inner = orig_create_bg(coro, name, eager_start)
        orig_add_done = inner.add_done_callback

        def add_done_callback(callback):
            report = MagicMock()
            report.cancelled.return_value = False
            report.exception.side_effect = RuntimeError("task state unavailable")
            orig_add_done(lambda _task: callback(report))

        inner.add_done_callback = add_done_callback
        return inner

    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ),
            patch.object(
                hass,
                "async_create_background_task",
                side_effect=wrap_create_background_task,
            ),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            extended = {
                "hourly": payload["hourly"]
                + [
                    {
                        "start": payload["hourly"][-1]["start"] + timedelta(hours=5),
                        "kwh": 0.3,
                        "cost": 4.0,
                    }
                ]
            }
            entry.runtime_data["coordinator"].data = extended
            entry.runtime_data["coordinator"].async_update_listeners()
            await hass.async_block_till_done()
    finally:
        dt_util.set_default_time_zone(previous)

    assert any(
        "インポートタスクの状態取得に失敗しました" in record.getMessage()
        for record in caplog.records
    )


async def test_coordinator_update_imports_both_and_skips_unchanged_signature(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ) as mock_add,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert mock_add.call_count == 2

            coordinator = entry.runtime_data["coordinator"]
            coordinator.data = payload
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            assert mock_add.call_count == 2

            extended = {
                "hourly": payload["hourly"]
                + [
                    {
                        "start": payload["hourly"][-1]["start"] + timedelta(hours=1),
                        "kwh": 0.7,
                        "cost": 20.0,
                    }
                ]
            }
            coordinator.data = extended
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            assert mock_add.call_count == 4
    finally:
        dt_util.set_default_time_zone(previous)


async def test_coordinator_update_imports_when_same_length_hourly_values_change(hass):
    """A corrected kWh for an existing hour must re-trigger import."""
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ) as mock_add,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert mock_add.call_count == 2

            corrected = {
                "hourly": [
                    dict(payload["hourly"][0]),
                    {**payload["hourly"][1], "kwh": 0.99, "cost": 99.0},
                ]
            }
            coordinator = entry.runtime_data["coordinator"]
            coordinator.data = corrected
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            assert mock_add.call_count == 4
    finally:
        dt_util.set_default_time_zone(previous)


async def test_unload_cancels_in_flight_statistics_import(hass):
    """Background import tasks are cancelled on config entry unload."""
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    import_started = asyncio.Event()
    import_completed = False

    async def blocking_import(_hourly_data):
        import_started.set()
        nonlocal import_completed
        await asyncio.Event().wait()
        import_completed = True

    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            runtime = entry.runtime_data
            runtime["importer"].async_import = AsyncMock(side_effect=blocking_import)
            runtime["cost_importer"].async_import = AsyncMock(
                side_effect=blocking_import
            )

            extended = {
                "hourly": payload["hourly"]
                + [
                    {
                        "start": payload["hourly"][-1]["start"] + timedelta(hours=1),
                        "kwh": 0.7,
                        "cost": 20.0,
                    }
                ]
            }
            runtime["coordinator"].data = extended
            runtime["coordinator"].async_update_listeners()
            await import_started.wait()

            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

            assert import_completed is False
    finally:
        dt_util.set_default_time_zone(previous)


async def test_coordinator_update_returns_early_when_hourly_missing(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    with (
        _api_client_patch(),
        _offline_fetch_patch(),
        patch(
            "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
        ) as mock_add,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert mock_add.call_count == 0

        coordinator = entry.runtime_data["coordinator"]
        coordinator.data = {"daily": []}
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert mock_add.call_count == 0


async def test_statistics_import_failure_is_logged(hass, caplog):
    entry = _new_entry()
    entry.add_to_hass(hass)
    payload = _settled_hourly_payload()
    tz = dt_util.get_time_zone("Asia/Tokyo")
    assert tz is not None
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(tz)
    caplog.set_level(logging.ERROR)
    try:
        with (
            _api_client_patch(),
            _fetch_patch(payload),
            freeze_time("2026-07-15 03:00:00"),
            patch(
                "custom_components.octopus_energy_jp.statistics.async_add_external_statistics"
            ),
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            importer = entry.runtime_data["importer"]
            importer.async_import = AsyncMock(side_effect=RuntimeError("import boom"))

            coordinator = entry.runtime_data["coordinator"]
            coordinator.data = {
                "hourly": payload["hourly"]
                + [
                    {
                        "start": payload["hourly"][-1]["start"] + timedelta(hours=2),
                        "kwh": 1.0,
                        "cost": 1.0,
                    }
                ]
            }
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
    finally:
        dt_util.set_default_time_zone(previous)

    assert any(
        "統計のインポートに失敗しました" in record.getMessage()
        for record in caplog.records
    )


async def test_legacy_usage_registry_entry_is_removed(hass):
    """A leftover ``{account}_usage`` entity is cleaned up during setup."""
    registry = er.async_get(hass)
    stale = registry.async_get_or_create("sensor", DOMAIN, f"{ACCOUNT}_usage")
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_usage")
        == stale.entity_id
    )

    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert (
            registry.async_get_entity_id("sensor", DOMAIN, f"{ACCOUNT}_usage") is None
        )

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_legacy_usage_registry_cleanup_failure_does_not_break_setup(hass):
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, f"{ACCOUNT}_usage")
    entry = _new_entry()
    entry.add_to_hass(hass)

    with (
        _api_client_patch(),
        _offline_fetch_patch(),
        patch.object(
            er.EntityRegistry,
            "async_remove",
            side_effect=RuntimeError("registry locked"),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_async_unload_entry_returns_platform_result(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ) as mock_unload:
        result = await async_unload_entry(hass, entry)
    assert result is True
    mock_unload.assert_awaited_once()


async def test_remove_entry_completes(hass):
    """Deleting the entry runs its cleanup without raising."""
    entry = _new_entry()
    entry.add_to_hass(hass)

    with _api_client_patch(), _offline_fetch_patch():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_async_remove_entry_removes_all_stores_and_tolerates_failures(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    removed: list[str] = []

    async def tracked_remove(self: Store) -> None:
        removed.append(self.key)
        if self.key.endswith("_statistics") and not self.key.endswith(
            "_statistics_cost"
        ):
            raise OSError("statistics store stuck")

    with patch.object(Store, "async_remove", tracked_remove):
        await async_remove_entry(hass, entry)

    expected = {
        f"{DOMAIN}_{entry.entry_id}_daily",
        f"{DOMAIN}_{entry.entry_id}_statistics",
        f"{DOMAIN}_{entry.entry_id}_statistics_cost",
    }
    assert set(removed) == expected


async def test_async_remove_entry_clears_recorder_statistics(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    mock_instance = MagicMock()
    with patch(
        "custom_components.octopus_energy_jp.get_instance",
        return_value=mock_instance,
    ):
        await async_remove_entry(hass, entry)

    mock_instance.async_clear_statistics.assert_called_once_with(
        [
            "octopus_energy_jp:a_test1234_consumption",
            "octopus_energy_jp:a_test1234_cost",
        ]
    )


async def test_async_remove_entry_swallows_recorder_clear_failure(hass):
    entry = _new_entry()
    entry.add_to_hass(hass)
    mock_instance = MagicMock()
    mock_instance.async_clear_statistics.side_effect = RuntimeError("recorder down")
    with patch(
        "custom_components.octopus_energy_jp.get_instance",
        return_value=mock_instance,
    ):
        await async_remove_entry(hass, entry)
