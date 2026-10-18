"""Eurotronic Spirit Z-Wave Plus TRV over Home Assistant's zwave_js entities."""

import asyncio
import logging
import math
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import voluptuous as vol
from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_STEP,
    PRESET_NONE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.number.const import ATTR_VALUE, SERVICE_SET_VALUE
from homeassistant.components.number.const import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.sensor.const import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .trv import Press, TrvMode

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData

_LOGGER = logging.getLogger(__name__)

# Not imported from the zwave_js package: importing it needs the Z-Wave server
# library, which this component does not depend on.
ZWAVE_JS_DOMAIN: Final = "zwave_js"

# zwave_js unique ids are "<home id>.<value id>", the value id being
# "<node>-<command class>-<endpoint>-<property>[-<property key>]".
COMMAND_CLASS_SWITCH_MULTILEVEL: Final = 38
COMMAND_CLASS_CONFIGURATION: Final = 112
VALVE_PROPERTY: Final = "currentValue"
OFFSET_PARAMETER: Final = 8
_VALUE_ID_PARTS: Final = 4

# The node status sensor's unique id is "<home id>.<node>.node_status"; it is
# enabled by default and reads alive, asleep, awake, dead or unknown. zwave_js
# keeps a dead node's value entities available, and marks the node alive only
# once it hears from it, so writes to a dead node are throttled, not stopped.
NODE_STATUS_UNIQUE_ID_SUFFIX: Final = ".node_status"
NODE_STATUS_DEAD: Final = "dead"
DEAD_NODE_RETRY_INTERVAL: Final = timedelta(minutes=30)

# Seconds to wait for a blocking service call to an unresponsive node.
SERVICE_TIMEOUT: Final = 30.0

# Thermostat Mode 31, valve-position mode; zwave_js lists it as a preset and
# reports no hvac_mode while it is active.
DIRECT_MODE_PRESET: Final = "Manufacturer specific"
SELF_REGULATION_HVAC_MODE: Final = HVACMode.HEAT

# zwave_js picks the setpoint to write from the mode it last read, so a setpoint
# written before heat mode reads back lands nowhere. Seconds.
MODE_CHANGE_TIMEOUT: Final = 10.0

# zwave_js reports no target_temp_step for the Spirit; its setpoint is in 0.5 °C.
DEFAULT_SETPOINT_STEP: Final = 0.5
# Decimals a snapped setpoint is rounded to, dropping the float error of the
# step multiplication; finer than any step a TRV uses.
SETPOINT_DECIMALS: Final = 2
SETPOINT_TOLERANCE: Final = 10**-SETPOINT_DECIMALS / 2

# Parameter 8 is in tenths of a °C, -50…50.
OFFSET_TENTHS_PER_DEGREE: Final = 10
OFFSET_LIMIT_TENTHS: Final = 50


class SpiritEntity(StrEnum):
    """The entities a Spirit TRV is driven through."""

    CLIMATE = "climate"
    VALVE_CONTROL = "valve_control"
    TEMPERATURE_OFFSET = "temperature_offset"


class _Outcome(StrEnum):
    """What became of a write."""

    SENT = "sent"
    NOT_SENT = "not_sent"
    # Timed out or cancelled by zwave_js: the node may still get the command.
    UNKNOWN = "unknown"


class SpiritEntityError(Exception):
    """A Spirit TRV entity the adapter needs is missing, disabled or deviceless."""

    def __init__(
        self,
        entity: SpiritEntity,
        entity_id: str | None,
        *,
        disabled: bool = False,
        no_device: bool = False,
    ) -> None:
        """Record which entity is unusable and why."""
        self.entity = entity
        self.entity_id = entity_id
        self.disabled = disabled
        self.no_device = no_device
        if disabled:
            problem = "is disabled"
        elif no_device:
            problem = "has no device"
        else:
            problem = "was not found"
        name = f"{entity} entity {entity_id}" if entity_id else f"{entity} entity"
        super().__init__(f"Spirit {name} {problem}")


def remove_offset(reported: float, offset: float) -> float:
    """
    Return the TRV's own reading from the reported temperature.

    Assumes the reported temperature already includes the parameter 8 offset.
    """
    return reported - offset


