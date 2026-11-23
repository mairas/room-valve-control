"""Tests for when a TRV room steps: its periodic schedule and target changes."""

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNKNOWN
from homeassistant.core import CoreState

from custom_components.room_valve_control.const import (
    DEFAULT_CONTROL_STEP,
    DEFAULT_STALE_TIMEOUT,
    TARGET_STEP_MAX_DELAY,
)

from .live_room import (
    ROOM_CLIMATE,
    TRVS,
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_set_room_target,
    async_setup_room,
    restore_rooms,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import FixedRandom

TARGET: Final = 21.0
# 1 °C below TARGET: 30 % at Kp 30, nothing integrated on the first step.
READING: Final = "20.0"
# Each 0.5 °C further off TARGET than the last, so every step writes the valves.
READINGS: Final = ("19.5", "19.0", "18.5")
# 2 and 3 °C above READING: 60 and 90 %, the first step having integrated
# nothing for the next one to use.
NEW_TARGET: Final = 22.0
NEW_OUTPUT: Final = 60
LATEST_TARGET: Final = 23.0
LATEST_OUTPUT: Final = 90
# Off the TRVs' own setpoint and each other, so each is written.
FALLBACK_TARGET: Final = 20.5
NEW_FALLBACK_TARGET: Final = 19.5
HALF: Final = 0.5
FIRST_INTERVAL: Final = DEFAULT_CONTROL_STEP * (1 - HALF)
TARGET_DELAY: Final = TARGET_STEP_MAX_DELAY * HALF
SOONER: Final = timedelta(seconds=5)
JUST_BEFORE: Final = timedelta(seconds=1)


def valve_writes(calls: list[TrvCall]) -> list[TrvCall]:
    """Return the valve writes among calls."""
    valves = {ids.valve for ids in TRVS}
    return [call for call in calls if call[0] == "set_value" and call[1] in valves]


async def _async_set_reading(hass: HomeAssistant, reading: str) -> None:
    # A changed reading in direct mode waits for the next step.
    hass.states.async_set(WALL_SENSOR, reading)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _async_assert_steps_at(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    intervals: list[timedelta],
) -> None:
    """Assert the room steps after each interval in turn, and not before."""
    for interval, reading in zip(intervals, READINGS, strict=False):
        calls.clear()
        await _async_set_reading(hass, reading)
        await async_advance(hass, freezer, interval - JUST_BEFORE)
        assert valve_writes(calls) == []
        await async_advance(hass, freezer, JUST_BEFORE)
        assert len(valve_writes(calls)) == len(TRVS)


async def test_first_step_after_start_comes_at_a_random_point(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """A starting room's next step is a random share of the step away, then periodic."""
    step_random.value = HALF
    hass.states.async_set(WALL_SENSOR, READING)
    restore_rooms(hass, {ROOM_CLIMATE: TARGET})
    await async_setup_room(hass)
    assert len(valve_writes(calls)) == len(TRVS)

    await _async_assert_steps_at(
        hass, freezer, calls, [FIRST_INTERVAL, DEFAULT_CONTROL_STEP]
    )


async def test_core_started_step_restarts_at_a_random_point(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """
    The step once Core has started takes a random phase too.

    Core starts every room at the same instant, which must not put them back
    in lockstep.
    """
    hass.set_state(CoreState.starting)
    restore_rooms(hass, {ROOM_CLIMATE: TARGET})
    await async_setup_room(hass)
    # With time frozen, reported at the instant Core reaches running.
    hass.states.async_set(WALL_SENSOR, READING)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []
    step_random.value = HALF

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(valve_writes(calls)) == len(TRVS)

    await _async_assert_steps_at(
        hass, freezer, calls, [FIRST_INTERVAL, DEFAULT_CONTROL_STEP]
    )


async def test_reading_that_ends_waiting_keeps_a_random_phase(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """
    The first reading after Core has started steps the room at a random phase.

    Sensors that report together, as after a bridge reconnects, must not put
    the rooms waiting for them in lockstep.
    """
    hass.set_state(CoreState.starting)
    restore_rooms(hass, {ROOM_CLIMATE: TARGET})
    await async_setup_room(hass)
    step_random.value = HALF
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []

    await async_advance(hass, freezer, SOONER)
    await _async_set_reading(hass, READING)
    assert len(valve_writes(calls)) == len(TRVS)

    await _async_assert_steps_at(
        hass, freezer, calls, [FIRST_INTERVAL, DEFAULT_CONTROL_STEP]
    )


async def test_unloading_cancels_the_next_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """An unloaded room never steps again."""
    hass.states.async_set(WALL_SENSOR, READING)
    restore_rooms(hass, {ROOM_CLIMATE: TARGET})
    entry = await async_setup_room(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    calls.clear()

    hass.states.async_set(WALL_SENSOR, READINGS[0])
    await async_advance(hass, freezer, DEFAULT_CONTROL_STEP)

    assert calls == []


async def _async_direct_room(
    hass: HomeAssistant, calls: list[TrvCall], step_random: FixedRandom
) -> MockConfigEntry:
    """Set up the room in direct mode at TARGET, then make the delays HALF."""
    hass.states.async_set(WALL_SENSOR, READING)
    entry = await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    assert len(valve_writes(calls)) == len(TRVS)
    calls.clear()
    step_random.value = HALF
    return entry


def valve_outputs(output: int) -> list[TrvCall]:
    """Return a step's valve writes of output to every TRV."""
    return [("set_value", ids.valve, output) for ids in TRVS]


async def test_first_target_steps_after_a_random_delay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """A room's first target steps within the delay, as any changed target."""
    step_random.value = HALF
    hass.states.async_set(WALL_SENSOR, READING)
    await async_setup_room(hass)

    await async_set_room_target(hass, TARGET)
    assert calls == []
    await async_advance(hass, freezer, TARGET_DELAY)

    assert len(valve_writes(calls)) == len(TRVS)


async def test_changed_target_steps_after_a_random_delay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """The valves follow a new target within the delay, not at the next period."""
    await _async_direct_room(hass, calls, step_random)

    await async_set_room_target(hass, NEW_TARGET)
    assert calls == []
    await async_advance(hass, freezer, TARGET_DELAY - JUST_BEFORE)
    assert calls == []
    await async_advance(hass, freezer, JUST_BEFORE)

    assert valve_writes(calls) == valve_outputs(NEW_OUTPUT)


async def test_unchanged_target_does_not_step(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """A target sent again, as a calendar does on reload, writes nothing."""
    await _async_direct_room(hass, calls, step_random)
    await _async_set_reading(hass, READINGS[0])

    await async_set_room_target(hass, TARGET)
    await async_advance(hass, freezer, TARGET_STEP_MAX_DELAY)

    assert calls == []


async def test_target_changes_within_the_delay_step_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """Targets changed again before the delayed step make one step, at the latest."""
    await _async_direct_room(hass, calls, step_random)

    await async_set_room_target(hass, NEW_TARGET)
    await async_advance(hass, freezer, SOONER)
    await async_set_room_target(hass, LATEST_TARGET)
    await async_advance(hass, freezer, TARGET_DELAY - SOONER)
    assert valve_writes(calls) == valve_outputs(LATEST_OUTPUT)
    calls.clear()

    await _async_set_reading(hass, READINGS[0])
    await async_advance(hass, freezer, TARGET_STEP_MAX_DELAY)

    assert calls == []


async def test_target_step_restarts_at_a_random_point(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """After a target-change step the next is a random share of the step away."""
    await _async_direct_room(hass, calls, step_random)
    await async_set_room_target(hass, NEW_TARGET)
    await async_advance(hass, freezer, TARGET_DELAY)
    assert valve_writes(calls) == valve_outputs(NEW_OUTPUT)

    await _async_assert_steps_at(
        hass, freezer, calls, [FIRST_INTERVAL, DEFAULT_CONTROL_STEP]
    )


async def test_unloading_cancels_a_pending_target_step(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """A target change just before an unload never steps the unloaded room."""
    entry = await _async_direct_room(hass, calls, step_random)
    await async_set_room_target(hass, NEW_TARGET)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_advance(hass, freezer, TARGET_STEP_MAX_DELAY)

    assert calls == []


async def test_fallback_target_is_written_at_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    step_random: FixedRandom,
) -> None:
    """In fallback the TRVs get a new target at once; its step writes nothing more."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    await async_setup_room(hass)
    await async_set_room_target(hass, FALLBACK_TARGET)
    await async_advance(hass, freezer, DEFAULT_STALE_TIMEOUT)
    assert calls == [("set_temperature", ids.climate, FALLBACK_TARGET) for ids in TRVS]
    calls.clear()
    step_random.value = HALF

    await async_set_room_target(hass, NEW_FALLBACK_TARGET)
    assert calls == [
        ("set_temperature", ids.climate, NEW_FALLBACK_TARGET) for ids in TRVS
    ]
    await async_advance(hass, freezer, TARGET_STEP_MAX_DELAY)

    assert calls == [
        ("set_temperature", ids.climate, NEW_FALLBACK_TARGET) for ids in TRVS
    ]
