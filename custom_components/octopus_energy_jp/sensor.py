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

from .const import CONF_ACCOUNT_NUMBER, DOMAIN, MAX_DAILY_ATTRIBUTE_DAYS
from .coordinator import OctopusEnergyJpCoordinator

# 30分値系列の属性上限: 1日48点 × 前日+当日バッファ相当
MAX_SERIES_ATTRIBUTE_POINTS = 96


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
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: d["yesterday_kwh"],
    ),
    OctopusSensorDescription(
        key="yesterday_kwh",
        translation_key="yesterday_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: d["yesterday_kwh"],
    ),
    OctopusSensorDescription(
        key="today_kwh",
        translation_key="today_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: d["today_kwh"],
    ),
    OctopusSensorDescription(
        key="month_kwh",
        translation_key="month_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: d["month_kwh"],
    ),
    OctopusSensorDescription(
        key="diff_kwh",
        translation_key="diff_kwh",
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
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: d["prev_month_kwh"],
    ),
    OctopusSensorDescription(
        key="month_diff_kwh",
        translation_key="month_diff_kwh",
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
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_yesterday"],
    ),
    OctopusSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_today"],
    ),
    OctopusSensorDescription(
        key="cost_month",
        translation_key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: d["cost_month"],
    ),
    OctopusSensorDescription(
        key="prev_month_cost",
        translation_key="prev_month_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: d["prev_month_cost"],
    ),
    OctopusSensorDescription(
        key="billing_kwh",
        translation_key="billing_kwh",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda d: (d.get("billing") or {}).get("kwh"),
    ),
    OctopusSensorDescription(
        key="billing_cost",
        translation_key="billing_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="JPY",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
        value_fn=lambda d: (d.get("billing") or {}).get("total"),
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
            model="Electricity",
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full aggregate payload on the main sensor."""
        if self.entity_description.key != "usage" or self.coordinator.data is None:
            return None
        d = self.coordinator.data
        daily = d.get("daily") or []
        yesterday_series = d.get("yesterday_series") or []
        today_series = d.get("today_series") or []
        return {
            "today_kwh": d.get("today_kwh"),
            "month_kwh": d.get("month_kwh"),
            "diff_kwh": d.get("diff_kwh"),
            "diff_pct": d.get("diff_pct"),
            "avg_rate": d.get("avg_rate"),
            "cost_yesterday": d.get("cost_yesterday"),
            "cost_today": d.get("cost_today"),
            "cost_month": d.get("cost_month"),
            "prev_month_kwh": d.get("prev_month_kwh"),
            "prev_month_cost": d.get("prev_month_cost"),
            "month_diff_kwh": d.get("month_diff_kwh"),
            "month_diff_pct": d.get("month_diff_pct"),
            "daily": daily[-MAX_DAILY_ATTRIBUTE_DAYS:],
            "yesterday_series": yesterday_series[-MAX_SERIES_ATTRIBUTE_POINTS:],
            "today_series": today_series[-MAX_SERIES_ATTRIBUTE_POINTS:],
            "plan_name": d.get("plan_name"),
            "last_update": d.get("last_update"),
        }
