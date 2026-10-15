"""Tests for the per-room control state machine."""

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from custom_components.room_valve_control.controller import PIController, PIGains
from custom_components.room_valve_control.room import (
    ControlMode,
    Reading,
    Room,
    RoomSettings,
)
from custom_components.room_valve_control.trv import TrvMode

if TYPE_CHECKING:
    from collections.abc import Sequence

KP: Final = 30.0
TI: Final = timedelta(hours=2)
GAINS: Final = PIGains(kp=KP, ti=TI)
CONTROL_STEP: Final = timedelta(minutes=15)
STALE_TIMEOUT: Final = timedelta(hours=1)
MIN_CHANGE: Final = 5.0
SETTINGS: Final = RoomSettings(
    gains=GAINS,
    control_step=CONTROL_STEP,
    min_valve_change=MIN_CHANGE,
    stale_timeout=STALE_TIMEOUT,
)
START: Final = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
TARGET: Final = 21.0

Call = tuple[object, ...]


class FakeTrv:
    """A TRV that records the writes it receives and applies the sent ones."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        available: bool = True,
        mode: TrvMode = TrvMode.SELF_REGULATING,
        setpoint: float | None = None,
        valve: float | None = None,
        own_temperature: float | None = None,
        offset: float | None = None,
        sends: bool = True,
    ) -> None:
        """Create a TRV in the given state."""
        self.available = available
        self.mode = mode
        self.setpoint = setpoint
        self.valve = valve
        self.own_temperature = own_temperature
        self.offset = offset
        self.sends = sends
        self.calls: list[Call] = []

    async def enter_direct(self) -> bool:
        """Record the mode write."""
        self.calls.append(("direct",))
        if self.sends:
            self.mode = TrvMode.DIRECT
        return self.sends

    async def enter_self_regulation(self, setpoint: float) -> int:
        """Record heat mode, then setpoint, writing only what differs."""
        sent = 0
        if self.mode is not TrvMode.SELF_REGULATING:
            self.calls.append(("heat",))
            if not self.sends:
                return sent
            self.mode = TrvMode.SELF_REGULATING
            sent += 1
        if self.setpoint != setpoint:
            self.calls.append(("setpoint", setpoint))
            if self.sends:
                self.setpoint = setpoint
                sent += 1
        return sent

    async def set_valve(self, percent: int) -> bool:
        """Record the valve write."""
        self.calls.append(("valve", percent))
        if self.sends:
            self.valve = percent
        return self.sends

    async def set_offset(self, offset: float) -> bool:
        """Record the offset write."""
        self.calls.append(("offset", offset))
        if self.sends:
            self.offset = offset
        return self.sends


def make_room(
    trvs: Sequence[FakeTrv], integral: float = 0.0, device_writes: int = 0
) -> Room:
    """Create a room started at START."""
    return Room(
        SETTINGS,
        trvs,
        PIController(GAINS, integral),
        START,
        name="living room",
        device_writes=device_writes,
    )


def fresh(value: float, now: datetime) -> list[Reading]:
    """Return one reading reported at now."""
    return [Reading(value, now)]


def at(minutes: float) -> datetime:
    """Return the time minutes after START."""
    return START + timedelta(minutes=minutes)


def warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the captured records at warning level or above."""
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def clear(*trvs: FakeTrv) -> None:
    """Forget the calls recorded so far."""
    for trv in trvs:
        trv.calls.clear()


async def go_direct(room: Room, *trvs: FakeTrv, reading: float = 20.0) -> datetime:
    """Set the target and run a direct step at START; return its time."""
    await room.set_target(TARGET)
    await room.step(START, fresh(reading, START))
    clear(*trvs)
    return START


async def go_fallback(room: Room, *trvs: FakeTrv, now: datetime) -> None:
    """Run a step without readings at now and forget its calls."""
    await room.step(now, [])
    assert room.control_mode is ControlMode.FALLBACK
    clear(*trvs)


