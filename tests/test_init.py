"""Tests for setting up, scheduling and unloading the rooms."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import pytest
from homeassistant.components.climate.const import ATTR_CURRENT_TEMPERATURE
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    EVENT_HOMEASSISTANT_STOP,
    STATE_UNKNOWN,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.room_valve_control import runtime
from custom_components.room_valve_control.const import (
    CONF_KP,
    DEFAULT_CONTROL_STEP,
    DEFAULT_STALE_TIMEOUT,
    DOMAIN,
)
from custom_components.room_valve_control.room import ControlMode
from custom_components.room_valve_control.spirit import DIRECT_MODE_PRESET

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
    async_setup_spirits,
    async_wait_for,
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
OTHER_TARGET: Final = 22.0
RELOAD_DELAY: Final = timedelta(seconds=RELOAD_AFTER_UPDATE_DELAY + 1)
# Off the TRVs' own setpoint, so a fallback step writes it.
FALLBACK_TARGET: Final = 20.5
RENAMED_SENSOR: Final = "sensor.lounge_temperature"
LOWER_READING: Final = "19.5"
OTHER_SENSOR: Final = "sensor.living_room_window_temperature"
RENAMED_CLIMATE: Final = "climate.lounge"
INTEGRATOR_SENSOR: Final = "sensor.living_room_integrator"
MODE_SENSOR: Final = "sensor.living_room_control_mode"
WRITES_SENSOR: Final = "sensor.living_room_device_writes"
# Seconds the Core stop hand back may take in a test; the writes take far less.
HAND_BACK_BOUND: Final = 0.1
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
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
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
    await hass.async_block_till_done(wait_background_tasks=True)

    assert calls == entering_direct()


async def test_reading_from_before_the_start_is_not_used(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """A state restored or retained before the room started is not a reading."""
    hass.states.async_set(WALL_SENSOR, READING)
    freezer.tick(timedelta(seconds=1))
    await async_setup_room(hass)
    await async_set_room_target(hass, TARGET)
    mode = hass.states.get(MODE_SENSOR)
    assert mode is not None
    assert mode.state == "waiting"
    assert calls == []

    hass.states.async_set(WALL_SENSOR, READING, force_update=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert calls == entering_direct()


async def test_reading_change_in_direct_waits_for_next_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """In direct mode a new reading takes effect at the periodic step."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    calls.clear()

    hass.states.async_set(WALL_SENSOR, "19.5")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == []

    await async_advance(hass, freezer)

    # 1.5 °C error: 45 % proportional; the first step integrated nothing.
    assert calls == [("set_value", ids.valve, 45) for ids in TRVS]


async def test_unchanged_value_reported_after_fallback_steps_back_to_direct(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """A sensor back with its old value only reports, which still steps at once."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    while ("set_hvac_mode", TRVS[0].climate, "heat") not in calls:
        await async_advance(hass, freezer)
    calls.clear()
    reported_before = hass.states.get(WALL_SENSOR)
    assert reported_before is not None

    hass.states.async_set(WALL_SENSOR, READING)
    await hass.async_block_till_done(wait_background_tasks=True)

    reported = hass.states.get(WALL_SENSOR)
    assert reported is not None
    assert reported.last_changed == reported_before.last_changed
    # Three intervals of direct steps integrated 45 %, which the fallback kept.
    assert calls == [
        call
        for ids in TRVS
        for call in (
            ("set_preset_mode", ids.climate, DIRECT_MODE_PRESET),
            ("set_value", ids.valve, 75),
        )
    ]


async def test_unload_hands_back_every_trv(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """Unloading puts every TRV in heat mode at the room target."""
    entry = await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    calls.clear()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert calls == handing_back()


async def test_stop_hands_back_before_the_stop_event(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """Core stop hands the TRVs back before zwave_js disconnects on the stop event."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
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
async def test_stop_hand_back_is_bounded(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At Core stop a TRV that never answers is cut off at the hand back bound."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    stall = asyncio.Event()
    trvs[0].stall = stall
    monkeypatch.setattr(runtime, "HAND_BACK_TIMEOUT", HAND_BACK_BOUND)

    @callback
    def stop_event(_event: Event) -> None:
        stall.set()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_event)

    with caplog.at_level(logging.WARNING):
        await hass.async_stop()

    assert ("set_hvac_mode", TRVS[0].climate, "heat") in calls
    assert "cut off" in caplog.text


async def _async_stall_the_first_step(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """
    Set the target and wait until its step reaches the second TRV's valve.

    Time is not frozen here: a frozen clock would never end the hand back bound.
    """
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TEMPERATURE: TARGET},
        blocking=True,
    )
    await async_wait_for(lambda: ("set_value", TRVS[1].valve, OUTPUT) in calls)
    calls.clear()


