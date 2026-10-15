"""Per-room control: waiting, direct valve control, or fallback to the TRVs."""

import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from .controller import PIController, should_write_valve
from .trv import Trv, TrvMode

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# Tenths of a °C the TRV offset may lag the wanted one; finer updates would
# cost device writes for a correction the TRV's own regulation barely notices.
# Compared in whole tenths so a nominal 0.3 °C is not read as 0.2999...
OFFSET_MIN_CHANGE_TENTHS: Final = 3
TENTHS_PER_DEGREE: Final = 10


def _tenths(degrees: float) -> int:
    return round(degrees * TENTHS_PER_DEGREE)


class ControlMode(StrEnum):
    """Which way the room is being controlled."""

    WAITING = "waiting"
    DIRECT = "direct"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class RoomSettings:
    """A room's control parameters."""

    control_step: timedelta
    min_valve_change: float
    stale_timeout: timedelta


@dataclass(frozen=True)
class Reading:
    """A wall sensor value in °C and when the sensor last reported it."""

    value: float
    reported: datetime


class Room:
    """The control state machine of one room and its TRVs."""

    def __init__(  # noqa: PLR0913
        self,
        settings: RoomSettings,
        trvs: Sequence[Trv],
        controller: PIController,
        started_at: datetime,
        *,
        name: str,
        device_writes: int = 0,
    ) -> None:
        """Create a room; name is used in log messages."""
        self._settings = settings
        self._trvs = trvs
        self._controller = controller
        self._started_at = started_at
        self._name = name
        self._device_writes = device_writes
        self._mode = ControlMode.WAITING
        self._target: float | None = None
        self._last_output: int | None = None
        self._last_error: float | None = None
        self._last_direct_step: datetime | None = None
        # Indices of the TRVs this room switched to direct mode and has not
        # switched out since; finding one of them elsewhere is an external change.
        self._directed: set[int] = set()

    @property
    def control_mode(self) -> ControlMode:
        """Return the room's control mode."""
        return self._mode

    @property
    def target(self) -> float | None:
        """Return the target temperature exactly as it was set."""
        return self._target

    @property
    def last_output(self) -> int | None:
        """Return the valve output of the last direct step, None outside it."""
        return self._last_output

    @property
    def last_error(self) -> float | None:
        """Return target minus reading of the last direct step, None outside it."""
        return self._last_error

    @property
    def integrator(self) -> float:
        """Return the PI integral term in %."""
        return self._controller.integral

    @property
    def device_writes(self) -> int:
        """Return the number of writes sent to the TRVs."""
        return self._device_writes

    def current_reading(
        self, now: datetime, readings: Sequence[Reading]
    ) -> float | None:
        """Return the mean of the finite readings within the stale timeout."""
        fresh = [
            r.value
            for r in readings
            if math.isfinite(r.value)
            and now - r.reported <= self._settings.stale_timeout
        ]
        return sum(fresh) / len(fresh) if fresh else None

    def needs_immediate_step(self, now: datetime, readings: Sequence[Reading]) -> bool:
        """Return whether a fresh reading should end waiting or fallback now."""
        return (
            self._mode is not ControlMode.DIRECT
            and self.current_reading(now, readings) is not None
        )

    async def set_target(self, target: float) -> None:
        """Set the target; in fallback, write it to the TRVs right away."""
        self._target = target
        if self._mode is ControlMode.FALLBACK:
            await self._self_regulate(target)

    async def hand_back(self) -> None:
        """Put every available TRV into self-regulation at the target."""
        if self._target is not None:
            await self._self_regulate(self._target)

    async def step(self, now: datetime, readings: Sequence[Reading]) -> None:
        """Run one control step."""
        if self._target is None:
            return
        reading = self.current_reading(now, readings)
        if reading is not None:
            await self._step_direct(now, self._target, reading)
            return
        in_startup_grace = now - self._started_at < self._settings.stale_timeout
        if self._mode is ControlMode.WAITING and in_startup_grace:
            return
        self._mode = ControlMode.FALLBACK
        self._last_output = None
        self._last_error = None
        await self._self_regulate(self._target)

    async def _step_direct(self, now: datetime, target: float, reading: float) -> None:
        self._mode = ControlMode.DIRECT
        elapsed = self._settings.control_step
        if self._last_direct_step is not None:
            elapsed = min(now - self._last_direct_step, elapsed)
        self._last_direct_step = now
        available = [(i, trv) for i, trv in enumerate(self._trvs) if trv.available]
        output = round(
            self._controller.step(target, reading, elapsed, integrate=bool(available))
        )
        self._last_output = output
        self._last_error = target - reading
        for index, trv in available:
            # Entering direct mode moves the valve to its last direct-mode value,
            # which the readback does not show yet.
            entered = await self._ensure_direct(index, trv)
            if (
                entered
                or trv.valve is None
                or should_write_valve(
                    output, trv.valve, self._settings.min_valve_change
                )
            ):
                self._device_writes += await trv.set_valve(output)
            await self._update_offset(trv, reading)

    async def _ensure_direct(self, index: int, trv: Trv) -> bool:
        """Put the TRV in direct mode; return whether it was switched just now."""
        if trv.mode is TrvMode.DIRECT:
            return False
        if index in self._directed:
            _LOGGER.warning(
                "%s: TRV %d left direct mode (now %s) without the room switching it",
                self._name,
                index + 1,
                trv.mode,
            )
        if await trv.enter_direct():
            self._device_writes += 1
            self._directed.add(index)
            return True
        self._directed.discard(index)
        return False

    async def _update_offset(self, trv: Trv, reading: float) -> None:
        if trv.own_temperature is None:
            return
        # Compared clamped: the readback can never exceed the device limit.
        limit = trv.max_offset
        wanted = max(-limit, min(limit, reading - trv.own_temperature))
        if (
            trv.offset is None
            or abs(_tenths(wanted) - _tenths(trv.offset)) >= OFFSET_MIN_CHANGE_TENTHS
        ):
            self._device_writes += await trv.set_offset(wanted)

    async def _self_regulate(self, setpoint: float) -> None:
        for index, trv in enumerate(self._trvs):
            if trv.available:
                self._device_writes += await trv.enter_self_regulation(setpoint)
                self._directed.discard(index)