def offset_to_tenths(offset: float) -> int:
    """Return parameter 8's value for an offset in °C, clamped to its range."""
    tenths = round(offset * OFFSET_TENTHS_PER_DEGREE)
    return max(-OFFSET_LIMIT_TENTHS, min(OFFSET_LIMIT_TENTHS, tenths))


def _state_mode(state: State) -> TrvMode:
    """Return direct in mode 31, self-regulating in plain heat, else unknown."""
    if state.state == STATE_UNAVAILABLE:
        return TrvMode.UNKNOWN
    preset = state.attributes.get(ATTR_PRESET_MODE)
    if preset == DIRECT_MODE_PRESET:
        return TrvMode.DIRECT
    # Energy heat and Full power are presets that also read as heat.
    if state.state == SELF_REGULATION_HVAC_MODE and preset in (None, PRESET_NONE):
        return TrvMode.SELF_REGULATING
    return TrvMode.UNKNOWN


def _state_setpoint(state: State) -> float | None:
    value = state.attributes.get(ATTR_TEMPERATURE)
    return float(value) if isinstance(value, int | float) else None


def _same_setpoint(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=SETPOINT_TOLERANCE)


def _is_value(entry: er.RegistryEntry, command_class: int, property_: str) -> bool:
    _, _, value_id = entry.unique_id.partition(".")
    parts = value_id.split("-")
    if len(parts) != _VALUE_ID_PARTS:
        return False
    _node, value_command_class, _endpoint, value_property = parts
    return value_command_class == str(command_class) and value_property == property_


def _usable(
    entity: SpiritEntity, entry: er.RegistryEntry | None, entity_id: str | None
) -> er.RegistryEntry:
    if entry is None:
        raise SpiritEntityError(entity, entity_id)
    if entry.disabled:
        raise SpiritEntityError(entity, entry.entity_id, disabled=True)
    return entry


def _find_number(
    numbers: list[er.RegistryEntry],
    entity: SpiritEntity,
    command_class: int,
    property_: str,
) -> er.RegistryEntry:
    entry = next((e for e in numbers if _is_value(e, command_class, property_)), None)
    return _usable(entity, entry, None)


