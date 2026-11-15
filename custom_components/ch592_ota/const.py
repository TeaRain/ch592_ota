"""Constants for the CH592 OTA integration."""

from __future__ import annotations

DOMAIN = "ch592_ota"

CONF_FIRMWARE = "firmware"
CONF_VERSION = "version"
CONF_CHUNK = "chunk"
CONF_MANIFEST = "manifest_url"
CONF_AUTO_INSTALL = "auto_install_on_ota_mode"

# Pressing B for 3 s puts the device into OTA mode; the integration can then
# start the upload by itself when a newer firmware is published.
DEFAULT_AUTO_INSTALL = True

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
ERASE_TIMEOUT = 60
STATUS_TIMEOUT = 20
REBOOT_TIMEOUT = 180
