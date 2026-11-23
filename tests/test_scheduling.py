"""Tests for when a TRV room steps: its periodic schedule and target changes."""

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from custom_components.room_valve_control.const import DEFAULT_CONTROL_STEP

from .live_room import (
    ROOM_CLIMATE,
    TRVS,
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_setup_room,
    restore_rooms,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from .conftest import FixedRandom

TARGET: Final = 21.0
# 1 °C below TARGET: 30 % at Kp 30, nothing integrated on the first step.
READING: Final = "20.0"
# Each 0.5 °C further off TARGET than the last, so every step writes the valves.
READINGS: Final = ("19.5", "19.0", "18.5")
HALF: Final = 0.5
FIRST_INTERVAL: Final = DEFAULT_CONTROL_STEP * (1 - HALF)
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