async def test_fresh_reading_below_target_enters_direct_and_writes_valve() -> None:
    """The first direct step selects direct mode and writes the rounded output."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await room.set_target(TARGET)

    await room.step(START, fresh(20.49, START))

    # Kp * 0.51 = 15.3, rounded once to a whole percent.
    assert trv.calls == [("direct",), ("valve", 15)]
    assert isinstance(trv.calls[1][1], int)
    assert room.control_mode is ControlMode.DIRECT
    assert room.last_output == 15  # noqa: PLR2004
    assert room.last_error == pytest.approx(0.51)
    assert room.device_writes == 2  # noqa: PLR2004


async def test_entering_direct_writes_valve_despite_matching_readback() -> None:
    """On entering mode 31 the valve takes its last mode-31 value; the readback lags."""
    trv = FakeTrv(valve=15.0)
    room = make_room([trv])
    await room.set_target(TARGET)

    await room.step(START, fresh(20.49, START))

    assert trv.calls == [("direct",), ("valve", 15)]


async def test_small_error_writes_nothing_until_output_moves_by_min_change() -> None:
    """A 0.1 °C error only writes once the integrator moves the output 5 %."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=40.0)
    room = make_room([trv], integral=40.0)
    await room.set_target(TARGET)

    outputs: list[int | None] = []
    for step in range(20):
        now = at(step * 15)
        await room.step(now, fresh(TARGET - 0.1, now))
        outputs.append(room.last_output)
        if trv.calls:
            break

    assert trv.calls == [("valve", 45)]
    assert len(outputs) > 1
    assert all(41 <= o < 45 for o in outputs[:-1] if o is not None)  # noqa: PLR2004


async def test_stale_reading_falls_back_with_heat_then_target() -> None:
    """A reading older than the stale timeout hands the TRV back."""
    trv = FakeTrv(setpoint=19.0)
    room = make_room([trv])
    await go_direct(room, trv)
    trv.setpoint = 19.0

    now = START + STALE_TIMEOUT + CONTROL_STEP
    await room.step(now, [Reading(20.0, START)])

    assert trv.calls == [("heat",), ("setpoint", TARGET)]
    assert room.control_mode is ControlMode.FALLBACK
    assert room.device_writes == 4  # noqa: PLR2004


async def test_fallback_step_with_everything_matching_writes_nothing() -> None:
    """A second fallback step finds mode and setpoint right and sends nothing."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))
    writes = room.device_writes

    await room.step(at(105), [])

    assert trv.calls == []
    assert room.device_writes == writes


async def test_fallback_freezes_integrator_and_return_resumes_from_it() -> None:
    """Back in direct mode the output comes from the pre-fallback integrator."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await room.step(at(15), fresh(20.0, at(15)))
    integral = room.integrator
    assert integral > 0

    for minutes in (90, 105, 120, 135):
        await room.step(at(minutes), [])
    assert room.control_mode is ControlMode.FALLBACK
    assert room.integrator == integral
    clear(trv)

    await room.step(at(150), fresh(20.5, at(150)))

    assert room.control_mode is ControlMode.DIRECT
    assert room.last_output == round(KP * 0.5 + integral)
    assert trv.calls[0] == ("direct",)


async def test_valve_closed_during_fallback_is_rewritten_on_return() -> None:
    """A readback of 0 after fallback is corrected even if the output is unchanged."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=40.0)
    room = make_room([trv], integral=40.0)
    await go_direct(room, trv, reading=TARGET)
    assert room.last_output == 40  # noqa: PLR2004
    await go_fallback(room, trv, now=at(90))
    trv.valve = 0.0

    await room.step(at(105), fresh(TARGET, at(105)))

    assert trv.calls == [("direct",), ("valve", 40)]


async def test_valve_without_readback_is_written() -> None:
    """A TRV whose valve reads back nothing gets the output."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=None)
    room = make_room([trv], integral=40.0)
    await room.set_target(TARGET)

    await room.step(START, fresh(TARGET, START))

    assert trv.calls == [("valve", 40)]


async def test_target_change_in_fallback_is_written_immediately() -> None:
    """In fallback a new target goes to the TRVs without waiting for a step."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))

    await room.set_target(22.5)

    assert trv.calls == [("setpoint", 22.5)]
    assert room.target == 22.5  # noqa: PLR2004


async def test_target_change_in_direct_takes_effect_at_next_step() -> None:
    """In direct mode a new target touches nothing until the next step."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)

    await room.set_target(22.0)
    assert trv.calls == []

    await room.step(at(15), fresh(20.0, at(15)))
    assert room.last_error == pytest.approx(2.0)


async def test_target_is_stored_exactly() -> None:
    """The room echoes the exact value it was given."""
    room = make_room([FakeTrv()])

    await room.set_target(20.123)

    assert room.target == 20.123  # noqa: PLR2004


