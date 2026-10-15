"""Tests for setting up, scheduling and unloading the rooms."""

import asyncio
import logging
from typing import TYPE_CHECKING, Final

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.room_valve_control import runtime
from custom_components.room_valve_control.const import (
    DEFAULT_CONTROL_STEP,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
)
from custom_components.room_valve_control.spirit import DIRECT_MODE_PRESET

from .live_room import (
    ROOM_CLIMATE,
    ROOM_TITLE,
    TRVS,
    WALL_SENSOR,
    TrvCall,
    async_advance,
    async_set_room_target,
    async_setup_room,
    async_setup_spirits,
    register_sensor,
    registry_ids,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant

TARGET: Final = 21.0
# 1 °C below TARGET: Kp 30 gives 30 %; the TRVs read 20.5 °C at offset 0.
READING: Final = "20.0"
OUTPUT: Final = 30
OFFSET_TENTHS: Final = -5
# Off the TRVs' own setpoint, so a fallback step writes it.
FALLBACK_TARGET: Final = 20.5
OTHER_SENSOR: Final = "sensor.living_room_window_temperature"
STEPS_IN_STARTUP_GRACE: Final = DEFAULT_STALE_TIMEOUT // DEFAULT_CONTROL_STEP - 1


def entering_direct(output: int = OUTPUT) -> list[TrvCall]:
    """Return the writes of a step that puts both TRVs in direct mode."""
    return [
        call
        for ids in TRVS
        for call in (
            ("set_preset_mode", ids.climate, DIRECT_MODE_PRESET),
            ("set_value", ids.valve, output),
            ("set_config_parameter", ids.climate, OFFSET_TENTHS),
        )
    ]


def handing_back(target: float = TARGET) -> list[TrvCall]:
    """Return the writes that put both TRVs from direct into heat at target."""
    return [
        call
        for ids in TRVS
        for call in (
            ("set_hvac_mode", ids.climate, "heat"),
            ("set_temperature", ids.climate, target),
        )
    ]


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """An entry without rooms sets up and unloads."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.usefixtures("zwave_js_flow")
async def test_disabled_offset_entity_retries_setup(hass: HomeAssistant) -> None:
    """A TRV whose parameter 8 entity is disabled leaves the entry not ready."""
    await async_setup_spirits(
        hass, [], offset_disabled_by=er.RegistryEntryDisabler.USER
    )

    entry = await async_setup_room(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason is not None
    assert TRVS[0].offset in entry.reason
    assert hass.states.get(ROOM_CLIMATE) is None


async def test_unknown_sensor_writes_nothing_until_stale_timeout(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """Without a reading the TRVs are left alone until the stale timeout."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    await async_setup_room(hass)
    await async_set_room_target(hass, 20.5)

    for _ in range(STEPS_IN_STARTUP_GRACE):
        await async_advance(hass, freezer)
    assert calls == []

    await async_advance(hass, freezer)

    assert calls == [("set_temperature", ids.climate, 20.5) for ids in TRVS]


async def test_fresh_reading_steps_at_once(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """A reading arriving while waiting puts the TRVs in direct mode at once."""
    hass.states.async_set(WALL_SENSOR, STATE_UNKNOWN)
    await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    assert calls == []

    hass.states.async_set(WALL_SENSOR, READING)
    await hass.async_block_till_done()

    assert calls == entering_direct()


async def test_reading_change_in_direct_waits_for_next_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """In direct mode a new reading takes effect at the periodic step."""
    hass.states.async_set(WALL_SENSOR, READING)
    await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    calls.clear()

    hass.states.async_set(WALL_SENSOR, "19.5")
    await hass.async_block_till_done()
    assert calls == []

    await async_advance(hass, freezer)

    # 1.5 °C error: 45 % proportional plus the 15 % integrated so far.
    assert calls == [("set_value", ids.valve, 60) for ids in TRVS]


async def test_unchanged_value_reported_after_fallback_steps_back_to_direct(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """A sensor back with its old value only reports, which still steps at once."""
    hass.states.async_set(WALL_SENSOR, READING)
    await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    while ("set_hvac_mode", TRVS[0].climate, "heat") not in calls:
        await async_advance(hass, freezer)
    calls.clear()
    reported_before = hass.states.get(WALL_SENSOR)
    assert reported_before is not None

    hass.states.async_set(WALL_SENSOR, READING)
    await hass.async_block_till_done()

    reported = hass.states.get(WALL_SENSOR)
    assert reported is not None
    assert reported.last_changed == reported_before.last_changed
    # Four direct steps integrated 60 %, which the fallback kept.
    assert calls == [
        call
        for ids in TRVS
        for call in (
            ("set_preset_mode", ids.climate, DIRECT_MODE_PRESET),
            ("set_value", ids.valve, 90),
        )
    ]


async def test_unload_hands_back_every_trv(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """Unloading puts every TRV in heat mode at the room target."""
    hass.states.async_set(WALL_SENSOR, READING)
    entry = await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    calls.clear()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert calls == handing_back()


async def test_stop_hands_back_before_the_stop_event(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """Core stop hands the TRVs back before zwave_js disconnects on the stop event."""
    hass.states.async_set(WALL_SENSOR, READING)
    await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    calls.clear()
    at_stop_event: list[TrvCall] = []

    @callback
    def stop_event(_event: Event) -> None:
        at_stop_event.extend(calls)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_event)

    await hass.async_stop()

    assert at_stop_event == handing_back()


@pytest.mark.usefixtures("zwave_js_flow")
async def test_hand_back_is_bounded(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TRV that never answers is cut off at the hand back timeout."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    hass.states.async_set(WALL_SENSOR, READING)
    entry = await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    stall = asyncio.Event()
    trvs[0].stall = stall
    monkeypatch.setattr(runtime, "HAND_BACK_TIMEOUT", 0.01)

    with caplog.at_level(logging.WARNING):
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert ("set_hvac_mode", TRVS[0].climate, "heat") in calls
    assert "cut off" in caplog.text
    stall.set()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("zwave_js_flow")
async def test_removed_trv_leaves_the_entry_not_ready(hass: HomeAssistant) -> None:
    """A TRV whose registry entry is gone needs the room reconfigured."""
    await async_setup_spirits(hass, [])
    stored = registry_ids(hass, [ids.climate for ids in TRVS])
    er.async_get(hass).async_remove(TRVS[1].climate)
    await hass.async_block_till_done()

    entry = await async_setup_room(hass, trvs=stored)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason is not None
    assert ROOM_TITLE in entry.reason
    assert stored[1] in entry.reason
    assert hass.states.get(ROOM_CLIMATE) is None


async def test_removed_wall_sensor_is_dropped(
    hass: HomeAssistant, calls: list[TrvCall], caplog: pytest.LogCaptureFixture
) -> None:
    """A wall sensor that no longer exists is logged and the others are used."""
    gone = register_sensor(hass, OTHER_SENSOR)
    er.async_get(hass).async_remove(OTHER_SENSOR)
    hass.states.async_set(WALL_SENSOR, READING)

    with caplog.at_level(logging.WARNING):
        await async_setup_room(hass, sensors=[gone, *registry_ids(hass, [WALL_SENSOR])])
    await async_set_room_target(hass, TARGET)

    assert gone in caplog.text
    assert calls == entering_direct()


async def test_room_without_wall_sensors_falls_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With none of its sensors left the room says so and the TRVs regulate."""
    stored = registry_ids(hass, [WALL_SENSOR])
    er.async_get(hass).async_remove(WALL_SENSOR)
    hass.states.async_set(WALL_SENSOR, READING)

    with caplog.at_level(logging.WARNING):
        await async_setup_room(hass, sensors=stored)
    await async_set_room_target(hass, FALLBACK_TARGET)
    for _ in range(STEPS_IN_STARTUP_GRACE + 1):
        await async_advance(hass, freezer)

    assert "no wall sensor left" in caplog.text
    assert calls == [("set_temperature", ids.climate, FALLBACK_TARGET) for ids in TRVS]
