# Implementation Plan: `nobo_hub_simple` — Simplified Nobø Ecohub integration for Home Assistant

**Audience:** Claude Code, fresh session. This document is self-contained; do not assume prior conversation context.
**Goal:** A Home Assistant custom integration (HACS-installable) forked from the official `nobo_hub` integration that exposes each Nobø zone as a dead-simple climate entity: **HEAT/OFF + one target temperature**, and that **actively enforces** its desired state against any external change (Nobø app, schedules, overrides).

---

## 0. Prerequisites — one panel per zone (user setup, done in the Nobø app)

The Nobø protocol controls **zones**, not individual panels. All behaviour-defining fields live on the zone (`week_profile_id`, `temp_comfort_c`, `override_allowed` — see `STRUCT_KEYS_ZONE`); components (panels) have no setpoint or profile of their own, and component-level overrides are not usable (pynobo's `async_create_override` supports GLOBAL and ZONE targets only, and overrides can't express OFF or a setpoint regardless).

Therefore, for per-panel control, the user must configure **one zone per panel in the Nobø app before installing this integration**. pynobo cannot create zones or reassign components to zones (`async_update_zone` edits existing zones only), so the integration must not attempt restructuring — it consumes whatever zone layout exists.

Requirements for Claude Code:
- Document this prerequisite prominently in the README, including: zone names become HA device/entity names and HomeKit accessory names, so name zones per room/panel in the app first.
- Target deployment is ~5 zones; no scaling work needed, but do not hardcode zone counts anywhere.
- A zone whose sensor-less panel reports no temperature is normal — handled per §9.5.

---

## 1. Background and the two design facts everything hinges on

The official integration lives at
`https://github.com/home-assistant/core/tree/dev/homeassistant/components/nobo_hub`
and uses the `pynobo` library (`https://github.com/echoromeo/pynobo`, on PyPI as `pynobo`).

Its climate entity exposes HVAC modes HEAT/AUTO, four preset modes (none/comfort/eco/away), and `TARGET_TEMPERATURE_RANGE` (two setpoints: eco=low, comfort=high). Plus a per-zone week-profile `select` entity and a global override `select`. This maps confusingly to the Nobø app's mental model and renders terribly via the HomeKit bridge (dual-handle heat-cool UI). We are removing all of that.

**Fact 1 — Nobø overrides cannot turn a zone OFF.**
The hub protocol's override modes are only `NORMAL / COMFORT / ECO / AWAY` (see `nobo.API.OVERRIDE_MODES` in pynobo). "Off" is only reachable as a *week profile state*. Therefore ON/OFF must be implemented by **reassigning the zone's week profile**, not by creating overrides:

- **ON** = assign the zone a synthetic "always comfort" week profile AND set the zone's `temp_comfort_c` to the single target temperature.
- **OFF** = assign the zone a synthetic "always off" week profile. (Heater-side frost protection in OFF is hardware behavior; do not surface it in the UI.)

**Fact 2 — Enforcement is event-driven, not a tight resend timer.**
pynobo keeps a persistent TCP socket to the hub and fires registered callbacks (`hub.register_callback(...)`) within ~1 s of any state change pushed by the hub (e.g., someone using the Nobø app). Enforcement = on every callback (plus a slow safety-net poll), diff observed vs desired state and reassert only on mismatch. Do NOT blind-resend on a short timer: the protocol is stateful/fragile, every `ADD_OVERRIDE` creates a persistent record on the hub, and resends gain no latency over the callback.

---

## 2. Verified pynobo API surface (pynobo ≥ as published June 2026; verify version pin)

Confirmed by inspection of the installed package — use these signatures:

```python
nobo.async_update_zone(zone_id: str, name=None, week_profile_id=None,
                       temp_comfort_c=None, temp_eco_c=None,
                       override_allowed=None) -> None
nobo.async_add_week_profile(name: str, profile: list[str] | None = None) -> None
nobo.async_update_week_profile(week_profile_id: str, name=None, profile=None) -> None
nobo.async_remove_week_profile(week_profile_id: str) -> None
nobo.async_create_override(mode, type, target_type, target_id='-1',
                           end_time='-1', start_time='-1') -> None
nobo.get_current_zone_mode(zone_id, now: datetime | None = None) -> str   # 'comfort'|'eco'|'away'|'off'|...
nobo.get_current_zone_temperature(zone_id)
nobo.register_callback(cb); nobo.deregister_callback(cb)
nobo.hub_serial; nobo.zones; nobo.week_profiles; nobo.overrides; nobo.hub_info
```

