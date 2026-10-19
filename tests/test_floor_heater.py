"""Tests for the floor-heating thermostat adapter."""

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.room_valve_control.floor_heater import (
    HOLD_PRESET,
    ClimateFloorHeater,
    FloorHeater,
    FloorHeaterEntityError,
)

from .live_room import async_advance, async_wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, ServiceCall

CLIMATE_ID: Final = "climate.bathroom_floor"
UNIQUE_ID: Final = "0xa4c138aabbccddee_climate"
PLATFORM: Final = "mqtt"
FLOOR_MAX: Final = 26.0
FLOOR_MIN: Final = 17.0
FALLBACK: Final = 22.0
PERIOD: Final = timedelta(minutes=30)
MIN_ON_TIME: Final = timedelta(minutes=5)
PROGRAM_PRESET: Final = "program"
SERVICES: Final = ("set_hvac_mode", "set_preset_mode", "set_temperature")

# (service, value) of a write that reached the climate entity.
type Write = tuple[str, object]

ON: Final[Write] = ("set_temperature", FLOOR_MAX)
OFF: Final[Write] = ("set_temperature", FLOOR_MIN)


def set_climate(
    hass: HomeAssistant,
    state: str = "heat",
    *,
    preset: str = HOLD_PRESET,
    setpoint: float = FALLBACK,
) -> None:
    """Set the climate entity's state as Zigbee2MQTT reports a BHT-002."""
    hass.states.async_set(
        CLIMATE_ID,
        state,
        {
            "hvac_modes": ["off", "heat"],
            "preset_mode": preset,
            "preset_modes": [HOLD_PRESET, PROGRAM_PRESET],
            "temperature": setpoint,
        },
    )


def _value(call: ServiceCall) -> object:
    for key in ("hvac_mode", "preset_mode", "temperature"):
        if key in call.data:
            return call.data[key]
    return None


@pytest.fixture
def registered(hass: HomeAssistant) -> er.RegistryEntry:
    """Register the climate entity and report it in heat mode and hold."""
    entry = er.async_get(hass).async_get_or_create(
        "climate", PLATFORM, UNIQUE_ID, suggested_object_id="bathroom_floor"
    )
    set_climate(hass)
    return entry


@pytest.fixture
def writes(hass: HomeAssistant) -> list[Write]:
    """Stub the climate services and record the writes in order."""
    recorded: list[Write] = []

    async def handler(call: ServiceCall) -> None:
        recorded.append((call.service, _value(call)))

    for service in SERVICES:
        hass.services.async_register("climate", service, handler)
    return recorded


@pytest.fixture
def edge_writes() -> list[int]:
    """Record what the adapter reports of its timer's writes."""
    return []


@pytest.fixture
async def heater(
    hass: HomeAssistant,
    registered: er.RegistryEntry,  # noqa: ARG001
    edge_writes: list[int],
) -> AsyncIterator[ClimateFloorHeater]:
    """Return an adapter over the registered climate entity; drop its edge after."""
    device = ClimateFloorHeater(
        hass,
        CLIMATE_ID,
        on_setpoint=FLOOR_MAX,
        off_setpoint=FLOOR_MIN,
        min_on_time=MIN_ON_TIME,
        on_edge_written=edge_writes.append,
    )
    yield device
    await device.cancel_edge()


async def test_implements_protocol(heater: ClimateFloorHeater) -> None:
    """The adapter is a FloorHeater."""
    device: FloorHeater = heater
    assert device.name == CLIMATE_ID


async def test_unknown_entity_fails(hass: HomeAssistant) -> None:
    """A climate entity that is not in the registry is reported."""
    with pytest.raises(FloorHeaterEntityError) as excinfo:
        ClimateFloorHeater(
            hass,
            CLIMATE_ID,
            on_setpoint=FLOOR_MAX,
            off_setpoint=FLOOR_MIN,
            min_on_time=MIN_ON_TIME,
            on_edge_written=lambda _sent: None,
        )

    assert excinfo.value.entity_id == CLIMATE_ID
    assert not excinfo.value.disabled


