from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_ch592_remote_test_pkg"


class BleakError(Exception):
    pass


class PlainBleakClient:
    pass


class CachedBleakClient:
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


_module("bleak", BleakClient=PlainBleakClient)
_module("bleak.exc", BleakError=BleakError)
_module(
    "bleak_retry_connector",
    BleakClientWithServiceCache=CachedBleakClient,
    establish_connection=None,
)
_module("homeassistant")
_module("homeassistant.components")
_module(
    "homeassistant.components.bluetooth",
    async_ble_device_from_address=lambda *args, **kwargs: object(),
)
_module("homeassistant.core", HomeAssistant=object, callback=lambda function: function)
package = _module(PACKAGE)
package.__path__ = [str(REPO / "custom_components/ch592_ota")]
const = _load(f"{PACKAGE}.const", REPO / "custom_components/ch592_ota/const.py")
ble_ota = _load(f"{PACKAGE}.ble_ota", REPO / "custom_components/ch592_ota/ble_ota.py")


class FakeClient:
    mtu_size = 247
    is_connected = True

    def __init__(self, status=b"\x00\x00"):
        self.char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID)
        self.services = [types.SimpleNamespace(characteristics=[self.char])]
        self.status = status
        self.writes = []
        self.disconnected = False
        self.disconnect_gate = None
        self.connect_calls = []
        self.connect_results = []

    async def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self.connect_results:
            result = self.connect_results.pop(0)
            if result is not None:
                raise result
        self.is_connected = True

    async def write_gatt_char(self, char, payload, response=False):
        self.writes.append((char, payload, response))

    async def read_gatt_char(self, char):
        return self.status

    async def disconnect(self):
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        self.disconnected = True
        self.is_connected = False


class RemoteOtaBleTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_uses_plain_client_exact_frame_and_status(self):
        fake = FakeClient()
        classes = []
        plain_class = ble_ota.BleakClient

        async def connect(client_class, *args, **kwargs):
            classes.append(plain_class)
            return fake

        def plain_client(*args, **kwargs):
            classes.append(plain_class)
            return fake

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(ble_ota, "BleakClient", side_effect=plain_client),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(classes, [PlainBleakClient])
        self.assertEqual(
            fake.writes,
            [(fake.char, bytes.fromhex("8800A55A4F54"), True)],
        )
        self.assertTrue(fake.disconnected)

    async def test_wake_connection_covers_full_normal_broadcast_period(self):
        fake = FakeClient()

        async def connect(*args, **kwargs):
            return fake

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(ble_ota, "BleakClient", return_value=fake),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 1)
        self.assertAlmostEqual(fake.connect_calls[0]["timeout"], 65.0, delta=0.01)

    async def test_wake_retries_immediate_failure_within_same_window(self):
        fake = FakeClient()
        fake.connect_results = [BleakError("window closed"), None]

        async def connect(*args, **kwargs):
            return fake

        async def no_sleep(_delay):
            return None

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(ble_ota.asyncio, "sleep", side_effect=no_sleep),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 2)
        self.assertAlmostEqual(fake.connect_calls[0]["timeout"], 65.0, delta=0.01)
        self.assertLessEqual(fake.connect_calls[1]["timeout"], 65.0)
        self.assertGreater(fake.connect_calls[1]["timeout"], 0)

    async def test_wake_rejects_error_status(self):
        fake = FakeClient(status=b"\xFE\x00")

        async def connect(*args, **kwargs):
            return fake

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(ble_ota, "BleakClient", return_value=fake),
        ):
            with self.assertRaises(ble_ota.OtaError):
                await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")
        self.assertTrue(fake.disconnected)

    async def test_threshold_after_mode_switch_uses_fresh_gatt(self):
        fake = FakeClient()
        classes = []

        async def connect(client_class, *args, **kwargs):
            classes.append(client_class)
            return fake

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
        ):
            await ble_ota.async_set_threshold(
                object(), "AA:BB:CC:DD:EE:FF", 165, fresh_services=True
            )
        self.assertEqual(classes, [PlainBleakClient])

    async def test_wake_disconnect_is_bounded(self):
        fake = FakeClient()
        fake.disconnect_gate = asyncio.Event()

        async def connect(*args, **kwargs):
            return fake

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(ble_ota, "REMOTE_OTA_DISCONNECT_TIMEOUT", 0, create=True),
        ):
            await asyncio.wait_for(
                ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF"),
                timeout=0.1,
            )

    async def _run_upload(self, fresh_services, classes, timeouts):
        client = ble_ota.Ch592OtaClient(
            object(),
            "AA:BB:CC:DD:EE:FF",
            b"\x6f\x00\x00\x00",
            lambda *args: None,
            fresh_services=fresh_services,
        )
        fake = FakeClient()

        async def connect(client_class, *args, **kwargs):
            classes.append(client_class)
            timeouts.append(kwargs["timeout"])
            return fake

        client._find_ota_char = lambda: fake.char
        client._async_info = AsyncMock(
            return_value={
                "features": const.OTA_FEATURE_PAGE_SPLIT,
                "image_size": const.IMAGE_SIZE,
                "block_size": const.FLASH_BLOCK_SIZE,
                "protocol": 8,
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

    async def test_mode_switch_requires_fresh_gatt_discovery(self):
        classes = []
        timeouts = []
        await self._run_upload(False, classes, timeouts)
        await self._run_upload(True, classes, timeouts)
        self.assertEqual(classes, [CachedBleakClient, PlainBleakClient])
        self.assertEqual(timeouts, [30.0, 30.0])


if __name__ == "__main__":
    unittest.main()
