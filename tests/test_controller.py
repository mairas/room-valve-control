"""Tests for the PI controller and the valve write decision."""

import math
import random
from dataclasses import dataclass
from datetime import timedelta

import pytest

from custom_components.room_valve_control.controller import (
    VALVE_MAX,
    VALVE_MIN,
    PIController,
    PIGains,
    should_write_valve,
)

GAINS = PIGains(kp=30.0, ti=timedelta(hours=2))
STEP = timedelta(minutes=15)
MIN_CHANGE = 5.0
TARGET = 21.0


def test_positive_error_raises_output() -> None:
    """A room below target opens the valve proportionally."""
    controller = PIController(GAINS)

    output = controller.step(TARGET, TARGET - 0.5, STEP, integrate=True)

    assert output == pytest.approx(GAINS.kp * 0.5)


def test_integrator_accumulates_parallel_form() -> None:
    """Each step adds Kp/Ti times error times elapsed time to the integrator."""
    controller = PIController(GAINS)
    error = 0.2

    controller.step(TARGET, TARGET - error, STEP, integrate=True)

    expected = GAINS.kp / GAINS.ti.total_seconds() * error * STEP.total_seconds()
    assert controller.integral == pytest.approx(expected)


def test_sustained_small_error_drifts_until_write() -> None:
    """A small persistent error eventually moves the output by the minimum change."""
    controller = PIController(GAINS, integral=40.0)
    readback = controller.step(TARGET, TARGET, STEP, integrate=True)
    small_error = 0.1
    max_steps = 100

    steps = 0
    output = readback
    while not should_write_valve(output, readback, MIN_CHANGE):
        output = controller.step(TARGET, TARGET - small_error, STEP, integrate=True)
        steps += 1
        assert steps < max_steps

    assert steps > 1
    assert output - readback >= MIN_CHANGE


def test_zero_error_holds_output() -> None:
    """At zero error the output stays put and no write is requested."""
    controller = PIController(GAINS, integral=37.0)
    readback = 37.0

    outputs = [controller.step(TARGET, TARGET, STEP, integrate=True) for _ in range(10)]

    assert outputs == [pytest.approx(37.0)] * 10
    assert not any(should_write_valve(o, readback, MIN_CHANGE) for o in outputs)


def test_saturation_high_stops_integrator() -> None:
    """At 99 with a positive error the integrator stops and recovers in one step."""
    controller = PIController(GAINS, integral=80.0)
    large_error = 2.0

    first = controller.step(TARGET, TARGET - large_error, STEP, integrate=True)
    frozen = controller.integral
    for _ in range(20):
        output = controller.step(TARGET, TARGET - large_error, STEP, integrate=True)
        assert output == VALVE_MAX

    assert first == VALVE_MAX
    assert controller.integral == frozen
    assert controller.step(TARGET, TARGET + 0.2, STEP, integrate=True) < VALVE_MAX


def test_saturation_low_stops_integrator() -> None:
    """At 0 with a negative error the integrator stops and recovers in one step."""
    controller = PIController(GAINS, integral=20.0)
    large_error = -2.0

    first = controller.step(TARGET, TARGET - large_error, STEP, integrate=True)
    frozen = controller.integral
    for _ in range(20):
        output = controller.step(TARGET, TARGET - large_error, STEP, integrate=True)
        assert output == VALVE_MIN

    assert first == VALVE_MIN
    assert controller.integral == frozen
    assert controller.step(TARGET, TARGET - 0.2, STEP, integrate=True) > VALVE_MIN


def test_saturated_integrator_unwinds_toward_range() -> None:
    """Saturated high, an error pulling back into range still integrates."""
    controller = PIController(GAINS, integral=120.0)

    controller.step(TARGET, TARGET + 0.1, STEP, integrate=True)

    assert controller.integral < 120.0  # noqa: PLR2004


def test_integration_not_allowed_freezes_integrator() -> None:
    """Without integration the integrator keeps its value whatever the error."""
    controller = PIController(GAINS, integral=42.0)

    for error in (-3.0, -0.5, 0.0, 0.5, 3.0):
        controller.step(TARGET, TARGET - error, STEP, integrate=False)

    assert controller.integral == 42.0  # noqa: PLR2004


@pytest.mark.parametrize(
    ("output", "readback", "expected"),
    [
        (50.0, 50.0, False),
        (54.9, 50.0, False),
        (55.0, 50.0, True),
        (45.0, 50.0, True),
        (3.0, 6.0, False),
        (0.0, 6.0, True),
        (0.0, 0.0, False),
        (96.0, 93.0, False),
        (99.0, 93.0, True),
        (99.0, 99.0, False),
    ],
)
def test_should_write_valve(*, output: float, readback: float, expected: bool) -> None:
    """Writes need the minimum change, except to reach a fully closed or open valve."""
    assert should_write_valve(output, readback, MIN_CHANGE) is expected


def test_closing_to_zero_written_only_at_zero() -> None:
    """Output 3 against readback 6 waits; the write comes once the output is 0."""
    controller = PIController(GAINS, integral=3.0)
    readback = 6.0

    near_closed = controller.step(TARGET, TARGET, STEP, integrate=True)
    closed = controller.step(TARGET, TARGET + 1.0, STEP, integrate=True)

    assert not should_write_valve(near_closed, readback, MIN_CHANGE)
    assert closed == VALVE_MIN
    assert should_write_valve(closed, readback, MIN_CHANGE)


