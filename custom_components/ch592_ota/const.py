"""Constants for the CH592 OTA integration."""

from __future__ import annotations

DOMAIN = "ch592_ota"

CONF_FIRMWARE = "firmware"
CONF_VERSION = "version"
CONF_CHUNK = "chunk"
CONF_MANIFEST = "manifest_url"
CONF_AUTO_INSTALL = "auto_install_on_ota_mode"

# Automatic installation is opt-in: entering OTA mode alone is not consent.
DEFAULT_AUTO_INSTALL = False

DEFAULT_DEVICE_NAME = "CH592"
DEFAULT_FIRMWARE = "www/firmware/ch592_ota.bin"

# Firmware repository published by tools/publish_firmware.py.
# Tried in order until one answers, so a temporary Gitee/GitHub outage or a
# still-private mirror does not break the update check.
DEFAULT_MANIFEST_URLS = (
    "https://gitee.com/zigbridge/ch592-gas-broadcaster"
    "/raw/master/firmware/manifest.json",
    "https://raw.githubusercontent.com/TeaRain/ch592-gas-broadcaster"
    "/master/firmware/manifest.json",
)
DEFAULT_MANIFEST_URL = DEFAULT_MANIFEST_URLS[0]
MANIFEST_UPDATE_INTERVAL_MINUTES = 30

SERVICE_FLASH = "flash"
SERVICE_ABORT = "abort"
SERVICE_SET_GAS = "set_gas"
SERVICE_CANCEL_GAS = "cancel_gas"
SERVICE_SET_THRESHOLD = "set_threshold"
SERVICE_CANCEL_THRESHOLD = "cancel_threshold"
# Development helper: re-import the integration from disk without a HA restart.
SERVICE_RELOAD_CODE = "reload_code"

BTHOME_SERVICE_DATA_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
OTA_SERVICE_UUID = "0000fee0-0000-1000-8000-00805f9b34fb"
OTA_CHAR_UUID = "0000fee1-0000-1000-8000-00805f9b34fb"

# WCH backup-OTA memory map (see OTA_Image/README.md).
IMAGE_A_START = 0x1000
IMAGE_SIZE = 216 * 1024
FLASH_BLOCK_SIZE = 4096
OTA_IMAGE_MAGIC = 0x35415347

CMD_IAP_PROM = 0x80
CMD_IAP_ERASE = 0x81
CMD_IAP_VERIFY = 0x82
CMD_IAP_END = 0x83
CMD_IAP_INFO = 0x84
CMD_IAP_CRC32 = 0x85
CMD_IAP_SET_GAS = 0x86
CMD_IAP_SET_THRESHOLD = 0x87
CMD_IAP_ENTER_OTA = 0x88
REMOTE_OTA_FRAME = bytes((CMD_IAP_ENTER_OTA, 0x00, 0xA5, 0x5A, 0x4F, 0x54))
REMOTE_OTA_MIN_VERSION = (1, 6, 0)
REMOTE_OTA_WAKE_TIMEOUT = 240
REMOTE_OTA_SWITCH_TIMEOUT = 20
REMOTE_OTA_DISCONNECT_TIMEOUT = 10

# Config-write protocol: payloads carry a fixed magic so a stray packet can
# never rewrite the billing counter or the detector threshold.
CONFIG_MAGIC = 0x5AA5
GAS_SET_MAGIC = CONFIG_MAGIC
GAS_MAX_UNITS = 100_000_000  # firmware rolls over at 100 000 m³
GAS_SET_PENDING_SECONDS = 15 * 60

# Pulse threshold written over the OTA channel (firmware >= 1.4.1). The default
# matches the firmware's compiled-in value; lower is more sensitive but counts
# noise, higher misses pulses.
THRESHOLD_DEFAULT = 100
THRESHOLD_MIN = 20
THRESHOLD_MAX = 2000

OTA_STATUS_OK = 0x00
OTA_STATUS_UNKNOWN_CMD = 0xFE
OTA_STATUS_BAD_ADDRESS = 0xFF

# Practical limits.
DEFAULT_CHUNK = 64
MIN_CHUNK = 16
# PROGRAM accepts unacknowledged writes; the status is read once per window.
# VERIFY uses the same FLASH_ROM_* page machinery as PROGRAM, so it keeps the
# page-safe 64-byte chunk: 240-byte VERIFY writes stall the transfer.
PROGRAM_STATUS_WINDOW = 32
VERIFY_CHUNK_MAX = 64
# Re-read the device INFO (which carries the supply voltage) every N status
# windows while uploading, so the voltage trace is visible during the transfer.
VDD_SAMPLE_WINDOWS = 4
# Largest PROGRAM payload the device can take once it reports page splitting
# (firmware >= 1.2.9); a single-command length field is one byte.
# Capped at 128 on purpose: the ESPHome proxy turns writes above that into
# prepare/execute long writes (dozens of round trips each), which is both slow
# and what made the 240-byte attempt fail mid-upload.
OTA_CHUNK_MAX = 128
OTA_FEATURE_PAGE_SPLIT = 0x01
OTA_FEATURE_CRC32 = 0x02
OTA_FEATURE_SET_GAS = 0x04
OTA_FEATURE_SET_THRESHOLD = 0x08
ERASE_TIMEOUT = 60
STATUS_TIMEOUT = 20
REBOOT_TIMEOUT = 180
