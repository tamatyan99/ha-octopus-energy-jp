"""Sensors for Octopus Energy Japan."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACCOUNT_NUMBER, DOMAIN
from .coordinator import OctopusEnergyJpCoordinator


@dataclass(frozen=True, kw_only=True)
class OctopusSensorDescription(SensorEntityDescription):
    """Sensor description with a value accessor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[OctopusSensorDescription, ...] = (
    OctopusSensorDescription(
        key="usage",
        translation_key="usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["yesterday_kwh"],
    ),
    OctopusSensorDescription(
        key="yesterday_kwh",
        translation_key="yesterday_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["yesterday_kwh"],
    ),
    OctopusSensorDescription(
        key="today_kwh",
        translation_key="today_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["today_kwh"],
    ),
    OctopusSensorDescription(
        key="month_kwh",
        translation_key="month_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda d: d["month_kwh"],
    ),
    OctopusSensorDescription(
        key="diff_kwh",
        translation_key="diff_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["diff_kwh"],
    ),
    OctopusSensorDescription(
        key="prev_month_kwh",
        translation_key="prev_month_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["prev_month_kwh"],
    ),
    OctopusSensorDescription(
        key="month_diff_kwh",
        translation_key="month_diff_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d["month_diff_kwh"],
    ),
    OctopusSensorDescription(
        key="cost_yesterday",
        translation_key="cost_yesterday",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_yesterday"],
    ),
    OctopusSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_today"],
    ),
    OctopusSensorDescription(
        key="cost_month",
        translation_key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_month"],
    ),
    OctopusSensorDescription(
        key="prev_month_cost",
        translation_key="prev_month_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        suggested_display_precision=0,
        value_fn=lambda d: d["prev_month_cost"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator: OctopusEnergyJpCoordinator = entry.runtime_data["coordinator"]
    async_add_entities(
        OctopusSensor(coordinator, description, entry) for description in SENSORS
    )


class OctopusSensor(CoordinatorEntity[OctopusEnergyJpCoordinator], SensorEntity):
    """An aggregated usage/cost sensor."""

    entity_description: OctopusSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OctopusEnergyJpCoordinator,
        description: OctopusSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        account = entry.data[CONF_ACCOUNT_NUMBER]
        self._attr_unique_id = f"{account}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account)},
            name=f"Octopus Energy ({account})",
            manufacturer="Octopus Energy Japan",
            model=coordinator.data.get("plan_name") if coordinator.data else None,
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full aggregate payload on the main sensor."""
        if self.entity_description.key != "usage" or self.coordinator.data is None:
            return None
        d = self.coordinator.data
        return {
            "today_kwh": d["today_kwh"],
            "month_kwh": d["month_kwh"],
            "diff_kwh": d["diff_kwh"],
            "diff_pct": d["diff_pct"],
            "avg_rate": d["avg_rate"],
            "cost_yesterday": d["cost_yesterday"],
            "cost_today": d["cost_today"],
            "cost_month": d["cost_month"],
            "prev_month_kwh": d["prev_month_kwh"],
            "prev_month_cost": d["prev_month_cost"],
            "month_diff_kwh": d["month_diff_kwh"],
            "month_diff_pct": d["month_diff_pct"],
            "daily": d["daily"],
            "yesterday_series": d["yesterday_series"],
            "today_series": d["today_series"],
            "plan_name": d["plan_name"],
            "last_update": d["last_update"],
        }
