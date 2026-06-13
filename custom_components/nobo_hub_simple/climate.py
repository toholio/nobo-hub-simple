"""Climate platform for the Nobø Ecohub (Simple) integration.

Each managed zone is exposed as a dead-simple thermostat: HEAT/OFF plus a
single whole-degree target temperature. The integration is authoritative — the
reported state is the *desired* state, which the reconciler actively enforces
against the hub.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_NAME, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NoboHubConfigEntry
from .const import (
    ATTR_SERIAL,
    DOMAIN,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
)
from .entity import NoboBaseEntity
from .reconcile import clamp_setpoint

# Writes are serialized inside the reconciler (single asyncio lock), so the
# platform does not need its own update parallelism limit beyond this.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: NoboHubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a climate entity for each managed zone."""
    reconciler = config_entry.runtime_data
    async_add_entities(
        NoboZoneClimate(reconciler, zone_id) for zone_id in reconciler.managed_zones
    )


class NoboZoneClimate(NoboBaseEntity, ClimateEntity):
    """A single Nobø zone as an Off/Heat thermostat with one setpoint."""

    _attr_name = None
    # We implement async_turn_on/off directly; opt out of the legacy shim.
    _enable_turn_on_off_backwards_compatibility = False
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMPERATURE
    _attr_max_temp = MAX_TEMPERATURE
    _attr_target_temperature_step = 1

    def __init__(self, reconciler, zone_id: str) -> None:
        """Initialize the zone climate entity."""
        super().__init__(reconciler)
        self._id = zone_id
        hub = reconciler.hub
        self._attr_unique_id = f"{hub.hub_serial}:{zone_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{hub.hub_serial}:{zone_id}")},
            name=hub.zones[zone_id][ATTR_NAME],
            via_device=(DOMAIN, hub.hub_info[ATTR_SERIAL]),
            suggested_area=hub.zones[zone_id][ATTR_NAME],
        )
        self._read_state()

    # -- Commands ------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HEAT or OFF."""
        mode = "heat" if hvac_mode == HVACMode.HEAT else "off"
        await self._reconciler.async_set_mode(self._id, mode)
        self._read_state()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the zone on (HEAT)."""
        await self._reconciler.async_set_mode(self._id, "heat")
        self._read_state()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the zone off."""
        await self._reconciler.async_set_mode(self._id, "off")
        self._read_state()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature (implies HEAT)."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        setpoint = clamp_setpoint(temperature, MIN_TEMPERATURE, MAX_TEMPERATURE)
        await self._reconciler.async_set_setpoint(self._id, setpoint)
        self._read_state()
        self.async_write_ha_state()

    # -- State ---------------------------------------------------------------

    @callback
    def _read_state(self) -> None:
        """Copy desired state + hub temperature onto entity attributes."""
        hub = self._nobo
        if not self._reconciler.connected or self._id not in hub.zones:
            self._attr_available = False
            return
        self._attr_available = True

        desired = self._reconciler.get_desired(self._id)
        if desired is None:
            # Zone was restored/unmanaged at runtime.
            self._attr_available = False
            return

        self._attr_hvac_mode = (
            HVACMode.HEAT if desired.mode == "heat" else HVACMode.OFF
        )
        self._attr_target_temperature = desired.setpoint

        current = hub.get_current_zone_temperature(self._id)
        self._attr_current_temperature = (
            None if current is None else float(current)
        )

        if desired.mode == "off":
            self._attr_hvac_action = HVACAction.OFF
        elif self._attr_current_temperature is None:
            # No sensor in this zone: can't infer heating vs idle.
            self._attr_hvac_action = None
        elif self._attr_current_temperature < desired.setpoint:
            self._attr_hvac_action = HVACAction.HEATING
        else:
            self._attr_hvac_action = HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether the hub currently matches desired state."""
        return {"in_sync": self._reconciler.is_in_sync(self._id)}
