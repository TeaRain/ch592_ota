from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_ch592_remote_test_pkg"


class BleakError(Exception):
    pass


class PlainBleakClient:
    def __init__(self, *args, **kwargs):
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
    "aioesphomeapi",
    BluetoothProxyFeature=types.SimpleNamespace(CACHE_CLEARING=1 << 7),
)
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
    async_scanner_devices_by_address=lambda *args, **kwargs: [],
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


class FakeEsphomeBackend:
    def __init__(self, current_services, cached_services):
        self.current_services = current_services
        self.cached_services = cached_services
        self._cache = object()
        self.clear_calls = 0
        self.clear_gate = None
        self.clear_result = True

    async def clear_cache(self):
        self.clear_calls += 1
        self.cached_services = None
        if self.clear_gate is not None:
            await self.clear_gate.wait()
        return self.clear_result


class FakeEsphomeCache:
    """Model the per-proxy host cache held by ESPHomeClientData."""

    def __init__(self, backend):
        self.backend = backend
        self.clear_calls = 0
        backend._cache = self

    def clear_gatt_services_cache(self, address):
        self.clear_calls += 1
        self.backend.cached_services = None

    def clear_gatt_mtu_cache(self, address):
        pass


class FakeProxyApi:
    def __init__(self):
        self.calls = []
        self.error = None
        self.gate = None
        self.success = True

    async def bluetooth_device_clear_cache(self, address, timeout=30.0):
        self.calls.append((address, timeout))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(success=self.success)


def _esphome_path(backend, source, feature_flags=1 << 7):
    cache = FakeEsphomeCache(backend)
    api = FakeProxyApi()
    device_info = types.SimpleNamespace(
        bluetooth_proxy_feature_flags_compat=lambda api_version: feature_flags
    )
    client_data = types.SimpleNamespace(
        bluetooth_device=types.SimpleNamespace(cache=cache),
        client=api,
        device_info=device_info,
        api_version=object(),
    )
    connector = types.SimpleNamespace(
        client=partial(object, client_data=client_data),
        source=source,
        can_connect=lambda: True,
    )
    scanner = types.SimpleNamespace(connector=connector)
    path = types.SimpleNamespace(
        scanner=scanner, ble_device=object(), advertisement=object()
    )
    return path, cache, api


class FakeHaWrapperClient(FakeClient):
    """Model HA's deferred backend selection and public clear_cache wrapper."""

    def __init__(self, backend, expected_handle=None):
        super().__init__()
        self.backend = backend
        self.expected_handle = expected_handle
        self._backend = None
        self.is_connected = False
        self.preconnect_clear_calls = 0
        self.disconnect_error = None

    async def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        self._backend = self.backend
        self.is_connected = True
        # ESPHome REMOTE_CACHING uses the host cache even when
        # dangerous_use_bleak_cache=False. A subsequent connection after
        # clear_cache uses CONNECT_V3_WITHOUT_CACHE and discovers fresh handles.
        if self.backend.cached_services is not None:
            self.services = self.backend.cached_services
        else:
            self.services = self.backend.current_services
            self.backend.cached_services = self.services

    async def clear_cache(self):
        if self._backend is None:
            # HA's disconnected wrapper delegates to the BlueZ cache clearer;
            # it cannot clear an ESPHome backend that has not been selected.
            self.preconnect_clear_calls += 1
            return True
        return await self._backend.clear_cache()

    async def disconnect(self):
        if self.disconnect_error is not None:
            raise self.disconnect_error
        await super().disconnect()
        self._backend = None

    async def write_gatt_char(self, char, payload, response=False):
        if self.expected_handle is not None and char.handle != self.expected_handle:
            raise BleakError(
                "Bluetooth GATT Error handle=52 error=1 "
                "description=Invalid handle"
            )
        await super().write_gatt_char(char, payload, response)


class RemoteOtaBleTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_bluetooth_import_and_wake_do_not_require_esphome(self):
        package_name = f"{PACKAGE}_without_esphome"
        local_package = _module(package_name)
        local_package.__path__ = [str(REPO / "custom_components/ch592_ota")]
        _load(f"{package_name}.const", REPO / "custom_components/ch592_ota/const.py")
        fake = FakeClient()

        with patch.dict(sys.modules, {"aioesphomeapi": None}):
            try:
                local_ble = _load(
                    f"{package_name}.ble_ota",
                    REPO / "custom_components/ch592_ota/ble_ota.py",
                )
            except ModuleNotFoundError as err:
                self.fail(f"local Bluetooth import required ESPHome: {err}")
            with (
                patch.object(
                    local_ble, "async_ble_device_from_address", return_value=object()
                ),
                patch.object(local_ble, "BleakClient", return_value=fake),
            ):
                await local_ble.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(fake.writes[0][1], bytes.fromhex("8800A55A4F54"))

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

    async def test_wake_reconnects_after_clearing_stale_ota_service_cache(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(normal_services, ota_services)
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(ble_ota.asyncio, "sleep", AsyncMock()),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 2)
        self.assertEqual(backend.clear_calls, 1)
        self.assertEqual(fake.preconnect_clear_calls, 0)
        self.assertEqual(fake.writes[0][0].handle, 16)

    async def test_wake_fallback_rejects_failed_remote_clear(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(normal_services, ota_services)
        backend.clear_result = False
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
        ):
            with self.assertRaisesRegex(ble_ota.OtaError, "缓存"):
                await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 1)
        self.assertEqual(fake.writes, [])

    async def test_wake_preclears_all_esphome_proxies_and_connects_once(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        selected = FakeEsphomeBackend(normal_services, ota_services)
        other = FakeEsphomeBackend(normal_services, ota_services)
        selected_path, selected_cache, selected_api = _esphome_path(
            selected, "proxy-a"
        )
        other_path, other_cache, other_api = _esphome_path(other, "proxy-b")
        fake = FakeHaWrapperClient(selected, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[selected_path, other_path],
                create=True,
            ),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 1)
        self.assertEqual(selected.clear_calls, 0)
        self.assertEqual(selected_cache.clear_calls, 1)
        self.assertEqual(other_cache.clear_calls, 1)
        self.assertEqual(len(selected_api.calls), 1)
        self.assertEqual(len(other_api.calls), 1)
        self.assertEqual(fake.writes[0][0].handle, 16)

    async def test_wake_proxy_clear_failure_prevents_connection(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        backend = FakeEsphomeBackend(normal_services, normal_services)
        path, _cache, api = _esphome_path(backend, "proxy-a")
        api.error = BleakError("rpc unavailable")
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[path],
                create=True,
            ),
        ):
            with self.assertRaisesRegex(ble_ota.OtaError, "proxy-a"):
                await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(fake.connect_calls, [])
        self.assertEqual(fake.writes, [])

    async def test_wake_preconnect_proxy_clear_timeout_is_bounded(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        backend = FakeEsphomeBackend(normal_services, normal_services)
        path, _cache, api = _esphome_path(backend, "proxy-a")
        api.gate = asyncio.Event()
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[path],
                create=True,
            ),
        ):
            with self.assertRaisesRegex(ble_ota.OtaError, "超时"):
                await asyncio.wait_for(
                    ble_ota.async_request_ota(
                        object(), "AA:BB:CC:DD:EE:FF", timeout=0.02
                    ),
                    timeout=0.1,
                )

        self.assertEqual(fake.connect_calls, [])

    async def test_wake_proxy_without_remote_clear_still_preclears_host(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(normal_services, ota_services)
        path, cache, api = _esphome_path(backend, "proxy-a", feature_flags=0)
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[path],
                create=True,
            ),
        ):
            await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 1)
        self.assertEqual(cache.clear_calls, 1)
        self.assertEqual(api.calls, [])
        self.assertEqual(fake.writes[0][0].handle, 16)

    async def test_wake_attempt_is_capped_to_single_broadcast_cycle(self):
        fake = FakeClient()

        async def blocking_connect(**kwargs):
            fake.connect_calls.append(kwargs)
            await asyncio.Event().wait()

        fake.connect = blocking_connect
        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
            patch.object(ble_ota, "REMOTE_OTA_CONNECT_TIMEOUT", 0.02),
        ):
            with self.assertRaisesRegex(ble_ota.OtaError, "连接超时"):
                await ble_ota.async_request_ota(
                    object(), "AA:BB:CC:DD:EE:FF", timeout=1.0
                )

        self.assertLessEqual(fake.connect_calls[0]["timeout"], 0.02)

    async def test_wake_reconnect_requires_confirmed_disconnect(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(normal_services, ota_services)
        fake = FakeHaWrapperClient(backend, expected_handle=16)
        fake.disconnect_error = BleakError("disconnect failed")

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
        ):
            with self.assertRaises(ble_ota.OtaError):
                await ble_ota.async_request_ota(object(), "AA:BB:CC:DD:EE:FF")

        self.assertEqual(len(fake.connect_calls), 1)
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.is_connected)

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

    async def test_wake_cache_clear_is_bounded_by_connection_deadline(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(normal_services, ota_services)
        backend.clear_gate = asyncio.Event()
        fake = FakeHaWrapperClient(backend, expected_handle=16)

        with (
            patch.object(ble_ota, "async_ble_device_from_address", return_value=object()),
            patch.object(ble_ota, "BleakClient", return_value=fake),
        ):
            with self.assertRaises(ble_ota.OtaError):
                await asyncio.wait_for(
                    ble_ota.async_request_ota(
                        object(), "AA:BB:CC:DD:EE:FF", timeout=0.02
                    ),
                    timeout=0.1,
                )

        self.assertEqual(backend.clear_calls, 1)

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

    async def test_fresh_config_preclears_proxy_and_connects_once(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        path, cache, api = _esphome_path(backend, "proxy-a")
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        connect_calls = []

        async def connect(client_class, *args, **kwargs):
            connect_calls.append(kwargs)
            await fake.connect(**kwargs)
            return fake

        with (
            patch.object(ble_ota, "establish_connection", connect),
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[path],
                create=True,
            ),
        ):
            await ble_ota.async_set_threshold(
                object(), "AA:BB:CC:DD:EE:FF", 165, fresh_services=True
            )

        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(backend.clear_calls, 0)
        self.assertEqual(cache.clear_calls, 1)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(fake.writes[0][0].handle, 52)

    async def test_fresh_config_fallback_rejects_failed_remote_clear(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        backend.clear_result = False
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        connect_calls = []

        async def connect(client_class, *args, **kwargs):
            connect_calls.append(kwargs)
            await fake.connect(**kwargs)
            return fake

        with patch.object(ble_ota, "establish_connection", connect):
            with self.assertRaisesRegex(ble_ota.OtaError, "缓存"):
                await ble_ota.async_set_threshold(
                    object(), "AA:BB:CC:DD:EE:FF", 165, fresh_services=True
                )

        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(fake.writes, [])

    async def test_fresh_upload_preclears_proxy_and_connects_once(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        path, cache, api = _esphome_path(backend, "proxy-a")
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        connect_calls = []

        async def connect(client_class, *args, **kwargs):
            self.assertIs(kwargs["use_services_cache"], False)
            connect_calls.append(kwargs)
            await fake.connect(**kwargs)
            return fake

        client = ble_ota.Ch592OtaClient(
            object(),
            "AA:BB:CC:DD:EE:FF",
            b"\x6f\x00\x00\x00",
            lambda *args: None,
            fresh_services=True,
        )
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
            patch.object(
                ble_ota,
                "async_scanner_devices_by_address",
                return_value=[path],
                create=True,
            ),
        ):
            await client.async_run()

        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(backend.clear_calls, 0)
        self.assertEqual(cache.clear_calls, 1)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(fake.services[0].characteristics[0].handle, 52)

    async def test_fresh_upload_reconnects_after_clearing_normal_service_cache(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        connect_calls = []

        async def connect(client_class, *args, **kwargs):
            self.assertIs(kwargs["use_services_cache"], False)
            connect_calls.append(kwargs)
            await fake.connect(**kwargs)
            return fake

        with patch.object(ble_ota, "establish_connection", connect):
            client = await ble_ota._async_establish_ota_connection(
                object(), "AA:BB:CC:DD:EE:FF", fresh_services=True
            )

        self.assertEqual(len(connect_calls), 2)
        self.assertEqual(backend.clear_calls, 1)
        self.assertEqual(fake.preconnect_clear_calls, 0)
        self.assertEqual(client.services[0].characteristics[0].handle, 52)

    async def test_fresh_reconnect_requires_confirmed_disconnect(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        fake.disconnect_error = BleakError("disconnect failed")
        connect_calls = []

        async def connect(client_class, *args, **kwargs):
            connect_calls.append(kwargs)
            await fake.connect(**kwargs)
            return fake

        with patch.object(ble_ota, "establish_connection", connect):
            with self.assertRaises(ble_ota.OtaError):
                await ble_ota._async_establish_ota_connection(
                    object(), "AA:BB:CC:DD:EE:FF", fresh_services=True
                )

        self.assertEqual(len(connect_calls), 1)
        self.assertTrue(fake.is_connected)

    async def test_fresh_cancel_exposes_inflight_client_to_outer_cleanup(self):
        normal_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=16)
        ota_char = types.SimpleNamespace(uuid=const.OTA_CHAR_UUID, handle=52)
        normal_services = [types.SimpleNamespace(characteristics=[normal_char])]
        ota_services = [types.SimpleNamespace(characteristics=[ota_char])]
        backend = FakeEsphomeBackend(ota_services, normal_services)
        fake = FakeHaWrapperClient(backend, expected_handle=52)
        fake.disconnect_gate = asyncio.Event()
        disconnect_started = asyncio.Event()
        original_disconnect = fake.disconnect

        async def blocking_disconnect():
            disconnect_started.set()
            await original_disconnect()

        fake.disconnect = blocking_disconnect

        async def connect(client_class, *args, **kwargs):
            await fake.connect(**kwargs)
            return fake

        client = ble_ota.Ch592OtaClient(
            object(),
            "AA:BB:CC:DD:EE:FF",
            b"\x6f\x00\x00\x00",
            lambda *args: None,
            fresh_services=True,
        )
        with patch.object(ble_ota, "establish_connection", connect):
            task = asyncio.create_task(client.async_run())
            await asyncio.wait_for(disconnect_started.wait(), timeout=0.1)
            task.cancel()
            fake.disconnect_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIs(client._client, fake)
        self.assertFalse(fake.is_connected)

    async def test_mode_switch_requires_fresh_gatt_discovery(self):
        classes = []
        timeouts = []
        await self._run_upload(False, classes, timeouts)
        await self._run_upload(True, classes, timeouts)
        self.assertEqual(classes, [CachedBleakClient, PlainBleakClient])
        self.assertEqual(timeouts, [30.0, 30.0])


if __name__ == "__main__":
    unittest.main()
