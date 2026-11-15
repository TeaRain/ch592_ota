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

from .ble_ota import Ch592OtaClient, OtaMtuError
from .bthome import parse_bthome_version
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

        version = parse_bthome_version(service_info)
        if version is not None:
            self.data["firmware_version"] = version
            self.data["ads"] = int(self.data["ads"]) + 1
            self.data["last_seen"] = datetime.now(UTC)
            self.data["ota_mode"] = False
            self._notify()

        elif not self.data["ota_mode"] and self._is_ota_advertisement(service_info):
            _LOGGER.info(
                "检测到 OTA 广播 (name=%s, uuids=%s)",
                service_info.name,
                list(service_info.service_uuids),
            )
            self.data["ota_mode"] = True
            self._notify()
            self._maybe_auto_install()

        # Throttled refresh so the diagnostic attributes stay current.
        now = asyncio.get_running_loop().time()
        if now - self._last_diag_notify >= 5:
            self._last_diag_notify = now
            self._notify()

    @staticmethod
    def _is_ota_advertisement(service_info: BluetoothServiceInfoBleak) -> bool:
        """Recognise the OTA firmware advertisement.

        The advertisement carries 0xFEE0 (and 0xFFE0), but the name from the
        scan response is checked too so detection does not depend on the proxy
        reporting the UUID list.
        """
        name = (service_info.name or "").strip()
        if "ota" in name.lower():
            return True
        if any(u.lower() == OTA_SERVICE_UUID for u in service_info.service_uuids):
            return True
        return any(
            u.lower() in (OTA_SERVICE_UUID, "fee0")
            for u in service_info.service_data
        )

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
        """
        if not self.auto_install:
            return
        if self._task is not None and not self._task.done():
            return
        if self._auto_task is not None and not self._auto_task.done():
            return

        manifest = self.cloud.data if self.cloud else None
        if not isinstance(manifest, dict):
            _LOGGER.debug("OTA 模式已进入，但没有固件清单，跳过自动升级")
            return
        version = manifest_version(manifest)
        installed = self.data.get("firmware_version")
        installed_str = str(installed) if installed else None
        if not is_newer(version, installed_str):
            _LOGGER.debug(
                "OTA 模式已进入，但没有新固件 (设备 %s, 最新 %s)",
                installed_str,
                version,
            )
            return

        _LOGGER.info(
            "检测到 OTA 模式且有新固件 %s（当前 %s），自动开始升级",
            version,
            installed_str,
        )
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
        self.data.update(
            status="starting",
            phase="starting",
            progress=0,
            uploaded=0,
            total=len(image),
            error=None,
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

    async def _async_run_ota(self, image: bytes, target: str | None) -> None:
        try:
            client: Ch592OtaClient | None = None
            # Chunks must be multiples of 16 so the WCH addr16 field stays aligned.
            requested = self._requested_chunk
            chunks: list[int | None] = [requested] if requested else [None]
            for candidate in (64, 32, 16):
                if candidate not in chunks and (
                    requested is None or candidate < requested
                ):
                    chunks.append(candidate)
            for chunk in chunks:
                client = Ch592OtaClient(
                    self.hass, self.address, image, self._on_progress
                )
                self._client = client
                try:
                    # 64-byte chunks need ~6900 write+read round trips for 216 KiB;
                    # through an ESPHome proxy that can take 10+ minutes.
                    await asyncio.wait_for(client.async_run(chunk), timeout=1800)
                    break
                except OtaMtuError as err:
                    _LOGGER.warning(
                        "OTA 写入失败，尝试更小的分片 (chunk=%s): %s", chunk, err
                    )
                    self.data["error"] = str(err)
                    self._notify()
                    await asyncio.sleep(2)
            else:
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
            self.data.update(status="failed", phase="failed", error=str(err))
            self._notify()
        finally:
            self._client = None
            self._task = None
            self._notify()

    async def _async_wait_for_reboot(
        self, start_seq: int, target: str | None
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REBOOT_TIMEOUT
        while loop.time() < deadline:
            if int(self.data["ads"]) > start_seq:
                if target is None or self.data["firmware_version"] == target:
                    return True
            await asyncio.sleep(1)
        return False
