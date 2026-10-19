"""Tests for the config flow and the room subentry flow."""

import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import patch

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigSubentry,
    ConfigSubentryData,
)
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.room_valve_control.const import (
    CONF_CONTROL_STEP,
    CONF_FALLBACK_FLOOR_SETPOINT,
    CONF_FLOOR_HEATERS,
    CONF_FLOOR_MAX,
    CONF_FLOOR_MIN,
    CONF_KP,
    CONF_MAX_TARGET,
    CONF_MIN_CHANGE,
    CONF_MIN_ON_TIME,
    CONF_MIN_TARGET,
    CONF_SENSORS,
    CONF_STALE_TIMEOUT,
    CONF_TI,
    CONF_TRVS,
    DOMAIN,
    SUBENTRY_TYPE_ROOM,
)

from .helpers import add_offset_entity, add_spirit_device, add_valve_entity
from .live_room import FLOOR_KEYS

if TYPE_CHECKING:
    from homeassistant.config_entries import SubentryFlowResult
    from homeassistant.core import HomeAssistant

COMPONENT_DIR: Final = Path(__file__).parents[1] / "custom_components" / DOMAIN
SETUP_ENTRY: Final = "custom_components.room_valve_control.async_setup_entry"

SENSOR: Final = "sensor.bedroom_temperature"
UNREGISTERED_SENSOR: Final = "sensor.yaml_temperature"
RENAMED_SENSOR: Final = "sensor.main_bedroom_temperature"
RENAMED_TRV: Final = "climate.main_bedroom_trv"
TRV_1: Final = "climate.bedroom_trv_1"
TRV_2: Final = "climate.bedroom_trv_2"
DISABLED_OFFSET_TRV: Final = "climate.office_trv"
DISABLED_OFFSET_ID: Final = "number.office_trv_temperature_offset"
NOT_A_SPIRIT: Final = "climate.heat_pump"
DEVICELESS_TRV: Final = "climate.deviceless_trv"
HEATER_1: Final = "climate.bathroom_floor"
HEATER_2: Final = "climate.hallway_floor"
DISABLED_HEATER: Final = "climate.sauna_floor"
UNREGISTERED_HEATER: Final = "climate.yaml_floor"
ROOM_THERMOSTAT: Final = "climate.hall"
ZERO_DURATION: Final = {"hours": 0, "minutes": 0, "seconds": 0}

DEFAULT_PARAMETERS: Final = {
    CONF_KP: 30.0,
    CONF_TI: 1800.0,
    CONF_CONTROL_STEP: 900.0,
    CONF_MIN_CHANGE: 5.0,
    CONF_STALE_TIMEOUT: 2700.0,
    CONF_MIN_TARGET: 5.0,
    CONF_MAX_TARGET: 30.0,
    CONF_FLOOR_MAX: 26.0,
    CONF_FLOOR_MIN: 17.0,
    CONF_FALLBACK_FLOOR_SETPOINT: 22.0,
    CONF_MIN_ON_TIME: 300.0,
}

ERROR_KEYS: Final = (
    "no_sensor",
    "sensor_not_registered",
    "no_trv_or_heater",
    "trvs_and_heaters",
    "trv_in_use",
    "trv_entity_missing",
    "trv_entity_disabled",
    "trv_no_device",
    "not_positive",
    "not_finite",
    "min_change_out_of_range",
    "target_range_empty",
    "heater_in_use",
    "heater_not_registered",
    "heater_entity_disabled",
    "heater_is_room",
    "not_whole_degrees",
    "floor_setpoints_out_of_order",
    "min_on_time_too_long",
)


def _add_full_spirit(
    hass: HomeAssistant,
    node_id: int,
    climate_id: str,
    offset_disabled_by: er.RegistryEntryDisabler | None = None,
) -> None:
    name = climate_id.split(".", 1)[1]
    spirit = add_spirit_device(hass, node_id, climate_id)
    add_valve_entity(spirit, f"number.{name}_valve_control")
    add_offset_entity(
        spirit, f"number.{name}_temperature_offset", disabled_by=offset_disabled_by
    )


