"""Sensor entities for the Entelar custom integration.

Day-1 scaffold: ONE sensor (battery state of charge). Once verified working,
the SENSOR_DESCRIPTIONS list grows to cover the full 15+ entities we had
on the MQTT bridge.
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, MANUFACTURER, MODEL, MODEL_METER,
    ATTR_BATTERY_SOC, ATTR_PV_KW, ATTR_LOAD_KW, ATTR_GRID_KW, ATTR_BATTERY_KW,
    ATTR_PV_TODAY_KWH, ATTR_PV_MTD_KWH,
    ATTR_GRID_IMPORT_MTD, ATTR_GRID_EXPORT_MTD,
    ATTR_BATTERY_CHARGED_MTD, ATTR_BATTERY_DISCHARGED_MTD,
    ATTR_METER_POWER_KW, ATTR_METER_IMPORT_KWH, ATTR_METER_EXPORT_KWH,
)
from .coordinator import EntelarCoordinator


@dataclass(frozen=True, kw_only=True)
class EntelarSensorEntityDescription(SensorEntityDescription):
    """Sensor description with a `device` tag ('site' or 'meter')."""

    device: str = "site"


# Module-level list of sensors to create. Each entry's `key` must match a
# field name in the dict returned by snapshot_site().
#
# Day 2 scope: live values (5) + PV TD/MTD/BOL from snapshot (3).
# Day 3 will add grid+battery MTD/lifetime (computed from daily history) and
# cost sensors. Day 1 had just battery_soc; this expands to 8.
SENSOR_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    # --- Live measurements ---
    SensorEntityDescription(
        key=ATTR_BATTERY_SOC,
        translation_key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key=ATTR_PV_KW,
        translation_key="pv_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_LOAD_KW,
        translation_key="load_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_GRID_KW,
        translation_key="grid_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_BATTERY_KW,
        translation_key="battery_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    # --- Cumulative PV energy (from snapshot, portal-accurate) ---
    SensorEntityDescription(
        key=ATTR_PV_TODAY_KWH,
        translation_key="pv_today_kwh",
        device_class=SensorDeviceClass.ENERGY,
        # state_class total_increasing on a "today" value works because the
        # portal increments it continuously until midnight when it resets.
        # HA handles the reset as a counter rollover.
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_PV_MTD_KWH,
        translation_key="pv_month_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    # --- Cumulative grid + battery MTD (computed from daily history) ---
    # Lifetime metrics are NOT exposed as entities -- they live as external
    # statistics under `entelar:*` (see statistics_manager.py) to avoid
    # contamination between seeded historical data and live state-recording.
    SensorEntityDescription(
        key=ATTR_GRID_IMPORT_MTD,
        translation_key="grid_import_mtd",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_GRID_EXPORT_MTD,
        translation_key="grid_export_mtd",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_BATTERY_CHARGED_MTD,
        translation_key="battery_charged_mtd",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    SensorEntityDescription(
        key=ATTR_BATTERY_DISCHARGED_MTD,
        translation_key="battery_discharged_mtd",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
)


# Grid meter (Res_Meter) -- the inverter's grid-connection meter, a separate
# device. This is the solar circuit's grid exchange, NOT whole-house (the
# house's other circuits/phases are only seen by the utility revenue meter).
# The *_kwh registers are true lifetime odometer totals, so TOTAL_INCREASING
# lets HA derive long-term statistics for them automatically.
METER_SENSORS: tuple[EntelarSensorEntityDescription, ...] = (
    EntelarSensorEntityDescription(
        key=ATTR_METER_POWER_KW,
        translation_key="meter_power",
        device="meter",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    ),
    EntelarSensorEntityDescription(
        key=ATTR_METER_IMPORT_KWH,
        translation_key="meter_grid_import",
        device="meter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    EntelarSensorEntityDescription(
        key=ATTR_METER_EXPORT_KWH,
        translation_key="meter_grid_export",
        device="meter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Entelar sensors from a config entry."""
    coordinator: EntelarCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [EntelarSensor(coordinator, desc, entry) for desc in SENSOR_DESCRIPTIONS]
    # Add the whole-house grid meter's entities only when a meter was discovered.
    if (coordinator.data or {}).get("meter_id"):
        entities += [EntelarSensor(coordinator, desc, entry) for desc in METER_SENSORS]
    async_add_entities(entities)


class EntelarSensor(CoordinatorEntity[EntelarCoordinator], SensorEntity):
    """A single sensor reading one field from the coordinator's snapshot."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EntelarCoordinator,
        description: SensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        data = coordinator.data or {}
        site_id = data.get("site_id") or entry.entry_id
        site_name = data.get("site_name")
        if getattr(description, "device", "site") == "meter":
            # Separate meter device, linked to the site via `via_device`.
            meter_id = data.get("meter_id") or entry.entry_id
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"meter_{meter_id}")},
                name=data.get("meter_name") or "Grid Meter",
                manufacturer=MANUFACTURER,
                model=MODEL_METER,
                via_device=(DOMAIN, site_id),
                configuration_url="https://app.entelarenergy-emsportal.com/",
            )
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, site_id)},
                name=site_name or "Entelar Inverter",
                manufacturer=MANUFACTURER,
                model=MODEL,
                configuration_url="https://app.entelarenergy-emsportal.com/",
            )

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self.entity_description.key)