class SpiritTrv:
    """A Spirit TRV, driven through its climate and number entities."""

    def __init__(self, hass: HomeAssistant, climate_entity_id: str) -> None:
        """Find the TRV's entities; raise SpiritEntityError if one is unusable."""
        self._hass = hass
        self._setpoint_pending = False
        # When the last offset write went out, and the temperature then reported.
        self._offset_written_at: datetime | None = None
        self._temperature_at_offset_write: float | None = None
        # When the last write was let through to a node reading dead.
        self._dead_node_write_at: datetime | None = None
        # The setpoints written and not yet read back, oldest first.
        # zwave-js sends a node's commands in order, so a write reading back
        # means every earlier one has landed or failed.
        self._unconfirmed_setpoints: list[float] = []
        self._pressed: Press | None = None
        self._unsubscribes: list[CALLBACK_TYPE] = []
        registry = er.async_get(hass)
        climate = _usable(
            SpiritEntity.CLIMATE,
            registry.async_get(climate_entity_id),
            climate_entity_id,
        )
        if climate.device_id is None:
            raise SpiritEntityError(
                SpiritEntity.CLIMATE, climate_entity_id, no_device=True
            )
        device_entries = er.async_entries_for_device(
            registry, climate.device_id, include_disabled_entities=True
        )
        numbers = [
            entry
            for entry in device_entries
            if entry.domain == NUMBER_DOMAIN and entry.platform == ZWAVE_JS_DOMAIN
        ]
        self._node_status_unique_id = next(
            (
                entry.unique_id
                for entry in device_entries
                if entry.domain == SENSOR_DOMAIN
                and entry.platform == ZWAVE_JS_DOMAIN
                and entry.unique_id.endswith(NODE_STATUS_UNIQUE_ID_SUFFIX)
            ),
            None,
        )
        valve = _find_number(
            numbers,
            SpiritEntity.VALVE_CONTROL,
            COMMAND_CLASS_SWITCH_MULTILEVEL,
            VALVE_PROPERTY,
        )
        offset = _find_number(
            numbers,
            SpiritEntity.TEMPERATURE_OFFSET,
            COMMAND_CLASS_CONFIGURATION,
            str(OFFSET_PARAMETER),
        )
        # Kept by unique id and looked up on every read, so that renaming an
        # entity while loaded does not lose it.
        self._climate = (climate.domain, climate.unique_id, climate.entity_id)
        self._valve = (valve.domain, valve.unique_id, valve.entity_id)
        self._offset = (offset.domain, offset.unique_id, offset.entity_id)

    @property
    def name(self) -> str:
        """Return the climate entity's current id."""
        return self.climate_entity_id

    @property
    def max_offset(self) -> float:
        """Return parameter 8's limit in °C."""
        return OFFSET_LIMIT_TENTHS / OFFSET_TENTHS_PER_DEGREE

    @property
    def climate_entity_id(self) -> str:
        """Return the climate entity's current id."""
        return self._entity_id(*self._climate)

    @property
    def valve_entity_id(self) -> str:
        """Return the valve number entity's current id."""
        return self._entity_id(*self._valve)

    @property
    def offset_entity_id(self) -> str:
        """Return the parameter 8 number entity's current id."""
        return self._entity_id(*self._offset)

    def _entity_id(self, domain: str, unique_id: str, at_setup: str) -> str:
        # A removed entity keeps its last id, which then has no state.
        registry = er.async_get(self._hass)
        return (
            registry.async_get_entity_id(domain, ZWAVE_JS_DOMAIN, unique_id) or at_setup
        )

    @property
    def available(self) -> bool:
        """Return whether the climate entity reports."""
        state = self._hass.states.get(self.climate_entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    def _node_dead(self) -> bool:
        if self._node_status_unique_id is None:
            return False
        status_id = self._entity_id(SENSOR_DOMAIN, self._node_status_unique_id, "")
        status = self._hass.states.get(status_id) if status_id else None
        return status is not None and status.state == NODE_STATUS_DEAD

    def _dead_node_write_allowed(self) -> bool:
        """Let one write per retry interval through to a node reading dead."""
        if not self._node_dead():
            self._dead_node_write_at = None
            return True
        now = dt_util.utcnow()
        if (
            self._dead_node_write_at is not None
            and now - self._dead_node_write_at < DEAD_NODE_RETRY_INTERVAL
        ):
            return False
        self._dead_node_write_at = now
        return True

    @property
    def mode(self) -> TrvMode:
        """Return direct in mode 31, self-regulating in plain heat, else unknown."""
        state = self._hass.states.get(self.climate_entity_id)
        return TrvMode.UNKNOWN if state is None else _state_mode(state)

    @property
    def setpoint(self) -> float | None:
        """Return the climate entity's target temperature."""
        return self._climate_attribute(ATTR_TEMPERATURE)

    @property
    def pressed(self) -> Press | None:
        """Return the last press recorded while listening, until cleared."""
        return self._pressed

    def clear_press(self) -> None:
        """Forget the recorded press."""
        self._pressed = None

    @callback
    def start_listening(self) -> None:
        """
        Record presses from the climate entity's reports, once.

        A press is a setpoint change between two heat reports that is not the
        read-back of a setpoint written since the last one confirmed or the
        last mode 31 report. Entering heat, from mode 31 or anything else,
        carries the stored setpoint, so it is never a press.
        """
        if self._unsubscribes:
            return
        entity_id = self.climate_entity_id
        self._unsubscribes = [
            async_track_state_change_event(
                self._hass, entity_id, self._async_climate_changed
            ),
            async_track_entity_registry_updated_event(
                self._hass, entity_id, self._async_climate_registry_updated
            ),
        ]

    @callback
    def stop_listening(self) -> None:
        """Stop recording presses."""
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []

    @callback
    def _async_climate_registry_updated(
        self, event: Event[EventEntityRegistryUpdatedData]
    ) -> None:
        data = event.data
        if data["action"] == "update" and "entity_id" in data["changes"]:
            self.stop_listening()
            self.start_listening()

    @callback
    def _async_climate_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if new is not None and _state_mode(new) is TrvMode.DIRECT:
            # The node gets its commands in order: every earlier write has
            # landed or failed by the time it reports mode 31.
            self._unconfirmed_setpoints.clear()
        if (
            old is None
            or new is None
            or _state_mode(old) is not TrvMode.SELF_REGULATING
            or _state_mode(new) is not TrvMode.SELF_REGULATING
        ):
            return
        before, setpoint = _state_setpoint(old), _state_setpoint(new)
        if before is None or setpoint is None or setpoint == before:
            return
        unconfirmed = self._unconfirmed_setpoints
        read_back = next(
            (
                index
                for index, written in enumerate(unconfirmed)
                if _same_setpoint(setpoint, written)
            ),
            None,
        )
        if read_back is not None:
            del unconfirmed[: read_back + 1]
            return
        self._pressed = Press(setpoint, new.last_updated)

    @property
    def valve(self) -> float | None:
        """Return the valve number's state in %."""
        return self._number_state(self.valve_entity_id)

    @property
    def offset(self) -> float | None:
        """Return parameter 8 in °C."""
        tenths = self._number_state(self.offset_entity_id)
        return None if tenths is None else tenths / OFFSET_TENTHS_PER_DEGREE

    @property
    def own_temperature(self) -> float | None:
        """
        Return the TRV's reading without its offset.

        None after an offset write until the climate entity reports, later than
        the write, a temperature other than the one it reported at the write:
        the Spirit re-reports its temperature, which includes the offset, only
        minutes later. Every offset write changes the applied tenths, so it
        always moves the report.
        """
        state = self._hass.states.get(self.climate_entity_id)
        if state is None:
            return None
        reported = self._climate_attribute(ATTR_CURRENT_TEMPERATURE)
        if self._offset_written_at is not None:
            if (
                state.last_reported <= self._offset_written_at
                or reported == self._temperature_at_offset_write
            ):
                return None
            # Open for good, so a later return to the old reading is not gated.
            self._offset_written_at = None
        offset = self.offset
        if reported is None or offset is None:
            return None
        return remove_offset(reported, offset)

    async def enter_direct(self) -> bool:
        """Select the Manufacturer specific preset."""
        return await self._call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {
                ATTR_ENTITY_ID: self.climate_entity_id,
                ATTR_PRESET_MODE: DIRECT_MODE_PRESET,
            },
        )

    async def enter_self_regulation(
        self, setpoint: float, *, force_mode: bool = False
    ) -> int:
        """
        Set heat mode, then the setpoint, skipping what already matches.

        The setpoint is snapped to the TRV's step, a half step rounding up, so
        that the TRV reports back exactly what was written.

        After a mode change the setpoint is written only once heat mode reads
        back, and then without comparing: the reported setpoint is the one
        from before. If heat mode does not read back in time, the setpoint is
        left to the next call. With force_mode the mode is written whatever
        the TRV reports.
        """
        setpoint = self._on_step(setpoint)
        sent = 0
        if force_mode or self.mode is not TrvMode.SELF_REGULATING:
            if not await self._call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {
                    ATTR_ENTITY_ID: self.climate_entity_id,
                    ATTR_HVAC_MODE: SELF_REGULATION_HVAC_MODE,
                },
            ):
                return sent
            sent += 1
            self._setpoint_pending = True
            if not await self._wait_for_self_regulation():
                _LOGGER.info(
                    "%s: heat mode not reported within %s s; setpoint deferred",
                    self.climate_entity_id,
                    MODE_CHANGE_TIMEOUT,
                )
                return sent
        if not self._setpoint_pending and self._setpoint_matches(setpoint):
            return sent
        # Recorded before the write: the TRV may report it before it returns.
        self._unconfirmed_setpoints.append(setpoint)
        outcome = await self._send(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: self.climate_entity_id, ATTR_TEMPERATURE: setpoint},
        )
        if outcome is _Outcome.NOT_SENT:
            self._unconfirmed_setpoints.remove(setpoint)
        elif outcome is _Outcome.SENT:
            self._setpoint_pending = False
            sent += 1
            # A setpoint the TRV already reports reads back as no change.
            reported = self.setpoint
            if reported is not None and _same_setpoint(reported, setpoint):
                self._unconfirmed_setpoints.clear()
        return sent

    def _setpoint_step(self) -> float:
        return self._climate_attribute(ATTR_TARGET_TEMP_STEP) or DEFAULT_SETPOINT_STEP

    def _on_step(self, setpoint: float) -> float:
        step = self._setpoint_step()
        return round(math.floor(setpoint / step + 0.5) * step, SETPOINT_DECIMALS)

    def _setpoint_matches(self, setpoint: float) -> bool:
        reported = self.setpoint
        return (
            reported is not None
            and abs(reported - setpoint) < self._setpoint_step() / 2
        )

    async def _wait_for_self_regulation(self) -> bool:
        if self.mode is TrvMode.SELF_REGULATING:
            return True
        switched = asyncio.Event()

        @callback
        def _state_changed(_event: Event[EventStateChangedData]) -> None:
            if self.mode is TrvMode.SELF_REGULATING:
                switched.set()

        unsubscribe = async_track_state_change_event(
            self._hass, self.climate_entity_id, _state_changed
        )
        try:
            async with asyncio.timeout(MODE_CHANGE_TIMEOUT):
                await switched.wait()
        except TimeoutError:
            return False
        finally:
            unsubscribe()
        return True

    async def set_valve(self, percent: int) -> bool:
        """Write the valve number, unless the number entity is unavailable."""
        state = self._hass.states.get(self.valve_entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            return False
        return await self._call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: self.valve_entity_id, ATTR_VALUE: percent},
        )

    async def set_offset(self, offset: float) -> bool:
        """
        Write parameter 8, clamped to ±5.0 °C in 0.1 °C steps.

        Sends nothing when parameter 8 already reads the same tenths, so a
        write that would not move the reported temperature never gates it.
        """
        state = self._hass.states.get(self.offset_entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            return False
        tenths = offset_to_tenths(offset)
        current = self._number_state(self.offset_entity_id)
        if current is not None and round(current) == tenths:
            return False
        # Gated before the write, which may reach the node even when its
        # outcome is unknown. Only a write not sent restores the earlier gate.
        earlier_gate = (self._offset_written_at, self._temperature_at_offset_write)
        self._offset_written_at = dt_util.utcnow()
        self._temperature_at_offset_write = self._climate_attribute(
            ATTR_CURRENT_TEMPERATURE
        )
        # Not zwave_js.set_config_parameter: its handler gathers the command
        # with return_exceptions, so a cancelled command surfaces as TypeError.
        outcome = await self._send(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: self.offset_entity_id, ATTR_VALUE: tenths},
        )
        if outcome is _Outcome.NOT_SENT:
            self._offset_written_at, self._temperature_at_offset_write = earlier_gate
        else:
            # Reports during the write may predate the node applying it.
            self._offset_written_at = dt_util.utcnow()
        return outcome is _Outcome.SENT

    def _climate_attribute(self, attribute: str) -> float | None:
        state = self._hass.states.get(self.climate_entity_id)
        if state is None:
            return None
        value = state.attributes.get(attribute)
        return float(value) if isinstance(value, int | float) else None

    def _number_state(self, entity_id: str) -> float | None:
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    async def _call(self, domain: str, service: str, data: dict[str, object]) -> bool:
        return await self._send(domain, service, data) is _Outcome.SENT

    async def _send(
        self, domain: str, service: str, data: dict[str, object]
    ) -> _Outcome:
        if not self.available:
            return _Outcome.NOT_SENT
        if not self._dead_node_write_allowed():
            _LOGGER.debug(
                "%s: node is dead; write dropped: %s.%s",
                self.climate_entity_id,
                domain,
                service,
            )
            return _Outcome.NOT_SENT
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT):
                await self._hass.services.async_call(
                    domain, service, data, blocking=True
                )
        except TimeoutError:
            _LOGGER.warning(
                "%s: %s.%s timed out after %s s",
                self.climate_entity_id,
                domain,
                service,
                SERVICE_TIMEOUT,
            )
            return _Outcome.UNKNOWN
        except asyncio.CancelledError:
            # On a disconnect zwave-js-server-python cancels its pending command
            # futures, which the blocking call awaits in this task.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            _LOGGER.warning(
                "%s: %s.%s was cancelled", self.climate_entity_id, domain, service
            )
            return _Outcome.UNKNOWN
        # zwave_js entity methods raise ValueError and KeyError for values the
        # node does not offer in its current mode.
        except (HomeAssistantError, vol.Invalid, ValueError, KeyError) as err:
            _LOGGER.warning(
                "%s: %s.%s failed: %s", self.climate_entity_id, domain, service, err
            )
            return _Outcome.NOT_SENT
        self._dead_node_write_at = None
        return _Outcome.SENT
