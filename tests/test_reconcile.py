"""Unit tests for the pure reconciliation logic.

These exercise ``custom_components.nobo_hub_simple.reconcile`` directly. That
module imports nothing from Home Assistant or pynobo, so these run with plain
pytest and no hub.

The pynobo constant *values* are hard-coded here to match
``pynobo.nobo.API`` (verified against pynobo 1.9.0):
    OVERRIDE_NOT_ALLOWED   = '0'
    OVERRIDE_ALLOWED       = '1'
    OVERRIDE_MODE_NORMAL   = '0'
    OVERRIDE_TARGET_GLOBAL = '0'
    OVERRIDE_TARGET_ZONE   = '1'
    NAME_OFF               = 'off'
"""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "nobo_hub_simple"),
)

import reconcile as r  # noqa: E402

OVERRIDE_NOT_ALLOWED = "0"
OVERRIDE_ALLOWED = "1"
NORMAL = "0"
GLOBAL = "0"
ZONE = "1"

PROFILES = r.SyntheticProfiles(on_id="100", off_id="101")


def observed(profile="100", comfort=21, eco=15, override_allowed=OVERRIDE_NOT_ALLOWED):
    return r.ObservedZone(
        week_profile_id=profile,
        comfort=comfort,
        eco=eco,
        override_allowed=override_allowed,
    )


def plan(obs, desired, block=True):
    return r.compute_zone_plan(
        obs,
        desired,
        PROFILES,
        block_overrides=block,
        override_not_allowed=OVERRIDE_NOT_ALLOWED,
    )


# --- echo suppression / idempotency ----------------------------------------


def test_no_drift_returns_none_heat():
    desired = r.DesiredZoneState(mode="heat", setpoint=21)
    assert plan(observed(profile="100", comfort=21), desired) is None


def test_no_drift_returns_none_off():
    desired = r.DesiredZoneState(mode="off", setpoint=21)
    assert plan(observed(profile="101", comfort=21), desired) is None


# --- one corrective write per drift type ------------------------------------


def test_profile_drift_only():
    desired = r.DesiredZoneState(mode="heat", setpoint=21)
    p = plan(observed(profile="999", comfort=21), desired)
    assert p is not None
    assert p.kwargs() == {"week_profile_id": "100"}


def test_setpoint_drift_only():
    desired = r.DesiredZoneState(mode="heat", setpoint=23)
    p = plan(observed(profile="100", comfort=21), desired)
    assert p.kwargs() == {"temp_comfort_c": 23}


def test_override_allowed_drift_only():
    desired = r.DesiredZoneState(mode="heat", setpoint=21)
    p = plan(observed(profile="100", comfort=21, override_allowed=OVERRIDE_ALLOWED), desired)
    assert p.kwargs() == {"override_allowed": OVERRIDE_NOT_ALLOWED}


def test_block_overrides_disabled_ignores_override_allowed():
    desired = r.DesiredZoneState(mode="heat", setpoint=21)
    p = plan(
        observed(profile="100", comfort=21, override_allowed=OVERRIDE_ALLOWED),
        desired,
        block=False,
    )
    assert p is None


def test_turn_on_from_off_sets_profile_and_comfort():
    desired = r.DesiredZoneState(mode="heat", setpoint=22)
    p = plan(observed(profile="101", comfort=18), desired)
    assert p.kwargs() == {"week_profile_id": "100", "temp_comfort_c": 22}


def test_turn_off_sets_off_profile_only():
    desired = r.DesiredZoneState(mode="off", setpoint=22)
    # comfort stays whatever it was; we don't touch it when off.
    p = plan(observed(profile="100", comfort=22), desired)
    assert p.kwargs() == {"week_profile_id": "101"}


# --- the eco-clamp gotcha ---------------------------------------------------


def test_low_setpoint_below_eco_also_lowers_eco():
    desired = r.DesiredZoneState(mode="heat", setpoint=12)
    p = plan(observed(profile="100", comfort=21, eco=15), desired)
    assert p.temp_comfort_c == 12
    assert p.temp_eco_c == 12  # eco lowered to keep comfort >= eco


