"""Constants for the Nobø Ecohub (Simple) integration."""

from typing import Final

DOMAIN: Final = "nobo_hub_simple"

# --- Config entry data keys (connection) -----------------------------------
CONF_SERIAL: Final = "serial"

# --- Options flow keys ------------------------------------------------------
CONF_MANAGED_ZONES: Final = "managed_zones"
CONF_BLOCK_OVERRIDES: Final = "block_overrides"
CONF_CLEAR_GLOBAL_OVERRIDES: Final = "clear_global_overrides"
CONF_POLL_INTERVAL: Final = "poll_interval"

DEFAULT_BLOCK_OVERRIDES: Final = True
DEFAULT_CLEAR_GLOBAL_OVERRIDES: Final = True
DEFAULT_POLL_INTERVAL: Final = 120
MIN_POLL_INTERVAL: Final = 60
MAX_POLL_INTERVAL: Final = 600

# --- Device / manufacturer metadata ----------------------------------------
NOBO_MANUFACTURER: Final = "Glen Dimplex Nordic AS"
ATTR_HARDWARE_VERSION: Final = "hardware_version"
ATTR_SOFTWARE_VERSION: Final = "software_version"
ATTR_SERIAL: Final = "serial"
ATTR_TEMP_COMFORT_C: Final = "temp_comfort_c"
ATTR_TEMP_ECO_C: Final = "temp_eco_c"
ATTR_ZONE_ID: Final = "zone_id"

# --- Temperature bounds (whole degrees, matches pynobo validation) ----------
MIN_TEMPERATURE: Final = 7
MAX_TEMPERATURE: Final = 30

# --- Synthetic week profiles ------------------------------------------------
# Names deliberately contain no spaces: pynobo rewrites spaces to a
# non-breaking space ( ) on send, which complicates matching by name.
PROFILE_NAME_ALWAYS_ON: Final = "HA-AlwaysOn"
PROFILE_NAME_ALWAYS_OFF: Final = "HA-AlwaysOff"

# Week-profile encoding: list of 'HHMMS' entries, 7 days from Monday, each day
# beginning with a '0000'-prefixed entry. State digit: 1 = comfort, 4 = off.
# (See pynobo nobo.API.validate_week_profile.)
ALWAYS_ON_PROFILE: Final = ["00001"] * 7
ALWAYS_OFF_PROFILE: Final = ["00004"] * 7

# --- Reconciler tuning ------------------------------------------------------
RECONCILE_DEBOUNCE_S: Final = 2.0
FIGHT_WINDOW_S: Final = 300.0
FIGHT_THRESHOLD: Final = 10
BACKOFF_START_S: Final = 4.0
BACKOFF_MAX_S: Final = 60.0
PROFILE_DISCOVERY_TIMEOUT_S: Final = 10.0
# How long after writing a week-profile change we trust our own write while
# waiting for the hub's echo (V00) to land, to avoid a redundant re-write.
PROFILE_ECHO_GRACE_S: Final = 8.0

# --- Persistence (helpers.storage.Store) ------------------------------------
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = DOMAIN  # actual key is f"{prefix}.{entry_id}"

# --- Dispatcher signal ------------------------------------------------------
def signal_update(entry_id: str) -> str:
    """Dispatcher signal fired when zone/connection state changes."""
    return f"{DOMAIN}_{entry_id}_update"


# --- Services ---------------------------------------------------------------
SERVICE_RESTORE_ZONE: Final = "restore_zone"