async def test_one_of_three_trvs_unavailable_is_left_alone() -> None:
    """The available TRVs are controlled; nothing is sent to the other."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(available=False), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await room.set_target(TARGET)

    await room.step(START, fresh(20.0, START))

    assert trvs[0].calls == [("direct",), ("valve", 30)]
    assert trvs[1].calls == []
    assert trvs[2].calls == [("direct",), ("valve", 30)]


async def test_all_trvs_unavailable_freezes_integrator() -> None:
    """With no TRV reachable the integrator does not move."""
    trvs = [FakeTrv(available=False), FakeTrv(available=False)]
    room = make_room(trvs, integral=20.0)
    await room.set_target(TARGET)

    for step in range(4):
        now = at(step * 15)
        await room.step(now, fresh(20.0, now))

    assert room.integrator == 20.0  # noqa: PLR2004
    assert room.control_mode is ControlMode.DIRECT
    assert all(trv.calls == [] for trv in trvs)


async def test_trv_switched_out_of_direct_is_reentered_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TRV that left direct mode on its own is switched back with a warning."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    with caplog.at_level(logging.WARNING):
        await go_direct(room, trv)
    assert warnings(caplog) == []

    trv.mode = TrvMode.SELF_REGULATING
    with caplog.at_level(logging.WARNING):
        await room.step(at(15), fresh(20.0, at(15)))

    assert trv.calls[0] == ("direct",)
    assert len(warnings(caplog)) == 1
    assert "living room" in warnings(caplog)[0].getMessage()


async def test_trv_reentered_after_fallback_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leaving direct mode for fallback was the room's own doing."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))

    with caplog.at_level(logging.WARNING):
        await room.step(at(105), fresh(20.0, at(105)))

    assert trv.calls[0] == ("direct",)
    assert warnings(caplog) == []


async def test_stale_sensor_is_ignored_when_another_is_fresh() -> None:
    """Of two sensors, only the one reported within the timeout counts."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET)
    now = START + 2 * STALE_TIMEOUT

    await room.step(now, [Reading(20.0, now), Reading(10.0, START)])

    assert room.control_mode is ControlMode.DIRECT
    assert room.last_error == pytest.approx(1.0)


async def test_fresh_sensors_are_averaged() -> None:
    """The room reading is the mean of the fresh readings."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET)

    await room.step(START, [Reading(20.0, START), Reading(21.0, START)])

    assert room.last_error == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
async def test_non_finite_reading_is_ignored(bad: float) -> None:
    """A nan or inf reading counts as no reading."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET)

    await room.step(START, [Reading(bad, START), Reading(20.0, START)])
    assert room.last_error == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
async def test_only_non_finite_readings_count_as_none(bad: float) -> None:
    """With only non-finite readings the room stays waiting."""
    trv = FakeTrv()
    room = make_room([trv])
    await room.set_target(TARGET)

    await room.step(START, [Reading(bad, START)])

    assert room.control_mode is ControlMode.WAITING
    assert trv.calls == []


async def test_startup_without_readings_waits_until_stale_timeout() -> None:
    """TRVs are untouched until the stale timeout has passed since start."""
    trv = FakeTrv(mode=TrvMode.DIRECT, setpoint=19.0)
    room = make_room([trv])
    await room.set_target(TARGET)

    for minutes in (0, 15, 30, 45):
        await room.step(at(minutes), [])
    assert room.control_mode is ControlMode.WAITING
    assert trv.calls == []

    await room.step(START + STALE_TIMEOUT, [])

    assert room.control_mode is ControlMode.FALLBACK
    assert trv.calls == [("heat",), ("setpoint", TARGET)]


async def test_losing_readings_after_direct_falls_back_despite_startup() -> None:
    """The startup grace only applies before the room has left waiting."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)

    await room.step(at(15), [])

    assert room.control_mode is ControlMode.FALLBACK
    assert trv.calls == [("heat",), ("setpoint", TARGET)]


async def test_no_target_writes_nothing() -> None:
    """Without a target the room touches no TRV, fresh reading or not."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=0.0)
    room = make_room([trv])

    await room.step(START, fresh(20.0, START))
    await room.step(START + 2 * STALE_TIMEOUT, [])

    assert trv.calls == []
    assert room.control_mode is ControlMode.WAITING
    assert room.target is None


async def test_hand_back_puts_every_available_trv_in_self_regulation() -> None:
    """Hand back writes heat mode and the target to every reachable TRV."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(available=False), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await go_direct(room, *trvs)
    writes = room.device_writes

    await room.hand_back()

    assert trvs[0].calls == [("heat",), ("setpoint", TARGET)]
    assert trvs[1].calls == []
    assert trvs[2].calls == [("heat",), ("setpoint", TARGET)]
    assert room.device_writes == writes + 4


async def test_hand_back_without_target_touches_nothing() -> None:
    """Before any target the room has nothing to hand back."""
    trv = FakeTrv(mode=TrvMode.DIRECT)
    room = make_room([trv])

    await room.hand_back()

    assert trv.calls == []


@pytest.mark.parametrize(
    ("offset", "written"),
    [(None, True), (0.2, True), (0.3, False), (0.5, False), (0.9, True)],
)
async def test_offset_written_when_it_moves_at_least_threshold(
    offset: float | None, *, written: bool
) -> None:
    """The offset is rewritten at a change of 0.3 °C or more."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=30.0, own_temperature=20.0)
    trv.offset = offset
    room = make_room([trv])
    await room.set_target(TARGET)

    await room.step(START, fresh(20.5, START))

    offset_calls = [c for c in trv.calls if c[0] == "offset"]
    assert offset_calls == ([("offset", 0.5)] if written else [])