def test_setpoint_above_eco_leaves_eco_untouched():
    desired = r.DesiredZoneState(mode="heat", setpoint=20)
    p = plan(observed(profile="100", comfort=21, eco=15), desired)
    assert p.temp_comfort_c == 20
    assert p.temp_eco_c is None


# --- FakeHub mirrors pynobo validation: the plan must never raise -----------


class PynoboValidationError(Exception):
    pass


class FakeHub:
    """Minimal hub mirroring pynobo.async_update_zone's comfort>=eco rule."""

    def __init__(self, zone):
        self.zones = {"z": dict(zone)}

    def update_zone(self, **kwargs):
        z = self.zones["z"]
        comfort = int(kwargs.get("temp_comfort_c", z["temp_comfort_c"]))
        eco = int(kwargs.get("temp_eco_c", z["temp_eco_c"]))
        # This is the exact check pynobo performs (1.9.0 line 1203).
        if comfort < eco:
            raise PynoboValidationError(
                f"Comfort temperature({comfort}) cannot be less than eco({eco})"
            )
        z.update({k: int(v) if "temp" in k else v for k, v in kwargs.items()})


@pytest.mark.parametrize("setpoint", [7, 9, 12, 15, 16, 20, 30])
def test_plan_never_violates_pynobo_eco_rule(setpoint):
    hub = FakeHub({"week_profile_id": "100", "temp_comfort_c": 21, "temp_eco_c": 16})
    obs = observed(profile="100", comfort=21, eco=16)
    desired = r.DesiredZoneState(mode="heat", setpoint=setpoint)
    p = plan(obs, desired)
    # Applying our plan to a hub with pynobo's validation must not raise.
    if p is not None:
        hub.update_zone(**p.kwargs())
    assert int(hub.zones["z"]["temp_comfort_c"]) == setpoint


# --- override clearing ------------------------------------------------------


def _clears(overrides, managed, block=True, clear_global=True):
    return r.compute_override_clears(
        overrides,
        set(managed),
        block_overrides=block,
        clear_global=clear_global,
        normal_mode=NORMAL,
        zone_target=ZONE,
        global_target=GLOBAL,
        global_target_id="-1",
    )


def test_normal_overrides_ignored():
    overrides = [{"mode": NORMAL, "target_type": ZONE, "target_id": "z1"}]
    assert _clears(overrides, ["z1"]) == []


def test_zone_override_on_managed_zone_cleared():
    overrides = [{"mode": "1", "target_type": ZONE, "target_id": "z1"}]
    clears = _clears(overrides, ["z1"])
    assert clears == [r.OverrideClear(target_type=ZONE, target_id="z1")]


def test_zone_override_on_unmanaged_zone_not_cleared():
    overrides = [{"mode": "1", "target_type": ZONE, "target_id": "z2"}]
    assert _clears(overrides, ["z1"]) == []


def test_global_override_cleared_when_enabled():
    overrides = [{"mode": "2", "target_type": GLOBAL, "target_id": "-1"}]
    clears = _clears(overrides, ["z1"])
    assert clears == [r.OverrideClear(target_type=GLOBAL, target_id="-1")]


def test_global_override_not_cleared_when_disabled():
    overrides = [{"mode": "2", "target_type": GLOBAL, "target_id": "-1"}]
    assert _clears(overrides, ["z1"], clear_global=False) == []


def test_override_clears_deduped():
    overrides = [
        {"mode": "1", "target_type": GLOBAL, "target_id": "-1"},
        {"mode": "2", "target_type": GLOBAL, "target_id": "-1"},
    ]
    assert len(_clears(overrides, ["z1"])) == 1


# --- fight detection / backoff ----------------------------------------------


def make_backoff():
    return r.BackoffState(window=300.0, threshold=10, start_delay=4.0, max_delay=60.0)


def test_backoff_engages_after_threshold():
    b = make_backoff()
    engaged = False
    for i in range(11):
        engaged = b.note_correction(now=float(i))
    assert engaged is True
    assert b.is_backing_off
    assert b.should_skip(now=11.0) is True