async def test_disabled_entity_fails(
    hass: HomeAssistant,
    registered: er.RegistryEntry,  # noqa: ARG001
) -> None:
    """A disabled climate entity never reports, so it is refused."""
    er.async_get(hass).async_update_entity(
        CLIMATE_ID, disabled_by=er.RegistryEntryDisabler.USER
    )

    with pytest.raises(FloorHeaterEntityError) as excinfo:
        ClimateFloorHeater(
            hass,
            CLIMATE_ID,
            on_setpoint=FLOOR_MAX,
            off_setpoint=FLOOR_MIN,
            min_on_time=MIN_ON_TIME,
            on_edge_written=lambda _sent: None,
        )

    assert excinfo.value.disabled
    assert CLIMATE_ID in str(excinfo.value)


async def test_name_follows_entity_id(
    hass: HomeAssistant, heater: ClimateFloorHeater
) -> None:
    """The heater is named by its climate entity's current id."""
    er.async_get(hass).async_update_entity(CLIMATE_ID, new_entity_id="climate.renamed")

    assert heater.name == "climate.renamed"


async def test_available_follows_state(
    hass: HomeAssistant, heater: ClimateFloorHeater
) -> None:
    """The heater is available while its climate entity reports."""
    assert heater.available

    hass.states.async_set(CLIMATE_ID, STATE_UNAVAILABLE)

    assert not heater.available


async def test_duty_switches_on_then_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
    edge_writes: list[int],
) -> None:
    """40 % of 30 min heats for 12 min, and the off edge is reported."""
    sent = await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    assert sent == 1
    assert writes == [ON]

    await async_advance(hass, freezer, timedelta(minutes=12) - timedelta(seconds=1))
    assert writes == [ON]
    assert edge_writes == []

    await async_advance(hass, freezer, timedelta(seconds=1))
    assert writes == [ON, OFF]
    assert edge_writes == [1]


async def test_short_on_time_stays_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
    edge_writes: list[int],
) -> None:
    """An on-time under the minimum is dropped: off for the whole period."""
    sent = await heater.apply_duty(10, PERIOD, dt_util.utcnow())
    await async_advance(hass, freezer, PERIOD)

    assert sent == 1
    assert writes == [OFF]
    assert edge_writes == []


async def test_short_off_time_stays_on(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
    edge_writes: list[int],
) -> None:
    """An off-time under the minimum is dropped: on for the whole period."""
    await heater.apply_duty(90, PERIOD, dt_util.utcnow())
    await async_advance(hass, freezer, PERIOD)

    assert writes == [ON]
    assert edge_writes == []


async def test_new_period_replaces_pending_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
) -> None:
    """A period started early drops the old off edge and schedules its own."""
    await heater.apply_duty(40, PERIOD, dt_util.utcnow())
    await async_advance(hass, freezer, timedelta(minutes=8))

    await heater.apply_duty(50, PERIOD, dt_util.utcnow())
    # The first period's off edge would have come 4 min in.
    await async_advance(hass, freezer, timedelta(minutes=14))
    assert writes == [ON, ON]

    await async_advance(hass, freezer, timedelta(minutes=1))
    assert writes == [ON, ON, OFF]


async def test_heat_mode_written_before_setpoint(
    hass: HomeAssistant, heater: ClimateFloorHeater, writes: list[Write]
) -> None:
    """A thermostat switched off is put back in heat mode first."""
    set_climate(hass, "off")

    sent = await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    assert writes == [("set_hvac_mode", "heat"), ON]
    assert sent == len(writes)


async def test_hold_preset_written_before_setpoint(
    hass: HomeAssistant, heater: ClimateFloorHeater, writes: list[Write]
) -> None:
    """In the program preset the thermostat's schedule would win; hold first."""
    set_climate(hass, preset=PROGRAM_PRESET)

    sent = await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    assert writes == [("set_preset_mode", HOLD_PRESET), ON]
    assert sent == len(writes)


async def test_preset_left_alone_without_hold(
    hass: HomeAssistant, heater: ClimateFloorHeater, writes: list[Write]
) -> None:
    """An entity that offers no hold preset gets only the setpoint."""
    hass.states.async_set(CLIMATE_ID, "heat", {"temperature": FALLBACK})

    await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    assert writes == [ON]


