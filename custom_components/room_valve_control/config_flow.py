"""Config flow for the Room Valve Control integration, one subentry per room."""

from typing import TYPE_CHECKING, Any, Final, override

import voluptuous as vol
from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor.const import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME, PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    DurationSelector,
    EntitySelector,
    EntitySelectorConfig,
    EntityWithDeviceFilterSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_CONTROL_STEP,
    CONF_KP,
    CONF_MAX_TARGET,
    CONF_MIN_CHANGE,
    CONF_MIN_TARGET,
    CONF_SENSORS,
    CONF_STALE_TIMEOUT,
    CONF_TI,
    CONF_TRVS,
    DEFAULT_CONTROL_STEP,
    DEFAULT_KP,
    DEFAULT_MAX_TARGET,
    DEFAULT_MIN_CHANGE,
    DEFAULT_MIN_TARGET,
    DEFAULT_STALE_TIMEOUT,
    DEFAULT_TI,
    DOMAIN,
    SUBENTRY_TYPE_ROOM,
)
from .controller import ENDPOINT_TOLERANCE, VALVE_MAX
from .spirit import SpiritEntityError, SpiritTrv

if TYPE_CHECKING:
    from collections.abc import Mapping

DURATION_FIELDS: Final = (CONF_TI, CONF_CONTROL_STEP, CONF_STALE_TIMEOUT)
SECONDS_PER_MINUTE: Final = 60
SECONDS_PER_HOUR: Final = 3600

DEFAULT_ROOM: Final[dict[str, Any]] = {
    CONF_KP: DEFAULT_KP,
    CONF_TI: DEFAULT_TI.total_seconds(),
    CONF_CONTROL_STEP: DEFAULT_CONTROL_STEP.total_seconds(),
    CONF_MIN_CHANGE: DEFAULT_MIN_CHANGE,
    CONF_STALE_TIMEOUT: DEFAULT_STALE_TIMEOUT.total_seconds(),
    CONF_MIN_TARGET: DEFAULT_MIN_TARGET,
    CONF_MAX_TARGET: DEFAULT_MAX_TARGET,
}

# Shown in the minimum change error; the TRV-specific placeholders are only
# filled in when that error is.
LIMIT_PLACEHOLDERS: Final = {
    "endpoint_tolerance": f"{ENDPOINT_TOLERANCE:g}",
    "valve_max": f"{VALVE_MAX:g}",
}


def _number(unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            mode=NumberSelectorMode.BOX, step="any", unit_of_measurement=unit
        )
    )


def _room_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    def required(key: str) -> vol.Required:
        return vol.Required(key, default=defaults.get(key, vol.UNDEFINED))

    temperature = _number(UnitOfTemperature.CELSIUS)
    return vol.Schema(
        {
            required(CONF_NAME): TextSelector(),
            required(CONF_SENSORS): EntitySelector(
                EntitySelectorConfig(
                    filter=EntityWithDeviceFilterSelectorConfig(
                        domain=SENSOR_DOMAIN,
                        device_class=SensorDeviceClass.TEMPERATURE,
                    ),
                    multiple=True,
                )
            ),
            required(CONF_TRVS): EntitySelector(
                EntitySelectorConfig(
                    filter=EntityWithDeviceFilterSelectorConfig(domain=CLIMATE_DOMAIN),
                    multiple=True,
                )
            ),
            required(CONF_KP): _number(f"{PERCENTAGE}/{UnitOfTemperature.CELSIUS}"),
            required(CONF_TI): DurationSelector(),
            required(CONF_CONTROL_STEP): DurationSelector(),
            required(CONF_MIN_CHANGE): _number(PERCENTAGE),
            required(CONF_STALE_TIMEOUT): DurationSelector(),
            required(CONF_MIN_TARGET): temperature,
            required(CONF_MAX_TARGET): temperature,
        }
    )


def _duration_form_value(seconds: float) -> dict[str, int]:
    hours, rest = divmod(int(seconds), SECONDS_PER_HOUR)
    minutes, seconds_left = divmod(rest, SECONDS_PER_MINUTE)
    return {"hours": hours, "minutes": minutes, "seconds": seconds_left}


