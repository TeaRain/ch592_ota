"""Firmware update entity for a CH592 device.

This exposes the WCH backup-OTA upload as a native Home Assistant update entity:
the version card shows up in Settings -> Updates and on the device page, with an
install button and a progress bar.
"""

from __future__ import annotations

import logging

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import Ch592OtaCoordinator
from .entity import build_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: Ch592OtaCoordinator = entry.runtime_data
    async_add_entities([Ch592FirmwareUpdateEntity(coordinator)])


class Ch592FirmwareUpdateEntity(UpdateEntity):
    """Firmware update backed by the bundled .bin and its .json sidecar."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_should_poll = False
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, coordinator: Ch592OtaCoordinator) -> None:
        self.coordinator = coordinator
        self._address = coordinator.address
        self._attr_unique_id = f"{self._address}_firmware_update"
        self._latest_version: str | None = None
        self._latest_meta: dict[str, object] = {}

    # ------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )
        await self._async_load_latest()

    # --------------------------------------------------------------- update API

    @property
    def device_info(self):
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return True

    @property
    def installed_version(self) -> str | None:
        value = self.coordinator.data.get("firmware_version")
        return str(value) if value else None

    @property
    def latest_version(self) -> str | None:
        return self._latest_version or self.installed_version

    @property
    def release_summary(self) -> str | None:
        size = self._latest_meta.get("size")
        crc = self._latest_meta.get("crc32")
        parts = []
        if size:
            parts.append(f"{int(size) // 1024} KiB")
        if crc:
            parts.append(f"CRC {crc}")
        parts.append("升级前请长按 B 键 3 秒进入 OTA 模式")
        return " · ".join(parts)

    async def async_install(
        self, version: str | None = None, backup: bool = False
    ) -> None:
        """Start the OTA upload and wait for the device to come back."""
        await self.coordinator.async_start_ota()
        task = self.coordinator.current_task
        if task is not None:
            await task
        if self.coordinator.data.get("status") != "success":
            raise HomeAssistantError(
                str(self.coordinator.data.get("error") or "OTA 升级失败")
            )

    # --------------------------------------------------------------- internals

    async def _async_load_latest(self) -> None:
        try:
            path = self.coordinator.resolve_firmware_path()
            image, meta = await self.hass.async_add_executor_job(
                self.coordinator.read_image, path
            )
        except (OSError, HomeAssistantError) as err:
            _LOGGER.warning("无法读取固件文件: %s", err)
            return
        version = meta.get("version")
        if version:
            self._latest_version = str(version)
        else:
            self._latest_version = None
        self._latest_meta = meta
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        status = str(self.coordinator.data.get("status", "idle"))
        if status in ("starting", "running", "rebooting"):
            # update_percentage is 0-100 and only shown while in_progress.
            self._attr_in_progress = True
            self._attr_update_percentage = min(
                max(float(self.coordinator.data.get("progress", 0)), 0), 99
            )
        else:
            self._attr_in_progress = False
            self._attr_update_percentage = None
        self.async_write_ha_state()
