"""Real-HTTP fault injection for the ch592_ota mirror fallback.

Unlike the mocked tests in test_firmware_migration.py, this module drives the
production firmware.py through a real aiohttp loopback server: real sockets,
real JSON codec, real ClientSession and timeout machinery.

The system Python used by tools/project_verify.py has no aiohttp, so this
module is intentionally not named ``test_*.py`` (unittest discover would fail
to import it) and carries a skip guard. Run it through
``tools/run_http_fault_injection.py``, which creates an isolated venv with the
pinned aiohttp. The local suite is fully hermetic (loopback only). Pass
``--online`` to additionally run read-only checks against the four published
firmware mirrors; those pin the current 1.6.2 release metadata and must be
updated when a new firmware version is published.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import time
import types
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin

try:
    import aiohttp
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # system Python without the optional venv
    aiohttp = None
    web = None
    AIOHTTP_AVAILABLE = False

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_gaspulse_real_http_pkg"

EXPECTED_SIZE = 156952
EXPECTED_CRC = "0x8043E30E"
EXPECTED_SHA = (
    "18cfc3142328e6b46c01392c6ef9e77cd5503ffffce15007632623330a33b92c"
)


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


def load_integration():
    """Load const.py/firmware.py with real aiohttp, thin HA stubs only."""

    class HomeAssistantError(Exception):
        pass

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __init__(
            self,
            hass,
            logger,
            *,
            config_entry=None,
            name=None,
            update_interval=None,
        ):
            self.hass = hass
            self.logger = logger
            self.config_entry = config_entry
            self.name = name
            self.update_interval = update_interval

        @classmethod
        def __class_getitem__(cls, item):
            return cls

    package = _module(PACKAGE)
    package.__path__ = [str(REPO / "custom_components/ch592_ota")]
    shared_session = {}
    stubs = {
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
            async_get_clientsession=lambda hass: shared_session["session"],
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=UpdateFailed,
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
    # aiohttp must remain the real one, never a stub.
    assert sys.modules["aiohttp"] is aiohttp
    return const, firmware, shared_session


if AIOHTTP_AVAILABLE:
    CONST, FIRMWARE, SESSION_SLOT = load_integration()
else:  # pragma: no cover - exercised only on dependency-free interpreters
    CONST = FIRMWARE = None
    SESSION_SLOT = {}


def good_manifest(image: bytes, urls: list[str]) -> dict:
    return {
        "version": "1.6.2",
        "file": "ch592_ota.bin",
        "download_urls": urls,
        "size": len(image),
        "crc32": f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}",
        "sha256": hashlib.sha256(image).hexdigest(),
    }


class HttpSandbox:
    """Real aiohttp server; routes map path -> handler kind/payload."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.request_log: list[str] = []
        self.app = web.Application()
        self.app.router.add_get("/{tail:.*}", self._handle)
        self.runner = None
        self.base = ""

    async def _handle(self, request):
        path = request.path
        self.request_log.append(path)
        rule = self.routes.get(path, 404)
        if isinstance(rule, int):
            return web.Response(status=rule)
        if isinstance(rule, bytes):
            return web.Response(
                body=rule, content_type="application/octet-stream"
            )
        if isinstance(rule, dict):
            return web.json_response(rule)
        if rule == "BAD_JSON":
            return web.Response(
                text="{not valid json", content_type="application/json"
            )
        raise AssertionError(f"unknown rule {rule!r}")

    async def __aenter__(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *args):
        await self.runner.cleanup()

    def url(self, path):
        return self.base + path


