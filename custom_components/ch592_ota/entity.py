"""Shared helpers for CH592 OTA entities."""

from __future__ import annotations

from homeassistant.const import CONNECTION_BLUETOOTH
from homeassistant.helpers.device_registry import DeviceInfo

from .coordinator import Ch592OtaCoordinator


def build_device_info(coordinator: Ch592OtaCoordinator) -> DeviceInfo:
    """Return device info that merges with the BTHome device.

    The BTHome integration registers this peripheral with its Bluetooth
    connection, so using the same connection here puts the OTA entities on the
    same device page as the gas/battery/signal sensors.
    """
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        name=coordinator.entry.title,
        manufacturer="WCH",
        model="CH592 BLE Gas Meter",
    )
