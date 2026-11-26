from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_ch592_ble_test_pkg"


class BleakError(Exception):
    pass


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_module("bleak", BleakClient=object)
_module("bleak.exc", BleakError=BleakError)
_module(
    "bleak_retry_connector",
    BleakClientWithServiceCache=object,
    establish_connection=None,
)
_module("homeassistant")
_module("homeassistant.components")
_module(
    "homeassistant.components.bluetooth",
    async_ble_device_from_address=lambda *args: object(),
    async_scanner_devices_by_address=lambda *args, **kwargs: [],
)
_module("homeassistant.core", HomeAssistant=object)
package = _module(PACKAGE)
package.__path__ = [str(REPO / "custom_components/ch592_ota")]
const = _load(f"{PACKAGE}.const", REPO / "custom_components/ch592_ota/const.py")
ble_ota = _load(f"{PACKAGE}.ble_ota", REPO / "custom_components/ch592_ota/ble_ota.py")


class FakeClient:
    mtu_size = 247
    is_connected = True

    async def disconnect(self):
        self.is_connected = False


class BleOtaReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_protocol(self, protocol, features=None):
        client = ble_ota.Ch592OtaClient(
            object(), "AA:BB:CC:DD:EE:FF", b"\x6f\x00\x00\x00", lambda *args: None
        )
        fake_client = FakeClient()

        async def connect(*args, **kwargs):
            return fake_client

        client._find_ota_char = lambda: object()
        client._async_info = AsyncMock(
            return_value={
                "features": (
                    const.OTA_FEATURE_PAGE_SPLIT | const.OTA_FEATURE_CRC32
                    if features is None
                    else features
                ),
                "image_size": const.IMAGE_SIZE,
                "block_size": const.FLASH_BLOCK_SIZE,
                "protocol": protocol,
                "vdd": 3000,
            }
        )
        client._async_erase = AsyncMock()
        client._async_program = AsyncMock()
        client._async_crc32_verify = AsyncMock()
        client._async_verify = AsyncMock()
        client._async_end = AsyncMock()

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
        ):
            await client.async_run()
        return client

    async def test_legacy_crc_capability_uses_safe_byte_verification(self):
        client = await self._run_with_protocol(7)
        client._async_erase.assert_awaited_once()
        client._async_program.assert_awaited_once()
        client._async_crc32_verify.assert_not_called()
        client._async_verify.assert_awaited_once()
        client._async_end.assert_awaited_once()

    async def test_fast_crc_protocol_uses_single_round_trip_verification(self):
        client = await self._run_with_protocol(8)
        client._async_erase.assert_awaited_once()
        client._async_program.assert_awaited_once()
        client._async_crc32_verify.assert_awaited_once()
        client._async_verify.assert_not_called()
        client._async_end.assert_awaited_once()

    async def test_protocol_8_does_not_require_redundant_crc_capability_bit(self):
        client = await self._run_with_protocol(8, const.OTA_FEATURE_PAGE_SPLIT)
        client._async_crc32_verify.assert_awaited_once()
        client._async_verify.assert_not_called()
        client._async_end.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
