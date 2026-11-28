"""Poll the published firmware manifest and download new images.

The firmware repository (see tools/publish_firmware.py) exposes a small JSON
manifest next to the OTA image:

    {
      "version": "1.2.5",
      "file": "ch592_ota.bin",
      "size": 155808,
      "crc32": "0xA1D19F5F",
      "sha256": "...",
      "built": "2026-11-15T11:02:56+00:00",
      "notes": "1.2.5: ..."
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import zlib
from datetime import timedelta
from urllib.parse import urljoin

import aiohttp
from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_MANIFEST_URLS,
    KNOWN_MANIFEST_URLS,
    MANIFEST_UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

Manifest = dict[str, object]


def resolve_manifest_urls(configured: str | None) -> list[str]:
    """Expand managed built-in URLs while preserving custom endpoints."""
    value = str(configured or "").strip()
    if not value or value in KNOWN_MANIFEST_URLS:
        return list(DEFAULT_MANIFEST_URLS)
    return [value]


def build_download_urls(
    manifest_urls: list[str], selected_url: str, file_name: str
) -> list[str]:
    """Build same-image candidates, starting with the responding mirror."""
    urls = [urljoin(selected_url, file_name)]
    for manifest_url in manifest_urls:
        candidate = urljoin(manifest_url, file_name)
        if candidate not in urls:
            urls.append(candidate)
    return urls


class FirmwareCloudCoordinator(DataUpdateCoordinator[Manifest]):
    """Fetch the firmware manifest on a timer."""

    def __init__(
        self, hass: HomeAssistant, urls: list[str], entry: ConfigEntry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="ch592_ota firmware manifest",
            update_interval=timedelta(minutes=MANIFEST_UPDATE_INTERVAL_MINUTES),
        )
        self.urls = urls
        self.url = urls[0]

    async def _async_update_data(self) -> Manifest:
        errors: list[str] = []
        for url in self.urls:
            try:
                async with asyncio.timeout(30):
                    async with async_get_clientsession(self.hass).get(url) as resp:
                        if resp.status != 200:
                            errors.append(f"{url}: HTTP {resp.status}")
                            continue
                        payload = await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                errors.append(f"{url}: {err}")
                continue

            if not isinstance(payload, dict) or not payload.get("version"):
                errors.append(f"{url}: 缺少 version 字段")
                continue
            self.url = url
            payload["download_urls"] = build_download_urls(
                self.urls, url, str(payload.get("file") or "ch592_ota.bin")
            )
            payload["download_url"] = payload["download_urls"][0]
            _LOGGER.debug("固件清单: %s", payload)
            return payload

        raise UpdateFailed("读取固件清单失败: " + "; ".join(errors))


def manifest_version(manifest: Manifest | None) -> str | None:
    if not manifest:
        return None
    version = manifest.get("version")
    return str(version) if version else None


def is_newer(latest: str | None, installed: str | None) -> bool:
    """Return True when latest should be offered as an upgrade."""
    if not latest:
        return False
    if not installed:
        return True
    try:
        return AwesomeVersion(latest) > AwesomeVersion(installed)
    except Exception:  # noqa: BLE001 - non-semver firmware strings
        return latest != installed


async def async_download_firmware(
    hass: HomeAssistant, manifest: Manifest
) -> bytes:
    """Download one manifest's image, trying mirrors with identical metadata."""
    configured_urls = manifest.get("download_urls")
    if isinstance(configured_urls, (list, tuple)):
        urls = list(
            dict.fromkeys(
                str(url) for url in configured_urls if str(url or "").strip()
            )
        )
    else:
        urls = []
    if not urls:
        url = str(manifest.get("download_url") or "")
        urls = [url] if url else []
    if not urls:
        raise HomeAssistantError("固件清单里没有可下载的地址")

    expected_size_raw = manifest.get("size")
    expected_size: int | None = None
    if expected_size_raw is not None:
        try:
            expected_size = int(expected_size_raw)
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(
                f"固件清单 size 字段无效: {expected_size_raw!r}"
            ) from err

    errors: list[str] = []
    for url in urls:
        try:
            async with asyncio.timeout(120):
                async with async_get_clientsession(hass).get(url) as resp:
                    if resp.status != 200:
                        raise HomeAssistantError(f"HTTP {resp.status}")
                    image = await resp.read()

            if expected_size is not None and len(image) != expected_size:
                raise HomeAssistantError(
                    f"固件大小不符: 期望 {expected_size}, 实际 {len(image)}"
                )

            crc32 = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
            expected_crc = manifest.get("crc32")
            if expected_crc and str(expected_crc).upper() != crc32.upper():
                raise HomeAssistantError(
                    f"固件 CRC32 不符: 清单 {expected_crc}, 实际 {crc32}"
                )

            expected_sha = manifest.get("sha256")
            if expected_sha:
                actual_sha = hashlib.sha256(image).hexdigest()
                if str(expected_sha).lower() != actual_sha:
                    raise HomeAssistantError("固件 SHA-256 不符")
            return image
        except (HomeAssistantError, TimeoutError, aiohttp.ClientError) as err:
            errors.append(f"{url}: {err}")

    raise HomeAssistantError("所有固件下载地址均失败: " + "; ".join(errors))
