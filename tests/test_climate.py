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


def restore(hass: HomeAssistant, extra: dict[str, Any]) -> None:
    """Make the room thermostat restore extra as its stored data."""
    mock_restore_cache_with_extra_data(
        hass, [(State(ROOM_CLIMATE, HVACMode.HEAT), extra)]
    )


async def test_thermostat_is_heat_only_on_the_room_device(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """The thermostat sits on a device named after the room."""
    hass.states.async_set(WALL_SENSOR, "20.3")
    await async_setup_room(hass)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.HEAT]
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 20.3  # noqa: PLR2004
    assert state.attributes[ATTR_TEMPERATURE] is None
    assert state.attributes[ATTR_MIN_TEMP] == DEFAULT_MIN_TARGET
    assert state.attributes[ATTR_MAX_TEMP] == DEFAULT_MAX_TARGET
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 0.1  # noqa: PLR2004
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
    hass.states.async_set(WALL_SENSOR, "20.0")
    await async_setup_room(hass)

    await async_set_room_target(hass, RESTORED_TARGET)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == RESTORED_TARGET
    assert {call[1] for call in calls} >= {ids.valve for ids in TRVS}
    calls.clear()

    await async_set_room_target(hass, 21.3)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 21.3  # noqa: PLR2004
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
    hass.states.async_set(WALL_SENSOR, "20.0")
    await async_setup_room(hass)
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
