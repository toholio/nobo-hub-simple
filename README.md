# Nobø Ecohub (Simple) — Home Assistant custom integration

A deliberately opinionated fork of the official [`nobo_hub`](https://www.home-assistant.io/integrations/nobo_hub/)
integration. Each Nobø **zone** is exposed as a dead-simple climate entity —
**Heat / Off plus a single target temperature** — and the integration
**actively enforces** that desired state against any external change (the Nobø
app, schedules, overrides). No presets, no dual setpoints, no week-profile or
override `select` entities.

This renders cleanly through the HomeKit bridge as a plain thermostat with an
Off/Heat toggle and one setpoint dial.

> ⚠️ **Do not run this and the official `nobo_hub` integration against the same
> hub at the same time.** They will fight each other, and the Ecohub allows only
> a limited number of concurrent local TCP clients. This integration uses a
> separate domain (`nobo_hub_simple`) so it can coexist in the same HA install,
> but point each hub at exactly one integration.

---

## Prerequisite: one zone per panel (do this in the Nobø app first)

The Nobø protocol controls **zones**, not individual panels/heaters. Everything
this integration does — on/off, setpoint, override blocking — happens at the
zone level. Component-level (per-heater) control is not possible over the
protocol.

**Therefore, before installing, configure one zone per panel in the Nobø app.**
This integration consumes whatever zone layout exists; it cannot create zones or
move components between zones.

Also note:

- **Zone names become the HA device/entity names and the HomeKit accessory
  names.** Name your zones per room/panel in the app *before* setting up the
  integration.
- A zone with no temperature sensor simply reports no current temperature —
  this is normal and handled.

---

## Installation (HACS)

1. In HACS → Integrations → ⋯ → **Custom repositories**, add this repository as
   an *Integration*.
2. Install **Nobø Ecohub (Simple)** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Nobø Ecohub (Simple).**
   The hub is auto-discovered on the LAN (enter the last 3 digits of the
   serial), or choose *Manual* to enter the full 12-digit serial and IP.

Requires `pynobo==1.9.0` (installed automatically).

---

## How it works

Two facts drive the whole design:

1. **Nobø overrides cannot turn a zone off.** The hub's override modes are only
   Normal/Comfort/Eco/Away. "Off" exists only as a *week-profile state*. So
   on/off is implemented by reassigning the zone's week profile:
   - **On**  → a synthetic *always-comfort* week profile (`HA-AlwaysOn`) and the
     zone's comfort temperature set to your single setpoint.
   - **Off** → a synthetic *always-off* week profile (`HA-AlwaysOff`).
2. **Enforcement is event-driven.** pynobo keeps a persistent socket to the hub
   and pushes state changes within ~1 s. On every push (debounced), a slow
   safety-net poll, and immediately after any HA command, the integration diffs
   observed vs desired state and reasserts **only on mismatch** — no blind
   resend timer.

The two `HA-AlwaysOn` / `HA-AlwaysOff` week profiles are created automatically
and are **visible in the Nobø app**. Do not edit them there; the integration
repairs them if it detects tampering.

---

## Options

**Settings → Devices & Services → Nobø Ecohub (Simple) → Configure.**

| Option | Default | Meaning |
| --- | --- | --- |
| **Managed zones** | all zones | Only these zones get a climate entity and are enforced. Unmanaged zones are left completely untouched. |
| **Block overrides from the Nobø app** | on | Sets `override_allowed = NOT_ALLOWED` on managed zones so the hub itself refuses app overrides — most enforcement then happens for free. |
| **Clear global overrides** | on | Also resets any non-normal *global* override back to normal. Note this affects unmanaged zones too. |
| **Report setpoint as current temperature** | on | For zones whose panel has **no temperature sensor**, show the target temperature as the current temperature (and light the heating indicator while on) instead of reporting nothing. See below. Zones that have a sensor are unaffected. |
| **Safety-net poll interval** | 120 s | How often (60–600 s) to re-check the hub in addition to instant push enforcement. |

Changing options reloads the integration. Removing a zone from *Managed zones*
restores its original (pre-takeover) week profile and override setting.

### Service: `nobo_hub_simple.restore_zone`

Restores a single zone to its pre-takeover week profile/override and stops
managing it until you re-add it via options. Field: `zone_id`.

---

## Operational notes & troubleshooting

- **When Home Assistant is offline**, managed zones hold their last commanded
  state on the hub *indefinitely* — there is no schedule fallback. Heating
  continues at the last setpoint. With *Block overrides* on, the Nobø app's
  quick-override buttons are disabled for managed zones; to change a zone during
  an HA outage, edit the zone's comfort temperature or week profile in the app's
  zone settings, or use the `restore_zone` service before a long absence.
- **Zones without a temperature sensor.** Many Nobø panels are receivers with no
  sensor, so the zone reports no current temperature. A HomeKit thermostat
  always needs a current-temperature value, so the HomeKit bridge would
  otherwise show a misleading fallback (~21 °C). The **Report setpoint as current
  temperature** option (on by default) makes such zones report their target as
  the current temperature and light the heating indicator while on. Zones that
  *do* have a sensor always report the real reading. Turn the option off if you
  prefer the entity to report no current temperature (e.g. you template it
  yourself).
- **HomeKit minimum setpoint is 10 °C.** You can set 7–9 °C from Home Assistant,
  but the iOS Home app may clamp/display it at 10 °C.
- **Setpoints are whole degrees, 7–30 °C.** Fractional values from the UI are
  rounded.
- **`in_sync` attribute**: each climate entity exposes `in_sync` — `false`
  briefly while a correction is in flight, or persistently if an external
  controller is fighting the zone (a warning is logged and the integration backs
  off exponentially rather than spinning).

---

## Design deviations from the original plan

- **Persistence uses `helpers.storage.Store`, not the config-entry options**, for
  the per-zone desired state, the pre-takeover snapshot, and the synthetic
  profile ids. Writing those to options on every command would trigger an entry
  reload each time. Genuine *configuration* (managed zones, the toggles, poll
  interval) still lives in options.
- **DHCP auto-discovery is dropped** from this fork to avoid two integrations
  racing to claim the same discovered hub. Use LAN (UDP) discovery or manual
  entry in the config flow.

---

## Out of scope

Eco/away/scheduling features, per-heater control, energy/price features, and an
upstream PR to Home Assistant core (this behaviour is intentionally
opinionated — HACS only).

## License

See [LICENSE](LICENSE).
