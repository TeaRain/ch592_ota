from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
import zlib
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


async def _unexpected_remote_wake(*args, **kwargs):
    raise AssertionError("remote wake was not expected in this test")


_ble_module = _module(
    f"{PACKAGE}.ble_ota",
    Ch592OtaClient=Ch592OtaClient,
    OtaError=OtaError,
    OtaMtuError=OtaMtuError,
    async_request_ota=_unexpected_remote_wake,
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
        FakeStore.saved.clear()
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

    def test_auto_install_defaults_off_and_explicit_option_is_respected(self):
        self.assertFalse(self.coordinator.auto_install)
        self.entry.options[coordinator_module.CONF_AUTO_INSTALL] = True
        self.assertTrue(self.coordinator.auto_install)

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
        self.coordinator.data["ota_mode"] = True
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
        self.coordinator.data["ota_mode"] = True
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
            (number.Ch592ThresholdNumber, "采样阈值（等待OTA写入）"),
        ]
        for entity_class, label in expected:
            with self.subTest(entity=entity_class.__name__):
                self.assertEqual(entity_class._attr_name, label)

    async def test_ota_status_exposes_remote_wake_diagnostics(self):
        _load(f"{PACKAGE}.entity", REPO / "custom_components/ch592_ota/entity.py")
        module = _load(f"{PACKAGE}.sensor", REPO / "custom_components/ch592_ota/sensor.py")
        self.coordinator.data.update(
            wake_attempts=2, wake_error="proxy disconnected"
        )
        entity = module.Ch592OtaStatusSensor(self.coordinator, self.entry)

        attributes = entity.extra_state_attributes

        self.assertEqual(attributes["wake_attempts"], 2)
        self.assertEqual(attributes["wake_error"], "proxy disconnected")

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

    async def test_threshold_pending_survives_restart_and_cancel(self):
        self.coordinator.data["ota_mode"] = False
        await self.coordinator.async_set_threshold(140)
        await self.coordinator.async_stop()

        restored = Ch592OtaCoordinator(self.hass, self.entry)
        await restored.async_start()
        self.assertEqual(restored.data["threshold_pending"], 140)
        self.assertEqual(restored.data["threshold_target"], 140)
        await restored.async_cancel_threshold()
        await restored.async_stop()

        cleared = Ch592OtaCoordinator(self.hass, self.entry)
        await cleared.async_start()
        self.assertIsNone(cleared.data["threshold_pending"])
        await cleared.async_stop()

    async def test_threshold_pending_does_not_expire_before_timed_ota(self):
        self.coordinator.data["ota_mode"] = False
        await self.coordinator.async_set_threshold(145)
        future = asyncio.get_running_loop().time() + 4 * 86400
        fake_loop = types.SimpleNamespace(time=lambda: future)
        with patch.object(
            coordinator_module.asyncio, "get_running_loop", return_value=fake_loop
        ):
            self.assertEqual(self.coordinator._threshold_pending_value(), 145)

    async def test_pending_threshold_success_continues_to_auto_upgrade(self):
        self.coordinator.data["ota_mode"] = False
        await self.coordinator.async_set_threshold(150)
        self.coordinator.data["ota_mode"] = True
        events = []

        async def write_threshold(value):
            events.append(("write", value))
            self.coordinator.data.update(
                threshold_pending=None,
                threshold=value,
                threshold_result=f"已写入阈值 {value}",
            )

        original_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await original_sleep(0)

        with (
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
            patch.object(
                self.coordinator,
                "_maybe_auto_install",
                side_effect=lambda: events.append(("upgrade", None)),
            ),
            patch.object(coordinator_module.asyncio, "sleep", fast_sleep),
        ):
            await self.coordinator._async_auto_write_pending()

        self.assertEqual(events, [("write", 150), ("upgrade", None)])

    async def test_timed_window_threshold_then_real_auto_upgrade_flow(self):
        self.entry.options[coordinator_module.CONF_AUTO_INSTALL] = True
        self.coordinator.data.update(ota_mode=False, ota_confirmed=True)
        await self.coordinator.async_set_threshold(152)
        self.coordinator.data.update(ota_mode=True, firmware_version="1.4.9")
        self.coordinator._seen_bthome = True
        self.coordinator.cloud = types.SimpleNamespace(data={"version": "1.5.0"})
        events = []

        async def write_threshold(value):
            events.append("threshold")
            self.coordinator.data.update(
                threshold_pending=None,
                threshold=value,
                threshold_result=f"已写入阈值 {value}",
            )

        async def run_ota(image, target):
            events.append("ota")
            self.coordinator.data.update(status="success", phase="done", progress=100)
            self.coordinator._task = None

        original_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await original_sleep(0)

        with (
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
            patch.object(self.coordinator, "_async_run_ota", side_effect=run_ota),
            patch.object(coordinator_module.asyncio, "sleep", fast_sleep),
        ):
            await self.coordinator._async_auto_write_pending()
            auto_task = self.coordinator._auto_task
            self.assertIsNotNone(auto_task)
            await auto_task

        self.assertEqual(events, ["threshold", "ota"])
        self.assertEqual(self.coordinator.data["status"], "success")

    async def test_failed_pending_threshold_blocks_upgrade_and_stays_pending(self):
        self.coordinator.data["ota_mode"] = False
        await self.coordinator.async_set_threshold(155)
        self.coordinator.data["ota_mode"] = True

        async def fail_write(*args, **kwargs):
            raise HomeAssistantError("模拟写入失败")

        original_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await original_sleep(0)

        with (
            patch.object(coordinator_module, "async_set_threshold", fail_write),
            patch.object(self.coordinator, "_maybe_auto_install") as auto_install,
            patch.object(coordinator_module.asyncio, "sleep", fast_sleep),
        ):
            with self.assertLogs(coordinator_module._LOGGER, level="ERROR"):
                await self.coordinator._async_auto_write_pending()

        auto_install.assert_not_called()
        self.assertEqual(self.coordinator.data["threshold_pending"], 155)
        self.assertIn("写入失败", self.coordinator.data["threshold_result"])

    async def test_set_threshold_save_race_still_continues_auto_upgrade(self):
        self.coordinator.data["ota_mode"] = True
        save_started = asyncio.Event()
        allow_save = asyncio.Event()
        auto_sleep_started = asyncio.Event()
        allow_auto = asyncio.Event()

        async def gated_save(_state):
            save_started.set()
            await allow_save.wait()

        async def gated_sleep(_seconds):
            auto_sleep_started.set()
            await allow_auto.wait()

        async def write_threshold(value):
            self.coordinator.data.update(
                threshold_pending=None,
                threshold=value,
                threshold_result=f"已写入阈值 {value}",
            )

        with (
            patch.object(self.coordinator._threshold_store, "async_save", gated_save),
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
            patch.object(self.coordinator, "_maybe_auto_install") as auto_install,
            patch.object(coordinator_module.asyncio, "sleep", gated_sleep),
        ):
            setter = asyncio.create_task(self.coordinator.async_set_threshold(170))
            await save_started.wait()
            self.coordinator._maybe_apply_pending()
            auto_task = self.coordinator._gas_task
            self.assertIsNotNone(auto_task)
            await auto_sleep_started.wait()
            allow_save.set()
            await setter
            allow_auto.set()
            await auto_task

        auto_install.assert_called_once_with()

    async def test_cancel_during_threshold_save_prevents_stale_write(self):
        self.coordinator.data["ota_mode"] = True
        save_started = asyncio.Event()
        allow_save = asyncio.Event()
        save_calls = 0
        writes = []
        completed_states = []

        async def gated_save(state):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                save_started.set()
                await allow_save.wait()
            completed_states.append(state)

        async def write_threshold(value):
            writes.append(value)
            self.coordinator.data["threshold_pending"] = None

        with (
            patch.object(self.coordinator._threshold_store, "async_save", gated_save),
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
        ):
            setter = asyncio.create_task(self.coordinator.async_set_threshold(172))
            await save_started.wait()
            await self.coordinator.async_cancel_threshold()
            allow_save.set()
            with self.assertRaises(HomeAssistantError):
                await setter

        self.assertEqual(writes, [])
        self.assertIsNone(self.coordinator.data["threshold_pending"])
        self.assertIsNone(completed_states[-1]["pending"])

    async def test_newer_threshold_replaces_older_waiting_save(self):
        self.coordinator.data["ota_mode"] = True
        save_started = asyncio.Event()
        allow_save = asyncio.Event()
        save_calls = 0
        writes = []
        completed_states = []

        async def gated_save(state):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                save_started.set()
                await allow_save.wait()
            completed_states.append(state)

        async def write_threshold(value):
            writes.append(value)
            self.coordinator.data.update(
                threshold_pending=None,
                threshold=value,
                threshold_result=f"已写入阈值 {value}",
            )

        with (
            patch.object(self.coordinator._threshold_store, "async_save", gated_save),
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
        ):
            older = asyncio.create_task(self.coordinator.async_set_threshold(175))
            await save_started.wait()
            await self.coordinator.async_set_threshold(180)
            allow_save.set()
            with self.assertRaises(HomeAssistantError):
                await older

        self.assertEqual(writes, [180])
        self.assertEqual(self.coordinator.data["threshold"], 180)
        self.assertIsNone(completed_states[-1]["pending"])
        self.assertEqual(completed_states[-1]["target"], 180)

    @staticmethod
    def _remote_meta(image):
        return {
            "size": len(image),
            "crc32": f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}",
            "sha256": hashlib.sha256(image).hexdigest(),
        }

    def _emit_normal_advertisement(self):
        self.coordinator.data["ads"] = int(self.coordinator.data["ads"]) + 1
        self.coordinator.data["ota_mode"] = False
        self.coordinator._bt_event.set()

    async def test_cloud_download_is_cancellable_and_serialized(self):
        image = b"\x6f\x00\x00\x00"
        meta = self._remote_meta(image)
        meta.update(version="1.6.1", download_url="https://example.invalid/fw")
        started = asyncio.Event()
        allow_download = asyncio.Event()

        async def blocked_download(*args):
            started.set()
            await allow_download.wait()
            return image

        self.coordinator.data.update(firmware_version="1.6.0", ota_mode=False)
        with patch.object(
            coordinator_module, "async_download_firmware", blocked_download
        ):
            task = self.coordinator.async_install_cloud(meta, "1.6.1")
            await asyncio.wait_for(started.wait(), timeout=1)
            with self.assertRaises(HomeAssistantError):
                self.coordinator.async_install_cloud(meta, "1.6.1")
            await self.coordinator.async_abort()
            allow_download.set()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIsNone(self.coordinator._task)
        self.assertEqual(self.coordinator.data["status"], "aborted")

    async def test_legacy_normal_mode_requires_button_without_ble(self):
        image = b"\x6f\x00\x00\x00"
        request = unittest.mock.AsyncMock()
        with patch.object(coordinator_module, "async_request_ota", request, create=True):
            for version in (None, "1.5.0"):
                with self.subTest(version=version):
                    self.coordinator.data.update(
                        firmware_version=version, ota_mode=False
                    )
                    with self.assertRaises(HomeAssistantError):
                        await self.coordinator.async_start_ota(
                            image=image,
                            version="1.6.0",
                            meta=self._remote_meta(image),
                        )
        request.assert_not_called()

    async def test_remote_wake_requires_complete_verified_metadata(self):
        image = b"\x6f\x00\x00\x00"
        complete = self._remote_meta(image)

        async def run_ota(*args, **kwargs):
            return None

        with patch.object(self.coordinator, "_async_run_ota", side_effect=run_ota):
            for field in ("size", "crc32", "sha256"):
                with self.subTest(missing=field):
                    self.coordinator.data.update(
                        firmware_version="1.6.0", ota_mode=False
                    )
                    meta = dict(complete)
                    meta.pop(field)
                    with self.assertRaises(HomeAssistantError):
                        await self.coordinator.async_start_ota(
                            image=image, version="1.6.1", meta=meta
                        )

    async def test_remote_wake_waits_for_new_window_after_failed_connect(self):
        self.coordinator.data.update(
            firmware_version="1.6.0", ota_mode=False, ads=0, ota_ads=0
        )
        first_failed = asyncio.Event()
        calls = 0

        async def request(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_failed.set()
                raise OtaError("connect timeout")
            self.coordinator.data["ota_ads"] += 1
            self.coordinator.data["ota_mode"] = True
            self.coordinator._bt_event.set()

        with patch.object(coordinator_module, "async_request_ota", request, create=True):
            task = asyncio.create_task(self.coordinator._async_remote_wake())
            await asyncio.sleep(0)
            self._emit_normal_advertisement()
            await asyncio.wait_for(first_failed.wait(), timeout=1)
            self._emit_normal_advertisement()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(calls, 2)
        self.assertEqual(self.coordinator.data["wake_attempts"], 2)

    async def test_remote_wake_timeout_preserves_attempt_count_and_last_error(self):
        self.coordinator.data.update(ads=0, ota_ads=0, ota_mode=False)
        request_started = asyncio.Event()

        async def request(*args, **kwargs):
            request_started.set()
            raise coordinator_module.OtaError("connect timeout")

        with (
            patch.object(coordinator_module, "async_request_ota", request),
            patch.object(coordinator_module, "REMOTE_OTA_WAKE_TIMEOUT", 0.01),
        ):
            task = asyncio.create_task(self.coordinator._async_remote_wake())
            await asyncio.sleep(0)
            self._emit_normal_advertisement()
            await asyncio.wait_for(request_started.wait(), timeout=1)
            with self.assertRaises(coordinator_module.OtaError) as caught:
                await asyncio.wait_for(task, timeout=1)

        self.assertEqual(self.coordinator.data["wake_attempts"], 1)
        self.assertIn("1 次", str(caught.exception))
        self.assertIn("connect timeout", str(caught.exception))

    async def test_remote_wake_bounds_inflight_connection_to_remaining_deadline(self):
        self.coordinator.data.update(ads=0, ota_ads=0, ota_mode=False)
        request_started = asyncio.Event()
        timeouts = []

        async def request(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            request_started.set()
            await asyncio.sleep(1)

        with (
            patch.object(coordinator_module, "async_request_ota", request),
            patch.object(coordinator_module, "REMOTE_OTA_WAKE_TIMEOUT", 0.02),
        ):
            task = asyncio.create_task(self.coordinator._async_remote_wake())
            await asyncio.sleep(0)
            self._emit_normal_advertisement()
            await asyncio.wait_for(request_started.wait(), timeout=1)
            with self.assertRaises(coordinator_module.OtaError):
                await asyncio.wait_for(task, timeout=0.2)

        self.assertEqual(len(timeouts), 1)
        self.assertIsNotNone(timeouts[0])
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], 0.02)

    async def test_cancel_while_waiting_window_prevents_stale_wake(self):
        request = unittest.mock.AsyncMock()
        with patch.object(coordinator_module, "async_request_ota", request, create=True):
            task = asyncio.create_task(self.coordinator._async_remote_wake())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._emit_normal_advertisement()
            await asyncio.sleep(0)
        request.assert_not_called()

    async def test_ota_mode_without_new_raw_advertisement_is_stale(self):
        self.coordinator.data.update(ota_mode=True, ota_ads=4)
        self.assertFalse(
            await self.coordinator._async_wait_for_ota_switch(4, timeout=0)
        )
        self.coordinator.data["ota_ads"] = 5
        self.assertTrue(
            await self.coordinator._async_wait_for_ota_switch(4, timeout=0)
        )

    async def test_remote_wake_failure_does_not_claim_ota_mode_was_entered(self):
        async def wake():
            raise coordinator_module.OtaError(
                "普通模式唤醒连接失败: connect timeout"
            )

        with patch.object(self.coordinator, "_async_remote_wake", side_effect=wake):
            await self.coordinator._async_run_ota(
                b"\x6f\x00\x00\x00", "1.6.2", wake_required=True
            )

        error = self.coordinator.data["error"]
        self.assertIn("connect timeout", error)
        self.assertNotIn("重新长按 B", error)
        self.assertNotIn("已退出 OTA 模式", error)

    async def test_remote_wake_writes_threshold_before_upload_with_fresh_gatt(self):
        self.coordinator.data.update(ota_mode=False, threshold_pending=165)
        events = []
        fresh = []
        threshold_fresh = []

        async def wake():
            events.append("wake")
            self.coordinator.data["ota_mode"] = True

        async def write_threshold(value, fresh_services=False):
            events.append("threshold")
            threshold_fresh.append(fresh_services)
            self.coordinator.data["threshold_pending"] = None

        class FakeOta:
            def __init__(self, *args, **kwargs):
                fresh.append(kwargs.get("fresh_services", False))

            async def async_run(self, chunk):
                events.append("ota")
                self_outer._on_progress(98, "rebooting", 0, 0)

        self_outer = self.coordinator
        with (
            patch.object(self.coordinator, "_async_remote_wake", side_effect=wake),
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
            patch.object(coordinator_module, "Ch592OtaClient", FakeOta),
            patch.object(
                self.coordinator, "_async_wait_for_reboot", return_value=True
            ),
        ):
            await self.coordinator._async_run_ota(
                b"\x6f\x00\x00\x00", "1.6.1", wake_required=True
            )

        self.assertEqual(events, ["wake", "threshold", "ota"])
        self.assertEqual(threshold_fresh, [True])
        self.assertEqual(fresh, [True])

    async def test_remote_threshold_failure_prevents_any_upload(self):
        self.coordinator.data.update(ota_mode=False, threshold_pending=165)

        async def wake():
            self.coordinator.data["ota_mode"] = True

        threshold = unittest.mock.AsyncMock(side_effect=HomeAssistantError("write failed"))
        client = unittest.mock.Mock()
        with (
            patch.object(self.coordinator, "_async_remote_wake", side_effect=wake),
            patch.object(self.coordinator, "_async_write_threshold", threshold),
            patch.object(coordinator_module, "Ch592OtaClient", client),
            self.assertLogs(coordinator_module._LOGGER, level="ERROR"),
        ):
            await self.coordinator._async_run_ota(
                b"\x6f\x00\x00\x00", "1.6.1", wake_required=True
            )

        threshold.assert_awaited_once_with(165, fresh_services=True)
        client.assert_not_called()
        self.assertEqual(self.coordinator.data["threshold_pending"], 165)
        self.assertEqual(self.coordinator.data["status"], "failed")

    async def test_manual_ota_writes_pending_threshold_first(self):
        self.coordinator.data.update(ota_mode=True, threshold_pending=165)
        events = []

        async def write_threshold(value):
            events.append("threshold")
            self.coordinator.data["threshold_pending"] = None

        async def run_ota(image, target):
            events.append("ota")

        with (
            patch.object(
                self.coordinator, "_async_write_threshold", side_effect=write_threshold
            ),
            patch.object(self.coordinator, "_async_run_ota", side_effect=run_ota),
        ):
            await self.coordinator.async_start_ota(
                image=b"\x6f\x00\x00\x00", version="1.5.0"
            )
            await self.coordinator.current_task

        self.assertEqual(events, ["threshold", "ota"])

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

    async def test_unconfirmed_fallback_ota_does_not_auto_install(self):
        self.entry.options[coordinator_module.CONF_AUTO_INSTALL] = True
        self.coordinator._seen_bthome = True
        self.coordinator.data["firmware_version"] = "1.4.3"
        self.coordinator.cloud = types.SimpleNamespace(data={"version": "1.6.1"})

        for raw in (
            None,
            b"",
            bytes.fromhex("0503e0ff"),
            bytes.fromhex("0403e0feff"),
        ):
            with self.subTest(raw=raw):
                self.coordinator.data.update(ota_mode=False, auto_note=None)
                info = types.SimpleNamespace(
                    name="GasMeter OTA",
                    service_uuids=[coordinator_module.OTA_SERVICE_UUID],
                    service_data={},
                    raw=raw,
                )
                with (
                    patch.object(
                        self.coordinator,
                        "_async_auto_install",
                        new_callable=unittest.mock.Mock,
                    ) as install,
                    patch.object(self.hass, "async_create_task") as create_task,
                ):
                    self.coordinator._async_bt_callback(info, None)

                self.assertTrue(self.coordinator.data["ota_mode"])
                create_task.assert_not_called()
                install.assert_not_called()
                self.assertFalse(self.coordinator.data["ota_confirmed"])
                self.assertIn("原始", self.coordinator.data["auto_note"])

    async def test_raw_confirmed_ota_allows_explicitly_enabled_auto_install(self):
        self.entry.options[coordinator_module.CONF_AUTO_INSTALL] = True
        self.coordinator._seen_bthome = True
        self.coordinator.data["firmware_version"] = "1.4.3"
        self.coordinator.cloud = types.SimpleNamespace(data={"version": "1.6.1"})
        info = types.SimpleNamespace(
            name="GasMeter OTA",
            service_uuids=[coordinator_module.OTA_SERVICE_UUID],
            service_data={"0000fcd2-0000-1000-8000-00805f9b34fb": b"stale"},
            raw=bytes.fromhex("0201060503e0ffe0fe"),
        )

        with (
            patch.object(
                self.coordinator,
                "_async_auto_install",
                new_callable=unittest.mock.Mock,
            ) as install,
            patch.object(self.hass, "async_create_task") as create_task,
        ):
            self.coordinator._async_bt_callback(info, None)

        self.assertTrue(self.coordinator.data["ota_confirmed"])
        install.assert_called_once_with({"version": "1.6.1"}, "1.6.1")
        create_task.assert_called_once()

    async def test_valid_raw_after_fallback_enables_auto_install_in_same_window(self):
        self.entry.options[coordinator_module.CONF_AUTO_INSTALL] = True
        self.coordinator._seen_bthome = True
        self.coordinator.data["firmware_version"] = "1.4.3"
        self.coordinator.cloud = types.SimpleNamespace(data={"version": "1.6.1"})
        common = {
            "name": "GasMeter OTA",
            "service_uuids": [coordinator_module.OTA_SERVICE_UUID],
            "service_data": {},
        }

        with (
            patch.object(
                self.coordinator,
                "_async_auto_install",
                new_callable=unittest.mock.Mock,
            ) as install,
            patch.object(self.hass, "async_create_task") as create_task,
        ):
            self.coordinator._async_bt_callback(
                types.SimpleNamespace(**common, raw=b""), None
            )
            self.coordinator._async_bt_callback(
                types.SimpleNamespace(
                    **common, raw=bytes.fromhex("0201060503e0ffe0fe")
                ),
                None,
            )

        self.assertTrue(self.coordinator.data["ota_mode"])
        self.assertTrue(self.coordinator.data["ota_confirmed"])
        install.assert_called_once_with({"version": "1.6.1"}, "1.6.1")
        create_task.assert_called_once()

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
