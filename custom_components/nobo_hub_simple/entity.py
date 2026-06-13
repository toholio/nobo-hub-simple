"""Base entity for the Nobø Ecohub (Simple) integration.

Entities subscribe to a single dispatcher signal fired by the reconciler on
every hub push and on connection-state changes, rather than registering their
own pynobo callbacks. This keeps the reconciler the single owner of the hub
callback and gives entities a consistent, debounced view of state.
"""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import signal_update
from .reconciler import NoboReconciler


class NoboBaseEntity(Entity):
    """Base class for Nobø Ecohub (Simple) entities."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, reconciler: NoboReconciler) -> None:
        """Initialize the entity."""
        self._reconciler = reconciler
        self._nobo = reconciler.hub

    async def async_added_to_hass(self) -> None:
        """Subscribe to reconciler state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_update(self._reconciler.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Refresh entity state from the hub/reconciler and write it."""
        self._read_state()
        self.async_write_ha_state()

    @callback
    def _read_state(self) -> None:
        """Copy current state onto entity attributes. Must be overridden."""
        raise NotImplementedError