Relevant constants on `nobo.API`:

```python
WEEK_PROFILE_STATE_ECO     = '0'
WEEK_PROFILE_STATE_COMFORT = '1'
WEEK_PROFILE_STATE_AWAY    = '2'
WEEK_PROFILE_STATE_OFF     = '4'
OVERRIDE_MODE_NORMAL / OVERRIDE_MODE_COMFORT / OVERRIDE_MODE_ECO / OVERRIDE_MODE_AWAY
OVERRIDE_TYPE_NOW / OVERRIDE_TYPE_CONSTANT
OVERRIDE_TARGET_GLOBAL / OVERRIDE_TARGET_ZONE
OVERRIDE_ALLOWED / OVERRIDE_NOT_ALLOWED          # values for zone 'override_allowed' field
STRUCT_KEYS_ZONE = ['zone_id','name','week_profile_id','temp_comfort_c',
                    'temp_eco_c','override_allowed','deprecated_override_id']
```

**Week profile encoding** (verified from `get_week_profile_status` / `async_add_week_profile` source):
A profile is a `list[str]` of `'HHMMS'` entries — 4-digit time + 1 state digit — covering 7 days starting Monday. Each day begins with a `'0000'`-prefixed entry. Minimal valid profiles:

```python
ALWAYS_ON_PROFILE  = ['00001'] * 7   # comfort all day, every day
ALWAYS_OFF_PROFILE = ['00004'] * 7   # off all day, every day
```

Run `nobo.API.validate_week_profile(...)` on these during development to confirm they pass validation.

**Gotchas confirmed in pynobo source:**
- `async_add_week_profile` does **not** return the new profile id (the hub assigns it). After sending, you must wait for the hub's response push and then locate the profile in `hub.week_profiles` **by name**.
- pynobo replaces spaces in profile names with non-breaking space `\u00A0` before sending. When matching by name, either use names without spaces (recommended: `HA-AlwaysOn`, `HA-AlwaysOff`) or normalize `\u00A0` ↔ space.
- `temp_comfort_c` is stored/sent as whole-degree int/str. Official integration clamps 7–30 °C; keep that.

---

## 3. Repository layout

New standalone repo, HACS custom-integration layout. **New domain `nobo_hub_simple`** so it can coexist with (or cleanly replace) the official integration — but document that running both against the same hub simultaneously is unsupported (they will fight; the hub also has a limited number of concurrent client connections).

```
nobo_hub_simple/
├── hacs.json
├── README.md
├── custom_components/
│   └── nobo_hub_simple/
│       ├── __init__.py        # forked, light edits
│       ├── config_flow.py     # forked, options extended
│       ├── const.py           # forked, extended
│       ├── entity.py          # forked as-is
│       ├── climate.py         # REWRITTEN
│       ├── reconciler.py      # NEW — core of the integration
│       ├── sensor.py          # forked as-is (zone temperature sensors)
│       ├── manifest.json      # new domain, requirements: ["pynobo==<pin latest>"]
│       ├── strings.json / translations/en.json
│       └── (NO select.py — deleted)
└── tests/                     # pytest, mock pynobo
```

Start by vendoring the official component's files from `home-assistant/core` `dev` branch and renaming the domain everywhere. Keep the official `__init__.py` connection logic (it already handles stale-IP UDP rediscovery and clean shutdown on `EVENT_HOMEASSISTANT_STOP`).

---

## 4. Desired-state model

Per managed zone, the integration owns a desired state:

```python
@dataclass
class DesiredZoneState:
    mode: Literal["heat", "off"]
    setpoint: int          # whole °C, 7–30; meaningful only when mode == "heat"
```

**Persistence:** store in the config entry's options (`entry.options["desired"][zone_id]`) and update via `hass.config_entries.async_update_entry`. This survives HA restarts deterministically (preferred over RestoreEntity for the source of truth; the climate entity can still use RestoreEntity as a cosmetic fallback for first boot before the hub connects).

