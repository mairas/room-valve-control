"""Tests for a floor room over live fake floor thermostats."""

from datetime import timedelta
from typing import TYPE_CHECKING, Final

import pytest
from homeassistant.config_entries import (
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlow,
)
from homeassistant.const import ATTR_FRIENDLY_NAME, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import mock_config_flow

from custom_components.room_valve_control.const import (
    CONF_CONTROL_STEP,
    CONF_FALLBACK_FLOOR_SETPOINT,
    CONF_FLOOR_MAX,
    CONF_FLOOR_MIN,
    CONF_MIN_ON_TIME,
    DEFAULT_FALLBACK_FLOOR_SETPOINT,
    DEFAULT_FLOOR_MAX,
    DEFAULT_FLOOR_MIN,
    DEFAULT_MIN_ON_TIME,
    DOMAIN,
    ISSUE_ROOM_WITHOUT_DEVICES,
    ISSUE_UNUSABLE_FLOOR_HEATER,
)

from .live_room import (
    FLOOR_HEATERS,
    FLOOR_INTEGRATION,
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_set_room_target,
    async_setup_floor_heaters,
    async_setup_rooms,
    registry_ids,
    room_subentry,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOM_ID: Final = "bathroom_subentry"
ROOM_TITLE: Final = "Bathroom"
ROOM_CLIMATE: Final = "climate.bathroom"
WRITES_SENSOR: Final = "sensor.bathroom_device_writes"
DUTY_CYCLE_SENSOR: Final = "sensor.bathroom_duty_cycle"
HEATER: Final = FLOOR_HEATERS[0]
CONTROL_STEP: Final = timedelta(minutes=30)
TARGET: Final = 21.0
# 1 °C below TARGET: Kp 30 gives 30 %, nothing integrated on the first step,
# so the floor heats for 30 % of the 30 min control step.
READING: Final = "20.0"
ON_TIME: Final = timedelta(minutes=9)
JUST_BEFORE: Final = timedelta(seconds=1)
RELOAD_AFTER: Final = timedelta(minutes=5)


class FakeFloorFlow(ConfigFlow):
    """Lets the mocked floor thermostat entry set up; never shown."""

    VERSION = 1


@pytest.fixture
def floor_flow() -> Iterator[None]:
    """Register a config flow for the fake floor thermostats."""
    with mock_config_flow(FLOOR_INTEGRATION, FakeFloorFlow):
        yield


@pytest.fixture
async def floor_calls(
    hass: HomeAssistant,
    floor_flow: None,  # noqa: ARG001
) -> list[TrvCall]:
    """Set up the fake floor thermostats and return their writes, in order."""
    recorded: list[TrvCall] = []
    await async_setup_floor_heaters(hass, recorded)
    return recorded


async def async_setup_floor_room(
    hass: HomeAssistant,
    heaters: Sequence[str] | None = None,
    parameters: Mapping[str, float] | None = None,
) -> MockConfigEntry:
    """
    Add and set up the entry with the bathroom, heated by the floor.

    parameters override the stored defaults and the 30 min control step.
    """
    if heaters is None:
        heaters = registry_ids(hass, [HEATER])
    subentry = room_subentry(
        ROOM_ID,
        ROOM_TITLE,
        sensors=registry_ids(hass, [WALL_SENSOR]),
        heaters=heaters,
    )
    subentry["data"] = (
        dict(subentry["data"])
        | {CONF_CONTROL_STEP: CONTROL_STEP.total_seconds()}
        | dict(parameters or {})
    )
    return await async_setup_rooms(hass, [subentry])


def setpoint(value: float, heater: str = HEATER) -> TrvCall:
    """Return a setpoint write reaching heater."""
    return ("set_temperature", heater, value)


def writes(hass: HomeAssistant) -> str:
    """Return the room's device-writes sensor state."""
    state = hass.states.get(WRITES_SENSOR)
    assert state is not None
    return state.state


async def test_start_without_reading_holds_the_fallback(
    hass: HomeAssistant, floor_calls: list[TrvCall]
) -> None:
    """A starting floor room writes the fallback setpoint at once."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)

    await async_setup_floor_room(hass)

    assert floor_calls == [setpoint(DEFAULT_FALLBACK_FLOOR_SETPOINT)]


async def test_floor_heats_for_the_output_share_of_the_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, floor_calls: list[TrvCall]
) -> None:
    """The floor maximum is written at once, the minimum when the share is up."""
    hass.states.async_set(WALL_SENSOR, READING)
    await async_setup_floor_room(hass)
    floor_calls.clear()

    await async_set_room_target(hass, TARGET, ROOM_CLIMATE)
    assert floor_calls == [setpoint(DEFAULT_FLOOR_MAX)]
    await async_advance(hass, freezer, ON_TIME - JUST_BEFORE)
    assert floor_calls == [setpoint(DEFAULT_FLOOR_MAX)]

    await async_advance(hass, freezer, JUST_BEFORE)

    assert floor_calls == [setpoint(DEFAULT_FLOOR_MAX), setpoint(DEFAULT_FLOOR_MIN)]
    # The start fallback, the on edge and the off edge, before the next step.
    assert writes(hass) == "3"


async def test_output_is_named_duty_cycle(
    hass: HomeAssistant,
    floor_calls: list[TrvCall],  # noqa: ARG001
) -> None:
    """A floor room's output is the share of each step the floor heats."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)

    await async_setup_floor_room(hass)

    state = hass.states.get(DUTY_CYCLE_SENSOR)
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Bathroom Duty cycle"


async def test_configured_floor_parameters_reach_the_thermostat(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, floor_calls: list[TrvCall]
) -> None:
    """The stored floor setpoints and minimum on-time are the ones written."""
    hass.states.async_set(WALL_SENSOR, READING)
    short_step = timedelta(minutes=10)
    # 30 % of the 10 min step: shorter than the default minimum on-time, so
    # only the configured one keeps the on-period.
    short_on_time = timedelta(minutes=3)
    min_on_time = timedelta(minutes=2)
    assert min_on_time < short_on_time < DEFAULT_MIN_ON_TIME
    await async_setup_floor_room(
        hass,
        parameters={
            CONF_CONTROL_STEP: short_step.total_seconds(),
            CONF_FLOOR_MAX: 28.0,
            CONF_FLOOR_MIN: 15.0,
            CONF_FALLBACK_FLOOR_SETPOINT: 21.0,
            CONF_MIN_ON_TIME: min_on_time.total_seconds(),
        },
    )
    assert floor_calls == [setpoint(21.0)]
    floor_calls.clear()

    await async_set_room_target(hass, TARGET, ROOM_CLIMATE)
    assert floor_calls == [setpoint(28.0)]
    await async_advance(hass, freezer, short_on_time - JUST_BEFORE)
    assert floor_calls == [setpoint(28.0)]

    await async_advance(hass, freezer, JUST_BEFORE)

    assert floor_calls == [setpoint(28.0), setpoint(15.0)]


async def test_reload_during_an_on_period_holds_the_fallback(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, floor_calls: list[TrvCall]
) -> None:
    """Stopping writes the fallback, and the old off edge never comes."""
    hass.states.async_set(WALL_SENSOR, READING)
    entry = await async_setup_floor_room(hass)
    await async_set_room_target(hass, TARGET, ROOM_CLIMATE)
    await async_advance(hass, freezer, RELOAD_AFTER)
    floor_calls.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # Stop, start, and the reloaded room's first step with its restored target.
    assert floor_calls == [
        setpoint(DEFAULT_FALLBACK_FLOOR_SETPOINT),
        setpoint(DEFAULT_FALLBACK_FLOOR_SETPOINT),
        setpoint(DEFAULT_FLOOR_MAX),
    ]
    floor_calls.clear()

    await async_advance(hass, freezer, ON_TIME - RELOAD_AFTER)
    assert floor_calls == []
    await async_advance(hass, freezer, RELOAD_AFTER)

    assert floor_calls == [setpoint(DEFAULT_FLOOR_MIN)]


async def test_unloading_holds_the_fallback(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, floor_calls: list[TrvCall]
) -> None:
    """A disabled entry leaves the floor at the fallback, with no off edge after."""
    hass.states.async_set(WALL_SENSOR, READING)
    entry = await async_setup_floor_room(hass)
    await async_set_room_target(hass, TARGET, ROOM_CLIMATE)
    floor_calls.clear()

    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_advance(hass, freezer, CONTROL_STEP)

    assert floor_calls == [setpoint(DEFAULT_FALLBACK_FLOOR_SETPOINT)]


def heater_issue(hass: HomeAssistant, registry_id: str) -> object:
    """Return the repair issue raised for an unusable floor heater, or None."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_UNUSABLE_FLOOR_HEATER}_{registry_id}"
    )


@pytest.mark.parametrize("unusable", ["removed", "disabled"])
async def test_unusable_heater_is_left_out(
    hass: HomeAssistant, floor_calls: list[TrvCall], unusable: str
) -> None:
    """The room runs on its other heater; the unusable one gets an issue."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    stored = registry_ids(hass, FLOOR_HEATERS)
    registry = er.async_get(hass)
    if unusable == "removed":
        registry.async_remove(FLOOR_HEATERS[1])
    else:
        registry.async_update_entity(
            FLOOR_HEATERS[1], disabled_by=er.RegistryEntryDisabler.USER
        )

    entry = await async_setup_floor_room(hass, stored)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(ROOM_CLIMATE) is not None
    assert floor_calls == [setpoint(DEFAULT_FALLBACK_FLOOR_SETPOINT)]
    assert heater_issue(hass, stored[1]) is not None


async def test_room_without_usable_heater_is_not_set_up(
    hass: HomeAssistant, floor_calls: list[TrvCall]
) -> None:
    """A floor room whose only heater is gone has no entities and an issue."""
    stored = registry_ids(hass, [HEATER])
    er.async_get(hass).async_remove(HEATER)

    await async_setup_floor_room(hass, stored)

    assert hass.states.get(ROOM_CLIMATE) is None
    assert heater_issue(hass, stored[0]) is not None
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_ROOM_WITHOUT_DEVICES}_{ROOM_ID}"
    )
    assert floor_calls == []