async def test_unavailable_sends_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
    edge_writes: list[int],
) -> None:
    """Nothing is sent to an unavailable thermostat, and its edge reports 0."""
    hass.states.async_set(CLIMATE_ID, STATE_UNAVAILABLE)

    sent = await heater.apply_duty(40, PERIOD, dt_util.utcnow())
    await async_advance(hass, freezer, timedelta(minutes=12))

    assert sent == 0
    assert writes == []
    assert edge_writes == [0]
    assert await heater.hold_setpoint(FALLBACK) == 0


async def test_edges_written_whatever_the_read_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
) -> None:
    """A thermostat already reporting the edge setpoint still gets it."""
    set_climate(hass, setpoint=FLOOR_MAX)

    await heater.apply_duty(40, PERIOD, dt_util.utcnow())
    set_climate(hass, setpoint=FLOOR_MIN)
    await async_advance(hass, freezer, timedelta(minutes=12))

    assert writes == [ON, OFF]


async def test_failed_write_is_not_counted(
    hass: HomeAssistant, heater: ClimateFloorHeater
) -> None:
    """A service call that fails is not sent, and does not raise."""

    async def failing(_call: ServiceCall) -> None:
        msg = "no MQTT connection"
        raise HomeAssistantError(msg)

    hass.services.async_register("climate", "set_temperature", failing)

    assert await heater.apply_duty(40, PERIOD, dt_util.utcnow()) == 0


@pytest.mark.parametrize(
    ("reported", "failing"),
    [
        (("off", HOLD_PRESET), "set_hvac_mode"),
        (("heat", PROGRAM_PRESET), "set_preset_mode"),
    ],
    ids=["heat_mode", "hold_preset"],
)
async def test_failed_mode_write_skips_setpoint(
    hass: HomeAssistant,
    heater: ClimateFloorHeater,
    writes: list[Write],
    reported: tuple[str, str],
    failing: str,
) -> None:
    """Without heat mode or hold the setpoint would not hold, so it is not sent."""
    state, preset = reported
    set_climate(hass, state, preset=preset)

    async def failed(_call: ServiceCall) -> None:
        msg = "no MQTT connection"
        raise HomeAssistantError(msg)

    hass.services.async_register("climate", failing, failed)

    assert await heater.apply_duty(40, PERIOD, dt_util.utcnow()) == 0
    assert writes == []


async def test_hold_cancels_pending_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
    edge_writes: list[int],
) -> None:
    """The fallback setpoint is not overwritten by a later off edge."""
    await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    sent = await heater.hold_setpoint(FALLBACK)
    await async_advance(hass, freezer, PERIOD)

    assert sent == 1
    assert writes == [ON, ("set_temperature", FALLBACK)]
    assert edge_writes == []


async def test_cancel_edge_drops_pending_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
    writes: list[Write],
) -> None:
    """Cancelling the edge leaves the thermostat as it is."""
    await heater.apply_duty(40, PERIOD, dt_util.utcnow())

    await heater.cancel_edge()
    await async_advance(hass, freezer, PERIOD)

    assert writes == [ON]


async def test_hold_lands_after_off_in_flight(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    heater: ClimateFloorHeater,
) -> None:
    """A fallback called during the off write waits for it to return."""
    writes: list[Write] = []
    release = asyncio.Event()

    async def handler(call: ServiceCall) -> None:
        writes.append((call.service, _value(call)))
        if call.data.get("temperature") == FLOOR_MIN:
            await release.wait()

    hass.services.async_register("climate", "set_temperature", handler)
    await heater.apply_duty(40, PERIOD, dt_util.utcnow())
    freezer.tick(timedelta(minutes=12))
    async_fire_time_changed(hass)
    await async_wait_for(lambda: writes == [ON, OFF])

    hold = hass.async_create_task(heater.hold_setpoint(FALLBACK))
    for _ in range(10):
        await asyncio.sleep(0)
    assert writes == [ON, OFF]

    release.set()
    assert await hold == 1
    assert writes == [ON, OFF, ("set_temperature", FALLBACK)]