@pytest.mark.usefixtures("zwave_js_flow")
async def test_stop_hands_back_past_a_stalled_step(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core stop cancels a step stalled on a TRV and still stops the room."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    stall = asyncio.Event()
    trvs[1].stall_valve = stall
    trvs[1].stall = stall
    await _async_stall_the_first_step(hass, calls)
    monkeypatch.setattr(runtime, "HAND_BACK_TIMEOUT", HAND_BACK_BOUND)
    at_stop_event: list[TrvCall] = []
    mode_at_stop_event: list[str] = []

    @callback
    def stop_event(_event: Event) -> None:
        at_stop_event.extend(calls)
        mode = hass.states.get(MODE_SENSOR)
        assert mode is not None
        mode_at_stop_event.append(mode.state)
        stall.set()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_event)

    await hass.async_stop()

    assert mode_at_stop_event == ["stopped"]
    # The healthy TRV is handed back; the stalled one is cut off in heat mode.
    assert sorted(at_stop_event) == sorted(
        [
            ("set_hvac_mode", TRVS[0].climate, "heat"),
            ("set_temperature", TRVS[0].climate, TARGET),
            ("set_hvac_mode", TRVS[1].climate, "heat"),
        ]
    )


@pytest.mark.usefixtures("zwave_js_flow")
async def test_unload_hands_back_past_a_stalled_step(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unload cancels a stalled step and hands every TRV back, with no deadline."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    entry = await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    stall = asyncio.Event()
    trvs[1].stall_valve = stall
    await _async_stall_the_first_step(hass, calls)
    room = entry.runtime_data[ROOM_ID].room
    assert room is not None
    # Any deadline on the unload's hand back would cut it off at once.
    monkeypatch.setattr(runtime, "HAND_BACK_TIMEOUT", 0.0)

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert room.control_mode is ControlMode.STOPPED
    assert sorted(calls) == sorted(handing_back())
    stall.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("zwave_js_flow")
async def test_target_shows_while_a_step_is_stalled(hass: HomeAssistant) -> None:
    """The thermostat echoes a new target without waiting for a stalled step."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    stall = asyncio.Event()
    trvs[1].stall_valve = stall
    await _async_stall_the_first_step(hass, calls)

    set_target = hass.async_create_task(
        hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TEMPERATURE: OTHER_TARGET},
            blocking=True,
        )
    )

    def shows_target() -> bool:
        state = hass.states.get(ROOM_CLIMATE)
        assert state is not None
        return bool(state.attributes[ATTR_TEMPERATURE] == OTHER_TARGET)

    await async_wait_for(shows_target)
    valve = hass.states.get(TRVS[1].valve)
    assert valve is not None
    assert float(valve.state) == 0  # the step is still stalled
    stall.set()
    await set_target
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("zwave_js_flow")
async def test_removed_trv_leaves_the_entry_not_ready(hass: HomeAssistant) -> None:
    """A TRV whose registry entry is gone needs the room reconfigured."""
    await async_setup_spirits(hass, [])
    stored = registry_ids(hass, [ids.climate for ids in TRVS])
    er.async_get(hass).async_remove(TRVS[1].climate)
    await hass.async_block_till_done(wait_background_tasks=True)

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

    with caplog.at_level(logging.WARNING):
        await async_setup_room(hass, sensors=[gone, *registry_ids(hass, [WALL_SENSOR])])
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)

    assert gone in caplog.text
    assert calls == entering_direct()


async def test_renamed_wall_sensor_reloads_the_room(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """A wall sensor given a new entity id is read under it after a reload."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)

    er.async_get(hass).async_update_entity(WALL_SENSOR, new_entity_id=RENAMED_SENSOR)
    hass.states.async_remove(WALL_SENSOR)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set(RENAMED_SENSOR, LOWER_READING)
    await hass.async_block_till_done(wait_background_tasks=True)

    climate = hass.states.get(ROOM_CLIMATE)
    mode = hass.states.get(MODE_SENSOR)
    assert climate is not None
    assert mode is not None
    assert climate.attributes[ATTR_CURRENT_TEMPERATURE] == float(LOWER_READING)
    assert mode.state == "direct"
    assert ("set_hvac_mode", TRVS[0].climate, "heat") in calls


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


@pytest.mark.parametrize("change", ["disable", "remove"])
async def test_climate_entity_going_away_hands_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    calls: list[TrvCall],
    change: str,
) -> None:
    """Disabling or removing the room thermostat hands its TRVs back for good."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    calls.clear()
    registry = er.async_get(hass)

    if change == "disable":
        registry.async_update_entity(
            ROOM_CLIMATE, disabled_by=er.RegistryEntryDisabler.USER
        )
    else:
        registry.async_remove(ROOM_CLIMATE)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ROOM_CLIMATE) is None
    assert calls == handing_back()
    mode = hass.states.get(MODE_SENSOR)
    assert mode is not None
    assert mode.state == "stopped"

    # Disabling reloads the entry after a delay; the room stays handed back.
    await async_advance(hass, freezer, RELOAD_DELAY)
    await async_advance(hass, freezer)
    assert calls == handing_back()


async def test_reenabled_thermostat_restores_the_hand_back_writes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """The write counter saved on disabling includes the hand back's writes."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    registry = er.async_get(hass)
    registry.async_update_entity(
        ROOM_CLIMATE, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_advance(hass, freezer, RELOAD_DELAY)
    assert calls[-len(handing_back()) :] == handing_back()

    registry.async_update_entity(ROOM_CLIMATE, disabled_by=None)
    await async_advance(hass, freezer, RELOAD_DELAY)

    writes = hass.states.get(WRITES_SENSOR)
    assert writes is not None
    assert int(writes.state) == len(calls)


async def test_reload_hands_back_once(
    hass: HomeAssistant, calls: list[TrvCall]
) -> None:
    """A room change reloads the entry, which hands each TRV back once."""
    entry = await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    calls.clear()
    subentry = entry.subentries[ROOM_ID]

    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, CONF_KP: 40.0}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    handed_back = handing_back()
    assert calls[: len(handed_back)] == handed_back
    assert ("set_hvac_mode", TRVS[0].climate, "heat") not in calls[len(handed_back) :]


