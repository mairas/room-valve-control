"""Tests for the room's diagnostic sensors."""

from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import ATTR_OPTIONS, ATTR_STATE_CLASS
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.helpers import entity_registry as er

from .live_room import (
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_set_room_target,
    async_setup_room,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

OUTPUT: Final = "sensor.living_room_valve_output"
INTEGRATOR: Final = "sensor.living_room_integrator"
ERROR: Final = "sensor.living_room_control_error"
MODE: Final = "sensor.living_room_control_mode"
WRITES: Final = "sensor.living_room_device_writes"
SENSORS: Final = (OUTPUT, INTEGRATOR, ERROR, MODE, WRITES)


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    """Return the entity's state, which must exist."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


async def test_sensors_are_diagnostic_with_their_classes(
    hass: HomeAssistant,
    calls: list[TrvCall],  # noqa: ARG001
) -> None:
    """Each sensor has the unit and classes its value needs."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    await async_setup_room(hass)

    registry = er.async_get(hass)
    for entity_id in SENSORS:
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
    attributes = {
        entity_id: state.attributes
        for entity_id in SENSORS
        if (state := hass.states.get(entity_id))
    }
    assert attributes[OUTPUT][ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert attributes[INTEGRATOR][ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert attributes[ERROR][ATTR_DEVICE_CLASS] == "temperature"
    assert attributes[ERROR][ATTR_UNIT_OF_MEASUREMENT] == "°C"
    assert attributes[MODE][ATTR_OPTIONS] == [
        "waiting",
        "direct",
        "fallback",
        "stopped",
    ]
    assert attributes[WRITES][ATTR_STATE_CLASS] == "total_increasing"


async def test_sensors_before_any_step(
    hass: HomeAssistant,
    calls: list[TrvCall],  # noqa: ARG001
) -> None:
    """A waiting room has no output or error yet."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    await async_setup_room(hass)

    assert state_of(hass, OUTPUT) == STATE_UNKNOWN
    assert state_of(hass, ERROR) == STATE_UNKNOWN
    assert state_of(hass, MODE) == "waiting"
    assert float(state_of(hass, INTEGRATOR)) == 0
    assert int(state_of(hass, WRITES)) == 0


async def test_sensors_show_the_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """After direct steps the sensors show what they computed and wrote."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, "20.0")
    await async_set_room_target(hass, 21.0)

    await async_advance(hass, freezer)

    # Kp 30 x 1 °C; one 15 min interval at Ti 30 min integrates half of it.
    assert int(state_of(hass, OUTPUT)) == 30  # noqa: PLR2004
    assert float(state_of(hass, INTEGRATOR)) == 15  # noqa: PLR2004
    assert float(state_of(hass, ERROR)) == 1
    assert state_of(hass, MODE) == "direct"
    assert int(state_of(hass, WRITES)) == len(calls) > 0
