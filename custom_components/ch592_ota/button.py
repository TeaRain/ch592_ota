"""Buttons for the CH592 OTA integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
            Ch592FirmwareCheckButton(coordinator),
            Ch592OtaAbortButton(coordinator),
            Ch592WriteGasButton(coordinator),
            Ch592WriteThresholdButton(coordinator),
            Ch592ResetGasGuardButton(coordinator),
            Ch592CancelPendingButton(coordinator),
        ]
    )


class Ch592FirmwareCheckButton(ButtonEntity):
    """Check the firmware manifest right now instead of waiting for the timer."""

    _attr_has_entity_name = True
    _attr_name = "Check for updates"
    _attr_icon = "mdi:cloud-refresh"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
    _attr_name = "Abort OTA"
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


class Ch592WriteGasButton(ButtonEntity):
    """Write the draft gas value (arms it when the device is not in OTA mode)."""

    _attr_has_entity_name = True
    _attr_name = "Write gas value"
    _attr_icon = "mdi:content-save-edit"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_write_gas"

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
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "ota_mode": self.coordinator.data.get("ota_mode"),
            "pending": self.coordinator.data.get("gas_pending") is not None,
            "last_result": self.coordinator.data.get("gas_result"),
        }

    async def async_press(self) -> None:
        target = self.coordinator.data.get("gas_target")
        if target is None:
            raise HomeAssistantError("请先在 Gas target 里设置要写入的燃气值")
        await self.coordinator.async_set_gas(int(target))  # type: ignore[arg-type]


class Ch592WriteThresholdButton(ButtonEntity):
    """Write the draft pulse threshold (arms it when not in OTA mode)."""

    _attr_has_entity_name = True
    _attr_name = "Write pulse threshold"
    _attr_icon = "mdi:tune"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_write_threshold"

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
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "ota_mode": self.coordinator.data.get("ota_mode"),
            "pending": self.coordinator.data.get("threshold_pending") is not None,
            "last_result": self.coordinator.data.get("threshold_result"),
        }

    async def async_press(self) -> None:
        target = self.coordinator.data.get("threshold_target")
        if target is None:
            raise HomeAssistantError(
                "请先在 Pulse threshold 里设置要写入的阈值"
            )
        await self.coordinator.async_set_threshold(int(target))  # type: ignore[arg-type]


class Ch592CancelPendingButton(ButtonEntity):
    """Drop armed writes so pressing B upgrades again."""

    _attr_has_entity_name = True
    _attr_name = "Cancel pending writes"
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
        self.coordinator.cancel_threshold()


class Ch592ResetGasGuardButton(ButtonEntity):
    """Accept the device's current reading as the guarded total's baseline."""

    _attr_has_entity_name = True
    _attr_name = "Reset gas guard"
    _attr_icon = "mdi:backup-restore"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_reset_gas_guard"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return self.coordinator.data.get("gas_value") is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        raw = self.coordinator.data.get("gas_value")
        return {
            "current_value_m3": (
                round(int(raw) / 1000, 3) if raw is not None else None
            ),
            "blocked_since": self.coordinator.data.get("gas_guard_blocked_at"),
        }

    async def async_press(self) -> None:
        self.coordinator.reset_gas_guard()
