"""Shared utility functions for HawaAI backend."""


# All state strings that mean "someone is present / occupied"
_OCCUPIED_STATES = frozenset({
    "on", "occupied", "detected", "home",
    "presence", "true", "1", "active", "motion",
})


def parse_presence(state_val) -> bool:
    """
    Robustly parse an HA entity state value into True/False for occupancy.

    Handles all known HA presence sensor formats:
      - Standard binary_sensor: "on" / "off"
      - Aqara FP2 / mmWave:     "detected" / "clear"
      - Device tracker:          "home" / "away" / "not_home"
      - Custom sensors:          "occupied" / "presence" / "active"
      - Boolean / numeric:       "true" / "1"

    Returns True only for known-occupied strings.
    """
    if state_val is None:
        return False
    return str(state_val).lower().strip() in _OCCUPIED_STATES
