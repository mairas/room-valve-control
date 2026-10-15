"""The device-facing interface a room uses to drive its TRVs."""

from enum import StrEnum
from typing import Protocol


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

    async def enter_self_regulation(self, setpoint: float) -> int:
        """
        Switch to self-regulation at setpoint; return the writes sent.

        Writes only the parts (mode, setpoint) that differ from what the TRV
        reports. After a mode change the setpoint may be left to a later call,
        so the room calls this again until it returns 0.
        """
        ...

    async def set_valve(self, percent: int) -> bool:
        """Write the valve position; return whether the write was sent."""
        ...

    async def set_offset(self, offset: float) -> bool:
        """Write the temperature offset in °C; return whether it was sent."""
        ...