async def test_offset_not_written_without_own_temperature() -> None:
    """Without the TRV's own reading there is no offset to compute."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=30.0, own_temperature=None)
    room = make_room([trv])
    await room.set_target(TARGET)

    await room.step(START, fresh(20.0, START))

    assert trv.calls == []


async def test_failed_sends_are_not_counted() -> None:
    """Only writes the TRV reports as sent count as device writes."""
    failing = FakeTrv(valve=0.0, own_temperature=19.0, sends=False)
    working = FakeTrv(valve=0.0, own_temperature=19.0)
    room = make_room([failing, working], device_writes=5)
    await room.set_target(TARGET)

    await room.step(START, fresh(20.0, START))

    assert failing.calls == [("direct",), ("valve", 30), ("offset", 1.0)]
    assert working.calls == failing.calls
    assert room.device_writes == 5 + 3


async def test_failed_enter_direct_retry_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TRV the room never got into direct mode did not leave it externally."""
    trv = FakeTrv(valve=0.0, sends=False)
    room = make_room([trv])
    await go_direct(room, trv)

    with caplog.at_level(logging.WARNING):
        await room.step(at(15), fresh(20.0, at(15)))

    assert trv.calls[0] == ("direct",)
    assert warnings(caplog) == []


async def test_first_direct_step_integrates_one_control_step() -> None:
    """With no previous direct step the elapsed time is the control step."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET)

    await room.step(START, fresh(20.0, START))

    expected = PIController(GAINS)
    expected.step(TARGET, 20.0, CONTROL_STEP, integrate=True)
    assert room.integrator == pytest.approx(expected.integral)


async def test_elapsed_is_capped_at_control_step_after_long_gap() -> None:
    """A long gap between direct steps integrates no more than one step."""
    room = make_room([FakeTrv(valve=0.0)])
    await go_direct(room)
    expected = PIController(GAINS, room.integrator)
    expected.step(TARGET, 20.0, CONTROL_STEP, integrate=True)

    later = START + timedelta(hours=10)
    await room.step(later, fresh(20.0, later))

    assert room.integrator == pytest.approx(expected.integral)


async def test_elapsed_is_real_time_between_close_steps() -> None:
    """An immediate step shortly after another integrates only the time between."""
    room = make_room([FakeTrv(valve=0.0)])
    await go_direct(room)
    expected = PIController(GAINS, room.integrator)
    expected.step(TARGET, 20.0, timedelta(minutes=3), integrate=True)

    await room.step(at(3), fresh(20.0, at(3)))

    assert room.integrator == pytest.approx(expected.integral)


async def test_fallback_clears_output_and_error() -> None:
    """In fallback the room commands no valve and has no error."""
    room = make_room([FakeTrv(valve=0.0)])
    await go_direct(room)
    assert room.last_output is not None

    await room.step(at(90), [])

    assert room.last_output is None
    assert room.last_error is None


@pytest.mark.parametrize(
    ("mode_after", "readings_fresh", "expected"),
    [
        (ControlMode.WAITING, True, True),
        (ControlMode.WAITING, False, False),
        (ControlMode.FALLBACK, True, True),
        (ControlMode.FALLBACK, False, False),
        (ControlMode.DIRECT, True, False),
    ],
)
async def test_needs_immediate_step(
    mode_after: ControlMode, *, readings_fresh: bool, expected: bool
) -> None:
    """A fresh reading calls for a step only outside direct mode."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET)
    now = START + 2 * STALE_TIMEOUT
    if mode_after is ControlMode.DIRECT:
        await room.step(now, fresh(20.0, now))
    elif mode_after is ControlMode.FALLBACK:
        await room.step(now, [])
    assert room.control_mode is mode_after

    reported = now if readings_fresh else START
    assert room.needs_immediate_step(now, [Reading(20.0, reported)]) is expected


async def test_current_reading_is_mean_of_fresh_finite_readings() -> None:
    """The reading the glue shows as current temperature."""
    room = make_room([])
    now = START + 2 * STALE_TIMEOUT
    readings = [Reading(20.0, now), Reading(21.0, now), Reading(5.0, START)]

    assert room.current_reading(now, readings) == pytest.approx(20.5)
    assert room.current_reading(now, [Reading(math.nan, now)]) is None