def test_backoff_does_not_engage_under_threshold():
    b = make_backoff()
    for i in range(10):
        assert b.note_correction(now=float(i)) is False
    assert not b.is_backing_off


def test_backoff_exponential_growth_capped():
    b = make_backoff()
    # Engage repeatedly at the same instant to force escalation.
    delays = []
    for _ in range(6):
        for _ in range(11):
            b.note_correction(now=100.0)
        delays.append(b.backoff_until - 100.0)
        b.corrections.clear()
    # 4, 8, 16, 32, 60(capped), 60
    assert delays[0] == 4.0
    assert delays[-1] == 60.0
    assert max(delays) <= 60.0


def test_backoff_releases_after_clean_window():
    b = make_backoff()
    for i in range(11):
        b.note_correction(now=float(i))
    assert b.is_backing_off
    # Long after the window with no fresh corrections, a clean pass releases.
    b.note_clean(now=1000.0)
    assert not b.is_backing_off
    assert b.should_skip(now=1000.0) is False


# --- initial desired derivation & helpers -----------------------------------


def test_initial_desired_off():
    d = r.initial_desired_from_hub("off", 21, off_name="off", min_temp=7, max_temp=30)
    assert d.mode == "off"


def test_initial_desired_heat():
    d = r.initial_desired_from_hub("comfort", 23, off_name="off", min_temp=7, max_temp=30)
    assert d == r.DesiredZoneState(mode="heat", setpoint=23)


@pytest.mark.parametrize(
    "value,expected", [(6.4, 7), (7.0, 7), (20.6, 21), (30.9, 30), (100, 30)]
)
def test_clamp_setpoint(value, expected):
    assert r.clamp_setpoint(value, 7, 30) == expected


def test_desired_state_roundtrip():
    d = r.DesiredZoneState(mode="heat", setpoint=19)
    assert r.DesiredZoneState.from_dict(d.as_dict()) == d


def test_profile_matches():
    assert r.profile_matches(["00001"] * 7, ["00001"] * 7)
    assert not r.profile_matches(["00004"] * 7, ["00001"] * 7)
    assert not r.profile_matches(None, ["00001"] * 7)


# --- displayed current temperature & action (HomeKit sensorless handling) ---


def test_real_sensor_below_setpoint_heating():
    assert r.compute_current_and_action(18.0, 21, "heat", assume_from_target=True) == (
        18.0,
        "heating",
    )


def test_real_sensor_at_or_above_setpoint_idle():
    assert r.compute_current_and_action(22.0, 21, "heat", assume_from_target=True) == (
        22.0,
        "idle",
    )


def test_real_sensor_off_reports_off_and_keeps_reading():
    assert r.compute_current_and_action(19.5, 21, "off", assume_from_target=True) == (
        19.5,
        "off",
    )


def test_sensorless_heat_substitutes_setpoint_and_lights_flame():
    # No sensor, substitution on, zone on -> show setpoint, indicate heating.
    assert r.compute_current_and_action(None, 21, "heat", assume_from_target=True) == (
        21.0,
        "heating",
    )


def test_sensorless_off_substitutes_setpoint_action_off():
    assert r.compute_current_and_action(None, 21, "off", assume_from_target=True) == (
        21.0,
        "off",
    )


def test_sensorless_substitution_disabled_reports_none():
    # Substitution off: stay truthful (None), can't infer action while on.
    assert r.compute_current_and_action(None, 21, "heat", assume_from_target=False) == (
        None,
        None,
    )


def test_sensorless_off_substitution_disabled_still_off():
    assert r.compute_current_and_action(None, 21, "off", assume_from_target=False) == (
        None,
        "off",
    )


def test_real_sensor_unaffected_by_substitution_flag():
    # A real reading is never overwritten regardless of the flag.
    assert r.compute_current_and_action(18.0, 21, "heat", assume_from_target=True) == (
        r.compute_current_and_action(18.0, 21, "heat", assume_from_target=False)
    )
