"""The Nobø Ecohub (Simple) integration.

A forked, opinionated take on the official ``nobo_hub`` integration: every
managed zone becomes a plain HEAT/OFF thermostat with a single setpoint, and a
reconciler actively enforces that desired state against any external change.

Connection handling (stale-IP UDP rediscovery, clean shutdown) is kept from the
official integration.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from pynobo import nobo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_NAME,
    CONF_IP_ADDRESS,
    CONF_MAC,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, format_mac
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_HARDWARE_VERSION,
    ATTR_SOFTWARE_VERSION,
    CONF_SERIAL,
    DOMAIN,
    NOBO_MANUFACTURER,
    SERVICE_RESTORE_ZONE,
)
from .reconciler import NoboReconciler

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]

type NoboHubConfigEntry = ConfigEntry[NoboReconciler]

RESTORE_ZONE_SCHEMA = vol.Schema({vol.Required("zone_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: NoboHubConfigEntry) -> bool:
    """Set up Nobø Ecohub (Simple) from a config entry."""

    serial = entry.data[CONF_SERIAL]
    stored_ip = entry.data[CONF_IP_ADDRESS]

    async def _connect(ip: str) -> nobo:
        hub = nobo(
            serial=serial,
            ip=ip,
            discover=False,
            synchronous=False,
            timezone=dt_util.get_default_time_zone(),
        )
        await hub.connect()
        return hub

    try:
        hub = await _connect(stored_ip)
    except OSError as err:
        # Stored IP may be stale - try UDP rediscovery to pick up a new
        # DHCP lease (or a hub that's been moved).
        discovered = await nobo.async_discover_hubs(serial=serial)
        if not discovered:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"serial": serial, "ip": stored_ip},
            ) from err
        new_ip, _ = next(iter(discovered))
        try:
            hub = await _connect(new_ip)
        except OSError as rediscover_err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"serial": serial, "ip": new_ip},
            ) from rediscover_err
        if new_ip != stored_ip:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_IP_ADDRESS: new_ip}
            )

    reconciler = NoboReconciler(hass, entry, hub)
    entry.runtime_data = reconciler

    async def _async_close(event) -> None:
        """Close the Nobø Ecohub socket connection when HA stops."""
        await reconciler.async_shutdown()
        await hub.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_close)
    )

    device_registry = dr.async_get(hass)
    connections: set[tuple[str, str]] = set()
    if mac := entry.data.get(CONF_MAC):
        connections.add((CONNECTION_NETWORK_MAC, format_mac(mac)))
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, hub.hub_serial)},
        connections=connections,
        serial_number=hub.hub_serial,
        name=hub.hub_info[ATTR_NAME],
        manufacturer=NOBO_MANUFACTURER,
        model="Nobø Ecohub",
        sw_version=hub.hub_info[ATTR_SOFTWARE_VERSION],
        hw_version=hub.hub_info[ATTR_HARDWARE_VERSION],
    )

    # Start the socket receiver before bootstrap: creating the synthetic week
    # profiles relies on reading the hub's acknowledgement push.
    await hub.start()
    await reconciler.async_bootstrap()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: NoboHubConfigEntry
) -> None:
    """Reload the entry when options change (handles zone add/remove + restore)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NoboHubConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        reconciler = entry.runtime_data
        await reconciler.async_shutdown()
        await reconciler.hub.stop()
    return unload_ok


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RESTORE_ZONE):
        return

    async def _async_restore_zone(call: ServiceCall) -> None:
        zone_id = call.data["zone_id"]
        entries: list[NoboHubConfigEntry] = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            reconciler: NoboReconciler | None = getattr(entry, "runtime_data", None)
            if reconciler is not None and zone_id in reconciler.hub.zones:
                await reconciler.async_unmanage_zone(zone_id)
                return
        raise HomeAssistantError(f"No managed Nobø zone with id {zone_id}")

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_ZONE,
        _async_restore_zone,
        schema=RESTORE_ZONE_SCHEMA,
    )
