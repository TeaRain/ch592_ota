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
    CONF_FIRMWARE,
    CONF_VERSION,
    DEFAULT_FIRMWARE,
    IMAGE_SIZE,
    OTA_SERVICE_UUID,
    REBOOT_TIMEOUT,
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
        version = parse_bthome_version(service_info)
        if version is not None:
            self.data["firmware_version"] = version
            self.data["ads"] = int(self.data["ads"]) + 1
            self.data["last_seen"] = datetime.now(UTC)
            self.data["ota_mode"] = False
            self._notify()

        elif not self.data["ota_mode"] and any(
            uuid.lower() == OTA_SERVICE_UUID for uuid in service_info.service_uuids
        ):
            self.data["ota_mode"] = True
            self._notify()

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
    ) -> dict[str, object]:
        if self._task is not None and not self._task.done():
            raise HomeAssistantError("已有 OTA 任务正在运行")
        if chunk is not None:
            chunk = int(chunk)
            if chunk % 16 or not 16 <= chunk <= 64:
                raise HomeAssistantError("chunk 必须是 16~64 之间、16 的倍数")
        self._requested_chunk = chunk

        path = self.resolve_firmware_path(firmware)
        try:
            image, meta = await self.hass.async_add_executor_job(
                self._read_image, path
            )
        except OSError as err:
            raise HomeAssistantError(f"无法读取固件 {path}: {err}") from err

        if not image:
            raise HomeAssistantError(f"固件文件为空: {path}")
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