@pytest.fixture(autouse=True)
def registry_entries(hass: HomeAssistant) -> None:
    """Register a wall sensor, two usable Spirits and one with parameter 8 disabled."""
    er.async_get(hass).async_get_or_create(
        "sensor", "test", "bedroom", suggested_object_id="bedroom_temperature"
    )
    _add_full_spirit(hass, 11, TRV_1)
    _add_full_spirit(hass, 12, TRV_2)
    _add_full_spirit(
        hass,
        13,
        DISABLED_OFFSET_TRV,
        offset_disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    registry = er.async_get(hass)
    for heater in (HEATER_1, HEATER_2):
        registry.async_get_or_create(
            "climate", "test", heater, suggested_object_id=heater.split(".", 1)[1]
        )
    registry.async_get_or_create(
        "climate",
        "test",
        DISABLED_HEATER,
        suggested_object_id=DISABLED_HEATER.split(".", 1)[1],
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_get_or_create(
        "climate",
        DOMAIN,
        "hall_room",
        suggested_object_id=ROOM_THERMOSTAT.split(".", 1)[1],
    )


def _room_input(**overrides: Any) -> dict[str, Any]:
    return {
        CONF_NAME: "Bedroom",
        CONF_SENSORS: [SENSOR],
        CONF_TRVS: [TRV_1, TRV_2],
    } | overrides


def _floor_input(**overrides: Any) -> dict[str, Any]:
    """Return a floor room's form input, leaving the TRV field untouched."""
    return {
        CONF_NAME: "Bathroom",
        CONF_SENSORS: [SENSOR],
        CONF_FLOOR_HEATERS: [HEATER_1],
    } | overrides


async def _add_entry(
    hass: HomeAssistant, rooms: list[ConfigSubentryData] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, subentries_data=rooms or [])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _registry_ids(hass: HomeAssistant, *entity_ids: str) -> list[str]:
    registry = er.async_get(hass)
    entries = [registry.async_get(entity_id) for entity_id in entity_ids]
    return [entry.id for entry in entries if entry is not None]


def _room_data(
    hass: HomeAssistant, trvs: list[str], heaters: list[str] | None = None
) -> dict[str, Any]:
    return DEFAULT_PARAMETERS | {
        CONF_SENSORS: _registry_ids(hass, SENSOR),
        CONF_TRVS: _registry_ids(hass, *trvs),
        CONF_FLOOR_HEATERS: _registry_ids(hass, *(heaters or [])),
    }


def _room(
    hass: HomeAssistant,
    title: str,
    trvs: list[str],
    heaters: list[str] | None = None,
) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=_room_data(hass, trvs, heaters),
        subentry_type=SUBENTRY_TYPE_ROOM,
        title=title,
        unique_id=None,
    )


async def _create_room(
    hass: HomeAssistant, entry: MockConfigEntry, room_input: dict[str, Any]
) -> SubentryFlowResult:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], room_input
    )


