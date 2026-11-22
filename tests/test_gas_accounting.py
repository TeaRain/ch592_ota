from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "custom_components/ch592_ota/gas_accounting.py"


def load_model():
    spec = importlib.util.spec_from_file_location("gas_accounting_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GasAccountingTests(unittest.TestCase):
    def test_negative_offset_and_normal_increment(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(raw=1000, actual=900, when="2026-11-22T00:00:00+00:00")
        self.assertEqual(model.offset, -100)
        self.assertEqual(model.total, 900)
        self.assertEqual(model.consumption, 0)
        self.assertTrue(model.accept_raw(1010))
        self.assertEqual(model.total, 910)
        self.assertEqual(model.consumption, 10)

    def test_recalibration_does_not_create_consumption(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(1000, 900)
        model.accept_raw(1010)
        model.calibrate(2000, 5000)
        self.assertEqual(model.total, 5000)
        self.assertEqual(model.consumption, 10)
        self.assertEqual(model.offset, 3000)

    def test_replayed_old_raw_is_held_until_recovery(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(2000, 5000)
        self.assertFalse(model.accept_raw(1000))
        self.assertEqual(model.total, 5000)
        self.assertEqual(model.consumption, 0)
        self.assertTrue(model.reset_detected)
        self.assertTrue(model.accept_raw(2000))
        self.assertFalse(model.reset_detected)
        model.accept_raw(2010)
        self.assertEqual(model.total, 5010)
        self.assertEqual(model.consumption, 10)

    def test_natural_uint32_wrap_is_an_increment(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(module.COUNTER_MAX - 5, 100)
        self.assertTrue(model.accept_raw(4))
        self.assertEqual(model.total, 110)
        self.assertEqual(model.consumption, 10)
        self.assertEqual(model.wraps, 1)

    def test_reset_requires_calibration_but_preserves_consumption(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(100000, 5000)
        model.accept_raw(100010)
        self.assertFalse(model.accept_raw(0))
        self.assertEqual(model.total, 5010)
        self.assertEqual(model.consumption, 10)
        self.assertTrue(model.reset_detected)
        model.calibrate(0, 6000)
        self.assertEqual(model.total, 6000)
        self.assertEqual(model.consumption, 10)
        self.assertFalse(model.reset_detected)
        model.accept_raw(10)
        self.assertEqual(model.total, 6010)
        self.assertEqual(model.consumption, 20)

    def test_uncalibrated_model_does_not_publish_total(self):
        module = load_model()
        model = module.GasAccounting()
        self.assertFalse(model.calibrated)
        self.assertFalse(model.accept_raw(100))
        self.assertIsNone(model.total)

    def test_delta_limit_rejects_implausible_jump(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(0, 5000)
        self.assertTrue(model.accept_raw(module.MAX_COUNTER_DELTA))
        self.assertFalse(model.accept_raw(2 * module.MAX_COUNTER_DELTA + 1))

    def test_serialization_round_trip(self):
        module = load_model()
        model = module.GasAccounting()
        model.calibrate(module.COUNTER_MAX - 5, 100, when="2026-11-22T00:00:00+00:00")
        model.accept_raw(4)
        restored = module.GasAccounting.from_dict(model.to_dict())
        self.assertEqual(restored.to_dict(), model.to_dict())
        with self.assertRaises(ValueError):
            module.GasAccounting.from_dict({"offset": "bad"})


if __name__ == "__main__":
    unittest.main()
