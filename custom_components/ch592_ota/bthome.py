"""Minimal BTHome v2 parser used to read the firmware version text object.

In BTHome v2 the data length is implied by the object ID for fixed-size
objects; only variable-size objects (text, raw, ...) carry a length byte.
"""

from __future__ import annotations

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from .const import BTHOME_SERVICE_DATA_UUID

# Fixed data lengths from the BTHome v2 specification.
_FIXED_LENGTHS: dict[int, int] = {
    0x00: 1,   # packet id
    0x01: 1,   # battery
    0x02: 2,   # temperature
    0x03: 2,   # humidity
    0x04: 3,   # pressure
    0x05: 3,   # illuminance
    0x06: 2,   # mass
    0x07: 2,   # mass (lb)
    0x08: 2,   # dew point
    0x09: 1,   # count
    0x0A: 3,   # energy
    0x0B: 3,   # power
    0x0C: 2,   # voltage
    0x0D: 2,   # pm2.5
    0x0E: 2,   # pm10
    0x0F: 1,   # generic boolean
    0x10: 1,   # power state
    0x11: 1,   # opening
    0x12: 2,   # CO2
    0x13: 2,   # TVOC
    0x14: 2,   # moisture
    0x15: 1,   # battery low
    0x16: 1,   # battery charging
    0x17: 2,   # carbon monoxide
    0x18: 1,   # cold
    0x19: 1,   # connectivity
    0x1A: 1,   # door
    0x1B: 1,   # garage door
    0x1C: 1,   # gas
    0x1D: 1,   # heat
    0x1E: 1,   # light
    0x1F: 1,   # lock
    0x20: 1,   # moisture (binary)
    0x21: 1,   # motion
    0x22: 1,   # moving
    0x23: 1,   # occupancy
    0x24: 1,   # plug
    0x25: 1,   # power (binary)
    0x26: 1,   # presence
    0x27: 1,   # problem
    0x28: 1,   # running
    0x29: 1,   # safety
    0x2A: 1,   # smoke
    0x2B: 1,   # sound
    0x2C: 1,   # tamper
    0x2D: 1,   # vibration
    0x2E: 1,   # window
    0x2F: 1,   # humidity (binary)
    0x3D: 2,   # count (16-bit)
    0x3E: 4,   # count (32-bit)
    0x4C: 4,   # gas
}


def _service_data(service_info: BluetoothServiceInfoBleak) -> bytes | None:
    for uuid, data in service_info.service_data.items():
        if uuid.lower() in (BTHOME_SERVICE_DATA_UUID, "fcd2"):
            return bytes(data)
    return None


def has_bthome_data(service_info: BluetoothServiceInfoBleak) -> bool:
    """Whether this advertisement carries a BTHome (0xFCD2) payload."""
    return _service_data(service_info) is not None


def parse_bthome_fields(service_info: BluetoothServiceInfoBleak) -> dict[str, object]:
    """Return the objects this integration cares about from a BTHome v2 ad.

    ``version`` is the 0x53 text object, ``gas`` the 0x4C counter in 0.001 m³.
    """
    fields: dict[str, object] = {}
    data = _service_data(service_info)
    if not data or len(data) < 2:
        return fields

    # First byte is the BTHome device-information byte (0x40 for v2, unencrypted).
    index = 1
    while index < len(data):
        object_id = data[index]
        index += 1
        fixed = _FIXED_LENGTHS.get(object_id)
        if fixed is None:
            # Variable-size object: the next byte is the data length.
            if index >= len(data):
                return fields
            length = data[index]
            index += 1
        else:
            length = fixed

        if index + length > len(data):
            return fields
        value = data[index : index + length]
        index += length

        if object_id == 0x53:
            try:
                fields["version"] = value.decode("utf-8").rstrip("\x00")
            except UnicodeDecodeError:
                pass
        elif object_id == 0x4C and len(value) == 4:
            fields["gas"] = int.from_bytes(value, "little")
    return fields


def parse_bthome_version(service_info: BluetoothServiceInfoBleak) -> str | None:
    """Return the value of object 0x53 (text) from a BTHome v2 advertisement."""
    version = parse_bthome_fields(service_info).get("version")
    return str(version) if version is not None else None
