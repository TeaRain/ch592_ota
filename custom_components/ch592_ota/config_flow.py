"""Config flow for the CH592 OTA integration."""

from __future__ import annotations

import re

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback

from .const import (
    CONF_AUTO_INSTALL,
    CONF_FIRMWARE,
    CONF_MANIFEST,
    DEFAULT_AUTO_INSTALL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_FIRMWARE,
    DOMAIN,
)

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


class Ch592OtaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not MAC_RE.match(address):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME, "").strip() or DEFAULT_DEVICE_NAME,
                    data={CONF_ADDRESS: address},
                    options={
                        CONF_FIRMWARE: user_input.get(CONF_FIRMWARE, "").strip()
                        or DEFAULT_FIRMWARE
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME, default=DEFAULT_DEVICE_NAME): str,
                    vol.Optional(CONF_FIRMWARE, default=DEFAULT_FIRMWARE): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return Ch592OtaOptionsFlow()


class Ch592OtaOptionsFlow(config_entries.OptionsFlow):
    """Allow the firmware path to be changed later."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_FIRMWARE,
                        default=self.config_entry.options.get(
                            CONF_FIRMWARE, DEFAULT_FIRMWARE
                        ),
                    ): str,
                    vol.Optional(
                        CONF_MANIFEST,
                        default=self.config_entry.options.get(CONF_MANIFEST, ""),
                    ): str,
                    vol.Optional(
                        CONF_AUTO_INSTALL,
                        default=self.config_entry.options.get(
                            CONF_AUTO_INSTALL, DEFAULT_AUTO_INSTALL
                        ),
                    ): bool,
                }
            ),
        )