**Initial value on first takeover of a zone:** derive from the hub — if `get_current_zone_mode(zone)` == `'off'` → `mode="off"`; else `mode="heat"`, `setpoint = int(zone['temp_comfort_c'])`.

---

## 5. `reconciler.py` — the core

One `NoboReconciler` per config entry, owning the hub object and desired states.

### 5.1 Bootstrap (during `async_setup_entry`, after `hub.start()`)

1. **Snapshot originals (one-time, before any mutation):** for each zone that will be managed and isn't already snapshotted, save `{zone_id: {"week_profile_id": ..., "override_allowed": ...}}` into `entry.options["original"]`. This is the clean-uninstall restore data. Never overwrite an existing snapshot.
2. **Ensure synthetic profiles exist:** look in `hub.week_profiles` for names `HA-AlwaysOn` / `HA-AlwaysOff`. If missing, `async_add_week_profile(name, profile)` with the constants from §2, then wait (poll `hub.week_profiles` with timeout ~10 s) until they appear; cache their ids in `entry.options`. Validate cached ids on every startup (profiles can be deleted in the Nobø app) and recreate if gone.
3. **Block external overrides (config option, default ON):** for each managed zone, `async_update_zone(zone_id, override_allowed=nobo.API.OVERRIDE_NOT_ALLOWED)`. This makes the hub itself reject Nobø-app overrides for the zone — most enforcement happens for free.
4. Register `hub.register_callback(self._on_hub_update)`.
5. Run one full reconcile pass.

### 5.2 Reconcile algorithm (idempotent; runs on callback, on slow poll, and after every user command)

For each managed zone, compare observed vs desired:

```
observed_profile  = hub.zones[zid]['week_profile_id']
observed_setpoint = int(hub.zones[zid]['temp_comfort_c'])
observed_override_allowed = hub.zones[zid]['override_allowed']

want_profile = on_profile_id if desired.mode == "heat" else off_profile_id

drift if:
  observed_profile != want_profile
  or (desired.mode == "heat" and observed_setpoint != desired.setpoint)
  or (block_overrides and observed_override_allowed != OVERRIDE_NOT_ALLOWED)
```

On drift: a single `async_update_zone(zid, week_profile_id=want_profile, temp_comfort_c=..., override_allowed=...)` carrying only the fields that drifted (one command per zone per pass).

Additionally, if any **active zone override** targets a managed zone, or a **global override** ≠ NORMAL exists while any zone is managed and override-blocking is on, clear it: `async_create_override(OVERRIDE_MODE_NORMAL, OVERRIDE_TYPE_NOW, OVERRIDE_TARGET_ZONE/GLOBAL, target_id)`. (With `OVERRIDE_NOT_ALLOWED` set this should be rare — global overrides created in the app may still exist as records; clearing globally is a config option, default ON, because it affects unmanaged zones too. Document that.)

### 5.3 Scheduling, debounce, loop protection

- **Trigger sources:** (a) pynobo callback → schedule reconcile with **2 s debounce** (coalesce bursts; the hub pushes several messages per change); (b) safety-net poll every **120 s** (configurable 60–600 s); (c) immediately after any HA-initiated command.
- **Echo suppression:** after the reconciler writes, the hub echoes the change back as a callback. The idempotent diff naturally no-ops on the echo; no write-suppression window needed, but assert in tests that an echo does not trigger a second write.
- **Fight detection / backoff:** count corrections per zone in a rolling 5-minute window. If > 10, log `WARNING "External controller is fighting zone %s; backing off"` and switch that zone to exponential backoff (4 s → 8 s → … cap 60 s) until a window passes with ≤ 1 correction. Never silently spin.
- **Write failures:** `PynoboError` → log, retry on next trigger. Don't crash the entity; mark it `available = False` only if the hub connection itself drops (pynobo has `register_connection_callback` — use it for availability).

---

## 6. `climate.py` — rewritten entity