def _form_values(name: str | None, room: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(room)
    for key in DURATION_FIELDS:
        values[key] = _duration_form_value(room[key])
    if name is not None:
        values[CONF_NAME] = name
    return values


def _parse(user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Split the form into the room's name and its stored data."""
    room = {key: value for key, value in user_input.items() if key != CONF_NAME}
    for key in DURATION_FIELDS:
        room[key] = cv.time_period_dict(room[key]).total_seconds()
    return user_input[CONF_NAME], room


def _parameter_errors(room: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_KP, *DURATION_FIELDS):
        if room[key] <= 0:
            errors[key] = "not_positive"
    # At or below the endpoint tolerance a valve near 0 or 99 would never be
    # driven to the endpoint while a mid-range move of the same size would be.
    if not ENDPOINT_TOLERANCE < room[CONF_MIN_CHANGE] <= VALVE_MAX:
        errors[CONF_MIN_CHANGE] = "min_change_out_of_range"
    if room[CONF_MIN_TARGET] >= room[CONF_MAX_TARGET]:
        errors[CONF_MAX_TARGET] = "target_range_empty"
    return errors


def _trv_error(
    hass: HomeAssistant,
    entry: ConfigEntry,
    trvs: list[str],
    own_subentry_id: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Return the first selected TRV's error and its placeholders."""
    if not trvs:
        return "no_trv", {}
    other_rooms = {
        trv: subentry.title
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_ROOM
        and subentry.subentry_id != own_subentry_id
        for trv in subentry.data[CONF_TRVS]
    }
    for trv in trvs:
        if trv in other_rooms:
            return "trv_in_use", {"trv": trv, "room": other_rooms[trv]}
        try:
            SpiritTrv(hass, trv)
        except SpiritEntityError as err:
            if err.disabled:
                return "trv_entity_disabled", {
                    "trv": trv,
                    "entity": err.entity,
                    "entity_id": str(err.entity_id),
                }
            if err.no_device:
                return "trv_no_device", {"trv": trv}
            return "trv_entity_missing", {"trv": trv, "entity": err.entity}
    return None


def _validate(
    hass: HomeAssistant,
    entry: ConfigEntry,
    room: Mapping[str, Any],
    own_subentry_id: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the form's errors by field and the placeholders they use."""
    errors = _parameter_errors(room)
    if not room[CONF_SENSORS]:
        errors[CONF_SENSORS] = "no_sensor"
    placeholders: dict[str, str] = {}
    if trv_error := _trv_error(hass, entry, room[CONF_TRVS], own_subentry_id):
        errors[CONF_TRVS], placeholders = trv_error
    return errors, placeholders


class RoomValveControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single Room Valve Control entry."""

    VERSION = 1

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the room subentry flow."""
        return {SUBENTRY_TYPE_ROOM: RoomSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(title="Room Valve Control", data={})


class RoomSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a room."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a room."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            name, room = _parse(user_input)
            errors, placeholders = _validate(self.hass, self._get_entry(), room, None)
            if not errors:
                return self.async_create_entry(title=name, data=room)
        return self._show_room_form(
            "user", _form_values(None, DEFAULT_ROOM), user_input, errors, placeholders
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change a room; the entry's update listener reloads it."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            name, room = _parse(user_input)
            errors, placeholders = _validate(
                self.hass, entry, room, subentry.subentry_id
            )
            if not errors:
                return self.async_update_and_abort(
                    entry, subentry, title=name, data=room
                )
        return self._show_room_form(
            "reconfigure",
            _form_values(subentry.title, subentry.data),
            user_input,
            errors,
            placeholders,
        )

    def _show_room_form(
        self,
        step_id: str,
        defaults: Mapping[str, Any],
        user_input: Mapping[str, Any] | None,
        errors: dict[str, str],
        placeholders: dict[str, str],
    ) -> SubentryFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _room_schema(defaults), user_input
            ),
            errors=errors,
            description_placeholders=LIMIT_PLACEHOLDERS | placeholders,
        )
