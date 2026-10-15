"""Tests for the Eurotronic Spirit TRV adapter."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.room_valve_control import spirit
from custom_components.room_valve_control.spirit import (
    DIRECT_MODE_PRESET,
    SpiritEntity,
    SpiritEntityError,
    SpiritTrv,
)
from custom_components.room_valve_control.trv import TrvMode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

ZWAVE_JS: Final = "zwave_js"
HOME_ID: Final = 3245146787
NODE_ID: Final = 7
CLIMATE_ID: Final = "climate.living_room_trv"
VALVE_ID: Final = "number.living_room_trv_valve_control"
OFFSET_ID: Final = "number.living_room_trv_temperature_offset"
CLIMATE_UNIQUE_ID: Final = f"{HOME_ID}.{NODE_ID}-64-0-mode"
VALVE_UNIQUE_ID: Final = f"{HOME_ID}.{NODE_ID}-38-0-currentValue"
OFFSET_UNIQUE_ID: Final = f"{HOME_ID}.{NODE_ID}-112-0-8"
NODE_STATUS_ID: Final = "sensor.living_room_trv_node_status"
NODE_STATUS_UNIQUE_ID: Final = f"{HOME_ID}.{NODE_ID}.node_status"
CURRENT_TEMPERATURE: Final = 20.5
ADAPTER_SERVICES: Final = (
    ("climate", "set_preset_mode"),
    ("climate", "set_hvac_mode"),
    ("climate", "set_temperature"),
    ("number", "set_value"),
    (ZWAVE_JS, "set_config_parameter"),
)


@dataclass
class Spirit:
    """Registry entries of one mocked Spirit TRV."""

    device_id: str
    entity_registry: er.EntityRegistry


def _add_entity(
    spirit: Spirit,
    domain: str,
    unique_id: str,
    entity_id: str,
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> None:
    entry = spirit.entity_registry.async_get_or_create(
        domain,
        ZWAVE_JS,
        unique_id,
        device_id=spirit.device_id,
        suggested_object_id=entity_id.split(".", 1)[1],
        disabled_by=disabled_by,
    )
    assert entry.entity_id == entity_id


@pytest.fixture
def spirit_device(hass: HomeAssistant) -> Spirit:
    """Register a Spirit device with only its climate entity."""
    config_entry = MockConfigEntry(domain=ZWAVE_JS)
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(ZWAVE_JS, f"{HOME_ID}-{NODE_ID}")},
        manufacturer="Eurotronic",
        model="Spirit Z-Wave Plus",
    )
    spirit = Spirit(device.id, er.async_get(hass))
    _add_entity(spirit, "climate", CLIMATE_UNIQUE_ID, CLIMATE_ID)
    return spirit


@pytest.fixture
def full_spirit(hass: HomeAssistant, spirit_device: Spirit) -> Spirit:
    """Register a Spirit with its valve and offset entities and states."""
    _add_entity(spirit_device, "number", VALVE_UNIQUE_ID, VALVE_ID)
    _add_entity(spirit_device, "number", OFFSET_UNIQUE_ID, OFFSET_ID)
    set_climate(hass, "heat")
    hass.states.async_set(VALVE_ID, "40.0")
    hass.states.async_set(OFFSET_ID, "-12.0")
    return spirit_device


def set_climate(
    hass: HomeAssistant,
    state: str,
    preset: str = "none",
    setpoint: float | None = 21.0,
    step: float | None = None,
) -> None:
    """
    Set the climate entity's state as zwave_js reports it.

    A setpoint of None leaves the temperature out, as in mode 31.
    """
    attributes: dict[str, object] = {
        "preset_mode": preset,
        "current_temperature": CURRENT_TEMPERATURE,
    }
    if step is not None:
        attributes["target_temp_step"] = step
    if setpoint is not None:
        attributes["temperature"] = setpoint
    hass.states.async_set(CLIMATE_ID, state, attributes)


def record_service(
    hass: HomeAssistant, calls: list[ServiceCall], domain: str, service: str
) -> None:
    """Register a stub service that appends each call to calls."""

    async def handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register(domain, service, handler)


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Stub every service the adapter uses and record the calls in order."""
    recorded: list[ServiceCall] = []
    for domain, service in ADAPTER_SERVICES:
        record_service(hass, recorded, domain, service)
    return recorded