```python
_attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
_attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE
                            | ClimateEntityFeature.TURN_ON
                            | ClimateEntityFeature.TURN_OFF)
_attr_min_temp, _attr_max_temp = 7, 30        # slider bounds; HomeKit requires bounds — this is fine
_attr_target_temperature_step = 1
_attr_temperature_unit = UnitOfTemperature.CELSIUS
```

- **No preset modes. No temperature range. No AUTO.**
- `hvac_mode` (reported) = desired mode (the integration is authoritative); optionally flag drift via an extra state attribute `in_sync: bool`.
- `target_temperature` = desired setpoint; `current_temperature` from `get_current_zone_temperature` (may be `None` if the zone has no sensor — handle it, the official code does).
- `hvac_action`: `OFF` when off; else `HEATING` if `current_temperature is not None and current < target` else `IDLE`; `None`/omit if no sensor. This gives HomeKit a proper heating indicator.
- Commands (`async_set_hvac_mode`, `async_turn_on/off`, `async_set_temperature`) → update desired state in the reconciler → `await reconciler.reconcile_zone(zid)` → `async_write_ha_state()`. Round/clamp incoming temperature to int 7–30.
- Subscribe to reconciler/hub updates via the existing `NoboBaseEntity` callback pattern (keep `should_poll` behavior consistent with how the fork's entity.py dispatches updates).
- Keep unique IDs **identical in format** to the official integration (`f"{hub.hub_serial}:{zone_id}"`) but under the new domain — entities are distinct; do not attempt registry migration in v1.

**HomeKit outcome to verify manually:** entity exposes as a plain HomeKit thermostat with Off/Heat and a single setpoint dial.

---

## 7. Config flow / options

Fork the official `config_flow.py` (discovery + serial entry) unchanged for the connection step. Extend the **options flow**:

- `managed_zones: list[zone_id]` (multi-select, default: all zones) — unmanaged zones get NO climate entity from this integration (don't create a half-managed entity).
- `block_overrides: bool = True` (sets `OVERRIDE_NOT_ALLOWED` on managed zones)
- `clear_global_overrides: bool = True`
- `poll_interval: int = 120` (60–600)
- Drop the official `CONF_OVERRIDE_TYPE` option (now/constant) — irrelevant in this design except for the NORMAL-clearing call, which hardcodes `OVERRIDE_TYPE_NOW`.

On options change: re-run bootstrap (snapshot newly managed zones, restore newly *un*managed zones from snapshot — see §8).

---

## 8. Clean uninstall / unmanage path

When a zone is removed from `managed_zones`, or via a dedicated `nobo_hub_simple.restore_zone` service, restore from the §5.1 snapshot: `async_update_zone(zid, week_profile_id=original, override_allowed=original)`. If the original week profile no longer exists on the hub, fall back to the hub's default profile (id `'1'` is conventionally "Default"; verify against `hub.week_profiles` at runtime rather than hardcoding) and log a warning.
Optionally on full config-entry removal: offer to delete the synthetic profiles (`async_remove_week_profile`) — only if no zone still references them. Document in README that the two `HA-AlwaysOn`/`HA-AlwaysOff` profiles are visible in the Nobø app and must not be edited there.

---

## 9. Edge cases Claude Code must handle

1. **Profile-id discovery race:** hub assigns ids for new week profiles; after `async_add_week_profile`, poll `hub.week_profiles` for the name (remember `\u00A0` normalization) with timeout; fail setup with `ConfigEntryNotReady` if not found.
2. **Synthetic profile deleted/edited in the app:** detect on startup *and* in reconcile (check `hub.week_profiles[id]['profile']` still equals the expected constant); recreate/repair as drift.
3. **Zone deleted in the app:** mark entity unavailable (official `_read_state` already does this); drop its desired state.
4. **Hub reboot / reconnect:** pynobo reconnects; on connection-restored callback run a full reconcile (hub state may have changed while disconnected).
5. **No temperature sensor in zone:** `current_temperature = None`, no `hvac_action` heating/idle inference.
6. **Concurrent commands:** serialize hub writes through a single asyncio lock in the reconciler (`PARALLEL_UPDATES = 0` stays, but the reconciler is the real serialization point).
7. **Whole-degree setpoints only:** HA UI may send floats; round and reflect the rounded value back.

---

## 9.5 Operational considerations (document in README; some are verify items)

1. **Fail-safe state when HA is offline:** managed zones have no schedule — the hub holds the last commanded state (profile + setpoint) indefinitely. If HA is down, heating continues at the last setpoint with no fallback. Manual control via the Nobø app remains possible during an outage, but note that with `block_overrides` on, the app's quick override buttons are disabled for managed zones — control is via editing the zone's comfort temperature or reassigning its week profile in zone settings. Document this in the README's troubleshooting section. Document this prominently; mitigations are the §8 restore service or setting zones to a low setpoint before extended absence. Do NOT attempt cleverness like auto-restoring schedules on disconnect — the hub can't know whether HA is gone or just restarting.
2. **HomeKit setpoint floor:** HomeKit's `TargetTemperature` characteristic has a minimum of 10 °C. Setpoints of 7–9 °C are settable from HA but not from iOS; if a zone is set below 10 in HA, the Home app may display it clamped. Note in README; add to manual checklist (verify the HA HomeKit bridge's actual clamping behaviour rather than working around it preemptively).
3. **Concurrent hub connections (verify):** the Ecohub's limit on simultaneous local TCP clients is undocumented here. The Nobø app primarily uses the cloud path so coexistence is expected to work, but confirm on hardware: with the integration connected, the app on the same LAN can still control unmanaged zones. Never run two local integrations (official + this fork) against the same hub.
4. **Non-issues by design (do not add handling):** hub clock/timezone accuracy (constant profiles have no time component), `temp_eco_c` (never entered by the always-comfort profile; leave the field untouched), week-profile count limits (only 2 added).



- **Unit tests with a mocked `nobo` object** (mirror the official integration's test fixtures in `tests/components/nobo_hub/` of `home-assistant/core` as a starting point):
  - reconcile no-ops when observed == desired (echo suppression)
  - each drift type triggers exactly one corrective write
  - fight-detection backoff engages and releases
  - ON/OFF flows produce correct `async_update_zone` arguments (profile ids, temp)
  - snapshot/restore round-trip
  - options change adds/removes managed zones correctly
- **Manual hardware checklist (cannot be automated):**
  - `validate_week_profile(['00001']*7)` and `['00004']*7` accepted by a real hub
  - hub accepts `OVERRIDE_NOT_ALLOWED` zone update and the app then refuses overrides on that zone
  - latency: change zone in Nobø app → correction observed within ~3 s
  - HomeKit bridge renders single-setpoint Off/Heat thermostat
  - HomeKit: setpoint below 10 °C set in HA — confirm how the Home app displays/clamps it (§9.5.2)
  - Nobø app on same LAN can still control unmanaged zones while integration is connected (§9.5.3)

---

## 11. Out of scope (explicitly)

- Upstream PR to `home-assistant/core` — this behavior is intentionally opinionated; ship as HACS custom integration only.
- Eco/away/scheduling features of any kind.
- Component-level (per-heater) control; zones only, matching the official integration.
- Energy/price features.

## 12. Acceptance criteria

1. Each managed zone appears as exactly one climate entity: modes Off/Heat, one setpoint (7–30 °C, whole degrees), current temperature when available. No selects, no presets, no range.
2. Turning a zone off in HA results in the zone reporting `off` on the hub within 3 s; on at T° likewise reports `comfort` at T°.
3. Any change made in the Nobø app to a managed zone (mode, setpoint, profile, override) is reverted within ~3 s of the hub pushing it, without log spam or write loops.
4. State survives HA restart and hub reconnect.
5. Removing a zone from management restores its pre-takeover week profile and override permission.
6. Via HomeKit bridge: plain thermostat, Off/Heat, single dial.

---

## 13. References

- Official integration source: https://github.com/home-assistant/core/tree/dev/homeassistant/components/nobo_hub
- Official integration docs: https://www.home-assistant.io/integrations/nobo_hub/
- pynobo: https://github.com/echoromeo/pynobo (protocol notes in repo; API constants quoted in §2 verified against the published package)
- HA climate entity developer docs: https://developers.home-assistant.io/docs/core/entity/climate/
- HA HomeKit bridge: https://www.home-assistant.io/integrations/homekit/
- HACS custom integration structure: https://hacs.xyz/docs/publish/integration/
