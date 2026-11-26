"""Runtime state and OTA task management for one CH592 device."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from functools import wraps

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .ble_ota import (
    Ch592OtaClient,
    OtaError,
    OtaMtuError,
    async_request_ota,
    async_set_threshold,
)
from .bthome import has_bthome_data, parse_bthome_fields
from .const import (
    CONF_AUTO_INSTALL,
    CONF_FIRMWARE,
    CONF_VERSION,
    DEFAULT_AUTO_INSTALL,
    DEFAULT_FIRMWARE,
    DOMAIN,
    IMAGE_SIZE,
    OTA_SERVICE_UUID,
    REBOOT_TIMEOUT,
    REMOTE_OTA_MIN_VERSION,
    REMOTE_OTA_SWITCH_TIMEOUT,
    REMOTE_OTA_WAKE_TIMEOUT,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
)
from .gas_accounting import GasAccounting
from .firmware import (
    FirmwareCloudCoordinator,
    async_download_firmware,
    is_newer,
    manifest_version,
)

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[], None]


def _track_config_operation(function):
    """Make foreground service calls visible to abort and unload."""
    @wraps(function)
    async def wrapper(self, *args, **kwargs):
        task = asyncio.current_task()
        if task is not None:
            self._foreground_tasks.add(task)
        try:
            return await function(self, *args, **kwargs)
        finally:
            if task is not None:
                self._foreground_tasks.discard(task)
    return wrapper


class Ch592OtaCoordinator:
    """Holds the latest advertisement data and the running OTA task."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data["address"]
        self.data: dict[str, object] = {
            "firmware_version": None,
            "ads": 0,
            "last_seen": None,
            "ota_mode": False,
            "ota_ads": 0,
            "status": "idle",
            "phase": "idle",
            "progress": 0,
            "uploaded": 0,
            "total": 0,
            "error": None,
        }
        self._listeners: set[Listener] = set()
        self._unregister_bt = None
        self._task: asyncio.Task | None = None
        self._client: Ch592OtaClient | None = None
        self._requested_chunk: int | None = None
        self.cloud: FirmwareCloudCoordinator | None = None
        self._auto_task: asyncio.Task | None = None
        self._last_diag_notify = 0.0
        self._run_started: float | None = None
        # Set once a normal BTHome advertisement arrives, so a leftover OTA
        # mode from a previous session does not trigger an upgrade by itself.
        self._seen_bthome = False
        # Gas value written over the OTA channel: draft, armed intent, result.
        self.data["gas_value"] = None
        self.data["gas_target"] = None
        self.data["gas_pending"] = None
        self.data["gas_result"] = None
        self._gas_pending_until = 0.0
        self._gas_task: asyncio.Task | None = None
        self.gas = GasAccounting()
        self._gas_store = Store(
            hass, 1, f"{DOMAIN}.{getattr(entry, 'entry_id', self.address)}.gas"
        )
        self._gas_store_task: asyncio.Task | None = None
        self._ble_lock = asyncio.Lock()
        self._foreground_tasks: set[asyncio.Task] = set()
        self._pending_start: int | None = None
        self._start_counter = 0
        self._operation_generation = 0
        self._reboot_start_seq: int | None = None
        self._stopped = False
        self._bt_event = asyncio.Event()
        self.data["wake_attempts"] = 0
        self.data["wake_error"] = None
        # Pulse threshold (firmware >= 1.4.1): device value, draft, armed intent.
        self.data["threshold"] = None
        self.data["threshold_target"] = None
        self.data["threshold_pending"] = None
        self.data["threshold_result"] = None
        self._threshold_store = Store(
            hass, 1, f"{DOMAIN}.{getattr(entry, 'entry_id', self.address)}.threshold"
        )
        self._threshold_request_generation = 0

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        self._stopped = False
        try:
            stored = await self._gas_store.async_load()
            if stored:
                self.gas = GasAccounting.from_dict(stored)
        except (TypeError, ValueError) as err:
            _LOGGER.warning("燃气校准状态无效，等待重新校准: %s", err)
            self.gas = GasAccounting()
        self._sync_gas_data()
        await self._async_load_threshold_state()
        self._unregister_bt = async_register_callback(
            self.hass,
            self._async_bt_callback,
            {"address": self.address},
            BluetoothScanningMode.ACTIVE,
        )

    async def async_stop(self) -> None:
        self._stopped = True
        if self._gas_store_task is not None and not self._gas_store_task.done():
            self._gas_store_task.cancel()
            await asyncio.gather(self._gas_store_task, return_exceptions=True)
        self._gas_store_task = None
        if self.gas.calibrated:
            await self._gas_store.async_save(self.gas.to_dict())
        if self._unregister_bt is not None:
            self._unregister_bt()
            self._unregister_bt = None
        await self.async_abort(quiet=True)
        await self._async_save_threshold_state()

    # -------------------------------------------------------------- listeners

    @callback
    def async_add_listener(self, update_callback: Listener) -> Callable[[], None]:
        self._listeners.add(update_callback)

        @callback
        def remove_listener() -> None:
            self._listeners.discard(update_callback)

        return remove_listener

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ------------------------------------------------------------- bluetooth

    @callback
    def _async_bt_callback(
        self,
        service_info: BluetoothServiceInfoBleak,
        change,
    ) -> None:
        # Diagnostics: visible on binary_sensor.*_ota_mode attributes.
        self.data["ads_total"] = int(self.data.get("ads_total", 0)) + 1
        self.data["last_name"] = service_info.name
        self.data["last_uuids"] = list(service_info.service_uuids)[:6]
        self.data["last_data_uuids"] = list(service_info.service_data)[:6]

        # The OTA advertisement is the authoritative signal (it is the only one
        # carrying 0xFEE0) and must be checked before the BTHome parser: HA can
        # report a BTHome payload for the same address, and the cached device
        # name is "GasMeter OTA" in both modes, so the name proves nothing.
        if self._is_ota_advertisement(service_info):
            self.data["ota_ads"] = int(self.data.get("ota_ads", 0)) + 1
            if not self.data["ota_mode"]:
                _LOGGER.info(
                    "检测到 OTA 广播 (uuids=%s data=%s)",
                    list(service_info.service_uuids),
                    list(service_info.service_data),
                )
                self.data["ota_mode"] = True
                self._notify()
                # An armed config write (threshold and/or gas) wins over an
                # automatic upgrade: the user pressed B for that, and a
                # finished upgrade would reboot the device out of OTA mode
                # before the values could be written.
                if self._threshold_pending_value() is not None:
                    self._maybe_apply_pending()
                else:
                    self._maybe_auto_install()
        else:
            fields = parse_bthome_fields(service_info)
            version = fields.get("version")
            gas = fields.get("gas")
            threshold = fields.get("threshold")
            if gas is not None:
                self._update_raw_gas(int(gas))
            if threshold is not None:
                self.data["threshold"] = int(threshold)
            if version is not None:
                self._seen_bthome = True
                self.data["firmware_version"] = str(version)
                self.data["ads"] = int(self.data["ads"]) + 1
                self.data["last_seen"] = datetime.now(UTC)
                self.data["ota_mode"] = False
                self._notify()
            elif gas is not None or threshold is not None:
                self._notify()

        # Throttled refresh so the diagnostic attributes stay current.
        now = asyncio.get_running_loop().time()
        if now - self._last_diag_notify >= 5:
            self._last_diag_notify = now
            self._notify()
        self._bt_event.set()

    @staticmethod
    def _raw_16bit_uuids(raw: bytes | None) -> set[int] | None:
        """Parse 16-bit UUIDs from the current BLE advertisement, not HA's cache."""
        if not raw:
            return None
        uuids: set[int] = set()
        index = 0
        parsed = False
        while index < len(raw):
            length = raw[index]
            index += 1
            if length == 0:
                break
            end = index + length
            if end > len(raw):
                return None
            ad_type = raw[index]
            payload = raw[index + 1 : end]
            parsed = True
            if ad_type in (0x02, 0x03):  # partial/complete 16-bit UUID list
                for offset in range(0, len(payload) - 1, 2):
                    uuids.add(int.from_bytes(payload[offset : offset + 2], "little"))
            elif ad_type == 0x16 and len(payload) >= 2:  # 16-bit service data
                uuids.add(int.from_bytes(payload[:2], "little"))
            index = end
        return uuids if parsed else None

    @staticmethod
    def _is_ota_advertisement(service_info: BluetoothServiceInfoBleak) -> bool:
        """Recognise OTA from the current packet, ignoring cached GATT fields.

        HA can retain 0xFEE0 service UUIDs and 0xFCD2 service data discovered in
        earlier modes. The raw advertisement is authoritative when available;
        merged fields remain a compatibility fallback for older HA versions.
        """
        raw_uuids = Ch592OtaCoordinator._raw_16bit_uuids(
            getattr(service_info, "raw", None)
        )
        if raw_uuids is not None:
            return 0xFEE0 in raw_uuids and 0xFCD2 not in raw_uuids

        has_ota_uuid = any(
            u.lower() == OTA_SERVICE_UUID for u in service_info.service_uuids
        ) or any(
            u.lower() in (OTA_SERVICE_UUID, "fee0")
            for u in service_info.service_data
        )
        return has_ota_uuid and not has_bthome_data(service_info)

    @staticmethod
    def remote_ota_supported(version: object) -> bool:
        """Conservatively gate normal-mode wake to known firmware versions."""
        if version is None:
            return False
        try:
            parts = tuple(
                int(part) for part in str(version).strip().lstrip("vV").split(".")
            )
        except (TypeError, ValueError):
            return False
        return len(parts) == 3 and parts >= REMOTE_OTA_MIN_VERSION

    @staticmethod
    def _validate_remote_metadata(image: bytes, meta: dict[str, object]) -> None:
        for field in ("size", "crc32", "sha256"):
            if meta.get(field) is None:
                raise HomeAssistantError(f"免按键升级要求固件清单提供 {field}")
        if len(image) != int(meta["size"]):
            raise HomeAssistantError("免按键升级固件大小与清单不一致")
        actual_crc = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
        if str(meta["crc32"]).upper() != actual_crc.upper():
            raise HomeAssistantError("免按键升级固件 CRC32 与清单不一致")
        if str(meta["sha256"]).lower() != hashlib.sha256(image).hexdigest():
            raise HomeAssistantError("免按键升级固件 SHA-256 与清单不一致")

    def _assert_operation_available(self) -> None:
        """Reject overlapping device operations before any await creates a race."""
        if self._stopped:
            raise HomeAssistantError("设备配置已卸载")
        if self._pending_start is not None:
            raise HomeAssistantError("另一项 OTA 正在准备固件，请完成或取消后再试")
        current = asyncio.current_task()
        operations = (self._task, self._auto_task, self._gas_task)
        for task in (*operations, *self._foreground_tasks):
            if task is not None and not task.done() and task is not current:
                raise HomeAssistantError("另一项设备操作正在进行，请完成或取消后再试")

    def _sync_gas_data(self) -> None:
        self.data.update(
            gas_total=self.gas.total,
            gas_consumption_total=self.gas.consumption,
            gas_offset=self.gas.offset,
            gas_effective_offset=self.gas.effective_offset,
            gas_wraps=self.gas.wraps,
            gas_reset_detected=self.gas.reset_detected,
            gas_calibrated_at=self.gas.calibrated_at,
            gas_accepted_raw=self.gas.last_raw,
        )

    def _update_raw_gas(self, raw: int) -> None:
        self.data["gas_value"] = raw
        self.gas.accept_raw(raw)
        self._sync_gas_data()
        self._schedule_gas_store()

    def _schedule_gas_store(self) -> None:
        if self._stopped or not self.gas.calibrated:
            return
        if self._gas_store_task is None or self._gas_store_task.done():
            self._gas_store_task = self.hass.async_create_task(
                self._async_save_gas_later()
            )

    async def _async_save_gas_later(self) -> None:
        try:
            await asyncio.sleep(30)
            await self._gas_store.async_save(self.gas.to_dict())
        finally:
            self._gas_store_task = None

    async def async_calibrate_gas(self, actual_units: int) -> dict[str, object]:
        """Calibrate HA only; never writes the device counter over BLE."""
        raw = self.data.get("gas_value")
        if raw is None:
            raise HomeAssistantError("尚未收到设备原始燃气计数，无法校准")
        self.gas.calibrate(int(raw), int(actual_units))
        self._sync_gas_data()
        if self._gas_store_task is not None and not self._gas_store_task.done():
            self._gas_store_task.cancel()
            await asyncio.gather(self._gas_store_task, return_exceptions=True)
        self._gas_store_task = None
        await self._gas_store.async_save(self.gas.to_dict())
        self._notify()
        return {
            "result": "calibrated",
            "value": actual_units / 1000,
            "offset": self.gas.offset / 1000,
            "raw": raw / 1000,
        }

    # ----------------------------------------------------------- gas value

    def _gas_pending_value(self) -> int | None:
        """The armed gas value, or None when nothing is armed / it expired."""
        value = self.data.get("gas_pending")
        if value is None:
            return None
        now = asyncio.get_running_loop().time()
        if now > self._gas_pending_until:
            self.data["gas_pending"] = None
            self.data["gas_result"] = "待写入已超时取消"
            self._notify()
            return None
        return int(value)  # type: ignore[arg-type]

    @callback
    def set_gas_target(self, value_units: int) -> None:
        """Store the draft value; nothing is written to the device here."""
        self.data["gas_target"] = int(value_units)
        self._notify()

    @callback
    def cancel_gas(self) -> None:
        self.data["gas_pending"] = None
        self.data["gas_result"] = "已取消待写入"
        self._gas_pending_until = 0.0
        self._notify()

    async def async_set_gas(self, value_units: int) -> dict[str, object]:
        """Compatibility alias: calibrate HA instead of writing the device."""
        if value_units < 0:
            raise HomeAssistantError("燃气表读数不能为负数")
        self.data["gas_target"] = int(value_units)
        return await self.async_calibrate_gas(value_units)

    # ------------------------------------------------------- pulse threshold

    def _threshold_pending_value(self) -> int | None:
        """Return the pending threshold until it is written or cancelled."""
        value = self.data.get("threshold_pending")
        return int(value) if value is not None else None  # type: ignore[arg-type]

    async def _async_load_threshold_state(self) -> None:
        """Restore a threshold that is waiting for the next OTA window."""
        try:
            stored = await self._threshold_store.async_load()
            if not stored:
                return
            if not isinstance(stored, dict):
                raise ValueError("threshold state must be a dict")
            pending = stored.get("pending")
            target = stored.get("target")
            if pending is not None and (
                type(pending) is not int
                or not THRESHOLD_MIN <= pending <= THRESHOLD_MAX
            ):
                raise ValueError("invalid pending threshold")
            if target is not None and (
                type(target) is not int
                or not THRESHOLD_MIN <= target <= THRESHOLD_MAX
            ):
                raise ValueError("invalid threshold target")
            self.data["threshold_pending"] = pending
            self.data["threshold_target"] = target if target is not None else pending
            if pending is not None:
                self.data["threshold_result"] = "等待下次 OTA 窗口写入（按 B 或最长 3 天）"
        except (TypeError, ValueError) as err:
            _LOGGER.warning("待写阈值状态无效，已清除: %s", err)
            self.data["threshold_pending"] = None
            self.data["threshold_target"] = None
            await self._async_save_threshold_state()

    def _threshold_state_for_store(self) -> dict[str, int | None]:
        return {
            "pending": self.data.get("threshold_pending"),  # type: ignore[dict-item]
            "target": self.data.get("threshold_target"),  # type: ignore[dict-item]
        }

    async def _async_save_threshold_state(self) -> None:
        """Save until the persisted state matches the latest request.

        A store write can finish after a newer set/cancel operation or after a
        scheduled BLE write cleared pending. Repeating with a fresh snapshot
        prevents an older save from resurrecting stale state after HA restart.
        """
        while True:
            generation = self._threshold_request_generation
            snapshot = self._threshold_state_for_store()
            await self._threshold_store.async_save(snapshot)
            if (
                generation == self._threshold_request_generation
                and snapshot == self._threshold_state_for_store()
            ):
                return

    @callback
    def set_threshold_target(self, value: int) -> None:
        """Store the draft threshold; nothing is written to the device here."""
        self.data["threshold_target"] = int(value)
        self._notify()

    async def async_cancel_threshold(self) -> None:
        """Cancel the pending write and persist the cancellation."""
        self._threshold_request_generation += 1
        self.data["threshold_pending"] = None
        self.data["threshold_result"] = "已取消待写入"
        await self._async_save_threshold_state()
        self._notify()

    async def async_set_threshold(self, value: int) -> dict[str, object]:
        """Write in OTA mode or persist until the next manual/timed OTA window."""
        if not THRESHOLD_MIN <= value <= THRESHOLD_MAX:
            raise HomeAssistantError(
                f"阈值超出范围：{value}（{THRESHOLD_MIN}~{THRESHOLD_MAX}）"
            )
        self._assert_operation_available()
        self._threshold_request_generation += 1
        request_generation = self._threshold_request_generation

        self.data["threshold_target"] = int(value)
        self.data["threshold_pending"] = int(value)
        self.data["threshold_result"] = (
            None
            if self.data.get("ota_mode")
            else "等待下次 OTA 窗口写入（按 B 或最长 3 天）"
        )
        await self._async_save_threshold_state()
        if (
            self.data.get("threshold") == value
            and self.data.get("threshold_result") == f"已写入阈值 {value}"
            and self.data.get("threshold_pending") is None
        ):
            # The scheduled OTA writer completed the same request while the
            # foreground setter was waiting for its storage write.
            return {"result": "written", "threshold": value}
        if (
            request_generation != self._threshold_request_generation
            or self._threshold_pending_value() != value
        ):
            raise HomeAssistantError("阈值请求已被新值或取消操作替代")
        if self.data.get("ota_mode"):
            await self._async_write_threshold(value)
            return {"result": "written", "threshold": value}

        self._notify()
        return {
            "result": "armed",
            "threshold": value,
            "note": "将在下次 OTA 窗口写入：可按 B 立即触发，或等待每 3 天的定时窗口",
        }

    @_track_config_operation
    async def _async_write_threshold(
        self, value: int, fresh_services: bool = False
    ) -> None:
        self.data["threshold_result"] = "writing"
        self._notify()
        try:
            async with self._ble_lock:
                if self._stopped:
                    raise HomeAssistantError("设备配置已卸载")
                await async_set_threshold(
                    self.hass, self.address, value, fresh_services=fresh_services
                )
        except Exception as err:  # noqa: BLE001 - surface every failure
            self.data["threshold_result"] = f"写入失败: {err}"
            self._notify()
            raise HomeAssistantError(f"写入阈值失败: {err}") from err
        # The device does not reboot for a threshold write, so OTA mode stays
        # on and a pending gas value can follow on the same session.
        self.data.update(
            threshold_pending=None,
            threshold=int(value),
            threshold_result=f"已写入阈值 {value}",
        )
        await self._async_save_threshold_state()
        self._notify()

    @callback
    def _maybe_apply_pending(self) -> None:
        """Write the armed threshold once OTA mode appears."""
        if self._threshold_pending_value() is None:
            return
        if self._task is not None and not self._task.done():
            return
        if self._gas_task is not None and not self._gas_task.done():
            return
        self._gas_task = self.hass.async_create_task(
            self._async_auto_write_pending()
        )

    async def _async_auto_write_pending(self) -> None:
        try:
            # Give the device a moment to switch from advertising to connectable.
            await asyncio.sleep(2)
            threshold = self._threshold_pending_value()
            if threshold is None:
                # A foreground setter may have completed (or the user may have
                # cancelled) while this task was waiting. Do not strand a
                # firmware upgrade merely because pending is now empty.
                self._maybe_auto_install()
                return
            if threshold is not None:
                try:
                    await self._async_write_threshold(threshold)
                except Exception:  # noqa: BLE001 - recorded in threshold_result
                    self.data["auto_note"] = "阈值写入失败，本次 OTA 窗口不继续自动升级"
                    _LOGGER.exception("自动写入阈值失败")
                    self._notify()
                else:
                    # Threshold writes do not reboot, so a pending firmware
                    # upgrade can continue in this same OTA window.
                    self._maybe_auto_install()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - already recorded in the results
            _LOGGER.exception("自动写入配置失败")
        finally:
            self._gas_task = None

    # ------------------------------------------------------- auto update

    @property
    def auto_install(self) -> bool:
        return bool(
            self.entry.options.get(CONF_AUTO_INSTALL, DEFAULT_AUTO_INSTALL)
        )

    @callback
    def _maybe_auto_install(self) -> None:
        """Start the OTA as soon as the device advertises OTA mode.

        This is what makes "hold B for 3 s" enough: no button press in HA.
        Every skip reason is recorded in ``auto_note`` so the decision is
        visible on ``binary_sensor.*_ota_mode`` instead of only in the log.
        """
        def skip(reason: str) -> None:
            self.data["auto_note"] = reason
            _LOGGER.info("自动升级未启动: %s", reason)
            self._notify()

        if not self.auto_install:
            return skip("选项 auto_install_on_ota_mode 已关闭")
        if self._threshold_pending_value() is not None:
            return skip("有待写阈值，写入成功后再检查自动升级")
        if not self._seen_bthome:
            return skip("本次启动还没看到普通广播，可能是上次遗留的 OTA 模式，未自动升级")
        if self._task is not None and not self._task.done():
            return skip("已有升级任务在运行")
        if self._auto_task is not None and not self._auto_task.done():
            return skip("自动升级已在运行")

        manifest = self.cloud.data if self.cloud else None
        if not isinstance(manifest, dict):
            return skip("没有可用的固件清单")
        version = manifest_version(manifest)
        installed = self.data.get("firmware_version")
        installed_str = str(installed) if installed else None
        if not is_newer(version, installed_str):
            return skip(f"没有新固件（设备 {installed_str}，最新 {version}）")

        self.data["auto_note"] = f"开始自动升级到 {version}"
        _LOGGER.info(
            "检测到 OTA 模式且有新固件 %s（当前 %s），自动开始升级",
            version,
            installed_str,
        )
        self._notify()
        self._auto_task = self.hass.async_create_task(
            self._async_auto_install(manifest, version)
        )

    async def _async_auto_install(
        self, manifest: dict[str, object], version: str | None
    ) -> None:
        try:
            image = await async_download_firmware(self.hass, manifest)
            for attempt in (1, 2):
                # Give the device a moment to switch from advertising to
                # connectable; a fresh attempt waits longer.
                await asyncio.sleep(2 if attempt == 1 else 8)
                try:
                    await self.async_start_ota(
                        version=version, image=image, meta=manifest
                    )
                    task = self.current_task
                    if task is not None:
                        await task
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - retried below
                    _LOGGER.exception("自动升级第 %s 次尝试失败", attempt)
                    self.data.update(status="failed", phase="failed", error=str(err))
                    self._notify()
                if self.data.get("status") == "success":
                    return
                if int(self.data.get("progress") or 0) >= 5:
                    # The upload went somewhere and then broke: retrying from
                    # scratch rarely helps and costs another full upload.
                    break
                _LOGGER.warning(
                    "自动升级未成功（第 %s 次）: %s",
                    attempt,
                    self.data.get("error"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - surface every failure
            _LOGGER.exception("自动升级失败")
            self.data.update(status="failed", phase="failed", error=str(err))
            self._notify()
        finally:
            self._auto_task = None

    @property
    def last_seen(self) -> datetime | None:
        return self.data.get("last_seen")  # type: ignore[return-value]

    @property
    def current_task(self) -> asyncio.Task | None:
        """The running OTA task, so entities can wait for it to finish."""
        return self._task

    def async_install_cloud(
        self, manifest: dict[str, object], version: str | None
    ) -> asyncio.Task:
        """Register the download itself as an operation so abort/dedupe cover it."""
        self._assert_operation_available()
        task = self.hass.async_create_task(
            self._async_cloud_install(manifest, version),
            name=f"ch592_ota download {self.address}",
        )
        self._task = task
        return task

    async def _async_cloud_install(
        self, manifest: dict[str, object], version: str | None
    ) -> None:
        current = asyncio.current_task()
        try:
            image = await async_download_firmware(self.hass, manifest)
            # async_start_ota ignores the current registered outer task. It
            # replaces it with the real OTA runner only after all validation.
            await self.async_start_ota(
                version=version, image=image, meta=manifest
            )
            inner = self._task
            if inner is not None and inner is not current:
                await inner
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - surface download/start errors
            self.data.update(status="failed", phase="failed", error=str(err))
            self._notify()
        finally:
            if self._task is current:
                self._task = None

    # --------------------------------------------------------------- firmware

    def resolve_firmware_path(self, value: str | None = None) -> Path:
        raw = value or self.entry.options.get(CONF_FIRMWARE, DEFAULT_FIRMWARE)
        config_dir = Path(self.hass.config.config_dir).resolve()
        path = Path(raw)
        if not path.is_absolute():
            path = config_dir / path
        path = path.resolve()
        if not path.is_relative_to(config_dir):
            raise HomeAssistantError("固件路径必须位于 Home Assistant 的 config 目录内")
        return path

    async def async_start_ota(
        self,
        firmware: str | None = None,
        version: str | None = None,
        chunk: int | None = None,
        image: bytes | None = None,
        meta: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self._assert_operation_available()
        wake_required = not bool(self.data.get("ota_mode"))
        if wake_required and not self.remote_ota_supported(
            self.data.get("firmware_version")
        ):
            raise HomeAssistantError(
                "当前固件不支持免按键唤醒，请先按 B 或等待三天定时窗口升级"
            )
        pending_threshold = self._threshold_pending_value()
        if pending_threshold is not None and self.data.get("ota_mode"):
            # A threshold write does not reboot. Finish it before END of any
            # manual or automatic firmware upgrade in the same OTA window.
            await self._async_write_threshold(pending_threshold)
        self._start_counter += 1
        reservation = self._start_counter
        generation = self._operation_generation
        self._pending_start = reservation
        try:
            if chunk is not None:
                chunk = int(chunk)
                if chunk % 16 or not 16 <= chunk <= 64:
                    raise HomeAssistantError("chunk 必须是 16~64 之间、16 的倍数")
            self._requested_chunk = chunk

            if image is None:
                path = self.resolve_firmware_path(firmware)
                try:
                    image, meta = await self.hass.async_add_executor_job(
                        self._read_image, path
                    )
                except OSError as err:
                    raise HomeAssistantError(f"无法读取固件 {path}: {err}") from err
                source = str(path)
            else:
                meta = meta or {}
                source = "云端固件"

            if not image:
                raise HomeAssistantError(f"固件文件为空: {source}")
            if len(image) > IMAGE_SIZE - 16:
                raise HomeAssistantError(
                    f"固件过大: {len(image)} 字节，APP 区最多 {IMAGE_SIZE - 16} 字节"
                )
            if len(image) < 4 or (image[0] & 0x7F) != 0x6F:
                raise HomeAssistantError("文件不是有效的 CH592 APP 镜像（首字不是 JAL 指令）")

            actual_crc = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
            expected_crc = meta.get("crc32")
            if expected_crc and str(expected_crc).upper() != actual_crc.upper():
                raise HomeAssistantError(
                    f"固件 CRC32 不匹配: 元数据 {expected_crc}, 实际 {actual_crc}"
                )

            if wake_required:
                self._validate_remote_metadata(image, meta)

            if generation != self._operation_generation or self._stopped:
                raise asyncio.CancelledError

            target = version or meta.get(CONF_VERSION) or None
            self._run_started = asyncio.get_running_loop().time()
            self.data.update(
                status="starting",
                phase="starting",
                progress=0,
                uploaded=0,
                total=len(image),
                error=None,
                duration_s=None,
            )
            self._notify()

            runner = (
                self._async_run_ota(image, target, wake_required=True)
                if wake_required
                else self._async_run_ota(image, target)
            )
            self._task = self.hass.async_create_task(
                runner, name=f"ch592_ota {self.address}"
            )
            return {"status": "started", "version": target, "size": len(image)}

        finally:
            if self._pending_start == reservation:
                self._pending_start = None

    async def async_abort(self, quiet: bool = False) -> None:
        self._operation_generation += 1
        current = asyncio.current_task()
        tasks = {
            task
            for task in (
                self._task,
                self._auto_task,
                self._gas_task,
                *self._foreground_tasks,
            )
            if task is not None and not task.done() and task is not current
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._auto_task = None
        self._gas_task = None
        if not quiet:
            self.data["status"] = "aborted"
            self.data["phase"] = "aborted"
            self._notify()

    # ------------------------------------------------------------- OTA runner

    def read_image(self, path: Path) -> tuple[bytes, dict[str, object]]:
        """Read a firmware image and its sidecar metadata (executor-safe)."""
        return self._read_image(path)

    def _read_image(self, path: Path) -> tuple[bytes, dict[str, object]]:
        image = path.read_bytes()
        meta: dict[str, object] = {}
        sidecar = path.with_name(path.name + ".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _LOGGER.warning("无法解析固件元数据 %s", sidecar)
        if meta.get("size") not in (None, len(image)):
            _LOGGER.warning(
                "固件元数据大小 %s 与实际大小 %s 不一致",
                meta.get("size"),
                len(image),
            )
        return image, meta

    @callback
    def _on_ota_info(self, info: dict[str, object]) -> None:
        """Device INFO response: voltage, protocol and the negotiated chunks."""
        if info.get("vdd"):
            self.data["vdd"] = info["vdd"]
            self.data["vdd_min"] = info.get("vdd_min") or info["vdd"]
        if info.get("protocol"):
            self.data["protocol"] = info["protocol"]
        if info.get("features") is not None:
            self.data["features"] = int(info["features"])
        if info.get("threshold") is not None:
            self.data["threshold"] = info["threshold"]
        for key in ("chunk", "verify_chunk", "mtu"):
            if info.get(key) is not None:
                self.data[key] = info[key]
        self._notify()

    def _on_progress(
        self, percent: int, phase: str, done: int, total: int
    ) -> None:
        if phase == "rebooting":
            # Ch592OtaClient emits this synchronously immediately before END.
            self._reboot_start_seq = int(self.data["ads"])
        if (
            percent == self.data.get("progress")
            and phase == self.data.get("phase")
            and self.data.get("status") == "running"
        ):
            return
        self.data.update(
            status="running",
            phase=phase,
            progress=percent,
            uploaded=done,
            total=total,
        )
        self._notify()

    @staticmethod
    def _is_retryable(err: Exception) -> bool:
        """Link drops are worth a fresh attempt; protocol errors are not."""
        text = str(err).lower()
        return any(
            key in text
            for key in ("断开", "connect", "timeout", "timed out", "超时")
        )

    async def _async_wait_bt(self, predicate: Callable[[], bool], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while not predicate():
            self._bt_event.clear()
            if predicate():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._bt_event.wait(), remaining)
            except TimeoutError:
                return False
        return True

    async def _async_wait_for_ota_switch(
        self, start_seq: int, timeout: float = REMOTE_OTA_SWITCH_TIMEOUT
    ) -> bool:
        return await self._async_wait_bt(
            lambda: bool(self.data.get("ota_mode"))
            and int(self.data.get("ota_ads", 0)) > start_seq,
            timeout,
        )

    async def _async_remote_wake(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REMOTE_OTA_WAKE_TIMEOUT
        baseline_ads = int(self.data.get("ads", 0))
        self.data.update(wake_attempts=0, wake_error=None)

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self.data.update(
                status="starting", phase="waiting_window", progress=0
            )
            self._notify()
            seen = await self._async_wait_bt(
                lambda: int(self.data.get("ads", 0)) > baseline_ads
                or bool(self.data.get("ota_mode")),
                remaining,
            )
            if not seen:
                break
            if self.data.get("ota_mode"):
                return
            baseline_ads = int(self.data.get("ads", 0))
            self.data["wake_attempts"] = int(self.data.get("wake_attempts", 0)) + 1
            ota_baseline = int(self.data.get("ota_ads", 0))
            self.data.update(status="starting", phase="connecting_control")
            self._notify()
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait_for(
                    async_request_ota(
                        self.hass, self.address, timeout=remaining
                    ),
                    remaining,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retry at the next window
                self.data["wake_error"] = str(err)
                self._notify()
                if bool(self.data.get("ota_mode")) and int(
                    self.data.get("ota_ads", 0)
                ) > ota_baseline:
                    return
                continue

            self.data.update(status="starting", phase="requesting_ota")
            self._notify()
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self.data.update(status="starting", phase="waiting_ota_ad")
            self._notify()
            if await self._async_wait_for_ota_switch(
                ota_baseline, min(REMOTE_OTA_SWITCH_TIMEOUT, remaining)
            ):
                return
            self.data["wake_error"] = "已发送唤醒请求，但未收到新的 OTA 广播"
            self._notify()

        attempts = int(self.data.get("wake_attempts", 0))
        last_error = self.data.get("wake_error")
        message = f"等待普通广播唤醒窗口超时（{attempts} 次连接尝试）"
        if last_error:
            message += f"；最后错误：{last_error}"
        raise OtaError(message)

    async def _async_run_ota(
        self, image: bytes, target: str | None, wake_required: bool = False
    ) -> None:
        entered_ota = not wake_required
        try:
            self._reboot_start_seq = None
            if wake_required:
                await self._async_remote_wake()
                entered_ota = True
                pending_threshold = self._threshold_pending_value()
                if pending_threshold is not None:
                    await self._async_write_threshold(
                        pending_threshold, fresh_services=True
                    )
            # Chunks must be multiples of 16 so the WCH addr16 field stays aligned.
            requested = self._requested_chunk
            chunks: list[int | None] = [requested] if requested else [None]
            for candidate in (64, 32, 16):
                if candidate not in chunks and (
                    requested is None or candidate < requested
                ):
                    chunks.append(candidate)

            uploaded = False
            # The device stays in OTA mode for about 5 minutes after a dropped link,
            # and a full 128-byte upload now takes ~35 s, so retrying the whole
            # upload is cheaper than giving up.
            for attempt in range(1, 4):
                for chunk in chunks:
                    client = Ch592OtaClient(
                        self.hass,
                        self.address,
                        image,
                        self._on_progress,
                        self._on_ota_info,
                        fresh_services=wake_required,
                    )
                    self._client = client
                    try:
                        async with self._ble_lock:
                            if self._stopped:
                                raise HomeAssistantError("设备配置已卸载")
                            await asyncio.wait_for(
                                client.async_run(chunk), timeout=1800
                            )
                        uploaded = True
                        break
                    except OtaMtuError as err:
                        _LOGGER.warning(
                            "OTA 写入失败，尝试更小的分片 (chunk=%s): %s", chunk, err
                        )
                        self.data["error"] = str(err)
                        self._notify()
                        await asyncio.sleep(2)
                    except OtaError as err:
                        self.data["error"] = str(err)
                        self._notify()
                        # Only a drop *after* data went out is worth a full
                        # re-upload; a failed connection just wastes minutes.
                        made_progress = int(self.data.get("progress") or 0) > 10
                        if (
                            not made_progress
                            or not self._is_retryable(err)
                            or attempt >= 3
                        ):
                            raise
                        _LOGGER.warning(
                            "OTA 第 %s 次中断（%s），准备整体重传", attempt, err
                        )
                        await asyncio.sleep(2)
                        break
                if uploaded:
                    break
            if not uploaded:
                raise RuntimeError(str(self.data.get("error") or "OTA 上传失败"))

            start_seq = self._reboot_start_seq
            if start_seq is None:
                raise RuntimeError("OTA 客户端未提供 END 前广播基线")
            self.data.update(status="rebooting", phase="waiting_reboot", progress=98)
            self._notify()

            if await self._async_wait_for_reboot(start_seq, target):
                self.data.update(status="success", phase="done", progress=100, error=None)
            else:
                last = self.data.get("firmware_version")
                raise RuntimeError(
                    f"固件已上传，但 {REBOOT_TIMEOUT}s 内未看到目标版本 "
                    f"(期望 {target or '任意'}，最后看到 {last or '无'})"
                )
        except asyncio.CancelledError:
            self.data.update(status="aborted", phase="aborted")
            self._notify()
            raise
        except Exception as err:  # noqa: BLE001 - report every failure to the user
            _LOGGER.exception("CH592 OTA 失败")
            message = str(err)
            lowered = message.lower()
            if entered_ota and ("connect" in lowered or "未找到可连接" in message):
                message += "（设备已退出 OTA 模式，请重新长按 B 键 3 秒后重试）"
            self.data.update(status="failed", phase="failed", error=message)
            self._notify()
        finally:
            if self._run_started is not None:
                self.data["duration_s"] = round(
                    asyncio.get_running_loop().time() - self._run_started, 1
                )
                self._run_started = None
            self._client = None
            self._task = None
            self._notify()

    async def _async_wait_for_reboot(
        self, start_seq: int, target: str | None
    ) -> bool:
        """Require an advertisement newer than the immediate pre-END baseline.

        The version check alone is insufficient for a same-version reinstall:
        the cached value already equals the target before END is sent.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REBOOT_TIMEOUT
        while loop.time() < deadline:
            if (
                int(self.data["ads"]) > start_seq
                and (target is None or self.data["firmware_version"] == target)
            ):
                return True
            await asyncio.sleep(1)
        return False
