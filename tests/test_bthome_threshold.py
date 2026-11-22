from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "_ch592_bthome_test_pkg"


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.bluetooth", BluetoothServiceInfoBleak=object)
package = _module(PACKAGE)
package.__path__ = [str(REPO / "custom_components/ch592_ota")]
const = _load(f"{PACKAGE}.const", REPO / "custom_components/ch592_ota/const.py")
bthome = _load(f"{PACKAGE}.bthome", REPO / "custom_components/ch592_ota/bthome.py")


def info(payload):
    return types.SimpleNamespace(
        service_data={const.BTHOME_SERVICE_DATA_UUID: payload}
    )


class BthomeThresholdTests(unittest.TestCase):
    def test_threshold_is_parsed_with_existing_gas_and_version(self):
        payload = bytes.fromhex(
            "40 0c 30 0b 3d 00 00 4c 3c 00 00 00 "
            "3e 8c 00 00 00 53 05"
        ) + b"1.4.9"
        fields = bthome.parse_bthome_fields(info(payload))
        self.assertEqual(fields["gas"], 60)
        self.assertEqual(fields["version"], "1.4.9")
        self.assertEqual(fields["threshold"], 140)

    def test_legacy_advertisement_has_no_fabricated_threshold(self):
        payload = bytes.fromhex(
            "40 0c 30 0b 3d 00 00 4c 3c 00 00 00 53 05"
        ) + b"1.4.8"
        fields = bthome.parse_bthome_fields(info(payload))
        self.assertEqual(fields["gas"], 60)
        self.assertEqual(fields["version"], "1.4.8")
        self.assertNotIn("threshold", fields)


if __name__ == "__main__":
    unittest.main()
