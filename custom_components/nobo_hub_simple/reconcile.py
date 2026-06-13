"""Pure reconciliation logic for the Nobø Ecohub (Simple) integration.

This module intentionally imports **nothing** from Home Assistant or pynobo.
All decision-making lives here as plain functions over plain data so it can be
unit-tested without a running hub or HA instance. The reconciler
(`reconciler.py`) wires this logic to the live `pynobo` hub object and HA's
event loop, passing pynobo's constant *values* in rather than importing them
here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ZoneMode = Literal["heat", "off"]


@dataclass(frozen=True)
class DesiredZoneState:
    """The state the integration wants a zone to be in."""

    mode: ZoneMode
    setpoint: int  # whole °C, 7-30; only meaningful when mode == "heat"

    def as_dict(self) -> dict:
        """Serialise for persistence."""
        return {"mode": self.mode, "setpoint": self.setpoint}

    @classmethod
    def from_dict(cls, data: dict) -> "DesiredZoneState":
        """Deserialise from persisted storage."""
        return cls(mode=data["mode"], setpoint=int(data["setpoint"]))


@dataclass(frozen=True)
class ObservedZone:
    """A snapshot of the hub's current view of a zone."""

    week_profile_id: str
    comfort: int
    eco: int
    override_allowed: str


@dataclass(frozen=True)
class SyntheticProfiles:
    """Resolved hub ids of the two synthetic week profiles."""

    on_id: str
    off_id: str


@dataclass(frozen=True)
class ZonePlan:
    """The single `async_update_zone` call needed to correct one zone.

    Only fields that actually drifted are populated; everything else is left
    ``None`` so the corresponding hub field is untouched.
    """

    week_profile_id: str | None = None
    temp_comfort_c: int | None = None
    temp_eco_c: int | None = None
    override_allowed: str | None = None

    def kwargs(self) -> dict:
        """Return non-None fields as kwargs for ``nobo.async_update_zone``."""
        out: dict = {}
        if self.week_profile_id is not None:
            out["week_profile_id"] = self.week_profile_id
        if self.temp_comfort_c is not None:
            out["temp_comfort_c"] = self.temp_comfort_c
        if self.temp_eco_c is not None:
            out["temp_eco_c"] = self.temp_eco_c
        if self.override_allowed is not None:
            out["override_allowed"] = self.override_allowed
        return out


def clamp_setpoint(value: float, lo: int, hi: int) -> int:
    """Round to a whole degree and clamp into [lo, hi]."""
    return max(lo, min(hi, round(value)))


def compute_zone_plan(
    observed: ObservedZone,
    desired: DesiredZoneState,
    profiles: SyntheticProfiles,
    *,
    block_overrides: bool,
    override_not_allowed: str,
) -> ZonePlan | None:
    """Diff observed vs desired and return the corrective plan, or None.

    Returns ``None`` when the zone already matches desired state (the common
    case on echo callbacks — this is what makes reconciliation idempotent and
    loop-free).

    The eco-temperature handling deserves a note: pynobo's
    ``async_update_zone`` rejects any update where comfort < eco
    (``PynoboValidationError``). When the user picks a low setpoint that falls
    below the zone's stored eco temperature, we must lower eco in the *same*
    command, otherwise the write fails. We never *raise* eco (it is irrelevant
    to the always-comfort/always-off profiles), only lower it when forced.
    """

    want_profile = profiles.on_id if desired.mode == "heat" else profiles.off_id

    week_profile_id: str | None = None
    if observed.week_profile_id != want_profile:
        week_profile_id = want_profile

    temp_comfort_c: int | None = None
    if desired.mode == "heat" and observed.comfort != desired.setpoint:
        temp_comfort_c = desired.setpoint

    override_allowed: str | None = None
    if block_overrides and observed.override_allowed != override_not_allowed:
        override_allowed = override_not_allowed

    # Nothing drifted -> no write.
    if (
        week_profile_id is None
        and temp_comfort_c is None
        and override_allowed is None
    ):
        return None

    # Eco clamp: ensure the comfort value carried in this update is never
    # below the zone's eco value, or pynobo will reject the whole command.
    effective_comfort = temp_comfort_c if temp_comfort_c is not None else observed.comfort
    temp_eco_c: int | None = None
    if observed.eco > effective_comfort:
        temp_eco_c = effective_comfort

    return ZonePlan(
        week_profile_id=week_profile_id,
        temp_comfort_c=temp_comfort_c,
        temp_eco_c=temp_eco_c,
        override_allowed=override_allowed,
    )


@dataclass(frozen=True)
class OverrideClear:
    """An override that should be reset to NORMAL."""

    target_type: str
    target_id: str