@pytest.mark.usefixtures("calls")
async def test_reload_does_not_integrate_a_step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A room reloaded with new gains resumes from the integral it had."""
    entry = await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    await async_advance(hass, freezer)
    integrator = hass.states.get(INTEGRATOR_SENSOR)
    assert integrator is not None
    integral = float(integrator.state)
    assert integral > 0
    subentry = entry.subentries[ROOM_ID]

    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, CONF_KP: 40.0}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set(WALL_SENSOR, READING, force_update=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    mode = hass.states.get(MODE_SENSOR)
    integrator = hass.states.get(INTEGRATOR_SENSOR)
    assert mode is not None
    assert integrator is not None
    assert mode.state == "direct"
    assert float(integrator.state) == pytest.approx(integral)


@pytest.mark.usefixtures("zwave_js_flow")
async def test_target_set_during_reload_is_kept(hass: HomeAssistant) -> None:
    """A target set while a reload hands back is what the reloaded room runs at."""
    calls: list[TrvCall] = []
    trvs = await async_setup_spirits(hass, calls)
    entry = await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    stall = asyncio.Event()
    trvs[0].stall = stall
    subentry = entry.subentries[ROOM_ID]
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, CONF_KP: 40.0}
    )
    await async_wait_for(lambda: ("set_hvac_mode", TRVS[0].climate, "heat") in calls)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TEMPERATURE: OTHER_TARGET},
        blocking=True,
    )
    stall.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(ROOM_CLIMATE)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == OTHER_TARGET
    assert ("set_temperature", TRVS[1].climate, TARGET) in calls


async def test_renamed_climate_entity_keeps_the_room_running(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, calls: list[TrvCall]
) -> None:
    """A new entity id re-adds the thermostat without handing the TRVs back."""
    await async_setup_room(hass)
    hass.states.async_set(WALL_SENSOR, READING)
    await async_set_room_target(hass, TARGET)
    calls.clear()

    er.async_get(hass).async_update_entity(ROOM_CLIMATE, new_entity_id=RENAMED_CLIMATE)
    await hass.async_block_till_done(wait_background_tasks=True)
    renamed = hass.states.get(RENAMED_CLIMATE)
    hass.states.async_set(WALL_SENSOR, "19.5")
    await async_advance(hass, freezer)

    assert renamed is not None
    assert renamed.attributes[ATTR_TEMPERATURE] == TARGET
    # One step on the single, still running room: see the direct-mode test.
    assert calls == [("set_value", ids.valve, 45) for ids in TRVS]
