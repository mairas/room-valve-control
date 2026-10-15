"""Constants for the Room Valve Control integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "room_valve_control"

SUBENTRY_TYPE_ROOM: Final = "room"

# Room subentry data keys. The room's name is the subentry title. Durations are
# stored as seconds, temperatures in °C, Kp in % per °C, the minimum change in
# % points.
CONF_SENSORS: Final = "sensors"
CONF_TRVS: Final = "trvs"
CONF_KP: Final = "kp"
CONF_TI: Final = "ti"
CONF_CONTROL_STEP: Final = "control_step"
CONF_MIN_CHANGE: Final = "min_change"
CONF_STALE_TIMEOUT: Final = "stale_timeout"
CONF_MIN_TARGET: Final = "min_target"
CONF_MAX_TARGET: Final = "max_target"

# A 12 min integral time over-integrates at a 15 min step (81 simulated
# writes/day); 30 min tracks best within the write budget. The stale timeout sits
# above the wall sensors' longest report gap seen in ten days (26 min).
DEFAULT_KP: Final = 30.0
DEFAULT_TI: Final = timedelta(minutes=30)
DEFAULT_STALE_TIMEOUT: Final = timedelta(minutes=45)
DEFAULT_CONTROL_STEP: Final = timedelta(minutes=15)
DEFAULT_MIN_CHANGE: Final = 5.0
# Wide enough for every setpoint the calendar script writes.
DEFAULT_MIN_TARGET: Final = 5.0
DEFAULT_MAX_TARGET: Final = 30.0