@pytest.mark.usefixtures("full_spirit")
async def test_resolves_valve_and_offset_entities(hass: HomeAssistant) -> None:
    """Both number entities are found on the climate entity's device."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert trv.valve_entity_id == VALVE_ID
    assert trv.offset_entity_id == OFFSET_ID


async def test_lookup_ignores_entity_ids(
    hass: HomeAssistant, spirit_device: Spirit
) -> None:
    """Renamed entities are still found by their Z-Wave value."""
    _add_entity(spirit_device, "number", VALVE_UNIQUE_ID, "number.renamed_valve")
    _add_entity(spirit_device, "number", OFFSET_UNIQUE_ID, "number.renamed_offset")
    _add_entity(
        spirit_device, "number", f"{HOME_ID}.{NODE_ID}-112-0-7", "number.param_7"
    )

    trv = SpiritTrv(hass, CLIMATE_ID)

    assert trv.valve_entity_id == "number.renamed_valve"
    assert trv.offset_entity_id == "number.renamed_offset"


async def test_disabled_offset_entity_fails_naming_it(
    hass: HomeAssistant, spirit_device: Spirit
) -> None:
    """A disabled parameter 8 entity is reported with its entity id."""
    _add_entity(spirit_device, "number", VALVE_UNIQUE_ID, VALVE_ID)
    _add_entity(
        spirit_device,
        "number",
        OFFSET_UNIQUE_ID,
        OFFSET_ID,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )

    with pytest.raises(SpiritEntityError) as excinfo:
        SpiritTrv(hass, CLIMATE_ID)

    assert excinfo.value.entity is SpiritEntity.TEMPERATURE_OFFSET
    assert excinfo.value.entity_id == OFFSET_ID
    assert excinfo.value.disabled
    assert OFFSET_ID in str(excinfo.value)


async def test_missing_valve_entity_fails(
    hass: HomeAssistant, spirit_device: Spirit
) -> None:
    """A device without the valve number cannot be controlled."""
    _add_entity(spirit_device, "number", OFFSET_UNIQUE_ID, OFFSET_ID)

    with pytest.raises(SpiritEntityError) as excinfo:
        SpiritTrv(hass, CLIMATE_ID)

    assert excinfo.value.entity is SpiritEntity.VALVE_CONTROL
    assert excinfo.value.entity_id is None
    assert not excinfo.value.disabled


async def test_unknown_climate_entity_fails(hass: HomeAssistant) -> None:
    """A climate entity that is not in the registry is reported."""
    with pytest.raises(SpiritEntityError) as excinfo:
        SpiritTrv(hass, CLIMATE_ID)

    assert excinfo.value.entity is SpiritEntity.CLIMATE
    assert excinfo.value.entity_id == CLIMATE_ID


async def test_disabled_climate_entity_fails(
    hass: HomeAssistant, spirit_device: Spirit
) -> None:
    """A disabled climate entity never reports, so it is refused."""
    spirit_device.entity_registry.async_update_entity(
        CLIMATE_ID, disabled_by=er.RegistryEntryDisabler.USER
    )

    with pytest.raises(SpiritEntityError) as excinfo:
        SpiritTrv(hass, CLIMATE_ID)

    assert excinfo.value.entity is SpiritEntity.CLIMATE
    assert excinfo.value.disabled


@pytest.mark.usefixtures("full_spirit")
async def test_reads_device_state(hass: HomeAssistant) -> None:
    """Setpoint, valve, offset and the TRV's own reading come from states."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert trv.available
    assert trv.mode is TrvMode.SELF_REGULATING
    assert trv.setpoint == pytest.approx(21.0)
    assert trv.valve == pytest.approx(40.0)
    assert trv.offset == pytest.approx(-1.2)
    # The reported 20.5 °C already includes the -1.2 °C offset.
    assert trv.own_temperature == pytest.approx(21.7)


