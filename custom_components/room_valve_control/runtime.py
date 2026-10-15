"""A room's running control: its TRVs, state machine and step scheduling."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROL_STEP,
    CONF_KP,
    CONF_MAX_TARGET,
    CONF_MIN_CHANGE,
    CONF_MIN_TARGET,
    CONF_SENSORS,
    CONF_STALE_TIMEOUT,
    CONF_TI,
    DOMAIN,
)
from .controller import PIController, PIGains
from .registry import resolve_entity_id
from .room import Reading, Room, RoomSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import datetime

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import Event, EventStateChangedData, EventStateReportedData

    from .spirit import SpiritTrv

_LOGGER = logging.getLogger(__name__)

# Seconds for handing every room back at Core stop. Core gives all shutdown jobs
# together 20 s (STOPPING_STAGE_SHUTDOWN_TIMEOUT) before it fires the stop
# event, on which zwave_js disconnects; this leaves room for the other jobs and
# for logging what was cut off. A TRV stalled in its 30 s service timeout is
# cut off.
HAND_BACK_TIMEOUT: Final = 15.0

type StepCondition = Callable[[datetime, Sequence[Reading]], bool]
type RoomValveControlConfigEntry = ConfigEntry[dict[str, RoomRuntime]]


def entity_ids(
    hass: HomeAssistant, registry_ids: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Return the entity ids of stored registry ids, and the ids that are gone."""
    resolved = []
    missing = []
    for registry_id in registry_ids:
        entity_id = resolve_entity_id(hass, registry_id)
        if entity_id is None:
            missing.append(registry_id)
        else:
            resolved.append(entity_id)
    return resolved, missing


def room_gains(data: Mapping[str, Any]) -> PIGains:
    """Return the PI gains stored in a room subentry."""
    return PIGains(kp=data[CONF_KP], ti=timedelta(seconds=data[CONF_TI]))


def room_settings(data: Mapping[str, Any]) -> RoomSettings:
    """Return the control parameters stored in a room subentry."""
    return RoomSettings(
        control_step=timedelta(seconds=data[CONF_CONTROL_STEP]),
        min_valve_change=data[CONF_MIN_CHANGE],
        stale_timeout=timedelta(seconds=data[CONF_STALE_TIMEOUT]),
    )


