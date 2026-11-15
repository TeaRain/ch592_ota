"""WCH backup-OTA client for the CH592 gas-meter firmware.

Protocol (service 0xFEE0, characteristic 0xFEE1):

  INFO    [0x84, 0x00]                       -> 20-byte device info
  ERASE   [0x81, 0x00, addr16_lo, addr16_hi, blocks_lo, blocks_hi]
                                             -> 2-byte status when finished
  PROGRAM [0x80, len, addr16_lo, addr16_hi, data...]
                                             -> 2-byte status
  VERIFY  [0x82, len, addr16_lo, addr16_hi, data...]
                                             -> 2-byte status
  END     [0x83, 0x00]                       -> device resets

Addresses are expressed in 16-byte units and start at 0x1000 (Image A base).
The OTA application adds IMAGE_A_SIZE (216 KiB) so writes land in Image B.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import Callable

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant

from .const import (
    CMD_IAP_END,
    CMD_IAP_ERASE,
    CMD_IAP_INFO,
    CMD_IAP_PROM,
    CMD_IAP_VERIFY,
    DEFAULT_CHUNK,
    ERASE_TIMEOUT,
    IMAGE_A_START,
    IMAGE_SIZE,
    MIN_CHUNK,
    OTA_CHAR_UUID,
    OTA_CHUNK_MAX,
    OTA_FEATURE_CRC32,
    OTA_FEATURE_PAGE_SPLIT,
    OTA_IMAGE_MAGIC,
    OTA_STATUS_OK,
    PROGRAM_STATUS_WINDOW,
    STATUS_TIMEOUT,
    VERIFY_CHUNK_MAX,
    CMD_IAP_CRC32,
)

_LOGGER = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, int, int], None]


class OtaError(Exception):
    """Base OTA error."""


class OtaMtuError(OtaError):
    """A GATT write failed, possibly because the payload was too large."""


def _le16(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _le32(data: bytes) -> int:
    return int.from_bytes(data, "little")


class Ch592OtaClient:
    """Perform a WCH OTA upload over an existing BLE connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        image: bytes,
        progress_cb: ProgressCallback,
        info_cb: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if len(image) > IMAGE_SIZE - 16:
            raise OtaError("固件超过 APP 区可用大小")
        padded = bytearray(image)
        padded.extend(b"\xFF" * (IMAGE_SIZE - 16 - len(padded)))
        padded.extend(OTA_IMAGE_MAGIC.to_bytes(4, "little"))
        padded.extend(b"\xFF" * 12)

        self._hass = hass
        self._address = address
        self._image = bytes(padded)
        self._progress = progress_cb
        self._info_cb = info_cb
        self._client: BleakClient | None = None
        self._char = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._chunk = DEFAULT_CHUNK
        self._verify_chunk = VERIFY_CHUNK_MAX
        # Whether unacknowledged writes still work; disabled permanently once
        # the proxy/stack rejects one.
        self._no_rsp_ok = True
        # Filled in from the device INFO response (None when unavailable).
        self.vdd_mv: int | None = None

    # ------------------------------------------------------------------ public

    async def async_run(self, chunk: int | None = None) -> None:
        """Upload the image. Returns after the END command has been sent."""
        self._loop = asyncio.get_running_loop()
        self._notify(1, "connecting")

        device = async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if device is None:
            raise OtaError(
                "未找到可连接的设备。请长按 B 键 3 秒进入 OTA 模式，并确认蓝牙代理在线。"
            )

        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self._address,
                max_attempts=3,
                timeout=30.0,
            )
            self._char = self._find_ota_char()
            requested = (
                chunk
                if chunk is not None
                and chunk % 16 == 0
                and MIN_CHUNK <= chunk <= OTA_CHUNK_MAX
                else None
            )
            self._chunk = requested or self._pick_chunk()
            self._verify_chunk = min(VERIFY_CHUNK_MAX, self._pick_chunk_max())
            mtu = self._get_mtu()

            info = await self._async_info()
            self.vdd_mv = info.get("vdd") or None
            # Only firmware that splits PROGRAM writes at flash pages can take
            # chunks larger than the 256-byte page limit safely.
            if requested is None:
                if info.get("features", 0) & OTA_FEATURE_PAGE_SPLIT:
                    self._chunk = min(OTA_CHUNK_MAX, self._pick_chunk_max())
                else:
                    self._chunk = self._pick_chunk()
            if self._info_cb is not None:
                try:
                    self._info_cb(
                        {
                            **info,
                            "chunk": self._chunk,
                            "verify_chunk": self._verify_chunk,
                            "mtu": mtu,
                        }
                    )
                except Exception:  # noqa: BLE001 - diagnostics must never abort
                    _LOGGER.exception("OTA info callback failed")
            _LOGGER.debug(
                "OTA connected to %s, mtu=%s, chunk=%s verify_chunk=%s "
                "features=0x%02X proto=%s",
                self._address,
                mtu,
                self._chunk,
                self._verify_chunk,
                info.get("features", 0),
                info.get("protocol"),
            )
            self._notify(
                2,
                f"connected mtu={mtu} chunk={self._chunk} "
                f"verify={self._verify_chunk}",
            )

            image_size = info["image_size"]
            block_size = info["block_size"]
            if image_size != IMAGE_SIZE:
                raise OtaError(
                    f"设备镜像区大小异常: {image_size} (期望 {IMAGE_SIZE})"
                )

            self._notify(6, "erasing")
            await self._async_erase(image_size, block_size)

            self._notify(10, "uploading", 0, len(self._image))
            await self._async_program()

            if info.get("features", 0) & OTA_FEATURE_CRC32:
                try:
                    await self._async_crc32_verify()
                except OtaError as err:
                    # Either the image really is corrupt or the device's CRC
                    # disagrees with ours; the byte compare is authoritative and
                    # reports the first bad offset, so a broken CRC command can
                    # never become a dead end.
                    _LOGGER.warning("CRC32 校验未通过，退回逐字节校验: %s", err)
                    self._notify(86, "verifying", 0, len(self._image))
                    await self._async_verify()
            else:
                self._notify(86, "verifying", 0, len(self._image))
                await self._async_verify()

            self._notify(97, "rebooting")
            await self._async_end()
        finally:
            self._char = None
            if self._client is not None and self._client.is_connected:
                try:
                    await asyncio.wait_for(self._client.disconnect(), 10)
                except (TimeoutError, BleakError, EOFError):
                    pass

    async def async_disconnect(self) -> None:
        """Force a disconnect (used by abort)."""
        if self._client is not None and self._client.is_connected:
            try:
                await asyncio.wait_for(self._client.disconnect(), 10)
            except (TimeoutError, BleakError, EOFError):
                pass

    # ----------------------------------------------------------------- helpers

    def _notify(self, percent: int, phase: str, done: int = 0, total: int = 0) -> None:
        try:
            self._progress(percent, phase, done, total)
        except Exception:  # noqa: BLE001 - never let UI callbacks break the OTA
            _LOGGER.exception("OTA progress callback failed")

    def _find_ota_char(self):
        assert self._client is not None
        services = self._client.services
        if services is None:
            raise OtaError("未能发现 GATT 服务")
        for service in services:
            for char in service.characteristics:
                if char.uuid.lower() == OTA_CHAR_UUID:
                    return char
        raise OtaError("设备上没有找到 OTA 特征值，请确认已进入 OTA 模式")

    def _get_mtu(self) -> int:
        assert self._client is not None
        try:
            mtu = int(self._client.mtu_size)
        except (AttributeError, NotImplementedError, BleakError, OSError):
            mtu = 247
        return max(23, mtu)

    def _pick_chunk_max(self) -> int:
        """Largest payload the link can carry (ATT MTU - headers), 16-aligned."""
        usable = self._get_mtu() - 3 - 4  # ATT header + WCH command header
        return max(MIN_CHUNK, (usable // 16) * 16)

    def _pick_chunk(self) -> int:
        return max(MIN_CHUNK, min(DEFAULT_CHUNK, self._pick_chunk_max()))

    async def _write(self, payload: bytes, response: bool = True) -> None:
        """Write one ATT packet.

        ``response=False`` is used for the bulk PROGRAM phase: it saves one
        round trip per chunk, and the device still processes the write before
        the next one arrives (GATT writes are serialised in the stack). If the
        proxy or stack refuses unacknowledged writes we fall back to the
        acknowledged form for the rest of the upload.
        """
        assert self._client is not None and self._char is not None
        if not self._client.is_connected:
            raise OtaError("蓝牙连接已断开")
        use_response = response or not self._no_rsp_ok
        try:
            await self._client.write_gatt_char(
                self._char, payload, response=use_response
            )
            return
        except (BleakError, EOFError) as err:
            if use_response:
                raise OtaMtuError(f"写入失败: {err}") from err
            _LOGGER.debug("无响应写入被拒绝，后续改用带响应写入: %s", err)
            self._no_rsp_ok = False
        try:
            await self._client.write_gatt_char(self._char, payload, response=True)
        except (BleakError, EOFError) as err:
            raise OtaMtuError(f"写入失败: {err}") from err

    async def _read_response(self, timeout: float) -> bytes:
        assert self._client is not None and self._char is not None
        assert self._loop is not None
        deadline = self._loop.time() + timeout
        while self._loop.time() < deadline:
            try:
                data = await self._client.read_gatt_char(self._char)
            except (BleakError, EOFError, TimeoutError) as err:
                raise OtaMtuError(f"读取失败/连接中断: {err}") from err
            if data:
                return bytes(data)
            await asyncio.sleep(0.05)
        raise OtaError("等待设备响应超时")

    async def _read_status(
        self, timeout: float = STATUS_TIMEOUT, retries: int = 2
    ) -> None:
        """Read the 2-byte command status, tolerating a transient read error."""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if attempt:
                # Give a sagging coin cell a moment to recover.
                await asyncio.sleep(0.2 * attempt)
            try:
                response = await self._read_response(timeout)
            except OtaMtuError as err:
                last_error = err
                _LOGGER.debug("状态读取失败（第 %s 次）: %s", attempt + 1, err)
                continue
            if len(response) < 2:
                raise OtaError(f"响应长度异常: {response.hex()}")
            if response[0] != OTA_STATUS_OK:
                raise OtaError(f"设备返回错误状态 0x{response[0]:02X}")
            return
        raise last_error or OtaError("读取设备状态失败")

    # --------------------------------------------------------------- commands

    async def _async_info(self) -> dict[str, int]:
        await self._write(bytes((CMD_IAP_INFO, 0)))
        response = await self._read_response(STATUS_TIMEOUT)
        if len(response) < 9:
            raise OtaError(f"设备信息响应异常: {response.hex()}")
        info = {
            "image_flag": response[0],
            "image_size": _le32(response[1:5]),
            "block_size": int.from_bytes(response[5:7], "little"),
            "chip_id": int.from_bytes(response[7:9], "little"),
            # Firmware >= 1.2.9 reports capabilities; older ones send zeros.
            "features": response[9] if len(response) >= 10 else 0,
            "protocol": response[10] if len(response) >= 11 else 1,
            # Firmware >= 1.3.0 reports the supply voltage measured in OTA mode.
            "vdd": (
                int.from_bytes(response[11:13], "little")
                if len(response) >= 13
                else 0
            ),
        }
        _LOGGER.debug("OTA info: %s", info)
        return info

    async def _async_erase(self, image_size: int, block_size: int) -> None:
        if block_size <= 0:
            raise OtaError("设备返回的 Flash 块大小无效")
        blocks = (image_size + block_size - 1) // block_size
        addr16 = IMAGE_A_START >> 4
        payload = bytes((CMD_IAP_ERASE, 0)) + _le16(addr16) + _le16(blocks)
        await self._write(payload)
        await self._read_status(ERASE_TIMEOUT)
        _LOGGER.debug("OTA erased %s blocks of %s bytes", blocks, block_size)

    async def _async_program(self) -> None:
        """Stream the image.

        Each chunk is written without waiting for an ATT response and the
        status is only read once per ``PROGRAM_STATUS_WINDOW`` chunks — the
        device serialises GATT writes, so ordering is preserved and a missed
        flash write is still caught by the VERIFY pass.
        """
        total = len(self._image)
        chunk = self._chunk
        pending = 0
        for offset in range(0, total, chunk):
            data = self._image[offset : offset + chunk]
            if len(data) % 4:
                data += b"\xFF" * (4 - len(data) % 4)
            addr16 = (IMAGE_A_START + offset) >> 4
            payload = (
                bytes((CMD_IAP_PROM, len(data))) + _le16(addr16) + data
            )
            await self._write(payload, response=False)
            pending += 1
            done = min(offset + len(data), total)
            if pending >= PROGRAM_STATUS_WINDOW or done >= total:
                await self._read_status()
                pending = 0
            percent = 10 + (done * 75) // total
            self._notify(percent, "uploading", done, total)

    async def _async_crc32_verify(self) -> None:
        """Ask the device for the CRC32 of Image B — one round trip.

        This replaces the byte-by-byte VERIFY pass (which re-sends the whole
        image and is therefore ~half of the total airtime).
        """
        expected = zlib.crc32(self._image) & 0xFFFFFFFF
        self._notify(90, "verifying", 0, len(self._image))
        await self._write(bytes((CMD_IAP_CRC32, 0)))
        response = await self._read_response(ERASE_TIMEOUT)
        if len(response) < 6:
            raise OtaError(f"CRC32 响应异常: {response.hex()}")
        device_crc = _le32(response[0:4])
        _LOGGER.debug("OTA crc32 device=0x%08X client=0x%08X", device_crc, expected)
        if device_crc != expected:
            raise OtaError(
                f"校验失败: 设备 0x{device_crc:08X}, 期望 0x{expected:08X}"
            )
        self._notify(96, "verified", len(self._image), len(self._image))

    async def _async_verify(self) -> None:
        """Read the written image back and compare it.

        VERIFY only compares RAM against flash, so it is not limited by the
        256-byte page constraint and can use much larger chunks. The status is
        read after every chunk: a mismatch must never be masked by the next
        chunk's status.
        """
        total = len(self._image)
        chunk = max(MIN_CHUNK, min(self._verify_chunk, self._pick_chunk_max()))
        for offset in range(0, total, chunk):
            data = self._image[offset : offset + chunk]
            if len(data) % 4:
                data += b"\xFF" * (4 - len(data) % 4)
            addr16 = (IMAGE_A_START + offset) >> 4
            payload = (
                bytes((CMD_IAP_VERIFY, len(data))) + _le16(addr16) + data
            )
            # Acknowledged write: the ATT response is only produced after the
            # device has compared the chunk, so the status read cannot be stale.
            await self._write(payload)
            try:
                await self._read_status(ERASE_TIMEOUT)
            except OtaError as err:
                raise OtaError(f"校验失败 @0x{offset:05X}: {err}") from err
            done = min(offset + len(data), total)
            percent = 86 + (done * 10) // total
            self._notify(percent, "verifying", done, total)

    async def _async_end(self) -> None:
        assert self._client is not None and self._char is not None
        try:
            await self._client.write_gatt_char(
                self._char, bytes((CMD_IAP_END, 0)), response=True
            )
        except (BleakError, EOFError):
            # The device resets immediately after CMD_IAP_END.
            pass
