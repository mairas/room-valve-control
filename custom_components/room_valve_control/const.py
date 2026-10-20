"""Constants for the Room Valve Control integration."""

from datetime import timedelta
from typing import Final, TypedDict

DOMAIN: Final = "room_valve_control"

SUBENTRY_TYPE_ROOM: Final = "room"

# Repair issue translation keys; an issue's id adds the device's registry id
# or the room's subentry id.
ISSUE_UNUSABLE_TRV: Final = "unusable_trv"
ISSUE_UNUSABLE_FLOOR_HEATER: Final = "unusable_floor_heater"
ISSUE_ROOM_WITHOUT_DEVICES: Final = "room_without_devices"

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
CONF_FLOOR_HEATERS: Final = "floor_heaters"
CONF_FLOOR_MAX: Final = "floor_max"
CONF_FLOOR_MIN: Final = "floor_min"
CONF_FALLBACK_FLOOR_SETPOINT: Final = "fallback_floor_setpoint"
CONF_MIN_ON_TIME: Final = "min_on_time"


class RoomParameters(TypedDict):
    """A room's controller parameters, keyed by the CONF_ constants above."""

    kp: float
    ti: float
    control_step: float
    min_change: float
    stale_timeout: float
    min_target: float
    max_target: float
    floor_max: float
    floor_min: float
    fallback_floor_setpoint: float
    min_on_time: float


class RoomConfig(RoomParameters):
    """
    A room subentry's data, its sensors, TRVs and heaters by entity registry id.

    A room has TRVs or floor heaters, never both. Rooms stored before floor
    heaters lack the floor keys and read their defaults.
    """

    sensors: list[str]
    trvs: list[str]
    floor_heaters: list[str]


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
# A floor heater is switched on by writing the floor maximum and off by writing
# the floor minimum, so its thermostat limits the floor in both states.
DEFAULT_FLOOR_MAX: Final = 26.0
DEFAULT_FLOOR_MIN: Final = 17.0
# The floor setpoint a floor thermostat holds on its own without the room.
DEFAULT_FALLBACK_FLOOR_SETPOINT: Final = 22.0
# Shorter on- or off-times are dropped from a period, sparing the relay.
DEFAULT_MIN_ON_TIME: Final = timedelta(minutes=5)
