from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "custom_components/ch592_ota"


def _json(relative):
    return json.loads((REPO / relative).read_text(encoding="utf-8"))


def _const():
    spec = importlib.util.spec_from_file_location(
        "_gaspulse_branding_const", PACKAGE / "const.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GasPulseBrandingTests(unittest.TestCase):
    def test_ha_metadata_uses_gaspulse_without_changing_domain(self):
        manifest = _json("custom_components/ch592_ota/manifest.json")
        hacs = _json("hacs.json")
        self.assertEqual(manifest["name"], "GasPulse 蓝牙燃气表")
        self.assertEqual(hacs["name"], "GasPulse 蓝牙燃气表")
        self.assertEqual(manifest["version"], "0.9.2")
        self.assertEqual(manifest["domain"], "ch592_ota")

    def test_internal_identifiers_remain_compatible(self):
        const = _const()
        self.assertEqual(const.DOMAIN, "ch592_ota")
        self.assertEqual(const.DEFAULT_FIRMWARE, "www/firmware/ch592_ota.bin")

    def test_config_flow_titles_are_branded(self):
        strings = _json("custom_components/ch592_ota/strings.json")
        english = _json("custom_components/ch592_ota/translations/en.json")
        chinese = _json("custom_components/ch592_ota/translations/zh-Hans.json")
        self.assertEqual(
            strings["config"]["step"]["user"]["title"], "GasPulse 蓝牙燃气表"
        )
        self.assertEqual(
            strings["options"]["step"]["init"]["title"], "GasPulse 蓝牙燃气表选项"
        )
        self.assertEqual(
            english["config"]["step"]["user"]["title"], "GasPulse BLE Gas Meter"
        )
        self.assertEqual(
            english["options"]["step"]["init"]["title"],
            "GasPulse BLE Gas Meter options",
        )
        self.assertEqual(
            chinese["config"]["step"]["user"]["title"], "GasPulse 蓝牙燃气表"
        )
        self.assertEqual(
            chinese["options"]["step"]["init"]["title"], "GasPulse 蓝牙燃气表选项"
        )

    def test_manifest_option_explain_managed_mirror_fallback(self):
        for relative in (
            "custom_components/ch592_ota/strings.json",
            "custom_components/ch592_ota/translations/en.json",
            "custom_components/ch592_ota/translations/zh-Hans.json",
        ):
            with self.subTest(relative=relative):
                text = _json(relative)["options"]["step"]["init"]["data"][
                    "manifest_url"
                ]
                self.assertIn("GasPulse", text)
                self.assertNotIn("CH592 OTA", text)


if __name__ == "__main__":
    unittest.main()