def test_state_round_trips() -> None:
    """The serialised state restores an identical controller."""
    controller = PIController(GAINS)
    for error in (0.3, 0.7, -0.2):
        controller.step(TARGET, TARGET - error, STEP, integrate=True)

    restored = PIController.from_dict(GAINS, controller.as_dict())

    assert restored.as_dict() == controller.as_dict()
    assert restored.integral == controller.integral
    assert restored.step(TARGET, TARGET - 0.4, STEP, integrate=True) == controller.step(
        TARGET, TARGET - 0.4, STEP, integrate=True
    )


@pytest.mark.parametrize(
    "data",
    [{}, {"integral": "12"}, {"integral": None}, {"integral": True}],
)
def test_from_dict_rejects_malformed_state(data: dict[str, object]) -> None:
    """Stored state without a numeric integrator is refused."""
    with pytest.raises(TypeError, match="integral"):
        PIController.from_dict(GAINS, data)


# Simulation: starting gains, to be updated when Unit 1 converts the old ones.
SIM_KP = 30.0
SIM_TI = timedelta(hours=2)
SIM_SEED = 20261016
SIM_WARMUP = timedelta(days=1)
SIM_DURATION = timedelta(days=7)
SIM_SUBSTEP = timedelta(minutes=1)
WRITES_PER_DAY_LIMIT = 72

ROOM_TIME_CONSTANT = timedelta(hours=8)
HEATING_AT_FULL_VALVE = 4.0  # °C/h with the valve at 100 %
OUTDOOR_MEAN = 5.0
OUTDOOR_SWING = 3.0
SOLAR_GAIN_PEAK = 1.0  # °C/h at midday
SENSOR_NOISE = 0.05
OUTDOOR_COLDEST_HOUR = 5.0
SOLAR_RISE_HOUR = 8.0
SOLAR_SET_HOUR = 16.0
FULL_VALVE = 100.0
HOURS_PER_DAY = 24.0
SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class SimulationResult:
    """Outcome of a simulated period of direct control."""

    writes_per_day: float
    mean_abs_error: float


def _room_drift(hour_of_day: float, temperature: float, valve: float) -> float:
    """Return the room temperature change rate in °C/h."""
    outdoor = OUTDOOR_MEAN - OUTDOOR_SWING * math.cos(
        2 * math.pi * (hour_of_day - OUTDOOR_COLDEST_HOUR) / HOURS_PER_DAY
    )
    solar = 0.0
    if SOLAR_RISE_HOUR < hour_of_day < SOLAR_SET_HOUR:
        solar = SOLAR_GAIN_PEAK * math.sin(
            math.pi
            * (hour_of_day - SOLAR_RISE_HOUR)
            / (SOLAR_SET_HOUR - SOLAR_RISE_HOUR)
        )
    tau_hours = ROOM_TIME_CONSTANT.total_seconds() / SECONDS_PER_HOUR
    loss = (temperature - outdoor) / tau_hours
    return HEATING_AT_FULL_VALVE * valve / FULL_VALVE + solar - loss


def simulate(gains: PIGains) -> SimulationResult:
    """Run direct control on a first-order room model with noise and disturbances."""
    rng = random.Random(SIM_SEED)  # noqa: S311 - seeded for a repeatable run
    controller = PIController(gains)
    temperature = TARGET
    valve = VALVE_MIN
    writes = 0
    abs_errors: list[float] = []
    substeps = int(STEP / SIM_SUBSTEP)
    substep_hours = SIM_SUBSTEP.total_seconds() / SECONDS_PER_HOUR
    warmup_steps = int(SIM_WARMUP / STEP)
    total_steps = int((SIM_WARMUP + SIM_DURATION) / STEP)

    for step in range(total_steps):
        reading = temperature + rng.uniform(-SENSOR_NOISE, SENSOR_NOISE)
        output = controller.step(TARGET, reading, STEP, integrate=True)
        measured = step >= warmup_steps
        if should_write_valve(output, valve, MIN_CHANGE):
            valve = output
            writes += measured
        for substep in range(substeps):
            elapsed = step * STEP + substep * SIM_SUBSTEP
            hour_of_day = (elapsed.total_seconds() / SECONDS_PER_HOUR) % HOURS_PER_DAY
            temperature += _room_drift(hour_of_day, temperature, valve) * substep_hours
        if measured:
            abs_errors.append(abs(TARGET - temperature))

    days = SIM_DURATION / timedelta(days=1)
    return SimulationResult(
        writes_per_day=writes / days,
        mean_abs_error=sum(abs_errors) / len(abs_errors),
    )


def test_simulated_week_stays_within_write_budget() -> None:
    """A week of direct control at the starting gains stays under 72 writes/day."""
    result = simulate(PIGains(kp=SIM_KP, ti=SIM_TI))

    print(  # noqa: T201
        f"Kp={SIM_KP} Ti={SIM_TI}: {result.writes_per_day:.1f} writes/day, "
        f"MAE {result.mean_abs_error:.3f} °C"
    )
    assert result.writes_per_day < WRITES_PER_DAY_LIMIT
