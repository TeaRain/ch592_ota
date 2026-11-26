"""Sensors exposing firmware version and OTA progress."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Ch592OtaCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Ch592OtaCoordinator = entry.runtime_data
    async_add_entities(
        [
            Ch592FirmwareVersionSensor(coordinator, entry),
            Ch592OtaStatusSensor(coordinator, entry),
            Ch592OtaProgressSensor(coordinator, entry),
            Ch592CurrentThresholdSensor(coordinator, entry),
            Ch592RawGasCounterSensor(coordinator, entry),
            Ch592GasTotalSensor(coordinator, entry),
            Ch592GasConsumptionSensor(coordinator, entry),
        ]
    )


class Ch592OtaEntity(SensorEntity):
    """Common base for entities backed by the coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: Ch592OtaCoordinator, entry: ConfigEntry
    ) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._address = coordinator.address

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


class Ch592FirmwareVersionSensor(Ch592OtaEntity):
    """Firmware version reported in BTHome text object 0x53."""

    _attr_name = "固件版本"
    _attr_icon = "mdi:tag-text"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._address}_firmware_version"

    @property
    def native_value(self) -> str | None:
        value = self.coordinator.data.get("firmware_version")
        return str(value) if value else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        last_seen = self.coordinator.last_seen
        return {
            "address": self._address,
            "ota_mode": self.coordinator.data.get("ota_mode"),
            "last_seen": last_seen.isoformat() if last_seen else None,
        }


class Ch592OtaStatusSensor(Ch592OtaEntity):
    """Current OTA state: idle, uploading, verifying, success, failed..."""

    _attr_name = "升级状态"
    _attr_icon = "mdi:update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._address}_ota_status"

    @property
    def native_value(self) -> str:
        return str(self.coordinator.data.get("status", "idle"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "phase": self.coordinator.data.get("phase"),
            "error": self.coordinator.data.get("error"),
            "wake_attempts": self.coordinator.data.get("wake_attempts"),
            "wake_error": self.coordinator.data.get("wake_error"),
            "ota_mode": self.coordinator.data.get("ota_mode"),
            # Supply voltage measured by the device in OTA mode (firmware >= 1.3.0).
            "vdd_mv": self.coordinator.data.get("vdd"),
            # Lowest value seen during the upload (fresh coin cell sags under load).
            "vdd_min_mv": self.coordinator.data.get("vdd_min"),
            "device_protocol": self.coordinator.data.get("protocol"),
            "device_features": (
                f"0x{int(self.coordinator.data['features']):02X}"
                if self.coordinator.data.get("features") is not None
                else None
            ),
            "mtu": self.coordinator.data.get("mtu"),
            "chunk": self.coordinator.data.get("chunk"),
            "verify_chunk": self.coordinator.data.get("verify_chunk"),
            "duration_s": self.coordinator.data.get("duration_s"),
            "gas_pending": self.coordinator.data.get("gas_pending"),
            "gas_result": self.coordinator.data.get("gas_result"),
            "device_threshold": self.coordinator.data.get("threshold"),
            "threshold_pending": self.coordinator.data.get("threshold_pending"),
            "threshold_result": self.coordinator.data.get("threshold_result"),
        }


class Ch592OtaProgressSensor(Ch592OtaEntity):
    """Upload progress in percent."""

    _attr_name = "升级进度"
    _attr_icon = "mdi:progress-upload"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._address}_ota_progress"

    @property
    def native_value(self) -> int:
        return int(self.coordinator.data.get("progress", 0))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "uploaded": self.coordinator.data.get("uploaded"),
            "total": self.coordinator.data.get("total"),
        }


class Ch592CurrentThresholdSensor(Ch592OtaEntity):
    """Live threshold reported by normal advertisements after P/N changes."""

    _attr_name = "当前采样阈值（P/N按键调整）"
    _attr_icon = "mdi:tune-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._address}_current_threshold"

    @property
    def native_value(self) -> int | None:
        value = self.coordinator.data.get("threshold")
        return int(value) if value is not None else None


class Ch592GasSensor(Ch592OtaEntity):
    """Base for sensors whose values are stored in 0.001 m³ units."""

    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_suggested_display_precision = 3
    _data_key: str

    def __init__(self, coordinator, entry, unique_name: str) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{self._address}_{unique_name}"

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.get(self._data_key)
        return round(int(value) / 1000, 3) if value is not None else None


class Ch592RawGasCounterSensor(Ch592GasSensor):
    _attr_name = "设备原始计数"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL
    _data_key = "gas_value"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "raw_gas_counter")


class Ch592GasTotalSensor(Ch592GasSensor):
    _attr_name = "燃气表读数"
    _attr_icon = "mdi:gas-cylinder"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _data_key = "gas_total"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "gas_total")

    @property
    def available(self) -> bool:
        return self.coordinator.data.get(self._data_key) is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        def m3(value):
            return round(int(value) / 1000, 3) if value is not None else None
        return {
            "raw_value_m3": m3(self.coordinator.data.get("gas_value")),
            "accepted_raw_value_m3": m3(self.coordinator.data.get("gas_accepted_raw")),
            "offset_m3": m3(self.coordinator.data.get("gas_offset")),
            "effective_offset_m3": m3(self.coordinator.data.get("gas_effective_offset")),
            "wrap_count": self.coordinator.data.get("gas_wraps"),
            "calibrated_at": self.coordinator.data.get("gas_calibrated_at"),
            "raw_reset_detected": self.coordinator.data.get("gas_reset_detected"),
        }


class Ch592GasConsumptionSensor(Ch592GasSensor):
    _attr_name = "累计用气量（统计）"
    _attr_icon = "mdi:chart-line"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _data_key = "gas_consumption_total"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "gas_consumption_total")