async def test_user_step_creates_entry(hass: HomeAssistant) -> None:
    """Submitting the user step creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Room Valve Control"
    assert result["data"] == {}


async def test_second_entry_is_refused(hass: HomeAssistant) -> None:
    """Only one entry may exist."""
    MockConfigEntry(domain=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_room_with_defaults_is_stored(hass: HomeAssistant) -> None:
    """A room with one sensor and two TRVs is stored with the defaults."""
    entry = await _add_entry(hass)

    result = await _create_room(hass, entry, _room_input())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_TYPE_ROOM
    assert subentry.title == "Bedroom"
    assert dict(subentry.data) == _room_data(hass, [TRV_1, TRV_2])


async def test_room_stores_registry_ids(hass: HomeAssistant) -> None:
    """Sensors and TRVs are stored by entity registry id, not entity id."""
    entry = await _add_entry(hass)
    registry = er.async_get(hass)

    await _create_room(hass, entry, _room_input())

    (subentry,) = entry.subentries.values()
    sensor_entry = registry.async_get(SENSOR)
    trv_entries = [registry.async_get(TRV_1), registry.async_get(TRV_2)]
    assert sensor_entry is not None
    assert None not in trv_entries
    assert subentry.data[CONF_SENSORS] == [sensor_entry.id]
    assert subentry.data[CONF_TRVS] == [e.id for e in trv_entries if e is not None]


async def test_unregistered_sensor_is_refused(hass: HomeAssistant) -> None:
    """A sensor without a registry entry cannot be followed through renames."""
    hass.states.async_set(UNREGISTERED_SENSOR, "20.5")
    entry = await _add_entry(hass)

    result = await _create_room(
        hass, entry, _room_input(**{CONF_SENSORS: [SENSOR, UNREGISTERED_SENSOR]})
    )

    assert result["errors"] == {CONF_SENSORS: "sensor_not_registered"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["sensor"] == UNREGISTERED_SENSOR
    assert entry.subentries == {}


async def test_room_parameters_are_stored_in_seconds(hass: HomeAssistant) -> None:
    """Entered parameters are stored, durations as seconds."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass,
        entry,
        _room_input(
            **{
                CONF_KP: 12.5,
                CONF_TI: {"hours": 1, "minutes": 30, "seconds": 0},
                CONF_CONTROL_STEP: {"hours": 0, "minutes": 10, "seconds": 0},
                CONF_MIN_CHANGE: 99,
                CONF_STALE_TIMEOUT: {"hours": 1, "minutes": 0, "seconds": 0},
                CONF_MIN_TARGET: 17.0,
                CONF_MAX_TARGET: 17.5,
            }
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == _room_data(hass, [TRV_1, TRV_2]) | {
        CONF_KP: 12.5,
        CONF_TI: 5400.0,
        CONF_CONTROL_STEP: 600.0,
        CONF_MIN_CHANGE: 99.0,
        CONF_STALE_TIMEOUT: 3600.0,
        CONF_MIN_TARGET: 17.0,
        CONF_MAX_TARGET: 17.5,
    }


async def test_adding_a_room_reloads_the_entry(hass: HomeAssistant) -> None:
    """A new room takes effect by reloading the entry."""
    entry = await _add_entry(hass)

    with patch(SETUP_ENTRY, return_value=True) as setup_entry:
        await _create_room(hass, entry, _room_input())
        await hass.async_block_till_done()

    setup_entry.assert_called_once()


async def test_removing_a_room_reloads_the_entry(hass: HomeAssistant) -> None:
    """A removed room stops being controlled by reloading the entry."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1])])
    (subentry_id,) = entry.subentries

    with patch(SETUP_ENTRY, return_value=True) as setup_entry:
        hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()

    setup_entry.assert_called_once()


async def test_disabled_offset_entity_is_shown_on_trv_field(
    hass: HomeAssistant,
) -> None:
    """A TRV whose parameter 8 entity is disabled is refused, naming it."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass, entry, _room_input(**{CONF_TRVS: [TRV_1, DISABLED_OFFSET_TRV]})
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TRVS: "trv_entity_disabled"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["trv"] == DISABLED_OFFSET_TRV
    assert placeholders["entity_id"] == DISABLED_OFFSET_ID
    assert entry.subentries == {}


async def test_climate_that_is_not_a_spirit_is_refused(hass: HomeAssistant) -> None:
    """A climate entity without the Spirit's numbers is refused, naming it."""
    entry = await _add_entry(hass)

    result = await _create_room(hass, entry, _room_input(**{CONF_TRVS: [NOT_A_SPIRIT]}))

    assert result["errors"] == {CONF_TRVS: "trv_entity_missing"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["trv"] == NOT_A_SPIRIT
    assert placeholders["entity"] == "climate"


async def test_climate_without_device_is_refused(hass: HomeAssistant) -> None:
    """A climate entity that belongs to no device is refused as deviceless."""
    er.async_get(hass).async_get_or_create(
        "climate", "test", "deviceless", suggested_object_id="deviceless_trv"
    )
    entry = await _add_entry(hass)

    result = await _create_room(
        hass, entry, _room_input(**{CONF_TRVS: [DEVICELESS_TRV]})
    )

    assert result["errors"] == {CONF_TRVS: "trv_no_device"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["trv"] == DEVICELESS_TRV


async def test_trv_in_another_room_is_refused(hass: HomeAssistant) -> None:
    """A TRV already assigned to another room is refused, naming that room."""
    entry = await _add_entry(hass, [_room(hass, "Living room", [TRV_1])])

    result = await _create_room(hass, entry, _room_input())

    assert result["errors"] == {CONF_TRVS: "trv_in_use"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["trv"] == TRV_1
    assert placeholders["room"] == "Living room"
    assert len(entry.subentries) == 1


@pytest.mark.parametrize(
    ("overrides", "errors"),
    [
        ({CONF_SENSORS: []}, {CONF_SENSORS: "no_sensor"}),
        ({CONF_TRVS: []}, {CONF_TRVS: "no_trv_or_heater"}),
        (
            {CONF_FLOOR_HEATERS: [HEATER_1]},
            {CONF_FLOOR_HEATERS: "trvs_and_heaters"},
        ),
        ({CONF_KP: 0}, {CONF_KP: "not_positive"}),
        ({CONF_TI: ZERO_DURATION}, {CONF_TI: "not_positive"}),
        ({CONF_CONTROL_STEP: ZERO_DURATION}, {CONF_CONTROL_STEP: "not_positive"}),
        ({CONF_STALE_TIMEOUT: ZERO_DURATION}, {CONF_STALE_TIMEOUT: "not_positive"}),
        ({CONF_MIN_CHANGE: 2}, {CONF_MIN_CHANGE: "min_change_out_of_range"}),
        ({CONF_MIN_CHANGE: 100}, {CONF_MIN_CHANGE: "min_change_out_of_range"}),
        (
            {CONF_MIN_TARGET: 21.0, CONF_MAX_TARGET: 21.0},
            {CONF_MAX_TARGET: "target_range_empty"},
        ),
    ],
)
async def test_invalid_room_is_refused_on_its_field(
    hass: HomeAssistant, overrides: dict[str, Any], errors: dict[str, str]
) -> None:
    """Each constraint is reported on the field that breaks it."""
    entry = await _add_entry(hass)

    result = await _create_room(hass, entry, _room_input(**overrides))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert entry.subentries == {}


@pytest.mark.parametrize(
    "field", [CONF_KP, CONF_MIN_CHANGE, CONF_MIN_TARGET, CONF_MAX_TARGET]
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_non_finite_number_is_refused_on_its_field(
    hass: HomeAssistant, field: str, value: float
) -> None:
    """A NaN or infinite number is refused on its own field only."""
    entry = await _add_entry(hass)

    result = await _create_room(hass, entry, _room_input(**{field: value}))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: "not_finite"}
    assert entry.subentries == {}


async def test_repeated_selections_are_stored_once(hass: HomeAssistant) -> None:
    """A sensor or TRV selected twice is stored once, in selection order."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass,
        entry,
        _room_input(
            **{CONF_SENSORS: [SENSOR, SENSOR], CONF_TRVS: [TRV_2, TRV_1, TRV_2]}
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.data[CONF_SENSORS] == _registry_ids(hass, SENSOR)
    assert subentry.data[CONF_TRVS] == _registry_ids(hass, TRV_2, TRV_1)


async def test_floor_room_is_stored(hass: HomeAssistant) -> None:
    """A floor room stores its heaters by registry id, floor defaults, no TRV."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass, entry, _floor_input(**{CONF_FLOOR_HEATERS: [HEATER_2, HEATER_1]})
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == _room_data(hass, [], [HEATER_2, HEATER_1])


async def test_floor_parameters_are_stored(hass: HomeAssistant) -> None:
    """Entered floor setpoints are stored, the minimum on-time in seconds."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass,
        entry,
        _floor_input(
            **{
                CONF_CONTROL_STEP: {"hours": 0, "minutes": 30, "seconds": 0},
                CONF_FLOOR_MAX: 28,
                CONF_FLOOR_MIN: 15,
                CONF_FALLBACK_FLOOR_SETPOINT: 20,
                CONF_MIN_ON_TIME: {"hours": 0, "minutes": 15, "seconds": 0},
            }
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == _room_data(hass, [], [HEATER_1]) | {
        CONF_CONTROL_STEP: 1800.0,
        CONF_FLOOR_MAX: 28.0,
        CONF_FLOOR_MIN: 15.0,
        CONF_FALLBACK_FLOOR_SETPOINT: 20.0,
        CONF_MIN_ON_TIME: 900.0,
    }


@pytest.mark.parametrize(
    ("overrides", "errors"),
    [
        ({CONF_FLOOR_HEATERS: []}, {CONF_TRVS: "no_trv_or_heater"}),
        (
            {CONF_FLOOR_MIN: 22, CONF_FALLBACK_FLOOR_SETPOINT: 22},
            {CONF_FALLBACK_FLOOR_SETPOINT: "floor_setpoints_out_of_order"},
        ),
        (
            {CONF_FALLBACK_FLOOR_SETPOINT: 26, CONF_FLOOR_MAX: 26},
            {CONF_FLOOR_MAX: "floor_setpoints_out_of_order"},
        ),
        ({CONF_FLOOR_MAX: 26.5}, {CONF_FLOOR_MAX: "not_whole_degrees"}),
        ({CONF_FLOOR_MIN: 17.5}, {CONF_FLOOR_MIN: "not_whole_degrees"}),
        (
            {CONF_FALLBACK_FLOOR_SETPOINT: 21.5},
            {CONF_FALLBACK_FLOOR_SETPOINT: "not_whole_degrees"},
        ),
        (
            {CONF_MIN_ON_TIME: {"hours": 0, "minutes": 7, "seconds": 31}},
            {CONF_MIN_ON_TIME: "min_on_time_too_long"},
        ),
        (
            {CONF_FLOOR_HEATERS: [DISABLED_HEATER]},
            {CONF_FLOOR_HEATERS: "heater_entity_disabled"},
        ),
        (
            {CONF_FLOOR_HEATERS: [HEATER_1, UNREGISTERED_HEATER]},
            {CONF_FLOOR_HEATERS: "heater_not_registered"},
        ),
        (
            {CONF_FLOOR_HEATERS: [ROOM_THERMOSTAT]},
            {CONF_FLOOR_HEATERS: "heater_is_room"},
        ),
    ],
)
async def test_invalid_floor_room_is_refused_on_its_field(
    hass: HomeAssistant, overrides: dict[str, Any], errors: dict[str, str]
) -> None:
    """Each floor constraint is reported on the field that breaks it."""
    entry = await _add_entry(hass)

    result = await _create_room(hass, entry, _floor_input(**overrides))

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert entry.subentries == {}


async def test_min_on_time_of_half_the_control_step_is_accepted(
    hass: HomeAssistant,
) -> None:
    """The minimum on-time may be half the control step, the default 15 min."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass,
        entry,
        _floor_input(**{CONF_MIN_ON_TIME: {"hours": 0, "minutes": 7, "seconds": 30}}),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_floor_fields_of_a_trv_room_are_not_checked(
    hass: HomeAssistant,
) -> None:
    """A TRV room is not refused for floor values it does not use."""
    entry = await _add_entry(hass)

    result = await _create_room(
        hass,
        entry,
        _room_input(
            **{
                CONF_CONTROL_STEP: {"hours": 0, "minutes": 5, "seconds": 0},
                CONF_FLOOR_MAX: 26.5,
            }
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("other_trvs", "other_heaters", "selected"),
    [
        ([], [HEATER_1], [HEATER_2, HEATER_1]),
        ([TRV_1], [], [TRV_1]),
    ],
    ids=["heater_of_a_floor_room", "trv_of_a_trv_room"],
)
async def test_heater_in_another_room_is_refused(
    hass: HomeAssistant,
    other_trvs: list[str],
    other_heaters: list[str],
    selected: list[str],
) -> None:
    """A device already in another room cannot be a heater, naming that room."""
    entry = await _add_entry(hass, [_room(hass, "Hall", other_trvs, other_heaters)])

    result = await _create_room(
        hass, entry, _floor_input(**{CONF_FLOOR_HEATERS: selected})
    )

    assert result["errors"] == {CONF_FLOOR_HEATERS: "heater_in_use"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["heater"] == selected[-1]
    assert placeholders["room"] == "Hall"


async def test_floor_heater_of_another_room_cannot_be_a_trv(
    hass: HomeAssistant,
) -> None:
    """The TRV in-use check also covers other rooms' floor heaters."""
    entry = await _add_entry(hass, [_room(hass, "Hall", [], [TRV_1])])

    result = await _create_room(hass, entry, _room_input())

    assert result["errors"] == {CONF_TRVS: "trv_in_use"}


async def _reconfigure_room(
    hass: HomeAssistant, entry: MockConfigEntry, room_input: dict[str, Any]
) -> SubentryFlowResult:
    (subentry_id,) = entry.subentries
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], room_input
    )


async def test_reconfigure_updates_room_and_reloads_entry(
    hass: HomeAssistant,
) -> None:
    """A new minimum change is stored and the entry reloads to apply it."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1, TRV_2])])

    with patch(SETUP_ENTRY, return_value=True) as setup_entry:
        result = await _reconfigure_room(
            hass, entry, _room_input(**{CONF_MIN_CHANGE: 10})
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == _room_data(hass, [TRV_1, TRV_2]) | {
        CONF_MIN_CHANGE: 10.0
    }
    setup_entry.assert_called_once()


async def test_reconfigure_keeps_unsubmitted_values(hass: HomeAssistant) -> None:
    """The reconfigure form defaults to the room's stored values."""
    stored = _room(hass, "Bedroom", [TRV_1])
    stored["data"] = dict(stored["data"]) | {CONF_TI: 5400.0}
    entry = await _add_entry(hass, [stored])

    result = await _reconfigure_room(hass, entry, {CONF_NAME: "Main bedroom"})

    assert result["reason"] == "reconfigure_successful"
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Main bedroom"
    assert dict(subentry.data) == dict(stored["data"])


async def test_reconfigure_still_refuses_another_rooms_trv(
    hass: HomeAssistant,
) -> None:
    """A room's own TRVs are allowed, another room's are not."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1])])
    hass.config_entries.async_add_subentry(
        entry, _config_subentry(hass, "Office", TRV_2)
    )
    await hass.async_block_till_done()
    bedroom_id = next(
        sid for sid, sub in entry.subentries.items() if sub.title == "Bedroom"
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": bedroom_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _room_input(**{CONF_TRVS: [TRV_1, TRV_2]})
    )

    assert result["errors"] == {CONF_TRVS: "trv_in_use"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["room"] == "Office"


def _default(result: SubentryFlowResult, field: str) -> Any:
    schema = result["data_schema"]
    assert schema is not None
    key = next(key for key in schema.schema if key == field)
    return key.default()


async def test_reconfigure_follows_renamed_entities(hass: HomeAssistant) -> None:
    """Renamed sensors and TRVs are shown by their new ids and stay in the room."""
    entry = await _add_entry(hass)
    await _create_room(hass, entry, _room_input())
    (subentry,) = entry.subentries.values()
    stored = dict(subentry.data)
    registry = er.async_get(hass)
    registry.async_update_entity(SENSOR, new_entity_id=RENAMED_SENSOR)
    registry.async_update_entity(TRV_1, new_entity_id=RENAMED_TRV)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    assert _default(result, CONF_SENSORS) == [RENAMED_SENSOR]
    assert _default(result, CONF_TRVS) == [RENAMED_TRV, TRV_2]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Bedroom"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry.subentry_id].data) == stored


async def test_reconfigure_leaves_out_removed_entities(hass: HomeAssistant) -> None:
    """A stored TRV whose registry entry is gone is missing from the form."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1, TRV_2])])
    (subentry_id,) = entry.subentries
    er.async_get(hass).async_remove(TRV_2)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )

    assert _default(result, CONF_TRVS) == [TRV_1]


async def test_renamed_trv_stays_in_use(hass: HomeAssistant) -> None:
    """A TRV renamed after it joined a room cannot be added to another room."""
    entry = await _add_entry(hass, [_room(hass, "Living room", [TRV_1])])
    er.async_get(hass).async_update_entity(TRV_1, new_entity_id=RENAMED_TRV)

    result = await _create_room(hass, entry, _room_input(**{CONF_TRVS: [RENAMED_TRV]}))

    assert result["errors"] == {CONF_TRVS: "trv_in_use"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["trv"] == RENAMED_TRV
    assert placeholders["room"] == "Living room"


async def test_rejected_reconfigure_keeps_submitted_values(
    hass: HomeAssistant,
) -> None:
    """A reconfigure refused for a parameter shows the values the user entered."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1])])
    stored = dict(entry.subentries[next(iter(entry.subentries))].data)

    submitted = _room_input(**{CONF_KP: 0, CONF_MIN_CHANGE: 10})

    result = await _reconfigure_room(hass, entry, submitted)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {CONF_KP: "not_positive"}
    for field in (CONF_KP, CONF_MIN_CHANGE, CONF_TRVS):
        assert _suggested(result, field) == submitted[field]
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == stored


def _suggested(result: SubentryFlowResult, field: str) -> Any:
    schema = result["data_schema"]
    assert schema is not None
    key = next(key for key in schema.schema if key == field)
    return key.description["suggested_value"]


def _config_subentry(hass: HomeAssistant, title: str, trv: str) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(_room_data(hass, [trv])),
        subentry_type=SUBENTRY_TYPE_ROOM,
        title=title,
        unique_id=None,
    )


async def test_reconfigure_room_stored_before_floor_heaters(
    hass: HomeAssistant,
) -> None:
    """A room without the floor keys shows and stores their defaults."""
    stored = _room(hass, "Bedroom", [TRV_1])
    stored["data"] = {
        key: value for key, value in stored["data"].items() if key not in FLOOR_KEYS
    }
    entry = await _add_entry(hass, [stored])
    (subentry_id,) = entry.subentries

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert _default(result, CONF_FLOOR_HEATERS) == []
    assert _default(result, CONF_FLOOR_MAX) == DEFAULT_PARAMETERS[CONF_FLOOR_MAX]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Bedroom"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry_id].data) == _room_data(hass, [TRV_1])


async def test_reconfigure_trv_room_into_floor_room(hass: HomeAssistant) -> None:
    """Clearing the TRVs and selecting heaters makes the room a floor room."""
    entry = await _add_entry(hass, [_room(hass, "Bathroom", [TRV_1])])

    result = await _reconfigure_room(hass, entry, _floor_input(**{CONF_TRVS: []}))

    assert result["reason"] == "reconfigure_successful"
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == _room_data(hass, [], [HEATER_1])


async def test_reconfigure_keeps_its_own_heaters(hass: HomeAssistant) -> None:
    """A floor room's own heaters are not in use by another room."""
    entry = await _add_entry(hass, [_room(hass, "Bathroom", [], [HEATER_1])])

    result = await _reconfigure_room(hass, entry, {CONF_NAME: "Bathroom"})

    assert result["reason"] == "reconfigure_successful"


def test_strings_match_english_translation() -> None:
    """strings.json and the shipped English translation are the same."""
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text())
    english = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())

    assert strings == english


@pytest.mark.parametrize("step_id", ["user", "reconfigure"])
async def test_room_form_is_translated(hass: HomeAssistant, step_id: str) -> None:
    """Every room field has a label and a description, every error a text."""
    entry = await _add_entry(hass, [_room(hass, "Bedroom", [TRV_1])])
    (subentry_id,) = entry.subentries
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ROOM),
        context={"source": step_id, "subentry_id": subentry_id},
    )
    translations = await async_get_translations(
        hass, "en", "config_subentries", {DOMAIN}
    )
    prefix = f"component.{DOMAIN}.config_subentries.{SUBENTRY_TYPE_ROOM}"

    schema = result["data_schema"]
    placeholders = result["description_placeholders"]
    assert schema is not None
    assert placeholders is not None
    for field in schema.schema:
        assert f"{prefix}.step.{step_id}.data.{field}" in translations
        description = translations[f"{prefix}.step.{step_id}.data_description.{field}"]
        assert "{" not in description.format(**placeholders)
    for error in ERROR_KEYS:
        assert f"{prefix}.error.{error}" in translations
    assert f"{prefix}.abort.reconfigure_successful" in translations
