"""Integer-only accounting for a raw CH592 counter and HA calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

COUNTER_MAX = 0xFFFFFFFF
COUNTER_MODULO = COUNTER_MAX + 1
# Only a natural uint32 wrap or a normal burst should produce a small delta.
MAX_COUNTER_DELTA = 100_000


def _integer(value, name, *, minimum=None, maximum=None):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below its minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above its maximum")
    return value


@dataclass
class GasAccounting:
    offset: int | None = None
    total: int | None = None
    consumption: int = 0
    last_raw: int | None = None
    wraps: int = 0
    reset_detected: bool = False
    calibrated_at: str | None = None

    @property
    def calibrated(self) -> bool:
        return self.offset is not None and self.total is not None and self.last_raw is not None

    @property
    def effective_offset(self) -> int | None:
        if self.offset is None:
            return None
        return self.offset + self.wraps * COUNTER_MODULO

    def calibrate(self, raw: int, actual: int, when: str | None = None) -> None:
        raw = _integer(raw, "raw", minimum=0, maximum=COUNTER_MAX)
        actual = _integer(actual, "actual", minimum=0)
        self.offset = actual - raw
        self.total = actual
        self.last_raw = raw
        self.wraps = 0
        self.reset_detected = False
        self.calibrated_at = when or datetime.now(UTC).isoformat()

    def accept_raw(self, raw: int) -> bool:
        raw = _integer(raw, "raw", minimum=0, maximum=COUNTER_MAX)
        if not self.calibrated:
            return False
        if raw == self.last_raw:
            self.reset_detected = False
            return True

        delta = (raw - self.last_raw) % COUNTER_MODULO
        if delta > MAX_COUNTER_DELTA:
            self.reset_detected = True
            return False

        if raw < self.last_raw:
            self.wraps += 1
        self.last_raw = raw
        self.total += delta
        self.consumption += delta
        self.reset_detected = False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "total": self.total,
            "consumption": self.consumption,
            "last_raw": self.last_raw,
            "wraps": self.wraps,
            "reset_detected": self.reset_detected,
            "calibrated_at": self.calibrated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GasAccounting:
        if not isinstance(data, dict):
            raise ValueError("gas accounting state must be a dict")
        offset = data.get("offset")
        total = data.get("total")
        last_raw = data.get("last_raw")
        if offset is not None:
            offset = _integer(offset, "offset")
        if total is not None:
            total = _integer(total, "total", minimum=0)
        if last_raw is not None:
            last_raw = _integer(last_raw, "last_raw", minimum=0, maximum=COUNTER_MAX)
        calibrated_at = data.get("calibrated_at")
        if calibrated_at is not None and not isinstance(calibrated_at, str):
            raise ValueError("calibrated_at must be a string or None")
        return cls(
            offset=offset,
            total=total,
            consumption=_integer(data.get("consumption", 0), "consumption", minimum=0),
            last_raw=last_raw,
            wraps=_integer(data.get("wraps", 0), "wraps", minimum=0),
            reset_detected=bool(data.get("reset_detected", False)),
            calibrated_at=calibrated_at,
        )