@pytest.mark.usefixtures("full_spirit")
async def test_mode_direct_with_preset_and_unknown_hvac_mode(
    hass: HomeAssistant,
) -> None:
    """Mode 31 reads as direct although zwave_js has no hvac_mode for it."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    assert trv.available
    assert trv.mode is TrvMode.DIRECT


@pytest.mark.usefixtures("full_spirit")
async def test_mode_unknown_for_other_states(hass: HomeAssistant) -> None:
    """Neither direct nor heat reads as unknown."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, "off")

    assert trv.mode is TrvMode.UNKNOWN


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize("preset", ["Energy heat", "Full power"])
async def test_mode_unknown_for_heat_presets(hass: HomeAssistant, preset: str) -> None:
    """Presets that share the heat state are not self-regulation."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, "heat", preset=preset)

    assert trv.mode is TrvMode.UNKNOWN


@pytest.mark.usefixtures("full_spirit")
async def test_mode_self_regulating_without_preset(hass: HomeAssistant) -> None:
    """Heat without a preset attribute reads as self-regulation."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    hass.states.async_set(CLIMATE_ID, "heat", {"temperature": 21.0})

    assert trv.mode is TrvMode.SELF_REGULATING


def switch_to_heat_on_mode_write(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    stale_setpoint: float,
    *,
    deferred: bool = True,
) -> None:
    """Make the heat mode write report heat, at once or after it returns."""

    def report_heat() -> None:
        set_climate(hass, "heat", setpoint=stale_setpoint)

    async def handler(call: ServiceCall) -> None:
        calls.append(call)
        if deferred:
            hass.loop.call_soon(report_heat)
        else:
            report_heat()

    hass.services.async_register("climate", "set_hvac_mode", handler)


@pytest.fixture
def short_mode_change_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up waiting for a mode change almost at once."""
    monkeypatch.setattr(spirit, "MODE_CHANGE_TIMEOUT", 0.01)


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize("deferred", [False, True])
async def test_enter_self_regulation_sets_mode_then_setpoint(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    deferred: bool,  # noqa: FBT001
) -> None:
    """
    Once heat mode reads back, the setpoint is written regardless.

    The setpoint reported right after leaving mode 31 is stale, so it is
    written even when it already equals the target.
    """
    switch_to_heat_on_mode_write(hass, calls, stale_setpoint=19.5, deferred=deferred)
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    sent = await trv.enter_self_regulation(19.5)

    assert sent == 2  # noqa: PLR2004
    assert [(c.domain, c.service, dict(c.data)) for c in calls] == [
        ("climate", "set_hvac_mode", {"entity_id": CLIMATE_ID, "hvac_mode": "heat"}),
        (
            "climate",
            "set_temperature",
            {"entity_id": CLIMATE_ID, "temperature": 19.5},
        ),
    ]


@pytest.mark.usefixtures("full_spirit", "short_mode_change_timeout")
async def test_setpoint_waits_for_mode_change(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """
    No setpoint is written until heat mode reads back.

    The next call then writes it even though the reported setpoint matches.
    """
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    assert await trv.enter_self_regulation(19.5) == 1
    assert [c.service for c in calls] == ["set_hvac_mode"]

    set_climate(hass, "heat", setpoint=19.5)
    assert await trv.enter_self_regulation(19.5) == 1
    assert await trv.enter_self_regulation(19.5) == 0
    assert [c.service for c in calls] == ["set_hvac_mode", "set_temperature"]


@pytest.mark.usefixtures("full_spirit")
async def test_failed_mode_write_skips_setpoint(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """A failed heat mode write sends nothing more."""

    async def fail(call: ServiceCall) -> None:
        calls.append(call)
        msg = "asleep"
        raise HomeAssistantError(msg)

    hass.services.async_register("climate", "set_hvac_mode", fail)
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    assert await trv.enter_self_regulation(19.5) == 0
    assert [c.service for c in calls] == ["set_hvac_mode"]


@pytest.mark.usefixtures("full_spirit")
async def test_failed_setpoint_write_is_retried(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Mode sent but setpoint failed counts one, and the setpoint stays pending."""
    switch_to_heat_on_mode_write(hass, calls, stale_setpoint=19.5)
    failing = True

    async def setpoint(call: ServiceCall) -> None:
        calls.append(call)
        if failing:
            msg = "asleep"
            raise HomeAssistantError(msg)

    hass.services.async_register("climate", "set_temperature", setpoint)
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    assert await trv.enter_self_regulation(19.5) == 1
    failing = False
    assert await trv.enter_self_regulation(19.5) == 1
    assert [c.service for c in calls] == [
        "set_hvac_mode",
        "set_temperature",
        "set_temperature",
    ]


