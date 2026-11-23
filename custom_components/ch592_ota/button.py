"""Buttons for the CH592 OTA integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .const import DOMAIN
from .coordinator import Ch592OtaCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Ch592OtaCoordinator = entry.runtime_data

    # The number entities now perform calibration / arm threshold writes
    # directly. Remove the old two-step button entities from existing installs.
    registry = async_get_entity_registry(hass)
    for suffix in ("write_gas", "write_threshold"):
        entity_id = registry.async_get_entity_id(
            "button", DOMAIN, f"{coordinator.address}_{suffix}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)

    async_add_entities(
        [
            Ch592FirmwareCheckButton(coordinator),
            Ch592OtaAbortButton(coordinator),
            Ch592CancelPendingButton(coordinator),
        ]
    )


class Ch592FirmwareCheckButton(ButtonEntity):
    """Check the firmware manifest right now instead of waiting for the timer."""

    _attr_has_entity_name = True
    _attr_name = "检查固件更新"
    _attr_icon = "mdi:cloud-refresh"
    _attr_should_poll = False

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_check_firmware_update"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return self.coordinator.cloud is not None

    async def async_press(self) -> None:
        cloud = self.coordinator.cloud
        if cloud is None:
            raise HomeAssistantError("未启用固件清单检查")
        await cloud.async_refresh()
        if not cloud.last_update_success:
            raise HomeAssistantError(
                f"检查固件失败: {cloud.last_exception or '未知错误'}"
            )


class Ch592OtaAbortButton(ButtonEntity):
    """Cancel a running OTA upload without opening Developer Tools."""

    _attr_has_entity_name = True
    _attr_name = "取消升级"
    _attr_icon = "mdi:cancel"
    _attr_should_poll = False

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


class Ch592CancelPendingButton(ButtonEntity):
    """Drop an armed threshold write so pressing B upgrades again."""

    _attr_has_entity_name = True
    _attr_name = "取消待写入"
    _attr_icon = "mdi:close-circle-outline"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        # Keep the old unique_id so existing installs keep their entity.
        self._attr_unique_id = f"{self._address}_cancel_gas"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.data.get("gas_pending") is not None
            or self.coordinator.data.get("threshold_pending") is not None
        )

    async def async_press(self) -> None:
        self.coordinator.cancel_gas()
        await self.coordinator.async_cancel_threshold()
