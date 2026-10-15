"""Per-room PI control law and the per-TRV valve write decision."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

VALVE_MIN: Final = 0.0
# Z-Wave Switch Multilevel tops out at 99, which the Spirit treats as fully open.
VALVE_MAX: Final = 99.0
# % points a TRV may read back off an endpoint and still count as there; without
# it a valve reporting 1 after a write of 0 would be rewritten every step.
ENDPOINT_TOLERANCE: Final = 2.0

_INTEGRAL_KEY: Final = "integral"


@dataclass(frozen=True)
class PIGains:
    """Parallel-form PI gains: Kp in % per °C, Ti as the integral time."""

    kp: float
    ti: timedelta


class PIController:
    """PI controller from room temperature error to valve position in %."""

    def __init__(self, gains: PIGains, integral: float = 0.0) -> None:
        """Create a controller, resuming from a previous integrator value."""
        self._gains = gains
        self._integral = integral

    @property
    def integral(self) -> float:
        """Return the integral term in %."""
        return self._integral

    def step(
        self, target: float, reading: float, elapsed: timedelta, *, integrate: bool
    ) -> float:
        """Return the valve output for this step and advance the integrator."""
        error = target - reading
        unclamped = self._gains.kp * error + self._integral
        output = min(max(unclamped, VALVE_MIN), VALVE_MAX)
        pushing_past_max = unclamped >= VALVE_MAX and error > 0
        pushing_past_min = unclamped <= VALVE_MIN and error < 0
        if integrate and not (pushing_past_max or pushing_past_min):
            self._integral += (
                self._gains.kp
                * error
                * elapsed.total_seconds()
                / self._gains.ti.total_seconds()
            )
        return output

    def as_dict(self) -> dict[str, float]:
        """Return the state to store across restarts."""
        return {_INTEGRAL_KEY: self._integral}

    @classmethod
    def from_dict(cls, gains: PIGains, data: Mapping[str, object]) -> Self:
        """Restore a controller from stored state."""
        integral = data.get(_INTEGRAL_KEY)
        if isinstance(integral, bool) or not isinstance(integral, int | float):
            msg = f"stored {_INTEGRAL_KEY} is not a number: {integral!r}"
            raise TypeError(msg)
        return cls(gains, float(integral))


def should_write_valve(output: float, readback: float, min_change: float) -> bool:
    """Return whether a TRV whose valve reads back `readback` needs `output`."""
    if output in (VALVE_MIN, VALVE_MAX):
        return abs(output - readback) > ENDPOINT_TOLERANCE
    return abs(output - readback) >= min_change
