from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_gaspulse_firmware_migration_test_pkg"

OLD_GITEE = (
    "https://gitee.com/zigbridge/ch592-gas-broadcaster"
    "/raw/master/firmware/manifest.json"
)
OLD_GITHUB = (
    "https://raw.githubusercontent.com/TeaRain/ch592-gas-broadcaster"
    "/master/firmware/manifest.json"
)
NEW_GITEE = (
    "https://gitee.com/zigbridge/gaspulse-firmware"
    "/raw/master/firmware/manifest.json"
)
NEW_GITHUB = (
    "https://raw.githubusercontent.com/TeaRain/gaspulse-firmware"
    "/master/firmware/manifest.json"
)
MANIFEST_URLS = [NEW_GITHUB, NEW_GITEE, OLD_GITEE, OLD_GITHUB]


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_firmware_module():
    class ClientError(Exception):
        pass

    class HomeAssistantError(Exception):
        pass

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    package = _module(PACKAGE)
    package.__path__ = [str(REPO / "custom_components/ch592_ota")]
    stubs = {
        "aiohttp": _module("aiohttp", ClientError=ClientError),
        "awesomeversion": _module("awesomeversion", AwesomeVersion=object),
        "homeassistant": _module("homeassistant"),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None,
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=type("UpdateFailed", (Exception,), {}),
        ),
        PACKAGE: package,
    }
    with patch.dict(sys.modules, stubs):
        const = _load(
            f"{PACKAGE}.const", REPO / "custom_components/ch592_ota/const.py"
        )
        firmware = _load(
            f"{PACKAGE}.firmware", REPO / "custom_components/ch592_ota/firmware.py"
        )
    return const, firmware


CONST, FIRMWARE = _load_firmware_module()


class FakeResponse:
    def __init__(self, status=200, body=b"", payload=None):
        self.status = status
        self.body = body
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def read(self):
        return self.body

    async def json(self, content_type=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        response = self.responses.get(url, FakeResponse(status=404))
        if isinstance(response, Exception):
            raise response
        return response


def _manifest(image, urls):
    return {
        "version": "1.6.2",
        "file": "ch592_ota.bin",
        "download_urls": urls,
        "size": len(image),
        "crc32": f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}",
        "sha256": hashlib.sha256(image).hexdigest(),
    }


