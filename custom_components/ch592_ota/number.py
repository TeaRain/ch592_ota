"""Calibration and threshold draft values for the CH592 integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    GAS_MAX_UNITS,
    THRESHOLD_DEFAULT,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
)
from .coordinator import Ch592OtaCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Ch592OtaCoordinator = entry.runtime_data
    async_add_entities(
        [Ch592GasTargetNumber(coordinator), Ch592ThresholdNumber(coordinator)]
    )


class Ch592GasTargetNumber(NumberEntity):
    """Mechanical meter reading used to calculate the HA offset."""

    _attr_has_entity_name = True
    _attr_name = "机械表实际读数（输入后自动校准）"
    _attr_icon = "mdi:counter"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = GAS_MAX_UNITS / 1000  # 99 999.999 m³
    _attr_native_step = 0.001
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_mode = NumberMode.BOX
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_gas_target"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        target = self.coordinator.data.get("gas_target")
        if target is not None:
            return round(int(target) / 1000, 3)  # type: ignore[arg-type]
        gas = self.coordinator.data.get("gas_total")
        return round(int(gas) / 1000, 3) if gas is not None else None  # type: ignore[arg-type]

    async def async_set_native_value(self, value: float) -> None:
        actual_units = int(round(value * 1000))
        self.coordinator.set_gas_target(actual_units)
        await self.coordinator.async_calibrate_gas(actual_units)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        gas = self.coordinator.data.get("gas_total")
        pending = self.coordinator.data.get("gas_pending")
        return {
            "device_value_m3": round(int(gas) / 1000, 3) if gas is not None else None,  # type: ignore[arg-type]
            "pending_value_m3": round(int(pending) / 1000, 3) if pending is not None else None,  # type: ignore[arg-type]
            "last_result": self.coordinator.data.get("gas_result"),
            "操作说明": "输入机械表完整读数，按回车或点击输入框外即校准；无需按B，不修改设备原始计数",
        }


class Ch592ThresholdNumber(NumberEntity):
    """Set the TCRT5000 threshold, arming a write until OTA mode if needed."""

    _attr_has_entity_name = True
    _attr_name = "采样阈值（等待OTA写入）"
    _attr_icon = "mdi:tune-variant"
    _attr_native_min_value = THRESHOLD_MIN
    _attr_native_max_value = THRESHOLD_MAX
    _attr_native_step = 10
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_threshold"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> int | None:
        target = self.coordinator.data.get("threshold_target")
        if target is not None:
            return int(target)  # type: ignore[arg-type]
        device = self.coordinator.data.get("threshold")
        return int(device) if device is not None else THRESHOLD_DEFAULT  # type: ignore[arg-type]

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_threshold(int(round(value)))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "device_threshold": self.coordinator.data.get("threshold"),
            "pending_threshold": self.coordinator.data.get("threshold_pending"),
            "last_result": self.coordinator.data.get("threshold_result"),
            "操作说明": "输入后等待下次OTA窗口：按B立即触发或最长等待3天；已在OTA时直接写入",
        }