@unittest.skipUnless(AIOHTTP_AVAILABLE, "aiohttp unavailable; use run_http_fault_injection.py")
class RealHttpFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = aiohttp.ClientSession()
        SESSION_SLOT["session"] = self.session

    async def asyncTearDown(self):
        await self.session.close()

    def _coordinator(self, urls):
        coordinator = object.__new__(FIRMWARE.FirmwareCloudCoordinator)
        coordinator.hass = object()
        coordinator.urls = urls
        coordinator.url = urls[0]
        return coordinator

    async def test_s1_manifest_404_falls_back_to_second_mirror(self):
        async with HttpSandbox(
            {
                "/m0/manifest.json": 404,
                "/m1/manifest.json": {
                    "version": "1.6.2",
                    "file": "ch592_ota.bin",
                },
            }
        ) as box:
            urls = [box.url("/m0/manifest.json"), box.url("/m1/manifest.json")]
            coordinator = self._coordinator(urls)
            payload = await coordinator._async_update_data()
            expected = FIRMWARE.build_download_urls(
                urls, urls[1], "ch592_ota.bin"
            )
            self.assertEqual(coordinator.url, urls[1])
            self.assertEqual(payload["download_urls"], expected)
            self.assertEqual(
                box.request_log, ["/m0/manifest.json", "/m1/manifest.json"]
            )

    async def test_s2_all_manifests_broken_raises_update_failed(self):
        async with HttpSandbox(
            {"/m0/manifest.json": 404, "/m1/manifest.json": "BAD_JSON"}
        ) as box:
            urls = [box.url("/m0/manifest.json"), box.url("/m1/manifest.json")]
            coordinator = self._coordinator(urls)
            with self.assertRaises(FIRMWARE.UpdateFailed) as caught:
                await coordinator._async_update_data()
            self.assertTrue(all(u in str(caught.exception) for u in urls))
            self.assertEqual(
                box.request_log, ["/m0/manifest.json", "/m1/manifest.json"]
            )

    async def test_s3_manifest_without_version_is_skipped(self):
        async with HttpSandbox(
            {
                "/m0/manifest.json": {"error": "offline"},
                "/m1/manifest.json": {
                    "version": "1.6.2",
                    "file": "ch592_ota.bin",
                },
            }
        ) as box:
            urls = [box.url("/m0/manifest.json"), box.url("/m1/manifest.json")]
            coordinator = self._coordinator(urls)
            payload = await coordinator._async_update_data()
            self.assertEqual(payload["version"], "1.6.2")
            self.assertEqual(coordinator.url, urls[1])

    async def test_s4_bin_404_falls_back_to_next_mirror(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        async with HttpSandbox(
            {"/b0/ch592_ota.bin": 404, "/b1/ch592_ota.bin": image}
        ) as box:
            b0, b1 = box.url("/b0/ch592_ota.bin"), box.url("/b1/ch592_ota.bin")
            result = await FIRMWARE.async_download_firmware(
                object(), good_manifest(image, [b0, b1])
            )
            self.assertEqual(result, image)
            self.assertEqual(
                box.request_log, ["/b0/ch592_ota.bin", "/b1/ch592_ota.bin"]
            )

    async def test_s5_same_size_corrupt_image_falls_back(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        corrupt = bytearray(image)
        corrupt[0] ^= 0xFF
        corrupt = bytes(corrupt)
        async with HttpSandbox(
            {"/b0/ch592_ota.bin": corrupt, "/b1/ch592_ota.bin": image}
        ) as box:
            b0, b1 = box.url("/b0/ch592_ota.bin"), box.url("/b1/ch592_ota.bin")
            result = await FIRMWARE.async_download_firmware(
                object(), good_manifest(image, [b0, b1])
            )
            self.assertEqual(result, image)
            self.assertEqual(
                box.request_log, ["/b0/ch592_ota.bin", "/b1/ch592_ota.bin"]
            )

    async def test_s6_size_mismatch_falls_back(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        shorter = image[:-7]
        async with HttpSandbox(
            {"/b0/ch592_ota.bin": shorter, "/b1/ch592_ota.bin": image}
        ) as box:
            b0, b1 = box.url("/b0/ch592_ota.bin"), box.url("/b1/ch592_ota.bin")
            result = await FIRMWARE.async_download_firmware(
                object(), good_manifest(image, [b0, b1])
            )
            self.assertEqual(result, image)

    async def test_s7_all_candidates_corrupt_raises_home_assistant_error(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        corrupt = bytearray(image)
        corrupt[0] ^= 0xFF
        async with HttpSandbox(
            {
                "/b0/ch592_ota.bin": bytes(corrupt),
                "/b1/ch592_ota.bin": b"totally-different-bytes-here",
            }
        ) as box:
            b0, b1 = box.url("/b0/ch592_ota.bin"), box.url("/b1/ch592_ota.bin")
            with self.assertRaises(FIRMWARE.HomeAssistantError) as caught:
                await FIRMWARE.async_download_firmware(
                    object(), good_manifest(image, [b0, b1])
                )
            self.assertIn(b0, str(caught.exception))
            self.assertIn(b1, str(caught.exception))
            self.assertEqual(
                box.request_log, ["/b0/ch592_ota.bin", "/b1/ch592_ota.bin"]
            )

    async def test_s8_malformed_manifest_size_rejected_before_bin_request(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        async with HttpSandbox(
            {
                "/m0/manifest.json": {
                    "version": "1.6.2",
                    "file": "ch592_ota.bin",
                    "size": "not-a-number",
                },
                "/m0/ch592_ota.bin": image,
            }
        ) as box:
            manifest_url = box.url("/m0/manifest.json")
            payload = await self._coordinator(
                [manifest_url]
            )._async_update_data()
            with self.assertRaises(FIRMWARE.HomeAssistantError) as caught:
                await FIRMWARE.async_download_firmware(object(), payload)
            self.assertIn("size", str(caught.exception))
            self.assertEqual(box.request_log, ["/m0/manifest.json"])

    async def test_s9_coordinator_to_download_chain_uses_winning_mirror(self):
        image = b"Gaspulse-real-http-firmware-image-0123456789" * 64
        async with HttpSandbox(
            {
                "/m0/manifest.json": 404,
                "/m1/manifest.json": {
                    "version": "1.6.2",
                    "file": "ch592_ota.bin",
                },
                "/m0/ch592_ota.bin": 404,
                "/m1/ch592_ota.bin": image,
            }
        ) as box:
            urls = [box.url("/m0/manifest.json"), box.url("/m1/manifest.json")]
            coordinator = self._coordinator(urls)
            payload = await coordinator._async_update_data()
            result = await FIRMWARE.async_download_firmware(object(), payload)
            self.assertEqual(result, image)
            self.assertEqual(
                box.request_log,
                [
                    "/m0/manifest.json",
                    "/m1/manifest.json",
                    "/m1/ch592_ota.bin",
                ],
            )


def http_get(url: str, timeout: int = 60) -> bytes:
    """GET with bounded retries; raw.githubusercontent resets are intermittent
    from some networks, so fall back to the github.com/raw redirect host."""
    candidates = [url]
    if url.startswith("https://raw.githubusercontent.com/"):
        rest = url.removeprefix("https://raw.githubusercontent.com/")
        owner, repo, branch_path = rest.split("/", 2)
        candidates.append(f"https://github.com/{owner}/{repo}/raw/{branch_path}")
    last_error = None
    for candidate in candidates:
        for backoff in (0, 2, 5):
            try:
                if backoff:
                    time.sleep(backoff)
                req = urllib.request.Request(
                    candidate, headers={"User-Agent": "gaspulse-http-verify"}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status} for {candidate}")
                    return resp.read()
            except Exception as err:  # noqa: BLE001 - retried then surfaced
                last_error = err
    raise last_error


def run_real_mirror_check() -> bool:
    """Read-only live check of the four published firmware mirrors."""
    print("\n--- real published mirrors (read-only) ---")
    expected_order = [
        "https://raw.githubusercontent.com/TeaRain/gaspulse-firmware/master/firmware/manifest.json",
        "https://gitee.com/zigbridge/gaspulse-firmware/raw/master/firmware/manifest.json",
        "https://gitee.com/zigbridge/ch592-gas-broadcaster/raw/master/firmware/manifest.json",
        "https://raw.githubusercontent.com/TeaRain/ch592-gas-broadcaster/master/firmware/manifest.json",
    ]
    ok = list(CONST.DEFAULT_MANIFEST_URLS) == expected_order
    print(
        f"{'PASS' if ok else 'FAIL'}: DEFAULT_MANIFEST_URLS order "
        f"-> {CONST.DEFAULT_MANIFEST_URLS}"
    )

    manifests = []
    for url in expected_order:
        try:
            payload = json.loads(http_get(url).decode("utf-8"))
            manifests.append((url, payload))
            print(f"  manifest OK {url} -> version={payload.get('version')}")
        except Exception as err:  # noqa: BLE001 - reported, not swallowed
            ok = False
            print(f"FAIL: mirror manifest fetch {url} -> {err!r}")
    if len(manifests) != len(expected_order):
        return False

    keys = ("version", "file", "size", "crc32", "sha256")
    baseline = {k: manifests[0][1].get(k) for k in keys}
    same = all({k: p.get(k) for k in keys} == baseline for _, p in manifests)
    print(
        f"{'PASS' if same else 'FAIL'}: four manifests identical metadata "
        f"-> {json.dumps(baseline, ensure_ascii=False)}"
    )
    ok &= same

    metadata_ok = (
        baseline["size"] == EXPECTED_SIZE
        and str(baseline["crc32"]).upper() == EXPECTED_CRC.upper()
        and baseline["sha256"] == EXPECTED_SHA
    )
    print(f"{'PASS' if metadata_ok else 'FAIL'}: metadata matches pinned 1.6.2 evidence")
    ok &= metadata_ok

    for manifest_url, payload in manifests:
        bin_url = urljoin(manifest_url, str(payload["file"]))
        try:
            blob = http_get(bin_url, timeout=120)
        except Exception as err:  # noqa: BLE001
            print(f"FAIL: BIN fetch {bin_url} -> {err!r}")
            ok = False
            continue
        crc = f"0x{zlib.crc32(blob) & 0xFFFFFFFF:08X}"
        sha = hashlib.sha256(blob).hexdigest()
        bin_ok = (
            len(blob) == payload["size"]
            and crc.upper() == str(payload["crc32"]).upper()
            and sha == payload["sha256"]
        )
        print(
            f"  BIN {'OK ' if bin_ok else 'BAD'} {bin_url} "
            f"bytes={len(blob)} crc={crc} sha={sha[:12]}..."
        )
        ok &= bin_ok
    return ok


def main() -> int:
    online = "--online" in sys.argv
    result = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=2).result
    if not result.wasSuccessful():
        return 1
    if online:
        return 0 if run_real_mirror_check() else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