class FirmwareMigrationTests(unittest.TestCase):
    def test_resolve_manifest_urls_distinguishes_builtin_and_custom(self):
        for configured in (None, "", "  ", *MANIFEST_URLS):
            with self.subTest(configured=configured):
                self.assertEqual(
                    FIRMWARE.resolve_manifest_urls(configured), MANIFEST_URLS
                )
        custom = "  https://example.com/custom.json  "
        self.assertEqual(
            FIRMWARE.resolve_manifest_urls(custom),
            ["https://example.com/custom.json"],
        )

    def test_build_download_urls_puts_selected_mirror_first(self):
        expected = [
            OLD_GITHUB.rsplit("/", 1)[0] + "/ch592_ota.bin",
            NEW_GITHUB.rsplit("/", 1)[0] + "/ch592_ota.bin",
            NEW_GITEE.rsplit("/", 1)[0] + "/ch592_ota.bin",
            OLD_GITEE.rsplit("/", 1)[0] + "/ch592_ota.bin",
        ]
        self.assertEqual(
            FIRMWARE.build_download_urls(
                MANIFEST_URLS, OLD_GITHUB, "ch592_ota.bin"
            ),
            expected,
        )

    def test_cloud_manifest_contains_download_candidates(self):
        coordinator = object.__new__(FIRMWARE.FirmwareCloudCoordinator)
        coordinator.hass = object()
        coordinator.urls = MANIFEST_URLS
        coordinator.url = MANIFEST_URLS[0]
        session = FakeSession(
            {
                OLD_GITEE: FakeResponse(payload={"error": "offline"}),
                OLD_GITHUB: FakeResponse(
                    payload={"version": "1.6.2", "file": "ch592_ota.bin"}
                ),
            }
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            payload = asyncio.run(coordinator._async_update_data())
        self.assertEqual(payload["version"], "1.6.2")
        self.assertEqual(
            payload["download_urls"],
            FIRMWARE.build_download_urls(
                MANIFEST_URLS, OLD_GITHUB, "ch592_ota.bin"
            ),
        )

    def test_download_retries_after_http_failure(self):
        image = b"valid-gaspulse-image"
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: FakeResponse(status=404), second: FakeResponse(body=image)}
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            result = asyncio.run(
                FIRMWARE.async_download_firmware(
                    object(), _manifest(image, [first, second])
                )
            )
        self.assertEqual(result, image)
        self.assertEqual(session.requests, [first, second])

    def test_download_retries_after_hash_failure(self):
        image = b"valid-gaspulse-image"
        corrupt = b"VALID-GASPULSE-IMAGE"
        self.assertEqual(len(corrupt), len(image))
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: FakeResponse(body=corrupt), second: FakeResponse(body=image)}
        )
        manifest = _manifest(image, [first, second])
        # Force both candidates through size and CRC so this test isolates SHA-256.
        with (
            patch.object(FIRMWARE, "async_get_clientsession", return_value=session),
            patch.object(
                FIRMWARE.zlib, "crc32", return_value=int(manifest["crc32"], 16)
            ),
        ):
            result = asyncio.run(
                FIRMWARE.async_download_firmware(object(), manifest)
            )
        self.assertEqual(result, image)
        self.assertEqual(session.requests, [first, second])

    def test_download_retries_after_crc_failure(self):
        image = b"valid-gaspulse-image"
        corrupt = b"VALID-GASPULSE-IMAGE"
        self.assertEqual(len(corrupt), len(image))
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: FakeResponse(body=corrupt), second: FakeResponse(body=image)}
        )
        manifest = _manifest(image, [first, second])
        del manifest["sha256"]
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            result = asyncio.run(
                FIRMWARE.async_download_firmware(object(), manifest)
            )
        self.assertEqual(result, image)
        self.assertEqual(session.requests, [first, second])

    def test_download_retries_after_timeout(self):
        image = b"valid-gaspulse-image"
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: TimeoutError(), second: FakeResponse(body=image)}
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            result = asyncio.run(
                FIRMWARE.async_download_firmware(
                    object(), _manifest(image, [first, second])
                )
            )
        self.assertEqual(result, image)
        self.assertEqual(session.requests, [first, second])

    def test_download_rejects_non_numeric_manifest_size_before_http(self):
        image = b"valid-gaspulse-image"
        url = "https://one.example/ch592_ota.bin"
        manifest = _manifest(image, [url])
        manifest["size"] = "not-a-number"
        session = FakeSession({url: FakeResponse(body=image)})
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            with self.assertRaises(FIRMWARE.HomeAssistantError) as caught:
                asyncio.run(FIRMWARE.async_download_firmware(object(), manifest))
        self.assertIn("size", str(caught.exception))
        self.assertEqual(session.requests, [])

    def test_download_never_accepts_different_metadata(self):
        image = b"valid-gaspulse-image"
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: FakeResponse(body=b"bad-one"), second: FakeResponse(body=b"bad-two")}
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            with self.assertRaises(FIRMWARE.HomeAssistantError):
                asyncio.run(
                    FIRMWARE.async_download_firmware(
                        object(), _manifest(image, [first, second])
                    )
                )
        self.assertEqual(session.requests, [first, second])

    def test_all_candidates_rejected_by_hash_raise_aggregated_error(self):
        image = b"valid-gaspulse-image"
        corrupt = b"VALID-GASPULSE-IMAGE"
        self.assertEqual(len(corrupt), len(image))
        first = "https://one.example/ch592_ota.bin"
        second = "https://two.example/ch592_ota.bin"
        session = FakeSession(
            {first: FakeResponse(body=corrupt), second: FakeResponse(body=corrupt)}
        )
        manifest = _manifest(image, [first, second])
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            with self.assertRaises(FIRMWARE.HomeAssistantError) as caught:
                asyncio.run(
                    FIRMWARE.async_download_firmware(object(), manifest)
                )
        self.assertEqual(session.requests, [first, second])
        message = str(caught.exception)
        self.assertIn(first, message)
        self.assertIn(second, message)

    def _new_cloud_coordinator(self):
        coordinator = object.__new__(FIRMWARE.FirmwareCloudCoordinator)
        coordinator.hass = object()
        coordinator.urls = MANIFEST_URLS
        coordinator.url = MANIFEST_URLS[0]
        return coordinator

    def test_cloud_coordinator_falls_back_after_timeout(self):
        coordinator = self._new_cloud_coordinator()
        session = FakeSession(
            {
                MANIFEST_URLS[0]: TimeoutError(),
                MANIFEST_URLS[1]: FakeResponse(
                    payload={"version": "1.6.2", "file": "ch592_ota.bin"}
                ),
            }
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            payload = asyncio.run(coordinator._async_update_data())
        self.assertEqual(payload["version"], "1.6.2")
        self.assertEqual(coordinator.url, MANIFEST_URLS[1])
        self.assertEqual(session.requests, [MANIFEST_URLS[0], MANIFEST_URLS[1]])

    def test_cloud_coordinator_falls_back_after_bad_json(self):
        coordinator = self._new_cloud_coordinator()
        session = FakeSession(
            {
                MANIFEST_URLS[0]: FakeResponse(payload=ValueError("bad json")),
                MANIFEST_URLS[1]: FakeResponse(payload={"version": "1.6.2"}),
            }
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            payload = asyncio.run(coordinator._async_update_data())
        self.assertEqual(payload["version"], "1.6.2")
        self.assertEqual(coordinator.url, MANIFEST_URLS[1])
        self.assertEqual(session.requests, [MANIFEST_URLS[0], MANIFEST_URLS[1]])

    def test_cloud_coordinator_all_urls_fail_raises_update_failed(self):
        coordinator = self._new_cloud_coordinator()
        session = FakeSession(
            {
                MANIFEST_URLS[0]: TimeoutError(),
                MANIFEST_URLS[1]: FakeResponse(payload=ValueError("bad json")),
            }
        )
        with patch.object(FIRMWARE, "async_get_clientsession", return_value=session):
            with self.assertRaises(FIRMWARE.UpdateFailed) as caught:
                asyncio.run(coordinator._async_update_data())
        message = str(caught.exception)
        self.assertIn(MANIFEST_URLS[0], message)
        self.assertIn(MANIFEST_URLS[1], message)
        self.assertEqual(session.requests, MANIFEST_URLS)

    def test_setup_does_not_rewrite_manifest_option_in_config_entry(self):
        init_source = (REPO / "custom_components/ch592_ota/__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("async_update_entry", init_source)
        custom = "https://example.invalid/custom-manifest.json"

        class FakeEntry:
            def __init__(self, manifest_option):
                self.options = (
                    {CONST.CONF_MANIFEST: manifest_option}
                    if manifest_option is not None
                    else {}
                )

        for entry, expected in [
            (FakeEntry(None), MANIFEST_URLS),
            (FakeEntry(""), MANIFEST_URLS),
            (FakeEntry(OLD_GITEE), MANIFEST_URLS),
            (FakeEntry(custom), [custom]),
        ]:
            with self.subTest(option=entry.options):
                stored = dict(entry.options)
                # Mirror custom_components/ch592_ota/__init__.py setup entry line.
                urls = FIRMWARE.resolve_manifest_urls(
                    entry.options.get(CONST.CONF_MANIFEST)
                )
                self.assertEqual(urls, expected)
                self.assertEqual(entry.options, stored)


if __name__ == "__main__":
    unittest.main()
