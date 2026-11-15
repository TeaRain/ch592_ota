"""CH592 OTA integration.

Uploads WCH backup-OTA images to a CH592 gas-meter broadcaster through a
Home Assistant Bluetooth proxy. The device must be put into OTA mode by holding
button B for 3 seconds.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CHUNK,
    CONF_FIRMWARE,
    CONF_MANIFEST,
    CONF_VERSION,
    DEFAULT_MANIFEST_URL,
    DOMAIN,
    SERVICE_ABORT,
    SERVICE_FLASH,
)
from .firmware import FirmwareCloudCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "button", "sensor", "update"]

FLASH_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ADDRESS): cv.string,
        vol.Optional(CONF_FIRMWARE): cv.string,
        vol.Optional(CONF_VERSION): cv.string,
        vol.Optional(CONF_CHUNK): vol.All(
            vol.Coerce(int), vol.Range(min=16, max=64)
        ),
    }
)

ABORT_SCHEMA = vol.Schema({vol.Optional(CONF_ADDRESS): cv.string})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration-wide services."""
    hass.data.setdefault(DOMAIN, {})
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one CH592 device."""
    from .coordinator import Ch592OtaCoordinator

    _async_register_services(hass)
    coordinator = Ch592OtaCoordinator(hass, entry)
    entry.runtime_data = coordinator

    manifest_url = str(
        entry.options.get(CONF_MANIFEST, DEFAULT_MANIFEST_URL) or ""
    ).strip()
    if manifest_url:
        coordinator.cloud = FirmwareCloudCoordinator(hass, manifest_url, entry)
        # Refresh once, but don't block setup if the network is down.
        await coordinator.cloud.async_refresh()

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change so the update entity re-reads the sidecar."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        await coordinator.async_stop()
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("services_registered"):
        return
    data["services_registered"] = True

    async def async_handle_flash(call: ServiceCall) -> dict[str, object]:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ADDRESS))
        return await coordinator.async_start_ota(
            call.data.get(CONF_FIRMWARE),
            call.data.get(CONF_VERSION),
            call.data.get(CONF_CHUNK),
        )

    async def async_handle_abort(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ADDRESS))
        await coordinator.async_abort()

    hass.services.async_register(
        DOMAIN,
        SERVICE_FLASH,
        async_handle_flash,
        schema=FLASH_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ABORT,
        async_handle_abort,
        schema=ABORT_SCHEMA,
    )


def _get_coordinator(hass: HomeAssistant, address: str | None):
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]
    if address:
        wanted = address.strip().upper()
        for entry in entries:
            if entry.data.get(CONF_ADDRESS) == wanted:
                return entry.runtime_data
        raise ServiceValidationError(f"没有找到地址为 {wanted} 的 CH592 设备")

    if len(entries) == 1:
        return entries[0].runtime_data
    if not entries:
        raise ServiceValidationError("请先在集成页面添加 CH592 设备")
    raise ServiceValidationError("配置了多个设备，请通过 address 字段指定目标设备")
