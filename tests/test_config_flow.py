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
    CONF_KP,
    CONF_MAX_TARGET,
    CONF_MIN_CHANGE,
    CONF_MIN_TARGET,
    CONF_SENSORS,
    CONF_STALE_TIMEOUT,
    CONF_TI,
    CONF_TRVS,
    DOMAIN,
    SUBENTRY_TYPE_ROOM,
)

from .helpers import add_offset_entity, add_spirit_device, add_valve_entity

if TYPE_CHECKING:
    from homeassistant.config_entries import SubentryFlowResult
    from homeassistant.core import HomeAssistant

COMPONENT_DIR: Final = Path(__file__).parents[1] / "custom_components" / DOMAIN
SETUP_ENTRY: Final = "custom_components.room_valve_control.async_setup_entry"

SENSOR: Final = "sensor.bedroom_temperature"
TRV_1: Final = "climate.bedroom_trv_1"
TRV_2: Final = "climate.bedroom_trv_2"
DISABLED_OFFSET_TRV: Final = "climate.office_trv"
DISABLED_OFFSET_ID: Final = "number.office_trv_temperature_offset"
NOT_A_SPIRIT: Final = "climate.heat_pump"
DEVICELESS_TRV: Final = "climate.deviceless_trv"
ZERO_DURATION: Final = {"hours": 0, "minutes": 0, "seconds": 0}

DEFAULT_ROOM_DATA: Final = {
    CONF_SENSORS: [SENSOR],
    CONF_TRVS: [TRV_1, TRV_2],
    CONF_KP: 30.0,
    CONF_TI: 1800.0,
    CONF_CONTROL_STEP: 900.0,
    CONF_MIN_CHANGE: 5.0,
    CONF_STALE_TIMEOUT: 2700.0,
    CONF_MIN_TARGET: 5.0,
    CONF_MAX_TARGET: 30.0,
}

ERROR_KEYS: Final = (
    "no_sensor",
    "no_trv",
    "trv_in_use",
    "trv_entity_missing",
    "trv_entity_disabled",
    "trv_no_device",
    "not_positive",
    "not_finite",
    "min_change_out_of_range",
    "target_range_empty",
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
def spirits(hass: HomeAssistant) -> None:
    """Register two usable Spirits and one with parameter 8 disabled."""
    _add_full_spirit(hass, 11, TRV_1)
    _add_full_spirit(hass, 12, TRV_2)
    _add_full_spirit(
        hass,
        13,
        DISABLED_OFFSET_TRV,
        offset_disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )


def _room_input(**overrides: Any) -> dict[str, Any]:
    return {
        CONF_NAME: "Bedroom",
        CONF_SENSORS: [SENSOR],
        CONF_TRVS: [TRV_1, TRV_2],
    } | overrides


async def _add_entry(
    hass: HomeAssistant, rooms: list[ConfigSubentryData] | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, subentries_data=rooms or [])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _room(title: str, trvs: list[str]) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=DEFAULT_ROOM_DATA | {CONF_TRVS: trvs},
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
    assert dict(subentry.data) == DEFAULT_ROOM_DATA


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
    assert dict(subentry.data) == DEFAULT_ROOM_DATA | {
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
    entry = await _add_entry(hass, [_room("Bedroom", [TRV_1])])
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
    entry = await _add_entry(hass, [_room("Living room", [TRV_1])])

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
        ({CONF_TRVS: []}, {CONF_TRVS: "no_trv"}),
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
    assert subentry.data[CONF_SENSORS] == [SENSOR]
    assert subentry.data[CONF_TRVS] == [TRV_2, TRV_1]


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
    entry = await _add_entry(hass, [_room("Bedroom", [TRV_1, TRV_2])])

    with patch(SETUP_ENTRY, return_value=True) as setup_entry:
        result = await _reconfigure_room(
            hass, entry, _room_input(**{CONF_MIN_CHANGE: 10})
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    (subentry,) = entry.subentries.values()
    assert dict(subentry.data) == DEFAULT_ROOM_DATA | {CONF_MIN_CHANGE: 10.0}
    setup_entry.assert_called_once()


async def test_reconfigure_keeps_unsubmitted_values(hass: HomeAssistant) -> None:
    """The reconfigure form defaults to the room's stored values."""
    stored = _room("Bedroom", [TRV_1])
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
    entry = await _add_entry(hass, [_room("Bedroom", [TRV_1])])
    hass.config_entries.async_add_subentry(entry, _config_subentry("Office", TRV_2))
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


def _config_subentry(title: str, trv: str) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(DEFAULT_ROOM_DATA | {CONF_TRVS: [trv]}),
        subentry_type=SUBENTRY_TYPE_ROOM,
        title=title,
        unique_id=None,
    )


def test_strings_match_english_translation() -> None:
    """strings.json and the shipped English translation are the same."""
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text())
    english = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())

    assert strings == english


@pytest.mark.parametrize("step_id", ["user", "reconfigure"])
async def test_room_form_is_translated(hass: HomeAssistant, step_id: str) -> None:
    """Every room field and error has an English label."""
    entry = await _add_entry(hass, [_room("Bedroom", [TRV_1])])
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
    assert schema is not None
    for field in schema.schema:
        assert f"{prefix}.step.{step_id}.data.{field}" in translations
    for error in ERROR_KEYS:
        assert f"{prefix}.error.{error}" in translations
    assert f"{prefix}.abort.reconfigure_successful" in translations
