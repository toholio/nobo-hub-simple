"""Core reconciler for the Nobø Ecohub (Simple) integration.

Owns the live ``pynobo`` hub object and, for every *managed* zone, an
authoritative desired state (HEAT@setpoint / OFF). It enforces that desired
state against the hub:

* ON  = assign the synthetic "always comfort" week profile + set the zone's
  comfort temperature to the single setpoint.
* OFF = assign the synthetic "always off" week profile.

Enforcement is event-driven: on every hub push (debounced), a slow safety-net
poll, and immediately after any HA command, it diffs observed vs desired and
issues at most one ``async_update_zone`` per drifting zone. See ``reconcile.py``
for the (pure, unit-tested) decision logic.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from pynobo import PynoboError, nobo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ALWAYS_OFF_PROFILE,
    ALWAYS_ON_PROFILE,
    ATTR_TEMP_COMFORT_C,
    ATTR_TEMP_ECO_C,
    BACKOFF_MAX_S,
    BACKOFF_START_S,
    CONF_BLOCK_OVERRIDES,
    CONF_CLEAR_GLOBAL_OVERRIDES,
    CONF_MANAGED_ZONES,
    CONF_POLL_INTERVAL,
    DEFAULT_BLOCK_OVERRIDES,
    DEFAULT_CLEAR_GLOBAL_OVERRIDES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    FIGHT_THRESHOLD,
    FIGHT_WINDOW_S,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    PROFILE_DISCOVERY_TIMEOUT_S,
    PROFILE_ECHO_GRACE_S,
    PROFILE_NAME_ALWAYS_OFF,
    PROFILE_NAME_ALWAYS_ON,
    RECONCILE_DEBOUNCE_S,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    signal_update,
)
from .reconcile import (
    BackoffState,
    DesiredZoneState,
    ObservedZone,
    SyntheticProfiles,
    compute_override_clears,
    compute_zone_plan,
    initial_desired_from_hub,
    profile_matches,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """Undo pynobo's space -> non-breaking-space substitution for matching."""
    return name.replace(" ", " ")


