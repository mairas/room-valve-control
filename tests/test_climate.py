"""Tests for the room thermostat entity."""

from typing import TYPE_CHECKING, Any, Final

import pytest
from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (
    async_mock_restore_state_shutdown_restart,
    mock_restore_cache_with_extra_data,
)

from custom_components.room_valve_control.climate import TARGET_TEMPERATURE_STEP
from custom_components.room_valve_control.const import (
    DEFAULT_MAX_TARGET,
    DEFAULT_MIN_TARGET,
    DOMAIN,
)

from .live_room import (
    ROOM_CLIMATE,
    ROOM_ID,
    ROOM_TITLE,
    TRVS,
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_set_room_target,
    async_setup_room,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

INTEGRATOR_SENSOR: Final = "sensor.living_room_integrator"
WRITES_SENSOR: Final = "sensor.living_room_device_writes"
RESTORED_TARGET: Final = 20.8
RESTORED_INTEGRAL: Final = 12.5
RESTORED_WRITES: Final = 7
MODE_SENSOR: Final = "sensor.living_room_control_mode"
BELOW_TARGET: Final = "20.0"
LATER_READING: Final = "20.4"
NEXT_TARGET: Final = 21.3


def restore(hass: HomeAssistant, extra: dict[str, Any]) -> None:
    """Make the room thermostat restore extra as its stored data."""
    mock_restore_cache_with_extra_data(
        hass, [(State(ROOM_CLIMATE, HVACMode.HEAT), extra)]
    )


async def test_thermostat_is_heat_only_on_the_room_device(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """The thermostat sits on a device named after the room."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, LATER_READING)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.HEAT]
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == float(LATER_READING)
    assert state.attributes[ATTR_TEMPERATURE] is None
    assert state.attributes[ATTR_MIN_TEMP] == DEFAULT_MIN_TARGET
    assert state.attributes[ATTR_MAX_TEMP] == DEFAULT_MAX_TARGET
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == TARGET_TEMPERATURE_STEP
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ROOM_ID), hass.config_entries.async_entries(DOMAIN)[0].entry_id
    )
    assert device is not None
    assert device.name == ROOM_TITLE
    assert calls == []


async def test_target_is_echoed_and_first_target_steps_at_once(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """The first target acts at once; later ones wait for the step in direct."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, BELOW_TARGET)

    await async_set_room_target(hass, RESTORED_TARGET)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == RESTORED_TARGET
    assert {call[1] for call in calls} >= {ids.valve for ids in TRVS}
    calls.clear()

    await async_set_room_target(hass, NEXT_TARGET)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == NEXT_TARGET
    assert calls == []


async def test_restart_restores_target_integrator_and_writes(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """After a restart the room resumes from its stored state, writing nothing."""
    restore(
        hass,
        {
            "target": RESTORED_TARGET,
            "controller": {"integral": RESTORED_INTEGRAL},
            "device_writes": RESTORED_WRITES,
        },
    )
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)

    await async_setup_room(hass)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == RESTORED_TARGET
    integrator = hass.states.get(INTEGRATOR_SENSOR)
    writes = hass.states.get(WRITES_SENSOR)
    assert integrator is not None
    assert writes is not None
    assert float(integrator.state) == RESTORED_INTEGRAL
    assert int(writes.state) == RESTORED_WRITES
    assert calls == []


async def test_start_step_after_restore_enters_direct(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,  # noqa: ARG001
    calls: list[TrvCall],
) -> None:
    """A restored target and a reading as the room starts write at once."""
    restore(
        hass,
        {
            "target": RESTORED_TARGET,
            "controller": {"integral": RESTORED_INTEGRAL},
            "device_writes": RESTORED_WRITES,
        },
    )
    # With time frozen, reported at the instant the room starts.
    hass.states.async_set(WALL_SENSOR, BELOW_TARGET)

    await async_setup_room(hass)

    assert {call[1] for call in calls} >= {ids.valve for ids in TRVS}
    mode = hass.states.get(MODE_SENSOR)
    assert mode is not None
    assert mode.state == "direct"


async def test_reading_updates_the_current_temperature(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """In direct mode a new reading shows at once, before the next step."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, BELOW_TARGET)
    await async_set_room_target(hass, RESTORED_TARGET)
    calls.clear()

    hass.states.async_set(WALL_SENSOR, LATER_READING)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == float(LATER_READING)
    assert calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"target": "warm", "controller": {"integral": "x"}, "device_writes": True},
        {"controller": None},
    ],
)
async def test_malformed_stored_state_starts_fresh(
    hass: HomeAssistant, calls: list[TrvCall], extra: dict[str, Any]
) -> None:
    """Stored values of the wrong type are dropped, not trusted."""
    restore(hass, extra)
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)

    await async_setup_room(hass)

    state = hass.states.get(ROOM_CLIMATE)
    integrator = hass.states.get(INTEGRATOR_SENSOR)
    writes = hass.states.get(WRITES_SENSOR)
    assert state is not None
    assert integrator is not None
    assert writes is not None
    assert state.attributes[ATTR_TEMPERATURE] is None
    assert float(integrator.state) == 0
    assert int(writes.state) == 0
    assert calls == []


async def test_stored_state_round_trips(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """What the thermostat stores is what the steps left behind."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, BELOW_TARGET)
    await async_set_room_target(hass, RESTORED_TARGET)
    await async_advance(hass, freezer)

    stored = await async_mock_restore_state_shutdown_restart(hass)

    extra = stored.last_states[ROOM_CLIMATE].extra_data
    assert extra is not None
    # One 15 min interval at 0.8 °C error with Ti 30 min: 30 x 0.8 x 15 / 30 %.
    assert extra.as_dict() == {
        "target": RESTORED_TARGET,
        "controller": {"integral": pytest.approx(12.0)},
        "device_writes": len(calls),
    }
