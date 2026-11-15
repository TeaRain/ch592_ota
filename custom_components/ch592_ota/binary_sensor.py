"""Binary sensors for the CH592 OTA integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities([Ch592OtaModeSensor(coordinator)])


class Ch592OtaModeSensor(BinarySensorEntity):
    """Whether the device is advertising the connectable OTA service."""

    _attr_has_entity_name = True
    _attr_name = "OTA mode"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_ota_mode"

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
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("ota_mode"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Diagnostics: which advertisements this integration actually sees."""
        last_seen = self.coordinator.last_seen
        return {
            "auto_note": self.coordinator.data.get("auto_note"),
            "ads_total": self.coordinator.data.get("ads_total", 0),
            "last_name": self.coordinator.data.get("last_name"),
            "last_service_uuids": self.coordinator.data.get("last_uuids"),
            "last_service_data_uuids": self.coordinator.data.get("last_data_uuids"),
            "last_seen": last_seen.isoformat() if last_seen else None,
        }
