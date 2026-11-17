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
from homeassistant.helpers.restore_state import RestoreSensor

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
            Ch592GasTotalSensor(coordinator, entry),
            Ch592OtaStatusSensor(coordinator, entry),
            Ch592OtaProgressSensor(coordinator, entry),
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

    _attr_name = "Firmware version"
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

    _attr_name = "OTA status"
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
            "ota_mode": self.coordinator.data.get("ota_mode"),
            # Supply voltage measured by the device in OTA mode (firmware >= 1.3.0).
            "vdd_mv": self.coordinator.data.get("vdd"),
            # Lowest value seen during the upload (fresh coin cell sags under load).
            "vdd_min_mv": self.coordinator.data.get("vdd_min"),
            "device_protocol": self.coordinator.data.get("protocol"),
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

    _attr_name = "OTA progress"
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


class Ch592GasTotalSensor(RestoreSensor):
    """Monotonic gas total, safe for consumption statistics.

    The raw BTHome ``0x4C`` value can jump backwards: Home Assistant replays a
    saved old advertisement after a restart, and firmware <= 1.4.1 silently
    loses saves so a power loss can load an older DataFlash record. Feeding
    either into a Derivative or utility_meter helper produces negative (or
    inflated) consumption. This sensor only moves forward; use the
    ``Reset gas guard`` button, the ``ch592_ota.reset_gas_guard`` service or a
    deliberate gas write to accept a real downward correction.
    """

    _attr_has_entity_name = True
    _attr_name = "Gas total (guarded)"
    _attr_icon = "mdi:gas-cylinder"
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_device_class = SensorDeviceClass.GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: Ch592OtaCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_gas_total_guarded"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            try:
                if data.native_value is not None:
                    self.coordinator.seed_gas_guard(
                        int(round(float(data.native_value) * 1000))
                    )
            except (TypeError, ValueError):
                pass
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
        value = self.coordinator.data.get("gas_guard_value")
        return round(int(value) / 1000, 3) if value is not None else None  # type: ignore[arg-type]

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        raw = self.coordinator.data.get("gas_value")
        blocked = self.coordinator.data.get("gas_guard_blocked")
        return {
            "raw_value_m3": round(int(raw) / 1000, 3) if raw is not None else None,  # type: ignore[arg-type]
            "blocked_value_m3": (
                round(int(blocked) / 1000, 3) if blocked is not None else None
            ),
            "blocked_since": self.coordinator.data.get("gas_guard_blocked_at"),
        }