def compute_override_clears(
    overrides: list[dict],
    managed_zone_ids: set[str],
    *,
    block_overrides: bool,
    clear_global: bool,
    normal_mode: str,
    zone_target: str,
    global_target: str,
    global_target_id: str = "-1",
) -> list[OverrideClear]:
    """Decide which active overrides must be cleared back to NORMAL.

    - Zone overrides on a managed zone are cleared when override-blocking is on
      (they should already be refused by the hub, but stale records can linger).
    - Global overrides are cleared when ``clear_global`` is on; note this
      affects unmanaged zones too, which is why it is a separate option.
    """
    clears: list[OverrideClear] = []
    seen: set[tuple[str, str]] = set()
    for ov in overrides:
        if ov.get("mode") == normal_mode:
            continue
        target_type = ov.get("target_type")
        target_id = ov.get("target_id")
        if (
            block_overrides
            and target_type == zone_target
            and target_id in managed_zone_ids
        ):
            key = (zone_target, target_id)
        elif clear_global and target_type == global_target:
            key = (global_target, global_target_id)
        else:
            continue
        if key not in seen:
            seen.add(key)
            clears.append(OverrideClear(target_type=key[0], target_id=key[1]))
    return clears


def profile_matches(profile: list[str] | None, expected: list[str]) -> bool:
    """Whether a hub week-profile body still equals our synthetic constant.

    Used to detect a synthetic profile that was edited in the Nobø app, so the
    reconciler can repair it.
    """
    return profile == expected


@dataclass
class BackoffState:
    """Per-zone fight detection and exponential backoff.

    When an external controller repeatedly fights us (more corrections than
    ``threshold`` within ``window`` seconds), we stop hammering the fragile hub
    protocol and back off exponentially until the fight subsides. All time is
    supplied by the caller (monotonic seconds) to keep this testable.
    """

    window: float
    threshold: int
    start_delay: float
    max_delay: float
    corrections: list[float] = field(default_factory=list)
    backoff_until: float = 0.0
    _delay: float = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self.corrections = [t for t in self.corrections if t >= cutoff]

    def note_correction(self, now: float) -> bool:
        """Record a corrective write. Returns True if (further) backoff engaged."""
        self.corrections.append(now)
        self._prune(now)
        if len(self.corrections) > self.threshold:
            self._delay = (
                self.start_delay
                if self._delay == 0.0
                else min(self._delay * 2, self.max_delay)
            )
            self.backoff_until = now + self._delay
            return True
        return False

    def note_clean(self, now: float) -> None:
        """Record a drift-free pass; release backoff once the fight subsides."""
        self._prune(now)
        if now >= self.backoff_until and len(self.corrections) <= 1:
            self._delay = 0.0
            self.backoff_until = 0.0

    def should_skip(self, now: float) -> bool:
        """Whether reconciliation should be deferred for this zone right now."""
        return now < self.backoff_until

    @property
    def is_backing_off(self) -> bool:
        return self.backoff_until > 0.0


def compute_current_and_action(
    observed_current: float | None,
    setpoint: int,
    mode: ZoneMode,
    *,
    assume_from_target: bool,
) -> tuple[float | None, str | None]:
    """Compute the climate entity's displayed current temperature and action.

    HomeKit thermostats require a current-temperature value; a zone whose panel
    has no sensor reports ``None``, which the HomeKit bridge renders as a
    misleading fallback (~21 °C). When ``assume_from_target`` is set, we
    substitute the setpoint as the current temperature for such zones so the
    HomeKit dial reads sensibly, and light the "heating" indicator while on
    (the panel is energised toward the setpoint even though we can't measure
    the room).

    Returns ``(current_temperature, action)`` where action is one of
    ``"off"``, ``"heating"``, ``"idle"`` or ``None`` (unknown — no sensor and
    no substitution).

    Zones that report a real sensor value are unaffected: the substitution only
    applies when ``observed_current is None``.
    """
    current = observed_current
    synthesized = False
    if current is None and assume_from_target:
        current = float(setpoint)
        synthesized = True

    if mode == "off":
        action: str | None = "off"
    elif current is None:
        # Heat mode, no sensor, and substitution disabled: can't infer.
        action = None
    elif synthesized:
        # Sensorless zone, on: assume the panel is heating toward setpoint.
        action = "heating"
    elif current < setpoint:
        action = "heating"
    else:
        action = "idle"

    return current, action


def initial_desired_from_hub(
    current_mode: str,
    comfort_c: int,
    *,
    off_name: str,
    min_temp: int,
    max_temp: int,
) -> DesiredZoneState:
    """Derive the first desired state when a zone is taken over.

    ``current_mode`` is the result of ``nobo.get_current_zone_mode``; ``off_name``
    is ``nobo.API.NAME_OFF``.
    """
    if current_mode == off_name:
        return DesiredZoneState(mode="off", setpoint=clamp_setpoint(comfort_c, min_temp, max_temp))
    return DesiredZoneState(
        mode="heat", setpoint=clamp_setpoint(comfort_c, min_temp, max_temp)
    )
