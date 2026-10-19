"""Floor-heating thermostats switched on and off by their setpoint."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

import voluptuous as vol
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.climate.const import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Without hold the thermostat's own weekly program overwrites the setpoint.
HOLD_PRESET: Final = "hold"
HEAT_MODE: Final = HVACMode.HEAT

# Seconds to wait for a blocking service call before counting it not sent.
SERVICE_TIMEOUT: Final = 30.0

PERCENT: Final = 100


class FloorHeaterEntityError(Exception):
    """A floor heater's climate entity is missing or disabled."""

    def __init__(self, entity_id: str, *, disabled: bool = False) -> None:
        """Record which entity is unusable and why."""
        self.entity_id = entity_id
        self.disabled = disabled
        problem = "is disabled" if disabled else "was not found"
        super().__init__(f"Floor heater climate entity {entity_id} {problem}")


# Separate from Trv: a floor heater has no valve, offset or presses, and is
# switched in timed edges rather than written once per step.
class FloorHeater(Protocol):
    """
    A floor-heating thermostat the room switches on and off by duty cycle.

    Writes never raise: they return the number sent, so the room can count
    device writes, and log what failed.
    """

    @property
    def available(self) -> bool:
        """Return whether the device can be read and written."""
        ...

    async def apply_duty(self, output: float, period: timedelta, now: datetime) -> int:
        """
        Start a period of output % on, then off; return the writes sent now.

        Cancels the previous period's pending off edge first. The off edge is
        written later, from a timer, and reported through the heater's own
        callback rather than returned.
        """
        ...

    async def hold_setpoint(self, setpoint: float) -> int:
        """
        Cancel any pending edge, then write setpoint; return the writes sent.

        The thermostat then regulates the floor on its own.
        """
        ...


class ClimateFloorHeater:
    """
    A floor thermostat behind a climate entity, in heat mode and hold.

    "On" is the floor maximum, so the thermostat itself cuts the heat there
    whether or not the off edge ever comes. Edge setpoints are written whatever
    the entity reports, so a thermostat that missed an edge converges at the
    next one.
    """

    def __init__(  # noqa: PLR0913
        self,
        hass: HomeAssistant,
        climate_entity_id: str,
        *,
        on_setpoint: float,
        off_setpoint: float,
        min_on_time: timedelta,
        on_edge_written: Callable[[int], None],
    ) -> None:
        """
        Find the climate entity; raise FloorHeaterEntityError if it is unusable.

        on_edge_written is called with the writes each timed off edge sent.
        """
        entry = er.async_get(hass).async_get(climate_entity_id)
        if entry is None:
            raise FloorHeaterEntityError(climate_entity_id)
        if entry.disabled:
            raise FloorHeaterEntityError(climate_entity_id, disabled=True)
        self._hass = hass
        # Kept by registry id and looked up on every read, so that renaming the
        # entity while loaded does not lose it.
        self._registry_id = entry.id
        self._entity_id_at_setup = entry.entity_id
        self._on_setpoint = on_setpoint
        self._off_setpoint = off_setpoint
        self._min_on_time = min_on_time
        self._on_edge_written = on_edge_written
        self._unsubscribe_edge: CALLBACK_TYPE | None = None
        self._edge_task: asyncio.Task[None] | None = None
        # One write at a time, so an off edge never lands after the write that
        # was meant to replace it.
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Return the climate entity's current id."""
        return self.climate_entity_id

    @property
    def climate_entity_id(self) -> str:
        """Return the climate entity's current id."""
        # A removed entity keeps its last id, which then has no state.
        entry = er.async_get(self._hass).async_get(self._registry_id)
        return self._entity_id_at_setup if entry is None else entry.entity_id

    @property
    def available(self) -> bool:
        """Return whether the climate entity reports."""
        state = self._hass.states.get(self.climate_entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    async def apply_duty(self, output: float, period: timedelta, now: datetime) -> int:
        """
        Start a period of output % on, then off; return the writes sent now.

        An on-time or off-time shorter than the minimum on-time is dropped, so
        the period is all off or all on.
        """
        await self.cancel_edge()
        on_time = period * output / PERCENT
        if on_time < self._min_on_time:
            on_time = timedelta(0)
        elif period - on_time < self._min_on_time:
            on_time = period
        setpoint = self._on_setpoint if on_time else self._off_setpoint
        sent = await self._write(setpoint)
        if on_time and on_time < period:
            self._unsubscribe_edge = async_track_point_in_utc_time(
                self._hass, self._off_edge_due, now + on_time
            )
        return sent

    async def hold_setpoint(self, setpoint: float) -> int:
        """Cancel any pending edge, then write setpoint; return the writes sent."""
        await self.cancel_edge()
        return await self._write(setpoint)

    async def cancel_edge(self) -> None:
        """Drop the pending off edge, and wait for one already being written."""
        if self._unsubscribe_edge is not None:
            self._unsubscribe_edge()
            self._unsubscribe_edge = None
        task, self._edge_task = self._edge_task, None
        if task is not None:
            await task

    @callback
    def _off_edge_due(self, _now: datetime) -> None:
        self._unsubscribe_edge = None
        self._edge_task = self._hass.async_create_task(
            self._write_off_edge(), f"{self.name} off edge"
        )

    async def _write_off_edge(self) -> None:
        sent = await self._write(self._off_setpoint)
        self._on_edge_written(sent)

    async def _write(self, setpoint: float) -> int:
        """Write heat mode and hold where they differ, then the setpoint."""
        async with self._lock:
            state = self._hass.states.get(self.climate_entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                return 0
            sent = 0
            if state.state != HEAT_MODE:
                if not await self._call(
                    SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HEAT_MODE}
                ):
                    return sent
                sent += 1
            presets = state.attributes.get(ATTR_PRESET_MODES) or []
            if (
                HOLD_PRESET in presets
                and state.attributes.get(ATTR_PRESET_MODE) != HOLD_PRESET
            ):
                if not await self._call(
                    SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: HOLD_PRESET}
                ):
                    return sent
                sent += 1
            if await self._call(SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: setpoint}):
                sent += 1
            return sent

    async def _call(self, service: str, data: dict[str, object]) -> bool:
        entity_id = self.climate_entity_id
        try:
            async with asyncio.timeout(SERVICE_TIMEOUT):
                await self._hass.services.async_call(
                    CLIMATE_DOMAIN,
                    service,
                    {ATTR_ENTITY_ID: entity_id, **data},
                    blocking=True,
                )
        except TimeoutError:
            _LOGGER.warning(
                "%s: climate.%s timed out after %s s",
                entity_id,
                service,
                SERVICE_TIMEOUT,
            )
            return False
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("%s: climate.%s failed: %s", entity_id, service, err)
            return False
        return True
