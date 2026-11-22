from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_ch592_test_pkg"


class HomeAssistantError(Exception):
    pass


class ConfigEntry:
    pass


class DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


class FakeStore:
    saved = {}

    def __init__(self, hass, version, key):
        self.key = (version, key)

    async def async_load(self):
        return self.saved.get(self.key)

    async def async_save(self, data):
        self.saved[self.key] = data


class FakeEntityRegistry:
    def __init__(self):
        self.hidden = {}
        self.legacy = {}
        self.removed = []

    def async_update_entity(self, entity_id, *, hidden_by):
        self.hidden[entity_id] = hidden_by

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.legacy.get((domain, platform, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


_entity_registry = FakeEntityRegistry()


def callback(function):
    return function


class FakeHass:
    def __init__(self):
        self.config = types.SimpleNamespace(config_dir=str(REPO))

    def async_create_task(self, coro, name=None):
        return asyncio.create_task(coro, name=name)

    async def async_add_executor_job(self, function, *args):
        return function(*args)


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_dependencies():
    _module("homeassistant")
    _module("homeassistant.components")
    _module(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorStateClass=types.SimpleNamespace(
            MEASUREMENT="measurement", TOTAL="total", TOTAL_INCREASING="total_increasing"
        ),
        SensorDeviceClass=types.SimpleNamespace(GAS="gas"),
    )
    _module("homeassistant.components.button", ButtonEntity=type("ButtonEntity", (), {}))
    _module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=type("BinarySensorEntity", (), {}),
    )
    _module(
        "homeassistant.components.update",
        UpdateEntity=type("UpdateEntity", (), {}),
        UpdateDeviceClass=types.SimpleNamespace(FIRMWARE="firmware"),
        UpdateEntityFeature=types.SimpleNamespace(INSTALL=1, PROGRESS=2),
    )
    _module("homeassistant.components.number", NumberEntity=type("NumberEntity", (), {}), NumberMode=types.SimpleNamespace(BOX="box"))
    _module(
        "homeassistant.components.bluetooth",
        BluetoothScanningMode=types.SimpleNamespace(ACTIVE="active"),
        BluetoothServiceInfoBleak=object,
        async_register_callback=lambda *args, **kwargs: (lambda: None),
    )
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry)
    _module(
        "homeassistant.const",
        PERCENTAGE="%",
        UnitOfVolume=types.SimpleNamespace(CUBIC_METERS="m³"),
    )
    _module(
        "homeassistant.core",
        HomeAssistant=FakeHass,
        callback=callback,
    )
    _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.storage", Store=FakeStore)
    _module(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: _entity_registry,
        RegistryEntryHider=types.SimpleNamespace(INTEGRATION="integration"),
    )
    _module("homeassistant.helpers.entity", EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config"))
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("homeassistant.helpers.device_registry", CONNECTION_BLUETOOTH="bluetooth", DeviceInfo=dict)

    package = _module(PACKAGE)
    package.__path__ = [str(REPO / "custom_components/ch592_ota")]
    _load(f"{PACKAGE}.const", REPO / "custom_components/ch592_ota/const.py")
    _load(f"{PACKAGE}.gas_accounting", REPO / "custom_components/ch592_ota/gas_accounting.py")
    return package


_install_dependencies()

_operation_state = {"active": 0, "maximum": 0}


async def _tracked_config_write(*args, **kwargs):
    _operation_state["active"] += 1
    _operation_state["maximum"] = max(
        _operation_state["maximum"], _operation_state["active"]
    )
    try:
        await asyncio.sleep(0.03)
    finally:
        _operation_state["active"] -= 1


class OtaError(Exception):
    pass


class OtaMtuError(OtaError):
    pass


class Ch592OtaClient:
    def __init__(self, *args, **kwargs):
        pass

    async def async_run(self, chunk):
        return None


_ble_module = _module(
    f"{PACKAGE}.ble_ota",
    Ch592OtaClient=Ch592OtaClient,
    OtaError=OtaError,
    OtaMtuError=OtaMtuError,
    async_set_gas=_tracked_config_write,
    async_set_threshold=_tracked_config_write,
)
_module(
    f"{PACKAGE}.bthome",
    has_bthome_data=lambda info: any(
        uuid.lower() in ("0000fcd2-0000-1000-8000-00805f9b34fb", "fcd2")
        for uuid in info.service_data
    ),
    parse_bthome_fields=lambda info: {},
)


async def _download_valid_image(*args, **kwargs):
    return b"\x6f\x00\x00\x00"


_firmware_module = _module(
    f"{PACKAGE}.firmware",
    FirmwareCloudCoordinator=DataUpdateCoordinator,
    async_download_firmware=_download_valid_image,
    is_newer=lambda latest, installed: latest != installed,
    manifest_version=lambda manifest: manifest.get("version"),
)

coordinator_module = _load(
    f"{PACKAGE}.coordinator", REPO / "custom_components/ch592_ota/coordinator.py"
)
Ch592OtaCoordinator = coordinator_module.Ch592OtaCoordinator


class CoordinatorReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = FakeHass()
        self.entry = types.SimpleNamespace(
            data={"address": "AA:BB:CC:DD:EE:FF"}, options={}, entry_id="test-entry"
        )
        self.coordinator = Ch592OtaCoordinator(self.hass, self.entry)
        _entity_registry.hidden.clear()
        _entity_registry.legacy.clear()
        _entity_registry.removed.clear()
        _operation_state.update(active=0, maximum=0)

    def tearDown(self):
        for task in (
            self.coordinator._task,
            self.coordinator._auto_task,
            self.coordinator._gas_task,
        ):
            if task is not None and not task.done():
                task.cancel()

    async def test_start_ota_rejects_active_config_task(self):
        self.coordinator._gas_task = asyncio.create_task(asyncio.sleep(30))
        with self.assertRaises(HomeAssistantError):
            await self.coordinator.async_start_ota(
                image=b"\x6f\x00\x00\x00", version="1.4.4"
            )

    async def test_config_writes_do_not_overlap(self):
        await asyncio.gather(
            self.coordinator._async_write_threshold(100),
            self.coordinator._async_write_threshold(200),
        )
        self.assertEqual(_operation_state["maximum"], 1)

    async def test_abort_cancels_every_background_task(self):
        self.coordinator._task = asyncio.create_task(asyncio.sleep(30))
        self.coordinator._auto_task = asyncio.create_task(asyncio.sleep(30))
        self.coordinator._gas_task = asyncio.create_task(asyncio.sleep(30))
        tasks = [
            self.coordinator._task,
            self.coordinator._auto_task,
            self.coordinator._gas_task,
        ]
        await self.coordinator.async_abort()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertTrue(all(task.cancelled() for task in tasks))

    async def test_auto_validation_failure_replaces_stale_success(self):
        self.coordinator.data.update(status="success", progress=0, error=None)

        async def invalid_download(*args, **kwargs):
            return b"bad"

        original_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await original_sleep(0)

        with (
            patch.object(_firmware_module, "async_download_firmware", invalid_download),
            patch.object(coordinator_module, "async_download_firmware", invalid_download),
            patch.object(coordinator_module.asyncio, "sleep", fast_sleep),
        ):
            with self.assertLogs(coordinator_module._LOGGER, level="ERROR"):
                await self.coordinator._async_auto_install(
                    {"version": "1.4.4"}, "1.4.4"
                )
        self.assertEqual(self.coordinator.data["status"], "failed")
        self.assertIsNotNone(self.coordinator.data["error"])

    async def test_reboot_baseline_is_sampled_immediately_before_end(self):
        self.coordinator.data.update(firmware_version="1.4.4", ads=7)
        self.coordinator._on_progress(97, "rebooting", 0, 0)
        start_seq = self.coordinator._reboot_start_seq
        self.assertEqual(start_seq, 7)
        wait = asyncio.create_task(
            self.coordinator._async_wait_for_reboot(start_seq, "1.4.4")
        )
        try:
            await asyncio.sleep(0.01)
            self.assertFalse(wait.done())
        finally:
            wait.cancel()
            await asyncio.gather(wait, return_exceptions=True)

    async def test_path_ota_start_is_reserved_before_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"\x6f\x00\x00\x00")
            self.hass.config.config_dir = directory
            started = asyncio.Event()
            gate = asyncio.Event()

            async def blocked_read(function, path):
                started.set()
                await gate.wait()
                return function(path)

            self.hass.async_add_executor_job = blocked_read
            first = asyncio.create_task(
                self.coordinator.async_start_ota(firmware="firmware.bin")
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                with self.assertRaises(HomeAssistantError):
                    await asyncio.wait_for(
                        self.coordinator.async_start_ota(firmware="firmware.bin"),
                        timeout=0.05,
                    )
            finally:
                await self.coordinator.async_abort(quiet=True)
                gate.set()
                await asyncio.gather(first, return_exceptions=True)

    async def test_abort_during_file_read_prevents_delayed_start(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.bin"
            firmware.write_bytes(b"\x6f\x00\x00\x00")
            self.hass.config.config_dir = directory
            started = asyncio.Event()
            gate = asyncio.Event()

            async def blocked_read(function, path):
                started.set()
                await gate.wait()
                return function(path)

            self.hass.async_add_executor_job = blocked_read
            start = asyncio.create_task(
                self.coordinator.async_start_ota(firmware="firmware.bin")
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                await self.coordinator.async_abort()
                gate.set()
                with self.assertRaises(asyncio.CancelledError):
                    await start
                self.assertIsNone(self.coordinator._task)
            finally:
                gate.set()
                if not start.done():
                    start.cancel()
                await asyncio.gather(start, return_exceptions=True)

    async def test_stop_cancels_foreground_config_write(self):
        self.coordinator.data["ota_mode"] = True
        write = asyncio.create_task(self.coordinator.async_set_threshold(100))
        try:
            await asyncio.sleep(0)
            await self.coordinator.async_stop()
            self.assertTrue(write.done())
            self.assertTrue(write.cancelled())
        finally:
            if not write.done():
                write.cancel()
            await asyncio.gather(write, return_exceptions=True)

    async def test_calibration_uses_raw_without_ble_and_drives_totals(self):
        self.coordinator.data["gas_value"] = 1000
        result = await self.coordinator.async_calibrate_gas(900)
        self.assertEqual(result["result"], "calibrated")
        self.assertEqual(self.coordinator.gas.offset, -100)
        self.assertEqual(self.coordinator.data["gas_total"], 900)
        self.assertEqual(self.coordinator.data["gas_consumption_total"], 0)
        self.coordinator._update_raw_gas(1010)
        self.assertEqual(self.coordinator.data["gas_total"], 910)
        self.assertEqual(self.coordinator.data["gas_consumption_total"], 10)

    async def test_calibration_state_is_restored_on_start(self):
        self.coordinator.data["gas_value"] = 1000
        await self.coordinator.async_calibrate_gas(5000)
        self.coordinator._update_raw_gas(1010)
        await self.coordinator.async_stop()
        restored = Ch592OtaCoordinator(self.hass, self.entry)
        await restored.async_start()
        self.assertEqual(restored.data["gas_total"], 5010)
        self.assertEqual(restored.data["gas_consumption_total"], 10)
        await restored.async_stop()

    async def test_gas_entities_expose_three_roles(self):
        self.coordinator.data["gas_value"] = 1000
        await self.coordinator.async_calibrate_gas(900)
        self.coordinator._update_raw_gas(1010)
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.sensor", REPO / "custom_components/ch592_ota/sensor.py")
        raw = module.Ch592RawGasCounterSensor(self.coordinator, self.entry)
        total = module.Ch592GasTotalSensor(self.coordinator, self.entry)
        consumption = module.Ch592GasConsumptionSensor(self.coordinator, self.entry)
        self.assertEqual(raw.native_value, 1.01)
        self.assertEqual(total.native_value, 0.91)
        self.assertEqual(consumption.native_value, 0.01)
        self.assertEqual(total.extra_state_attributes["offset_m3"], -0.1)

    async def test_threshold_advertisement_drives_chinese_current_sensor(self):
        info = types.SimpleNamespace(
            name="GasMeter OTA",
            service_uuids=[],
            service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": b"payload"},
            raw=bytes.fromhex("0201061616d2fc40"),
        )
        with patch.object(
            coordinator_module,
            "parse_bthome_fields",
            return_value={"version": "1.4.9", "gas": 60, "threshold": 140},
        ):
            self.coordinator._async_bt_callback(info, None)
        self.assertEqual(self.coordinator.data["threshold"], 140)
        self.assertEqual(self.coordinator.data["firmware_version"], "1.4.9")

        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.sensor", REPO / "custom_components/ch592_ota/sensor.py")
        sensor = module.Ch592CurrentThresholdSensor(self.coordinator, self.entry)
        self.assertEqual(sensor.native_value, 140)
        self.assertEqual(sensor._attr_name, "当前采样阈值（P/N按键调整）")
        self.assertEqual(sensor._attr_entity_category, "diagnostic")
        self.assertTrue(sensor._attr_unique_id.endswith("_current_threshold"))

    async def test_ota_info_exposes_actual_device_features(self):
        self.coordinator._on_ota_info({"features": 0x0D})
        self.assertEqual(self.coordinator.data["features"], 0x0D)

    async def test_device_page_entity_labels_are_chinese(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        sensor = _load(f"{PACKAGE}.sensor", REPO / "custom_components/ch592_ota/sensor.py")
        button = _load(f"{PACKAGE}.button", REPO / "custom_components/ch592_ota/button.py")
        number = _load(f"{PACKAGE}.number", REPO / "custom_components/ch592_ota/number.py")
        binary = _load(f"{PACKAGE}.binary_sensor", REPO / "custom_components/ch592_ota/binary_sensor.py")
        update = _load(f"{PACKAGE}.update", REPO / "custom_components/ch592_ota/update.py")
        expected = [
            (sensor.Ch592FirmwareVersionSensor, "固件版本"),
            (sensor.Ch592OtaStatusSensor, "升级状态"),
            (sensor.Ch592OtaProgressSensor, "升级进度"),
            (sensor.Ch592RawGasCounterSensor, "设备原始计数"),
            (sensor.Ch592GasTotalSensor, "燃气表读数"),
            (sensor.Ch592GasConsumptionSensor, "累计用气量（统计）"),
            (sensor.Ch592CurrentThresholdSensor, "当前采样阈值（P/N按键调整）"),
            (binary.Ch592OtaModeSensor, "OTA模式"),
            (update.Ch592FirmwareUpdateEntity, "固件更新"),
            (button.Ch592FirmwareCheckButton, "检查固件更新"),
            (button.Ch592OtaAbortButton, "取消升级"),
            (button.Ch592CancelPendingButton, "取消待写入"),
            (number.Ch592GasTargetNumber, "机械表实际读数（输入后自动校准）"),
            (number.Ch592ThresholdNumber, "采样阈值（待写入，按B生效）"),
        ]
        for entity_class, label in expected:
            with self.subTest(entity=entity_class.__name__):
                self.assertEqual(entity_class._attr_name, label)

    async def test_saving_calibration_number_calibrates_immediately(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.number", REPO / "custom_components/ch592_ota/number.py")
        self.coordinator.data["gas_value"] = 1000
        await self.coordinator.async_calibrate_gas(5000)
        self.coordinator._update_raw_gas(1010)
        number = module.Ch592GasTargetNumber(self.coordinator)

        await number.async_set_native_value(0.900)

        self.assertEqual(self.coordinator.data["gas_value"], 1010)
        self.assertEqual(self.coordinator.data["gas_total"], 900)
        self.assertEqual(self.coordinator.gas.offset, -110)
        self.assertEqual(self.coordinator.data["gas_consumption_total"], 10)
        self.assertIsNone(self.coordinator.data["gas_pending"])

    async def test_legacy_write_buttons_are_removed_from_registry(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.button", REPO / "custom_components/ch592_ota/button.py")
        address = self.coordinator.address
        gas_id = "button.ch592_legacy_calibrate"
        threshold_id = "button.ch592_legacy_threshold"
        _entity_registry.legacy.update(
            {
                ("button", "ch592_ota", f"{address}_write_gas"): gas_id,
                ("button", "ch592_ota", f"{address}_write_threshold"): threshold_id,
            }
        )
        self.entry.runtime_data = self.coordinator
        added = []

        await module.async_setup_entry(self.hass, self.entry, added.extend)

        self.assertEqual(set(_entity_registry.removed), {gas_id, threshold_id})
        self.assertEqual(len(added), 3)
        self.assertFalse(
            any(
                entity._attr_unique_id.endswith(("_write_gas", "_write_threshold"))
                for entity in added
            )
        )

    async def test_firmware_check_is_a_control_not_a_diagnostic(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.button", REPO / "custom_components/ch592_ota/button.py")
        self.assertIsNone(
            getattr(module.Ch592FirmwareCheckButton, "_attr_entity_category", None)
        )

    async def test_saving_threshold_number_writes_immediately_in_ota_mode(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.number", REPO / "custom_components/ch592_ota/number.py")
        self.coordinator.data["ota_mode"] = True
        number = module.Ch592ThresholdNumber(self.coordinator)

        await number.async_set_native_value(140)

        self.assertEqual(self.coordinator.data["threshold"], 140)
        self.assertEqual(self.coordinator.data["threshold_result"], "已写入阈值 140")
        self.assertIsNone(self.coordinator.data["threshold_pending"])

    async def test_saving_threshold_number_arms_write_outside_ota_mode(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.number", REPO / "custom_components/ch592_ota/number.py")
        self.coordinator.data["ota_mode"] = False
        number = module.Ch592ThresholdNumber(self.coordinator)

        await number.async_set_native_value(160)

        self.assertEqual(self.coordinator.data["threshold_target"], 160)
        self.assertEqual(self.coordinator.data["threshold_pending"], 160)

    async def test_legacy_set_gas_calibrates_instead_of_arming_ota(self):
        self.coordinator.data.update(gas_value=1000, ota_mode=False)
        result = await self.coordinator.async_set_gas(900)
        self.assertEqual(result["result"], "calibrated")
        self.assertIsNone(self.coordinator.data["gas_pending"])

    async def test_same_version_reboot_requires_new_advertisement(self):
        self.coordinator.data.update(firmware_version="1.4.4", ads=5)
        wait = asyncio.create_task(
            self.coordinator._async_wait_for_reboot(5, "1.4.4")
        )
        await asyncio.sleep(0.01)
        self.assertFalse(wait.done())
        self.coordinator.data["ads"] = 6
        self.assertTrue(await asyncio.wait_for(wait, timeout=1))

    def test_ota_raw_packet_wins_over_cached_bthome_service_data(self):
        info = types.SimpleNamespace(
            service_uuids=[coordinator_module.OTA_SERVICE_UUID],
            service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": b"stale"},
            raw=bytes.fromhex("0201060503e0ffe0fe"),
        )
        self.assertTrue(Ch592OtaCoordinator._is_ota_advertisement(info))

    def test_bthome_raw_packet_wins_over_cached_ota_service_uuid(self):
        info = types.SimpleNamespace(
            service_uuids=[coordinator_module.OTA_SERVICE_UUID],
            service_data={},
            raw=bytes.fromhex(
                "0201061616d2fc400c6c0b3d00004c3c0000005305312e342e36"
            ),
        )
        self.assertFalse(Ch592OtaCoordinator._is_ota_advertisement(info))


if __name__ == "__main__":
    unittest.main()