@pytest.mark.usefixtures("full_spirit")
async def test_enter_self_regulation_skips_what_already_matches(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Only the setpoint is written when the TRV already self-regulates."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.enter_self_regulation(21.0) == 0
    assert await trv.enter_self_regulation(19.0) == 1
    assert [c.service for c in calls] == ["set_temperature"]


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize(
    ("requested", "step", "written"),
    [(21.2, None, False), (21.2, 0.1, True), (21.5, None, True), (21.1, 0.1, True)],
)
async def test_setpoint_matches_within_half_a_step(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    requested: float,
    step: float | None,
    written: bool,  # noqa: FBT001
) -> None:
    """A setpoint within half a step of the reported 21.0 °C is not rewritten."""
    set_climate(hass, "heat", setpoint=21.0, step=step)
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.enter_self_regulation(requested) == int(written)
    assert len(calls) == int(written)


@pytest.mark.usefixtures("full_spirit")
async def test_enter_direct_sets_manufacturer_preset(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """Direct mode is the Manufacturer specific preset."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.enter_direct()
    assert [(c.domain, c.service, dict(c.data)) for c in calls] == [
        (
            "climate",
            "set_preset_mode",
            {"entity_id": CLIMATE_ID, "preset_mode": "Manufacturer specific"},
        )
    ]


@pytest.mark.usefixtures("full_spirit")
async def test_set_valve_writes_number(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """The valve position goes to the valve number entity."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.set_valve(63)
    assert [(c.domain, c.service, dict(c.data)) for c in calls] == [
        ("number", "set_value", {"entity_id": VALVE_ID, "value": 63})
    ]


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize(
    ("offset", "tenths"),
    [(7.0, 50), (-7.0, -50), (-0.26, -3), (1.04, 10), (0.0, 0)],
)
async def test_set_offset_clamps_and_rounds(
    hass: HomeAssistant,
    calls: list[ServiceCall],
    offset: float,
    tenths: int,
) -> None:
    """Offsets are written as parameter 8 in tenths, within ±5.0 °C."""
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.set_offset(offset)
    assert [(c.domain, c.service, dict(c.data)) for c in calls] == [
        (
            ZWAVE_JS,
            "set_config_parameter",
            {"entity_id": CLIMATE_ID, "parameter": 8, "value": tenths},
        )
    ]


@pytest.mark.usefixtures("full_spirit")
async def test_unavailable_trv_sends_nothing(
    hass: HomeAssistant, calls: list[ServiceCall]
) -> None:
    """An unavailable climate entity reports so and no write is sent."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    hass.states.async_set(CLIMATE_ID, STATE_UNAVAILABLE)

    assert not trv.available
    assert trv.mode is TrvMode.UNKNOWN
    assert not await trv.enter_direct()
    assert await trv.enter_self_regulation(20.0) == 0
    assert not await trv.set_valve(50)
    assert not await trv.set_offset(1.0)
    assert calls == []


@pytest.mark.usefixtures("full_spirit")
async def test_unreadable_values_read_as_none(hass: HomeAssistant) -> None:
    """Unavailable number entities give no valve, offset or own reading."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    hass.states.async_set(VALVE_ID, STATE_UNAVAILABLE)
    hass.states.async_set(OFFSET_ID, STATE_UNKNOWN)

    assert trv.valve is None
    assert trv.offset is None
    assert trv.own_temperature is None


@pytest.mark.usefixtures("full_spirit")
async def test_failed_service_reports_not_sent(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A service error is logged and reported, never raised."""

    async def fail(call: ServiceCall) -> None:
        msg = f"{call.service} failed"
        raise HomeAssistantError(msg)

    for domain, service in ADAPTER_SERVICES:
        hass.services.async_register(domain, service, fail)
    trv = SpiritTrv(hass, CLIMATE_ID)
    set_climate(hass, STATE_UNKNOWN, preset=DIRECT_MODE_PRESET, setpoint=None)

    assert not await trv.enter_direct()
    assert await trv.enter_self_regulation(20.0) == 0
    assert not await trv.set_valve(50)
    assert not await trv.set_offset(1.0)
    assert "set_preset_mode failed" in caplog.text
    assert "set_config_parameter failed" in caplog.text


@pytest.mark.parametrize(
    ("status", "available"),
    [("dead", False), ("alive", True), ("asleep", True), ("unknown", True)],
)
async def test_node_status_decides_availability(
    hass: HomeAssistant,
    full_spirit: Spirit,
    calls: list[ServiceCall],
    status: str,
    available: bool,  # noqa: FBT001
) -> None:
    """A dead node is unavailable although its entities still report."""
    _add_entity(full_spirit, "sensor", NODE_STATUS_UNIQUE_ID, NODE_STATUS_ID)
    hass.states.async_set(NODE_STATUS_ID, status)
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert trv.available is available
    assert await trv.set_valve(50) is available
    assert len(calls) == int(available)


async def test_disabled_node_status_falls_back_to_climate(
    hass: HomeAssistant, full_spirit: Spirit
) -> None:
    """Without a node status reading, availability is the climate entity's."""
    _add_entity(
        full_spirit,
        "sensor",
        NODE_STATUS_UNIQUE_ID,
        NODE_STATUS_ID,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert trv.available
    hass.states.async_set(CLIMATE_ID, STATE_UNAVAILABLE)
    assert not trv.available


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize("error", [ValueError, KeyError])
async def test_entity_errors_report_not_sent(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception],
) -> None:
    """zwave_js raises ValueError and KeyError outside HomeAssistantError."""

    async def fail(call: ServiceCall) -> None:
        raise error(call.service)

    hass.services.async_register("climate", "set_temperature", fail)
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert await trv.enter_self_regulation(19.0) == 0
    assert "set_temperature failed" in caplog.text


@pytest.mark.usefixtures("full_spirit")
async def test_stalled_service_times_out(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A write that never completes is abandoned and reported not sent."""
    monkeypatch.setattr(spirit, "SERVICE_TIMEOUT", 0.01)

    async def stall(_call: ServiceCall) -> None:
        await asyncio.Event().wait()

    hass.services.async_register("number", "set_value", stall)
    trv = SpiritTrv(hass, CLIMATE_ID)

    assert not await trv.set_valve(50)
    assert "set_value timed out" in caplog.text


@pytest.mark.usefixtures("full_spirit")
@pytest.mark.parametrize("valve_state", [STATE_UNAVAILABLE, None])
async def test_set_valve_needs_valve_entity_state(
    hass: HomeAssistant, calls: list[ServiceCall], valve_state: str | None
) -> None:
    """An unavailable or stateless valve entity is not written."""
    trv = SpiritTrv(hass, CLIMATE_ID)
    if valve_state is None:
        hass.states.async_remove(VALVE_ID)
    else:
        hass.states.async_set(VALVE_ID, valve_state)

    assert not await trv.set_valve(50)
    assert calls == []
