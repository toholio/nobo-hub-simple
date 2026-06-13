"""Config flow for the Nobø Ecohub (Simple) integration."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

from pynobo import nobo
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_IP_ADDRESS, CONF_MAC
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from . import NoboHubConfigEntry
from .const import (
    CONF_BLOCK_OVERRIDES,
    CONF_CLEAR_GLOBAL_OVERRIDES,
    CONF_MANAGED_ZONES,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    DEFAULT_BLOCK_OVERRIDES,
    DEFAULT_CLEAR_GLOBAL_OVERRIDES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)


class NoboHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nobø Ecohub (Simple)."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_hubs: dict[str, Any] | None = None
        self._hub: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if self._discovered_hubs is None:
            self._discovered_hubs = dict(await nobo.async_discover_hubs())

        if not self._discovered_hubs:
            return await self.async_step_manual()

        if user_input is not None:
            if user_input["device"] == "manual":
                return await self.async_step_manual()
            self._hub = user_input["device"]
            return await self.async_step_selected()

        hubs = self._hubs()
        hubs["manual"] = "Manual"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("device"): vol.In(hubs)}),
        )

    async def async_step_selected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle configuration of a selected discovered device."""
        errors = {}
        if TYPE_CHECKING:
            assert self._discovered_hubs
            assert self._hub
        if user_input is not None:
            serial_prefix = self._discovered_hubs[self._hub]
            serial = f"{serial_prefix}{user_input['serial_suffix']}"
            try:
                return await self._create_configuration(serial, self._hub)
            except NoboHubConnectError as error:
                errors["base"] = error.msg

        user_input = user_input or {}
        return self.async_show_form(
            step_id="selected",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "serial_suffix", default=user_input.get("serial_suffix")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "hub": self._format_hub(self._hub, self._discovered_hubs[self._hub])
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle configuration of an undiscovered device."""
        errors = {}
        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            ip_address = user_input[CONF_IP_ADDRESS]
            try:
                return await self._create_configuration(serial, ip_address)
            except NoboHubConnectError as error:
                errors["base"] = error.msg

        user_input = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL, default=user_input.get(CONF_SERIAL)): str,
                    vol.Required(
                        CONF_IP_ADDRESS, default=user_input.get(CONF_IP_ADDRESS)
                    ): str,
                }
            ),
            errors=errors,
        )

    async def _create_configuration(
        self, serial: str, ip_address: str
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()
        name = await self._test_connection(serial, ip_address)
        return self.async_create_entry(
            title=name,
            data={
                CONF_SERIAL: serial,
                CONF_IP_ADDRESS: ip_address,
                CONF_MAC: None,
            },
        )

    async def _test_connection(self, serial: str, ip_address: str) -> str:
        if not len(serial) == 12 or not serial.isdigit():
            raise NoboHubConnectError("invalid_serial")
        try:
            socket.inet_aton(ip_address)
        except OSError as err:
            raise NoboHubConnectError("invalid_ip") from err
        hub = nobo(serial=serial, ip=ip_address, discover=False, synchronous=False)
        try:
            if not await hub.async_connect_hub(ip_address, serial):
                raise NoboHubConnectError("cannot_connect")
            return hub.hub_info["name"]
        except OSError as err:
            raise NoboHubConnectError("cannot_connect_ip") from err
        finally:
            await hub.close()

    @staticmethod
    def _format_hub(ip, serial_prefix):
        return f"{serial_prefix}XXX ({ip})"

    def _hubs(self):
        return {
            ip: self._format_hub(ip, serial_prefix)
            for ip, serial_prefix in self._discovered_hubs.items()
        }

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: NoboHubConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class NoboHubConnectError(HomeAssistantError):
    """Error with connecting to Nobø Ecohub."""

    def __init__(self, msg) -> None:
        """Instantiate error."""
        super().__init__()
        self.msg = msg


class OptionsFlowHandler(OptionsFlow):
    """Handle the options flow.

    Note: changing options triggers a full reload of the config entry (via the
    update listener registered in ``__init__``), which re-runs the reconciler
    bootstrap — snapshotting newly managed zones and restoring newly unmanaged
    ones.
    """

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # NumberSelector returns a float; store the interval as an int.
            user_input[CONF_POLL_INTERVAL] = int(user_input[CONF_POLL_INTERVAL])
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        zone_options = self._zone_options()
        default_managed = options.get(
            CONF_MANAGED_ZONES, [opt["value"] for opt in zone_options]
        )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_MANAGED_ZONES, default=default_managed
                ): SelectSelector(
                    SelectSelectorConfig(options=zone_options, multiple=True)
                ),
                vol.Required(
                    CONF_BLOCK_OVERRIDES,
                    default=options.get(
                        CONF_BLOCK_OVERRIDES, DEFAULT_BLOCK_OVERRIDES
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_CLEAR_GLOBAL_OVERRIDES,
                    default=options.get(
                        CONF_CLEAR_GLOBAL_OVERRIDES, DEFAULT_CLEAR_GLOBAL_OVERRIDES
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    def _zone_options(self) -> list[SelectOptionDict]:
        """Build the managed-zones multi-select from the live hub, if available."""
        reconciler = getattr(self.config_entry, "runtime_data", None)
        if reconciler is None:
            # Entry not loaded; fall back to whatever was previously stored.
            stored = self.config_entry.options.get(CONF_MANAGED_ZONES, [])
            return [SelectOptionDict(value=zid, label=zid) for zid in stored]
        return [
            SelectOptionDict(value=zid, label=zone.get("name", zid))
            for zid, zone in reconciler.hub.zones.items()
        ]