class RoomRuntime:
    """One room's TRVs and control state, stepped on a timer and on readings."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RoomValveControlConfigEntry,
        subentry: ConfigSubentry,
        trvs: Sequence[SpiritTrv],
    ) -> None:
        """Hold the room's configuration; the room starts with async_start."""
        self._hass = hass
        self._entry = entry
        self.subentry_id = subentry.subentry_id
        self.name = subentry.title
        self.min_target: float = subentry.data[CONF_MIN_TARGET]
        self.max_target: float = subentry.data[CONF_MAX_TARGET]
        self._sensor_ids = self._wall_sensors(hass, subentry.data[CONF_SENSORS])
        self._gains = room_gains(subentry.data)
        self._settings = room_settings(subentry.data)
        self._trvs = trvs
        self.room: Room | None = None
        self.controller: PIController | None = None
        # Keeps an immediate step's condition and the step together; Room
        # itself serializes its writes.
        self._step_lock = asyncio.Lock()
        self._stepped_with_target = False
        # Every step in flight or waiting for the step lock, so that handing
        # back can cancel them instead of waiting behind a stalled TRV write.
        self._step_tasks: set[asyncio.Task[None]] = set()
        self._handing_back = False
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._listeners: list[CALLBACK_TYPE] = []

    def _wall_sensors(
        self, hass: HomeAssistant, registry_ids: Sequence[str]
    ) -> list[str]:
        sensors, missing = entity_ids(hass, registry_ids)
        for registry_id in missing:
            _LOGGER.warning(
                "%s: wall sensor %s no longer exists; reconfigure the room",
                self.name,
                registry_id,
            )
        if not sensors:
            _LOGGER.warning(
                "%s: no wall sensor left; the TRVs will regulate on their own",
                self.name,
            )
        return sensors

    @property
    def device_info(self) -> DeviceInfo:
        """Return the room's device, shared by its entities."""
        return DeviceInfo(identifiers={(DOMAIN, self.subentry_id)}, name=self.name)

    @callback
    def add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call listener after every step and reading; return its remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def readings(self) -> list[Reading]:
        """Return the wall sensors' numeric states and when they reported."""
        readings = []
        for sensor_id in self._sensor_ids:
            state = self._hass.states.get(sensor_id)
            if state is None:
                continue
            try:
                value = float(state.state)
            except ValueError:
                continue
            readings.append(Reading(value, state.last_reported))
        return readings

    @property
    def current_temperature(self) -> float | None:
        """Return the room reading the next step would use."""
        if self.room is None:
            return None
        return self.room.current_reading(dt_util.utcnow(), self.readings())

    async def async_start(
        self,
        *,
        target: float | None,
        controller_state: Mapping[str, object] | None,
        device_writes: int,
    ) -> None:
        """Create the room from restored values, start stepping and step once."""
        controller = self._restore_controller(controller_state)
        room = Room(
            self._settings,
            self._trvs,
            controller,
            dt_util.utcnow(),
            name=self.name,
            device_writes=device_writes,
        )
        if target is not None:
            # A waiting room only stores the target.
            await room.set_target(target)
        self.room = room
        self.controller = controller
        self._unsubscribes = [
            async_track_time_interval(
                self._hass,
                self._periodic_step,
                self._settings.control_step,
                name=f"{DOMAIN} {self.name} step",
                cancel_on_shutdown=True,
            ),
            async_track_state_change_event(
                self._hass, self._sensor_ids, self._async_reading_event
            ),
            async_track_state_report_event(
                self._hass, self._sensor_ids, self._async_reading_event
            ),
        ]
        self._schedule_step(None)

    def _restore_controller(self, state: Mapping[str, object] | None) -> PIController:
        gains = self._gains
        if state is None:
            return PIController(gains)
        try:
            return PIController.from_dict(gains, state)
        except TypeError as err:
            _LOGGER.warning("%s: integrator reset to 0: %s", self.name, err)
            return PIController(gains)

    async def async_set_target(self, target: float) -> None:
        """Set the room's target; step at once if the room has not run with one."""
        if self.room is None:
            return
        await self.room.set_target(target)
        self._notify()
        self._schedule_step(lambda _now, _readings: not self._stepped_with_target)

    async def async_hand_back(self) -> None:
        """
        Stop stepping and put the room's TRVs into self-regulation, once.

        Cancels the steps in flight first, which releases the room's lock.
        """
        self._handing_back = True
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []
        if self._step_tasks:
            steps = set(self._step_tasks)
            for step in steps:
                step.cancel()
            # wait() rather than gathering, so the steps' cancellation is not
            # mistaken for this task's own, which a timeout relies on.
            await asyncio.wait(steps)
        if self.room is None:
            return
        try:
            await self.room.hand_back()
        finally:
            # The room is stopped even when the shutdown bound cuts this off.
            self._notify()

    @callback
    def _periodic_step(self, _now: datetime) -> None:
        self._schedule_step(None)

    @callback
    def _async_reading_event(
        self, _event: Event[EventStateChangedData] | Event[EventStateReportedData]
    ) -> None:
        self._notify()
        if self.room is not None and self.room.needs_immediate_step(
            dt_util.utcnow(), self.readings()
        ):
            self._schedule_step(self.room.needs_immediate_step)

    @callback
    def _schedule_step(self, condition: StepCondition | None) -> None:
        """Run a step as a task of the entry, which unloading cancels."""
        if self._handing_back:
            return
        task = self._entry.async_create_background_task(
            self._hass, self._async_step(condition), f"{DOMAIN} {self.name} step"
        )
        if not task.done():
            self._step_tasks.add(task)
            task.add_done_callback(self._step_tasks.discard)

    async def _async_step(self, condition: StepCondition | None) -> None:
        """Run a step, if condition still holds once the lock is held."""
        async with self._step_lock:
            room = self.room
            if room is None:
                return
            now = dt_util.utcnow()
            readings = self.readings()
            if condition is not None and not condition(now, readings):
                return
            await room.step(now, readings)
            if room.target is not None:
                self._stepped_with_target = True
        self._notify()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


async def async_hand_back_all(rooms: Iterable[RoomRuntime]) -> None:
    """Hand every room back at once."""
    await asyncio.gather(*(room.async_hand_back() for room in rooms))


async def async_hand_back_all_at_stop(rooms: Iterable[RoomRuntime]) -> None:
    """Hand every room back at once, bounded by HAND_BACK_TIMEOUT."""
    try:
        async with asyncio.timeout(HAND_BACK_TIMEOUT):
            await async_hand_back_all(rooms)
    except TimeoutError:
        _LOGGER.warning(
            "Handing the TRVs back took over %s s; the rest were cut off",
            HAND_BACK_TIMEOUT,
        )
