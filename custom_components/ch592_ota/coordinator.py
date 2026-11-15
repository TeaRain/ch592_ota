"""Runtime state and OTA task management for one CH592 device."""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .ble_ota import Ch592OtaClient, OtaError, OtaMtuError
from .bthome import has_bthome_data, parse_bthome_version
from .const import (
    CONF_AUTO_INSTALL,
    CONF_FIRMWARE,
    CONF_VERSION,
    DEFAULT_AUTO_INSTALL,
    DEFAULT_FIRMWARE,
    IMAGE_SIZE,
    OTA_SERVICE_UUID,
    REBOOT_TIMEOUT,
)
from .firmware import (
    FirmwareCloudCoordinator,
    async_download_firmware,
    is_newer,
    manifest_version,
)

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[], None]


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

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        self._unregister_bt = async_register_callback(
            self.hass,
            self._async_bt_callback,
            {"address": self.address},
            BluetoothScanningMode.ACTIVE,
        )

    async def async_stop(self) -> None:
        if self._unregister_bt is not None:
            self._unregister_bt()
            self._unregister_bt = None
        await self.async_abort(quiet=True)

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
            if not self.data["ota_mode"]:
                _LOGGER.info(
                    "检测到 OTA 广播 (uuids=%s data=%s)",
                    list(service_info.service_uuids),
                    list(service_info.service_data),
                )
                self.data["ota_mode"] = True
                self._notify()
                self._maybe_auto_install()
        else:
            version = parse_bthome_version(service_info)
            if version is not None:
                self._seen_bthome = True
                self.data["firmware_version"] = version
                self.data["ads"] = int(self.data["ads"]) + 1
                self.data["last_seen"] = datetime.now(UTC)
                self.data["ota_mode"] = False
                self._notify()

        # Throttled refresh so the diagnostic attributes stay current.
        now = asyncio.get_running_loop().time()
        if now - self._last_diag_notify >= 5:
            self._last_diag_notify = now
            self._notify()

    @staticmethod
    def _is_ota_advertisement(service_info: BluetoothServiceInfoBleak) -> bool:
        """Recognise the OTA firmware advertisement.

        Neither the name nor the UUID list is reliable on its own: HA caches
        "GasMeter OTA" as the device name, and the proxy keeps reporting
        0xFFE0/0xFEE0 as *service UUIDs* of normal BTHome broadcasts (left over
        from a GATT discovery during an earlier OTA). The BTHome 0xFCD2 service
        data is the trustworthy marker: the OTA firmware never sends it.
        """
        has_ota_uuid = any(
            u.lower() == OTA_SERVICE_UUID for u in service_info.service_uuids
        ) or any(
            u.lower() in (OTA_SERVICE_UUID, "fee0")
            for u in service_info.service_data
        )
        return has_ota_uuid and not has_bthome_data(service_info)

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
                except Exception:  # noqa: BLE001 - retried below
                    _LOGGER.exception("自动升级第 %s 次尝试失败", attempt)
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
        if self._task is not None and not self._task.done():
            raise HomeAssistantError("已有 OTA 任务正在运行")
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

        self._task = self.hass.async_create_task(
            self._async_run_ota(image, target), name=f"ch592_ota {self.address}"
        )
        return {"status": "started", "version": target, "size": len(image)}

    async def async_abort(self, quiet: bool = False) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
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
        for key in ("chunk", "verify_chunk", "mtu"):
            if info.get(key) is not None:
                self.data[key] = info[key]
        self._notify()

    def _on_progress(
        self, percent: int, phase: str, done: int, total: int
    ) -> None:
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

    async def _async_run_ota(self, image: bytes, target: str | None) -> None:
        try:
            # Chunks must be multiples of 16 so the WCH addr16 field stays aligned.
            requested = self._requested_chunk
            chunks: list[int | None] = [requested] if requested else [None]
            for candidate in (64, 32, 16):
                if candidate not in chunks and (
                    requested is None or candidate < requested
                ):
                    chunks.append(candidate)

            uploaded = False
            # The device stays in OTA mode for 8 minutes after a dropped link,
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
                    )
                    self._client = client
                    try:
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

            start_seq = int(self.data["ads"])
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
            if "connect" in lowered or "未找到可连接" in message:
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
        """Wait until the device advertises again after the reboot.

        A device reporting the target version is conclusive on its own; waiting
        for one *more* advertisement made a finished OTA sit at 98 % until the
        next 60 s broadcast, because the reboot advertisement is often already
        counted before ``start_seq`` is taken.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REBOOT_TIMEOUT
        while loop.time() < deadline:
            if target is not None and self.data["firmware_version"] == target:
                return True
            if target is None and int(self.data["ads"]) > start_seq:
                return True
            await asyncio.sleep(1)
        return False
