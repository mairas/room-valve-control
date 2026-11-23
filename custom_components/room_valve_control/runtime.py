"""A room's running control: its devices, state machine and step scheduling."""

import asyncio
import logging
import random
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROL_STEP,
    CONF_FALLBACK_FLOOR_SETPOINT,
    CONF_KP,
    CONF_MAX_TARGET,
    CONF_MIN_CHANGE,
    CONF_MIN_TARGET,
    CONF_SENSORS,
    CONF_STALE_TIMEOUT,
    CONF_TI,
    DEFAULT_FALLBACK_FLOOR_SETPOINT,
    DOMAIN,
    TARGET_STEP_MAX_DELAY,
)
from .controller import PIController, PIGains
from .registry import entity_ids
from .room import Reading, Room, RoomSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import Event, EventStateChangedData, EventStateReportedData
    from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData

    from .floor_heater import ClimateFloorHeater
    from .spirit import SpiritTrv

_LOGGER = logging.getLogger(__name__)

# Spreads the rooms' steps in time, not a secret; tests replace it.
step_random = random.Random()  # noqa: S311

type StepCondition = Callable[[datetime, Sequence[Reading]], bool]
type RoomValveControlConfigEntry = ConfigEntry[dict[str, RoomRuntime]]


class RoomRuntime:
    """
    One room's devices and control state, stepped on a timer and on readings.

    The devices are TRVs or floor heaters, never both. Every step schedules
    the next. A step that starts the room or follows a changed target takes the
    next one a random share of the control step away, so that rooms started or
    changed together do not step, and write their devices, together; any other
    step takes it a full control step away.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RoomValveControlConfigEntry,
        subentry: ConfigSubentry,
        trvs: Mapping[str, SpiritTrv],
        heaters: Mapping[str, ClimateFloorHeater],
    ) -> None:
        """
        Hold the room's configuration; the room starts with async_start.

        trvs and heaters are keyed by the entity registry ids the subentry
        stores.
        """
        self._hass = hass
        self._entry = entry
        self.subentry_id = subentry.subentry_id
        self.name = subentry.title
        self.min_target: float = subentry.data[CONF_MIN_TARGET]
        self.max_target: float = subentry.data[CONF_MAX_TARGET]
        self._sensor_ids = self._wall_sensors(hass, subentry.data[CONF_SENSORS])
        data = subentry.data
        self._gains = PIGains(kp=data[CONF_KP], ti=timedelta(seconds=data[CONF_TI]))
        self._settings = RoomSettings(
            control_step=timedelta(seconds=data[CONF_CONTROL_STEP]),
            min_valve_change=data[CONF_MIN_CHANGE],
            stale_timeout=timedelta(seconds=data[CONF_STALE_TIMEOUT]),
            fallback_floor_setpoint=data.get(
                CONF_FALLBACK_FLOOR_SETPOINT, DEFAULT_FALLBACK_FLOOR_SETPOINT
            ),
        )
        self._trvs = trvs
        self._heaters = heaters
        self.room: Room | None = None
        # While Core starts, wall sensor states are restored or retained ones
        # however old their value; readings count from when it has started.
        self._core_started = False
        self._readings_since: datetime | None = None
        # Keeps an immediate step's condition and the step together; Room
        # itself serializes its writes.
        self._step_lock = asyncio.Lock()
        self._cancel_next_step: CALLBACK_TYPE | None = None
        self._cancel_target_step: CALLBACK_TYPE | None = None
        # Every step in flight or waiting for the step lock, so that stopping
        # can cancel them instead of waiting behind a stalled TRV write.
        self._step_tasks: set[asyncio.Task[None]] = set()
        self._stopped = False
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
                "%s: no wall sensor left; the devices will regulate on their own",
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
        """
        Return the wall sensors' numeric states and when they reported.

        None while Core is starting, and none reported before it had started.
        """
        if not self._core_started:
            return []
        since = self._readings_since
        readings = []
        for sensor_id in self._sensor_ids:
            state = self._hass.states.get(sensor_id)
            if state is None or (since is not None and state.last_reported < since):
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
        """
        Create the room from restored values, start stepping and step once.

        Floor heaters first hold the fallback setpoint: one may still be on
        from before, with its off edge lost.
        """
        controller = self._restore_controller(controller_state)
        started_at = dt_util.utcnow()
        room = Room(
            self._settings,
            list(self._trvs.values()),
            controller,
            started_at,
            name=self.name,
            device_writes=device_writes,
            heaters=list(self._heaters.values()),
        )
        if target is not None:
            # A waiting room only stores the target.
            await room.set_target(target, started_at)
        await room.hold_fallback()
        self.room = room
        for trv in self._trvs.values():
            trv.start_listening()
        self._unsubscribes = [
            async_track_state_change_event(
                self._hass, self._sensor_ids, self._async_reading_event
            ),
            async_track_state_report_event(
                self._hass, self._sensor_ids, self._async_reading_event
            ),
            async_track_entity_registry_updated_event(
                self._hass, self._sensor_ids, self._async_sensor_registry_updated
            ),
            self._hass.bus.async_listen(
                EVENT_HOMEASSISTANT_STOP, self._async_core_stopping
            ),
        ]
        if self._hass.state is CoreState.running:
            self._core_started = True
        else:
            self._unsubscribes.append(
                async_at_started(self._hass, self._async_core_started)
            )
        self._schedule_step(None, new_phase=True)

    @callback
    def _async_core_started(self, _hass: HomeAssistant) -> None:
        self._core_started = True
        self._readings_since = dt_util.utcnow()
        # Core starts every room at once: this step must not align them again.
        self._schedule_step(None, new_phase=True)

    @callback
    def _async_core_stopping(self, _event: Event) -> None:
        self._drop_timers()

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
        """
        Set the room's target; a changed one steps the room after a random delay.

        Further changes before that step only set the target it steps to.
        """
        room = self.room
        if room is None:
            return
        changed = target != room.target
        await room.set_target(target, dt_util.utcnow())
        self._notify()
        if changed and self._cancel_target_step is None and self._may_schedule():
            delay = TARGET_STEP_MAX_DELAY * step_random.random()
            self._cancel_target_step = async_track_point_in_utc_time(
                self._hass, self._target_step_due, dt_util.utcnow() + delay
            )

    @property
    def trvs(self) -> Mapping[str, SpiritTrv]:
        """Return the room's TRVs by entity registry id."""
        return self._trvs

    @property
    def heats_floor(self) -> bool:
        """Return whether floor heaters, not TRVs, heat the room."""
        return bool(self._heaters)

    @callback
    def async_edge_written(self, sent: int) -> None:
        """Count the writes of a floor heater's timed off edge and show them."""
        if self.room is not None:
            self.room.count_edge_writes(sent)
        self._notify()

    async def async_stop(self) -> None:
        """
        Stop the room, leaving the TRVs as they are.

        Floor heaters drop their pending edge and hold the fallback setpoint.

        Returns once a target change in progress has finished. The room stops
        in the queue for its lock, so a target change arriving later only
        records the target.
        """
        steps = self._stop_stepping()
        if self.room is not None:
            await self.room.stop()
        await _async_wait(steps)
        self._notify()

    async def async_hand_back(self) -> None:
        """Stop stepping and hand the room's devices back to their own control."""
        await _async_wait(self._stop_stepping())
        if self.room is not None:
            await self.room.hand_back()
            self._notify()

    def _stop_stepping(self) -> set[asyncio.Task[None]]:
        """Drop the timer and listeners and cancel the steps; return those."""
        self._stopped = True
        self._drop_timers()
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []
        # The adapters outlive a reload; the next room starts them again.
        for trv in self._trvs.values():
            trv.stop_listening()
        # Cancelling releases the room's lock to whatever waits next.
        steps = set(self._step_tasks)
        for step in steps:
            step.cancel()
        return steps

    @callback
    def _periodic_step(self, _now: datetime) -> None:
        self._cancel_next_step = None
        self._schedule_step(None)

    @callback
    def _target_step_due(self, _now: datetime) -> None:
        self._cancel_target_step = None
        self._schedule_step(None, new_phase=True)

    def _may_schedule(self) -> bool:
        # Not once Core is stopping, which has dropped the timers.
        return not (self._stopped or self._hass.is_stopping)

    @callback
    def _drop_timers(self) -> None:
        self._drop_next_step()
        if self._cancel_target_step is not None:
            self._cancel_target_step()
            self._cancel_target_step = None

    @callback
    def _drop_next_step(self) -> None:
        if self._cancel_next_step is not None:
            self._cancel_next_step()
            self._cancel_next_step = None

    @callback
    def _arm_next_step(self, now: datetime, *, new_phase: bool) -> timedelta:
        """Replace the scheduled next step; return the time to it."""
        interval = self._settings.control_step
        if new_phase:
            # 1 - random() is in (0, 1]: never a second step at the same instant.
            interval *= 1 - step_random.random()
        self._drop_next_step()
        if self._may_schedule():
            self._cancel_next_step = async_track_point_in_utc_time(
                self._hass, self._periodic_step, now + interval
            )
        return interval

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
    def _async_sensor_registry_updated(
        self, event: Event[EventEntityRegistryUpdatedData]
    ) -> None:
        """Reload on a wall sensor's new entity id, which the listeners are keyed by."""
        data = event.data
        if data["action"] == "update" and "entity_id" in data["changes"]:
            self._hass.config_entries.async_schedule_reload(self._entry.entry_id)

    @callback
    def _schedule_step(
        self, condition: StepCondition | None, *, new_phase: bool = False
    ) -> None:
        """
        Run a step as a task of the entry, which unloading cancels.

        new_phase puts the next step a random share of the control step away,
        not a full one.
        """
        if self._stopped:
            return
        task = self._entry.async_create_background_task(
            self._hass,
            self._async_step(condition, new_phase=new_phase),
            f"{DOMAIN} {self.name} step",
        )
        if not task.done():
            self._step_tasks.add(task)
            task.add_done_callback(self._step_tasks.discard)

    async def _async_step(
        self, condition: StepCondition | None, *, new_phase: bool
    ) -> None:
        """Run a step, if condition still holds once the lock is held."""
        async with self._step_lock:
            room = self.room
            if room is None:
                return
            now = dt_util.utcnow()
            readings = self.readings()
            if condition is not None and not condition(now, readings):
                return
            self._arm_next_step(now, new_phase=new_phase)
            await room.step(now, readings)
        self._notify()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


async def _async_wait(steps: set[asyncio.Task[None]]) -> None:
    # wait() rather than gathering, which would raise the steps' cancellation
    # in this task.
    if steps:
        await asyncio.wait(steps)
