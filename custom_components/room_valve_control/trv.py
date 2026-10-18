"""The device-facing interface a room uses to drive its TRVs."""

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class TrvMode(StrEnum):
    """Which regulation a TRV is in."""

    DIRECT = "direct"
    SELF_REGULATING = "self_regulating"
    UNKNOWN = "unknown"


# A Protocol rather than an ABC: implementations share no code, and the room's
# tests can pass plain fakes without inheriting from anything.
class Trv(Protocol):
    """
    A radiator valve the room can put in direct or self-regulating mode.

    Readings are None when the device does not currently report them. Writes
    never raise: they return what was sent, so the room can count device writes,
    and log what failed.
    """

    @property
    def name(self) -> str:
        """Return a stable identifier for logs."""
        ...

    @property
    def max_offset(self) -> float:
        """Return the largest offset magnitude in °C the device applies."""
        ...

    @property
    def available(self) -> bool:
        """Return whether the device can be read and written."""
        ...

    @property
    def mode(self) -> TrvMode:
        """Return the TRV's current mode."""
        ...

    @property
    def setpoint(self) -> float | None:
        """Return the self-regulation setpoint in °C."""
        ...

    @property
    def external_setpoint(self) -> float | None:
        """
        Return a self-regulation setpoint set on the device itself.

        None unless the TRV self-regulates at a setpoint other than the one
        enter_self_regulation last asked for, once that write has read back.
        """
        ...

    @property
    def updated(self) -> datetime | None:
        """Return when the device last reported a change."""
        ...

    @property
    def valve(self) -> float | None:
        """Return the valve position the device reports, in %."""
        ...

    @property
    def own_temperature(self) -> float | None:
        """
        Return the TRV's own temperature reading in °C, without its offset.

        None after an offset write until the TRV reports with the new offset.
        """
        ...

    @property
    def offset(self) -> float | None:
        """Return the temperature offset in °C the TRV currently applies."""
        ...

    async def enter_direct(self) -> bool:
        """Switch to direct valve control; return whether the write was sent."""
        ...

    async def enter_self_regulation(
        self, setpoint: float, *, force_mode: bool = False
    ) -> int:
        """
        Switch to self-regulation at setpoint; return the writes sent.

        Writes only the parts (mode, setpoint) that differ from what the TRV
        reports. A setpoint left pending after a mode change is written on the
        next call; the room calls this on every fallback step.

        With force_mode the mode write is sent even if the TRV already reads
        self-regulating, and the setpoint is then written without comparing.
        zwave-js sends a node's commands in order, so the mode lands after a
        preset change still in flight.
        """
        ...

    async def set_valve(self, percent: int) -> bool:
        """Write the valve position; return whether the write was sent."""
        ...

    async def set_offset(self, offset: float) -> bool:
        """
        Write the temperature offset in °C; return whether it was sent.

        An offset the device already applies, at its resolution, is not sent.
        """
        ...