class NoboReconciler:
    """One reconciler per config entry."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, hub: nobo
    ) -> None:
        """Initialize the reconciler (does not touch the hub yet)."""
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry.entry_id}"
        )

        # Persisted state.
        self._desired: dict[str, DesiredZoneState] = {}
        self._original: dict[str, dict] = {}
        self._profiles: SyntheticProfiles | None = None

        # Runtime state.
        self._connected = True
        self._write_lock = asyncio.Lock()
        self._backoff: dict[str, BackoffState] = {}
        self._pending_profile: dict[str, tuple[str, float]] = {}
        self._unmanaged_runtime: set[str] = set()

        # Cancel handles.
        self._debounce_cancel = None
        self._poll_cancel = None
        self._registered = False

    # -- Config accessors ----------------------------------------------------

    @property
    def _options(self) -> dict:
        return dict(self.entry.options)

    @property
    def block_overrides(self) -> bool:
        return self._options.get(CONF_BLOCK_OVERRIDES, DEFAULT_BLOCK_OVERRIDES)

    @property
    def clear_global_overrides(self) -> bool:
        return self._options.get(
            CONF_CLEAR_GLOBAL_OVERRIDES, DEFAULT_CLEAR_GLOBAL_OVERRIDES
        )

    @property
    def poll_interval(self) -> int:
        return int(self._options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))

    @property
    def managed_zones(self) -> list[str]:
        """Zone ids under management, excluding any restored at runtime."""
        configured = self._options.get(CONF_MANAGED_ZONES)
        if configured is None:
            # Default: manage every zone the hub reports.
            configured = list(self.hub.zones)
        return [
            zid
            for zid in configured
            if zid in self.hub.zones and zid not in self._unmanaged_runtime
        ]

    @property
    def connected(self) -> bool:
        return self._connected

    def get_desired(self, zone_id: str) -> DesiredZoneState | None:
        return self._desired.get(zone_id)

    def is_in_sync(self, zone_id: str) -> bool:
        """Whether the hub currently matches desired state for a zone."""
        desired = self._desired.get(zone_id)
        if desired is None or self._profiles is None or zone_id not in self.hub.zones:
            return True
        plan = compute_zone_plan(
            self._observe(zone_id),
            desired,
            self._profiles,
            block_overrides=self.block_overrides,
            override_not_allowed=nobo.API.OVERRIDE_NOT_ALLOWED,
        )
        return plan is None

    # -- Lifecycle -----------------------------------------------------------

    async def async_bootstrap(self) -> None:
        """Run one-time setup; raises ConfigEntryNotReady on unrecoverable error.

        Setup runs after ``hub.start()`` so the socket receiver is live to ack
        the synthetic-profile creation. An options change triggers a full entry
        reload, so bootstrap is also where zones removed from management get
        restored to their pre-takeover state.
        """
        await self._async_load()

        # 0. Restore any zone that was managed last run but isn't any more
        #    (removed from managed_zones via the options flow).
        managed_now = set(self.managed_zones)
        for zid in (set(self._original) | set(self._desired)) - managed_now:
            await self.async_unmanage_zone(zid, persist=False)

        # 1. Snapshot pre-takeover state for every managed zone (never overwrite).
        for zid in self.managed_zones:
            if zid not in self._original:
                zone = self.hub.zones[zid]
                self._original[zid] = {
                    "week_profile_id": zone["week_profile_id"],
                    "override_allowed": zone["override_allowed"],
                }

        # 2. Ensure the two synthetic week profiles exist and are intact.
        await self._async_ensure_profiles()

        # 3. Derive an initial desired state for any newly managed zone.
        for zid in self.managed_zones:
            if zid not in self._desired:
                mode = self.hub.get_current_zone_mode(zid, dt_util.now())
                comfort = self._safe_int(
                    self.hub.zones[zid].get(ATTR_TEMP_COMFORT_C), MIN_TEMPERATURE
                )
                self._desired[zid] = initial_desired_from_hub(
                    mode,
                    comfort,
                    off_name=nobo.API.NAME_OFF,
                    min_temp=MIN_TEMPERATURE,
                    max_temp=MAX_TEMPERATURE,
                )

        await self._async_save()

        # 4. Register callbacks.
        if not self._registered:
            self.hub.register_callback(self._on_hub_update)
            self.hub.register_connection_callback(self._on_connection)
            self._registered = True
        self._connected = getattr(self.hub, "connected", True)

        # 5. Start the safety-net poll and run one full pass.
        self._start_poll()
        await self.async_reconcile_all()

    async def async_shutdown(self) -> None:
        """Deregister callbacks and cancel timers (called on unload)."""
        if self._registered:
            self.hub.deregister_callback(self._on_hub_update)
            self.hub.deregister_connection_callback(self._on_connection)
            self._registered = False
        if self._debounce_cancel is not None:
            self._debounce_cancel()
            self._debounce_cancel = None
        if self._poll_cancel is not None:
            self._poll_cancel()
            self._poll_cancel = None

    # -- Persistence ---------------------------------------------------------

    async def _async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self._desired = {
            zid: DesiredZoneState.from_dict(d)
            for zid, d in data.get("desired", {}).items()
        }
        self._original = dict(data.get("original", {}))
        profiles = data.get("profiles")
        if profiles and profiles.get("on_id") and profiles.get("off_id"):
            self._profiles = SyntheticProfiles(
                on_id=profiles["on_id"], off_id=profiles["off_id"]
            )

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "desired": {
                    zid: d.as_dict() for zid, d in self._desired.items()
                },
                "original": self._original,
                "profiles": (
                    {"on_id": self._profiles.on_id, "off_id": self._profiles.off_id}
                    if self._profiles
                    else None
                ),
            }
        )

    # -- Synthetic profiles --------------------------------------------------

    async def _async_ensure_profiles(self) -> None:
        on_id = await self._async_ensure_one_profile(
            self._profiles.on_id if self._profiles else None,
            PROFILE_NAME_ALWAYS_ON,
            ALWAYS_ON_PROFILE,
        )
        off_id = await self._async_ensure_one_profile(
            self._profiles.off_id if self._profiles else None,
            PROFILE_NAME_ALWAYS_OFF,
            ALWAYS_OFF_PROFILE,
        )
        self._profiles = SyntheticProfiles(on_id=on_id, off_id=off_id)

    async def _async_ensure_one_profile(
        self, cached_id: str | None, name: str, body: list[str]
    ) -> str:
        """Return a valid hub id for the named synthetic profile, creating or
        repairing it as needed. Raises ConfigEntryNotReady if creation can't be
        confirmed within the timeout."""
        # 1. Trust a cached id only if it still exists with the right body.
        if cached_id and cached_id in self.hub.week_profiles:
            existing = self.hub.week_profiles[cached_id]
            if not profile_matches(existing.get("profile"), body):
                _LOGGER.warning(
                    "Synthetic profile %s (id %s) was edited; repairing", name, cached_id
                )
                await self.hub.async_update_week_profile(cached_id, profile=body)
            return cached_id

        # 2. Look for it by name (it may exist from a previous run).
        found = self._find_profile_id(name)
        if found is not None:
            existing = self.hub.week_profiles[found]
            if not profile_matches(existing.get("profile"), body):
                await self.hub.async_update_week_profile(found, profile=body)
            return found

        # 3. Create it; the hub assigns the id, so poll by name until it lands.
        _LOGGER.info("Creating synthetic week profile %s", name)
        await self.hub.async_add_week_profile(name, body)
        deadline = self.hass.loop.time() + PROFILE_DISCOVERY_TIMEOUT_S
        while self.hass.loop.time() < deadline:
            found = self._find_profile_id(name)
            if found is not None:
                return found
            await asyncio.sleep(0.5)

        raise ConfigEntryNotReady(
            f"Timed out waiting for synthetic week profile '{name}' to appear on the hub"
        )

    def _find_profile_id(self, name: str) -> str | None:
        for pid, profile in self.hub.week_profiles.items():
            if _normalize_name(profile.get("name", "")) == name:
                return pid
        return None

    def _default_profile_id(self) -> str | None:
        """A safe fallback week profile id when an original is gone."""
        synthetic = set()
        if self._profiles:
            synthetic = {self._profiles.on_id, self._profiles.off_id}
        if "1" in self.hub.week_profiles and "1" not in synthetic:
            return "1"
        for pid in self.hub.week_profiles:
            if pid not in synthetic:
                return pid
        return None

    # -- Observation ---------------------------------------------------------

    def _observe(self, zone_id: str) -> ObservedZone:
        zone = self.hub.zones[zone_id]
        return ObservedZone(
            week_profile_id=zone["week_profile_id"],
            comfort=self._safe_int(zone.get(ATTR_TEMP_COMFORT_C), MIN_TEMPERATURE),
            eco=self._safe_int(zone.get(ATTR_TEMP_ECO_C), MIN_TEMPERATURE),
            override_allowed=zone["override_allowed"],
        )

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # -- Hub event callbacks (run on HA event loop) --------------------------

    @callback
    def _on_hub_update(self, _hub: nobo) -> None:
        """pynobo pushed a state change: refresh entities and reconcile (debounced)."""
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        self._schedule_reconcile()

    @callback
    def _on_connection(self, _hub: nobo, connected: bool) -> None:
        """Connection state changed."""
        self._connected = connected
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        if connected:
            # Hub state may have drifted while we were disconnected.
            self._schedule_reconcile()

    @callback
    def _schedule_reconcile(self) -> None:
        if self._debounce_cancel is not None:
            self._debounce_cancel()
        self._debounce_cancel = async_call_later(
            self.hass, RECONCILE_DEBOUNCE_S, self._debounced_reconcile
        )

    async def _debounced_reconcile(self, _now) -> None:
        self._debounce_cancel = None
        await self.async_reconcile_all()

    def _start_poll(self) -> None:
        if self._poll_cancel is not None:
            self._poll_cancel()
        self._poll_cancel = async_track_time_interval(
            self.hass, self._poll, timedelta(seconds=self.poll_interval)
        )

    async def _poll(self, _now) -> None:
        await self.async_reconcile_all()

    # -- Reconciliation ------------------------------------------------------

    async def async_reconcile_all(self) -> None:
        if self._profiles is None or not self._connected:
            return
        async with self._write_lock:
            # Clear stray overrides once per pass, then correct each zone.
            await self._reconcile_overrides_locked()
            for zid in self.managed_zones:
                await self._reconcile_zone_locked(zid)

    async def async_reconcile_zone(self, zone_id: str) -> None:
        if self._profiles is None or not self._connected:
            return
        async with self._write_lock:
            await self._reconcile_zone_locked(zone_id)

    async def _reconcile_zone_locked(self, zone_id: str) -> None:
        desired = self._desired.get(zone_id)
        if desired is None or zone_id not in self.hub.zones or self._profiles is None:
            return

        now = self.hass.loop.time()
        backoff = self._backoff.setdefault(
            zone_id,
            BackoffState(
                window=FIGHT_WINDOW_S,
                threshold=FIGHT_THRESHOLD,
                start_delay=BACKOFF_START_S,
                max_delay=BACKOFF_MAX_S,
            ),
        )
        if backoff.should_skip(now):
            return

        plan = compute_zone_plan(
            self._observe(zone_id),
            desired,
            self._profiles,
            block_overrides=self.block_overrides,
            override_not_allowed=nobo.API.OVERRIDE_NOT_ALLOWED,
        )

        if plan is None:
            self._pending_profile.pop(zone_id, None)
            backoff.note_clean(now)
            return

        # Echo grace: if the only outstanding drift is the week profile and it
        # matches a write we issued moments ago, the hub just hasn't echoed it
        # back yet. Don't re-send.
        only_profile = (
            plan.week_profile_id is not None
            and plan.temp_comfort_c is None
            and plan.override_allowed is None
        )
        pending = self._pending_profile.get(zone_id)
        if (
            only_profile
            and pending is not None
            and pending[0] == plan.week_profile_id
            and now - pending[1] < PROFILE_ECHO_GRACE_S
        ):
            return

        try:
            await self.hub.async_update_zone(zone_id, **plan.kwargs())
        except PynoboError as err:
            _LOGGER.warning(
                "Failed to correct zone %s (%s); will retry on next trigger: %s",
                zone_id,
                self.hub.zones.get(zone_id, {}).get("name", zone_id),
                err,
            )
            return

        if plan.week_profile_id is not None:
            self._pending_profile[zone_id] = (plan.week_profile_id, now)

        if backoff.note_correction(now):
            _LOGGER.warning(
                "External controller is fighting zone %s; backing off",
                self.hub.zones.get(zone_id, {}).get("name", zone_id),
            )

    async def _reconcile_overrides_locked(self) -> None:
        clears = compute_override_clears(
            list(self.hub.overrides.values()),
            set(self.managed_zones),
            block_overrides=self.block_overrides,
            clear_global=self.clear_global_overrides,
            normal_mode=nobo.API.OVERRIDE_MODE_NORMAL,
            zone_target=nobo.API.OVERRIDE_TARGET_ZONE,
            global_target=nobo.API.OVERRIDE_TARGET_GLOBAL,
            global_target_id=nobo.API.OVERRIDE_ID_NONE,
        )
        for clear in clears:
            try:
                await self.hub.async_create_override(
                    nobo.API.OVERRIDE_MODE_NORMAL,
                    nobo.API.OVERRIDE_TYPE_NOW,
                    clear.target_type,
                    target_id=clear.target_id,
                )
            except PynoboError as err:
                _LOGGER.warning("Failed to clear override %s: %s", clear, err)

    # -- Commands from the climate entity ------------------------------------

    async def async_set_mode(self, zone_id: str, mode: str) -> None:
        """Set HEAT or OFF for a zone and enforce immediately."""
        current = self._desired.get(zone_id)
        setpoint = current.setpoint if current else MIN_TEMPERATURE
        self._desired[zone_id] = DesiredZoneState(mode=mode, setpoint=setpoint)
        await self._async_save()
        await self.async_reconcile_zone(zone_id)

    async def async_set_setpoint(self, zone_id: str, setpoint: int) -> None:
        """Set the target temperature for a zone and enforce immediately.

        Setting a temperature implies HEAT (a zone with a setpoint is on).
        """
        self._desired[zone_id] = DesiredZoneState(mode="heat", setpoint=setpoint)
        await self._async_save()
        await self.async_reconcile_zone(zone_id)

    # -- Unmanage / restore --------------------------------------------------

    async def async_unmanage_zone(self, zone_id: str, *, persist: bool = True) -> None:
        """Restore a zone to its pre-takeover week profile and override setting,
        and stop managing it at runtime."""
        original = self._original.get(zone_id)
        async with self._write_lock:
            if zone_id in self.hub.zones:
                week_profile_id = None
                override_allowed = None
                if original:
                    week_profile_id = original.get("week_profile_id")
                    override_allowed = original.get("override_allowed")
                if (
                    week_profile_id is None
                    or week_profile_id not in self.hub.week_profiles
                ):
                    fallback = self._default_profile_id()
                    if fallback is not None:
                        _LOGGER.warning(
                            "Original week profile for zone %s is gone; "
                            "restoring to default profile %s",
                            zone_id,
                            fallback,
                        )
                        week_profile_id = fallback
                    else:
                        week_profile_id = None
                try:
                    kwargs = {}
                    if week_profile_id is not None:
                        kwargs["week_profile_id"] = week_profile_id
                    if override_allowed is not None:
                        kwargs["override_allowed"] = override_allowed
                    if kwargs:
                        await self.hub.async_update_zone(zone_id, **kwargs)
                except PynoboError as err:
                    _LOGGER.warning("Failed to restore zone %s: %s", zone_id, err)

        self._unmanaged_runtime.add(zone_id)
        self._desired.pop(zone_id, None)
        self._original.pop(zone_id, None)
        self._backoff.pop(zone_id, None)
        self._pending_profile.pop(zone_id, None)
        if persist:
            await self._async_save()
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
