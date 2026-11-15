"""Buttons for the CH592 OTA integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    async_add_entities([Ch592OtaAbortButton(coordinator)])


class Ch592OtaAbortButton(ButtonEntity):
    """Cancel a running OTA upload without opening Developer Tools."""

    _attr_has_entity_name = True
    _attr_name = "Abort OTA"
    _attr_icon = "mdi:cancel"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_ota_abort"

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

    async def async_press(self) -> None:
        await self.coordinator.async_abort()
