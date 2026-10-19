"""Tests for the per-room control state machine."""

import asyncio
import contextlib
import logging
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from custom_components.room_valve_control.const import (
    DEFAULT_FALLBACK_FLOOR_SETPOINT,
)
from custom_components.room_valve_control.controller import PIController, PIGains
from custom_components.room_valve_control.room import (
    ControlMode,
    Reading,
    Room,
    RoomSettings,
)
from custom_components.room_valve_control.trv import Press, TrvMode

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

KP: Final = 30.0
TI: Final = timedelta(hours=2)
GAINS: Final = PIGains(kp=KP, ti=TI)
CONTROL_STEP: Final = timedelta(minutes=15)
STALE_TIMEOUT: Final = timedelta(hours=1)
MIN_CHANGE: Final = 5.0
SETTINGS: Final = RoomSettings(
    control_step=CONTROL_STEP,
    min_valve_change=MIN_CHANGE,
    stale_timeout=STALE_TIMEOUT,
)
SPIRIT_MAX_OFFSET: Final = 5.0
START: Final = datetime(2026, 10, 16, 6, 0, tzinfo=UTC)
TARGET: Final = 21.0
OTHER_TARGET: Final = 22.5
LATEST_TARGET: Final = 23.0
RESTORED_INTEGRAL: Final = 10.0

Call = tuple[object, ...]


class FakeTrv:
    """A TRV that records the writes it receives and applies the sent ones."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        name: str = "climate.trv",
        max_offset: float = SPIRIT_MAX_OFFSET,
        available: bool = True,
        mode: TrvMode = TrvMode.SELF_REGULATING,
        setpoint: float | None = None,
        valve: float | None = None,
        own_temperature: float | None = None,
        offset: float | None = None,
        sends: bool = True,
        deferred_readbacks: int = 0,
    ) -> None:
        """Create a TRV in the given state."""
        self.name = name
        self.max_offset = max_offset
        self.available = available
        self.mode = mode
        self.setpoint = setpoint
        self.pressed: Press | None = None
        self.valve = valve
        self.own_temperature = own_temperature
        self.offset = offset
        self.sends = sends
        # Mode changes to self-regulation that do not read back in time, so the
        # call returns the mode write and leaves the setpoint to the next call.
        self.deferred_readbacks = deferred_readbacks
        # While set, every write waits for the gate after recording its call.
        self.gate: asyncio.Event | None = None
        self.suspended = asyncio.Event()
        self.calls: list[Call] = []

    async def _wait_for_gate(self) -> None:
        if self.gate is not None:
            self.suspended.set()
            await self.gate.wait()

    def clear_press(self) -> None:
        """Forget the press."""
        self.pressed = None

    async def enter_direct(self) -> bool:
        """Record the mode write."""
        self.calls.append(("direct",))
        await self._wait_for_gate()
        if self.sends:
            self.mode = TrvMode.DIRECT
        return self.sends

    async def enter_self_regulation(
        self, setpoint: float, *, force_mode: bool = False
    ) -> int:
        """Record heat mode, then setpoint, writing only what differs unless forced."""
        sent = 0
        if force_mode or self.mode is not TrvMode.SELF_REGULATING:
            self.calls.append(("heat",))
            await self._wait_for_gate()
            if not self.sends:
                return sent
            if self.deferred_readbacks:
                self.deferred_readbacks -= 1
                return 1
            self.mode = TrvMode.SELF_REGULATING
            sent += 1
        if force_mode or self.setpoint != setpoint:
            self.calls.append(("setpoint", setpoint))
            await self._wait_for_gate()
            if self.sends:
                self.setpoint = setpoint
                sent += 1
        return sent

    async def set_valve(self, percent: int) -> bool:
        """Record the valve write."""
        self.calls.append(("valve", percent))
        await self._wait_for_gate()
        if self.sends:
            self.valve = percent
        return self.sends

    async def set_offset(self, offset: float) -> bool:
        """Record the offset write; apply it clamped like the Spirit."""
        self.calls.append(("offset", offset))
        await self._wait_for_gate()
        if self.sends:
            self.offset = max(-self.max_offset, min(self.max_offset, offset))
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


def close_gates(*trvs: FakeTrv) -> asyncio.Event:
    """Make every write of the TRVs wait for the returned event."""
    gate = asyncio.Event()
    for trv in trvs:
        trv.gate = gate
        trv.suspended.clear()
    return gate


async def settle() -> None:
    """Let every runnable task run until it blocks."""
    for _ in range(10):
        await asyncio.sleep(0)


def clear(*trvs: FakeTrv) -> None:
    """Forget the calls recorded so far."""
    for trv in trvs:
        trv.calls.clear()


async def go_direct(room: Room, *trvs: FakeTrv, reading: float = 20.0) -> datetime:
    """Set the target and run a direct step at START; return its time."""
    await room.set_target(TARGET, START)
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
    await room.set_target(TARGET, START)

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
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.49, START))

    assert trv.calls == [("direct",), ("valve", 15)]


async def test_small_error_writes_nothing_until_output_moves_by_min_change() -> None:
    """A 0.1 °C error only writes once the integrator moves the output 5 %."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=40.0)
    room = make_room([trv], integral=40.0)
    await room.set_target(TARGET, START)

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

    # mypy keeps control_mode narrowed across the awaited step
    assert room.control_mode is ControlMode.DIRECT  # type: ignore[comparison-overlap]
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
    await room.set_target(TARGET, START)

    await room.step(START, fresh(TARGET, START))

    assert trv.calls == [("valve", 40)]


async def test_target_change_in_fallback_is_written_immediately() -> None:
    """In fallback a new target goes to the TRVs without waiting for a step."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))

    await room.set_target(22.5, START)

    assert trv.calls == [("setpoint", 22.5)]
    assert room.target == 22.5  # noqa: PLR2004


async def test_target_change_in_direct_takes_effect_at_next_step() -> None:
    """In direct mode a new target touches nothing until the next step."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)

    await room.set_target(22.0, START)
    assert trv.calls == []

    await room.step(at(15), fresh(20.0, at(15)))
    assert room.last_error == pytest.approx(2.0)


async def test_target_is_stored_exactly() -> None:
    """The room echoes the exact value it was given."""
    room = make_room([FakeTrv()])

    await room.set_target(20.123, START)

    assert room.target == 20.123  # noqa: PLR2004


async def test_one_of_three_trvs_unavailable_is_left_alone() -> None:
    """The available TRVs are controlled; nothing is sent to the other."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(available=False), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.0, START))

    assert trvs[0].calls == [("direct",), ("valve", 30)]
    assert trvs[1].calls == []
    assert trvs[2].calls == [("direct",), ("valve", 30)]


async def test_all_trvs_unavailable_freezes_integrator() -> None:
    """With no TRV reachable the integrator does not move."""
    trvs = [FakeTrv(available=False), FakeTrv(available=False)]
    room = make_room(trvs, integral=20.0)
    await room.set_target(TARGET, START)

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


async def test_external_change_warning_names_the_trv(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In a multi-TRV room the warning says which TRV left direct mode."""
    trvs = [
        FakeTrv(name="climate.window", valve=0.0),
        FakeTrv(name="climate.door", valve=0.0),
    ]
    room = make_room(trvs)
    await go_direct(room, *trvs)

    trvs[1].mode = TrvMode.SELF_REGULATING
    with caplog.at_level(logging.WARNING):
        await room.step(at(15), fresh(20.0, at(15)))

    assert len(warnings(caplog)) == 1
    message = warnings(caplog)[0].getMessage()
    assert "climate.door" in message
    assert "climate.window" not in message


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


@pytest.mark.parametrize("directed", [True, False])
async def test_trv_back_from_unavailable_in_self_regulation(
    caplog: pytest.LogCaptureFixture, *, directed: bool
) -> None:
    """
    A TRV missed by the fallback and found self-regulating warns if directed.

    The room never switched it out of direct mode, so finding it switched is a
    change the room did not make; a TRV it never directed is simply entered.
    """
    trv = FakeTrv(valve=0.0, available=directed)
    room = make_room([trv])
    await go_direct(room, trv)
    trv.available = False
    await go_fallback(room, trv, now=at(90))
    assert trv.calls == []
    trv.available = True
    trv.mode = TrvMode.SELF_REGULATING

    with caplog.at_level(logging.WARNING):
        await room.step(at(105), fresh(20.0, at(105)))

    assert trv.calls[0] == ("direct",)
    assert len(warnings(caplog)) == (1 if directed else 0)


def infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the messages captured at info level."""
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def press(trv: FakeTrv, setpoint: float, minutes: float = 10) -> None:
    """Set the TRV's setpoint by hand at minutes, which leaves mode 31."""
    trv.mode = TrvMode.SELF_REGULATING
    trv.setpoint = setpoint
    trv.pressed = Press(setpoint, at(minutes))


async def test_setpoint_set_on_a_direct_trv_becomes_the_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The room adopts it, puts the TRV back in direct mode and steps against it."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    press(trv, OTHER_TARGET)

    with caplog.at_level(logging.INFO):
        await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == OTHER_TARGET
    assert room.last_error == pytest.approx(OTHER_TARGET - 20.0)
    assert trv.calls[0] == ("direct",)
    assert trv.pressed is None
    assert room.control_mode is ControlMode.DIRECT
    assert warnings(caplog) == []
    assert infos(caplog) == [f"living room: target {OTHER_TARGET} from climate.trv"]


async def test_press_on_a_trv_direct_before_the_room_started_is_adopted() -> None:
    """After a restart the TRVs are already in mode 31; a press still counts."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    press(trv, OTHER_TARGET)

    await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == OTHER_TARGET
    assert trv.calls[0] == ("direct",)


async def test_direct_trv_out_without_a_press_is_not_adopted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TRV in heat at its stored setpoint, nobody having changed it, is re-entered."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    trv.mode = TrvMode.SELF_REGULATING
    trv.setpoint = OTHER_TARGET

    with caplog.at_level(logging.WARNING):
        await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == TARGET
    assert trv.calls[0] == ("direct",)
    assert len(warnings(caplog)) == 1


async def test_target_set_after_a_press_wins() -> None:
    """A press older than the room's last target change is dropped."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    press(trv, OTHER_TARGET, minutes=10)
    await room.set_target(LATEST_TARGET, at(12))

    await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == LATEST_TARGET
    assert trv.pressed is None


async def test_press_from_before_the_room_started_is_dropped() -> None:
    """A restored target counts as set when the room starts."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    press(trv, OTHER_TARGET, minutes=-5)

    await go_direct(room, trv)

    assert room.target == TARGET


async def test_press_at_the_target_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pressing back to the target is adopted without a log line."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    press(trv, TARGET)

    with caplog.at_level(logging.INFO):
        await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == TARGET
    assert trv.pressed is None
    assert infos(caplog) == []
    assert warnings(caplog) == []


async def test_unavailable_trv_is_not_adopted() -> None:
    """A TRV that cannot be read gives the room no setpoint."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    press(trv, OTHER_TARGET)
    trv.available = False

    await room.step(at(15), fresh(20.0, at(15)))

    assert room.target == TARGET


async def test_adopted_press_counts_as_the_last_target_change() -> None:
    """A press older than the one adopted, seen only later, is dropped."""
    late = FakeTrv(name="climate.window", valve=0.0)
    pressed = FakeTrv(name="climate.door", valve=0.0)
    room = make_room([late, pressed])
    await go_direct(room, late, pressed)
    press(late, LATEST_TARGET, minutes=5)
    late.available = False
    press(pressed, OTHER_TARGET, minutes=10)
    await room.step(at(15), fresh(20.0, at(15)))
    late.available = True

    await room.step(at(30), fresh(20.0, at(30)))

    assert room.target == OTHER_TARGET


async def test_fallback_trv_without_a_press_is_not_adopted() -> None:
    """A TRV reporting no setpoint of its own leaves the target as it is."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))

    await room.step(at(105), [])

    assert room.target == TARGET
    assert trv.calls == []


async def test_setpoint_set_on_a_fallback_trv_goes_to_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The room adopts it and writes it to its other TRVs."""
    pressed = FakeTrv(name="climate.window", valve=0.0)
    other = FakeTrv(name="climate.door", valve=0.0)
    room = make_room([pressed, other])
    await go_direct(room, pressed, other)
    await go_fallback(room, pressed, other, now=at(90))
    press(pressed, OTHER_TARGET, minutes=100)

    with caplog.at_level(logging.INFO):
        await room.step(at(105), [])

    assert room.target == OTHER_TARGET
    assert pressed.calls == []
    assert other.calls == [("setpoint", OTHER_TARGET)]
    assert infos(caplog) == [f"living room: target {OTHER_TARGET} from climate.window"]


async def test_setpoint_set_in_fallback_counts_when_readings_return() -> None:
    """A step that leaves fallback steps against the adopted target."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))
    press(trv, OTHER_TARGET, minutes=100)

    await room.step(at(105), fresh(20.0, at(105)))

    assert room.target == OTHER_TARGET
    assert room.last_error == pytest.approx(OTHER_TARGET - 20.0)


@pytest.mark.parametrize("in_fallback", [False, True])
async def test_latest_press_wins(*, in_fallback: bool) -> None:
    """Of TRVs set by hand since the last step, the one pressed last is adopted."""
    trvs = [
        FakeTrv(name="climate.window", valve=0.0),
        FakeTrv(name="climate.door", valve=0.0),
        FakeTrv(name="climate.corner", valve=0.0),
    ]
    room = make_room(trvs)
    await go_direct(room, *trvs)
    if in_fallback:
        await go_fallback(room, *trvs, now=at(90))
    press(trvs[0], OTHER_TARGET, minutes=95)
    press(trvs[1], LATEST_TARGET, minutes=100)
    press(trvs[2], 24.0, minutes=98)

    await room.step(at(105), fresh(20.0, at(105)))

    assert room.target == LATEST_TARGET
    assert [trv.pressed for trv in trvs] == [None, None, None]


async def test_room_without_target_adopts_nothing() -> None:
    """Without a target the room does nothing, a TRV's setpoint included."""
    trv = FakeTrv()
    trv.pressed = Press(OTHER_TARGET, START)
    room = make_room([trv])

    await room.step(START, fresh(20.0, START))
    await room.step(START + STALE_TIMEOUT + CONTROL_STEP, [])

    assert room.target is None
    assert trv.calls == []


async def test_stopped_room_adopts_nothing() -> None:
    """After a stop, setpoints set on the TRVs stay theirs."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await go_fallback(room, trv, now=at(90))
    await room.stop()
    press(trv, OTHER_TARGET, minutes=100)

    await room.step(at(105), [])

    assert room.target == TARGET
    assert trv.calls == []


async def test_stale_sensor_is_ignored_when_another_is_fresh() -> None:
    """Of two sensors, only the one reported within the timeout counts."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET, START)
    now = START + 2 * STALE_TIMEOUT

    await room.step(now, [Reading(20.0, now), Reading(10.0, START)])

    assert room.control_mode is ControlMode.DIRECT
    assert room.last_error == pytest.approx(1.0)


async def test_fresh_sensors_are_averaged() -> None:
    """The room reading is the mean of the fresh readings."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET, START)

    await room.step(START, [Reading(20.0, START), Reading(21.0, START)])

    assert room.last_error == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
async def test_non_finite_reading_is_ignored(bad: float) -> None:
    """A nan or inf reading counts as no reading."""
    room = make_room([FakeTrv(valve=0.0)])
    await room.set_target(TARGET, START)

    await room.step(START, [Reading(bad, START), Reading(20.0, START)])
    assert room.last_error == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
async def test_only_non_finite_readings_count_as_none(bad: float) -> None:
    """With only non-finite readings the room stays waiting."""
    trv = FakeTrv()
    room = make_room([trv])
    await room.set_target(TARGET, START)

    await room.step(START, [Reading(bad, START)])

    assert room.control_mode is ControlMode.WAITING
    assert trv.calls == []


async def test_startup_without_readings_waits_until_stale_timeout() -> None:
    """TRVs are untouched until the stale timeout has passed since start."""
    trv = FakeTrv(mode=TrvMode.DIRECT, setpoint=19.0)
    room = make_room([trv])
    await room.set_target(TARGET, START)

    for minutes in (0, 15, 30, 45):
        await room.step(at(minutes), [])
    assert room.control_mode is ControlMode.WAITING
    assert trv.calls == []

    await room.step(START + STALE_TIMEOUT, [])

    assert room.control_mode is ControlMode.FALLBACK  # type: ignore[comparison-overlap]
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


async def test_hand_back_from_waiting_hands_back() -> None:
    """A room still waiting for readings hands back TRVs left in direct mode."""
    trv = FakeTrv(mode=TrvMode.DIRECT, setpoint=19.0)
    room = make_room([trv])
    await room.set_target(TARGET, START)
    await room.step(START, [])
    assert room.control_mode is ControlMode.WAITING

    await room.hand_back()

    assert trv.calls == [("heat",), ("setpoint", TARGET)]
    assert room.control_mode is ControlMode.STOPPED  # type: ignore[comparison-overlap]


async def test_hand_back_waits_for_a_suspended_step() -> None:
    """A step suspended in a TRV write finishes before the hand back starts."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await room.set_target(TARGET, START)
    gate = close_gates(trv)
    step = asyncio.create_task(room.step(START, fresh(20.0, START)))
    await trv.suspended.wait()

    hand_back = asyncio.create_task(room.hand_back())
    await settle()
    assert trv.calls == [("direct",)]
    gate.set()
    await step
    await hand_back

    assert trv.calls == [("direct",), ("valve", 30), ("heat",), ("setpoint", TARGET)]
    assert trv.mode is TrvMode.SELF_REGULATING
    assert room.control_mode is ControlMode.STOPPED


async def test_hand_back_forces_heat_on_a_cancelled_enter_direct() -> None:
    """
    A TRV whose direct-mode write was cut off by a cancel gets heat mode anyway.

    The write may still reach the node, so its heat readback proves nothing.
    """
    directed = FakeTrv(valve=0.0)
    entering = FakeTrv(valve=0.0, setpoint=TARGET)
    untouched = FakeTrv(valve=0.0, setpoint=TARGET)
    room = make_room([directed, entering, untouched])
    await room.set_target(TARGET, START)
    close_gates(entering)
    step = asyncio.create_task(room.step(START, fresh(20.0, START)))
    await entering.suspended.wait()
    step.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await step
    entering.gate = None
    clear(directed, entering, untouched)

    await room.hand_back()

    assert directed.calls == [("heat",), ("setpoint", TARGET)]
    assert entering.calls == [("heat",), ("setpoint", TARGET)]
    assert untouched.calls == []


async def test_stopped_room_writes_nothing() -> None:
    """After hand back neither steps nor targets reach the TRVs."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    await room.hand_back()
    clear(trv)
    writes = room.device_writes

    await room.step(at(15), fresh(20.0, at(15)))
    await room.step(at(120), [])
    await room.set_target(OTHER_TARGET, START)
    await room.hand_back()

    assert trv.calls == []
    assert room.device_writes == writes
    assert room.control_mode is ControlMode.STOPPED
    # Kept for the restore, so a reloaded room starts from it.
    assert room.target == OTHER_TARGET
    assert room.last_output is None
    assert not room.needs_immediate_step(at(120), fresh(20.0, at(120)))


async def test_stop_ends_the_room_without_writing() -> None:
    """A stopped room leaves its TRVs as they are, and records later targets."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)

    await room.stop()
    await room.step(at(15), fresh(20.0, at(15)))
    await room.set_target(OTHER_TARGET, START)
    await room.hand_back()

    assert trv.calls == []
    assert room.control_mode is ControlMode.STOPPED
    assert room.target == OTHER_TARGET


async def test_stop_waits_for_a_fallback_target_write() -> None:
    """A target write in progress finishes first; one queued after writes nothing."""
    trv = FakeTrv(mode=TrvMode.SELF_REGULATING, setpoint=TARGET)
    room = make_room([trv])
    await room.set_target(TARGET, START)
    await go_fallback(room, trv, now=at(2 * STALE_TIMEOUT.total_seconds() / 60))
    gate = close_gates(trv)
    first = asyncio.create_task(room.set_target(OTHER_TARGET, START))
    await trv.suspended.wait()

    stop = asyncio.create_task(room.stop())
    later = asyncio.create_task(room.set_target(LATEST_TARGET, START))
    await settle()
    assert not stop.done()
    gate.set()
    await asyncio.gather(first, stop, later)

    assert trv.calls == [("setpoint", OTHER_TARGET)]
    assert room.control_mode is ControlMode.STOPPED
    assert room.target == LATEST_TARGET


async def test_target_set_during_hand_back_is_recorded_not_written() -> None:
    """A target arriving while the TRVs are handed back waits for nothing."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    gate = close_gates(trv)
    hand_back = asyncio.create_task(room.hand_back())
    await trv.suspended.wait()

    await room.set_target(OTHER_TARGET, START)
    gate.set()
    await hand_back

    assert room.target == OTHER_TARGET
    assert trv.calls == [("heat",), ("setpoint", TARGET)]


async def test_target_set_during_fallback_step_is_written_after_it() -> None:
    """A new target waits for the step's mode and setpoint writes to finish."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    gate = close_gates(trv)
    step = asyncio.create_task(room.step(at(90), []))
    await trv.suspended.wait()

    set_target = asyncio.create_task(room.set_target(22.5, START))
    await settle()
    assert trv.calls == [("heat",)]
    gate.set()
    await step
    await set_target

    assert trv.calls == [("heat",), ("setpoint", TARGET), ("setpoint", 22.5)]


async def test_target_set_during_step_is_recorded_at_once() -> None:
    """A step suspended in a TRV write does not delay the new target."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await room.set_target(TARGET, START)
    gate = close_gates(trv)
    step = asyncio.create_task(room.step(START, fresh(20.0, START)))
    await trv.suspended.wait()

    set_target = asyncio.create_task(room.set_target(OTHER_TARGET, START))
    await settle()

    assert room.target == OTHER_TARGET
    assert set_target.done()
    gate.set()
    await step


async def test_fallback_write_after_a_step_uses_the_latest_target() -> None:
    """Targets queued behind a fallback step write only the one current then."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    gate = close_gates(trv)
    step = asyncio.create_task(room.step(at(90), []))
    await trv.suspended.wait()

    first = asyncio.create_task(room.set_target(OTHER_TARGET, START))
    second = asyncio.create_task(room.set_target(LATEST_TARGET, START))
    await settle()
    gate.set()
    await asyncio.gather(step, first, second)

    assert trv.calls == [("heat",), ("setpoint", TARGET), ("setpoint", LATEST_TARGET)]


async def test_hand_back_writes_trvs_concurrently() -> None:
    """Each TRV's slow mode change does not hold up the others."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await go_direct(room, *trvs)
    gate = close_gates(*trvs)

    hand_back = asyncio.create_task(room.hand_back())
    await asyncio.wait_for(
        asyncio.gather(*(trv.suspended.wait() for trv in trvs)), timeout=1
    )
    gate.set()
    await hand_back

    assert all(trv.mode is TrvMode.SELF_REGULATING for trv in trvs)


async def test_fallback_writes_trvs_concurrently() -> None:
    """Falling back does not wait for one TRV's mode change to start the next."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await go_direct(room, *trvs)
    gate = close_gates(*trvs)

    step = asyncio.create_task(room.step(at(90), []))
    await asyncio.wait_for(
        asyncio.gather(*(trv.suspended.wait() for trv in trvs)), timeout=1
    )
    gate.set()
    await step

    assert all(trv.setpoint == TARGET for trv in trvs)


async def test_hand_back_retries_a_deferred_setpoint_once() -> None:
    """A mode that reads back late gets a second call, which writes the setpoint."""
    trvs = [FakeTrv(valve=0.0), FakeTrv(valve=0.0)]
    room = make_room(trvs)
    await go_direct(room, *trvs)
    trvs[0].deferred_readbacks = 1
    writes = room.device_writes

    await room.hand_back()

    assert trvs[0].calls == [("heat",), ("heat",), ("setpoint", TARGET)]
    assert trvs[1].calls == [("heat",), ("setpoint", TARGET)]
    assert room.device_writes == writes + 5


async def test_hand_back_retry_is_bounded() -> None:
    """A TRV whose mode never reads back is called twice, not more."""
    trv = FakeTrv(valve=0.0, deferred_readbacks=5)
    room = make_room([trv])
    await go_direct(room, trv)

    await room.hand_back()

    assert trv.calls == [("heat",), ("heat",)]


async def test_hand_back_does_not_retry_an_unsent_write() -> None:
    """A call that sent nothing is not retried; the device is not taking writes."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    trv.sends = False

    await room.hand_back()

    assert trv.calls == [("heat",)]


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
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.5, START))

    offset_calls = [c for c in trv.calls if c[0] == "offset"]
    assert offset_calls == ([("offset", 0.5)] if written else [])


async def test_offset_change_of_nominal_threshold_is_written() -> None:
    """20.7 - 20.0 - 0.4 is 0.2999... in floats; in tenths it is the threshold."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=30.0, own_temperature=20.0, offset=0.4)
    room = make_room([trv])
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.7, START))

    assert [c for c in trv.calls if c[0] == "offset"] == [
        ("offset", pytest.approx(0.7))
    ]


async def test_offset_beyond_device_limit_is_clamped_and_written_once() -> None:
    """A gap past the TRV's limit writes the limit, not the gap after every report."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=30.0, own_temperature=26.0)
    room = make_room([trv])
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.5, START))
    assert [c for c in trv.calls if c[0] == "offset"] == [("offset", -5.0)]
    clear(trv)

    await room.step(at(15), fresh(20.5, at(15)))

    assert [c for c in trv.calls if c[0] == "offset"] == []


async def test_offset_not_written_without_own_temperature() -> None:
    """Without the TRV's own reading there is no offset to compute."""
    trv = FakeTrv(mode=TrvMode.DIRECT, valve=30.0, own_temperature=None)
    room = make_room([trv])
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.0, START))

    assert trv.calls == []


async def test_failed_sends_are_not_counted() -> None:
    """Only writes the TRV reports as sent count as device writes."""
    failing = FakeTrv(valve=0.0, own_temperature=19.0, sends=False)
    working = FakeTrv(valve=0.0, own_temperature=19.0)
    room = make_room([failing, working], device_writes=5)
    await room.set_target(TARGET, START)

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


async def test_failed_reentry_after_external_change_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A re-entry that fails is retried next step without a second warning."""
    trv = FakeTrv(valve=0.0)
    room = make_room([trv])
    await go_direct(room, trv)
    trv.mode = TrvMode.SELF_REGULATING
    trv.sends = False

    with caplog.at_level(logging.WARNING):
        await room.step(at(15), fresh(20.0, at(15)))
    assert len(warnings(caplog)) == 1
    caplog.clear()
    clear(trv)

    with caplog.at_level(logging.WARNING):
        await room.step(at(30), fresh(20.0, at(30)))

    assert trv.calls[0] == ("direct",)
    assert warnings(caplog) == []


async def test_first_direct_step_does_not_integrate() -> None:
    """A new room has had no interval to integrate over, restored or not."""
    room = make_room([FakeTrv(valve=0.0)], integral=RESTORED_INTEGRAL)
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.0, START))

    assert room.integrator == RESTORED_INTEGRAL
    assert room.last_output == round(KP * 1.0 + RESTORED_INTEGRAL)


async def test_second_direct_step_integrates_the_time_since_the_first() -> None:
    """Integration starts from the room's first direct step."""
    room = make_room([FakeTrv(valve=0.0)])
    await go_direct(room)
    expected = PIController(GAINS)
    expected.step(TARGET, 20.0, CONTROL_STEP, integrate=True)

    await room.step(at(15), fresh(20.0, at(15)))

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
    await room.set_target(TARGET, START)
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


async def test_controller_state_is_what_the_controller_restores_from() -> None:
    """The room's controller state restores a controller with its integral."""
    room = make_room([FakeTrv(valve=0.0)])
    await go_direct(room)
    await room.step(at(15), fresh(20.0, at(15)))

    restored = PIController.from_dict(GAINS, room.controller_state)

    assert restored.integral == room.integrator > 0


# Floor rooms

PERCENT: Final = 100
OWN_FALLBACK_SETPOINT: Final = 24.0


class FakeFloorHeater:
    """A floor heater that records its calls and keeps the off edge it owes."""

    def __init__(
        self,
        *,
        name: str = "climate.floor",
        available: bool = True,
        sends: bool = True,
    ) -> None:
        """Create a heater in the given state."""
        self.name = name
        self.available = available
        self.sends = sends
        # When the off edge of the running period is due, None without one.
        self.pending_off: datetime | None = None
        self.on_edge_written: Callable[[int], None] | None = None
        # While set, every write waits for the gate after recording its call.
        self.gate: asyncio.Event | None = None
        self.suspended = asyncio.Event()
        self.calls: list[Call] = []

    async def _wait_for_gate(self) -> None:
        if self.gate is not None:
            self.suspended.set()
            await self.gate.wait()

    async def apply_duty(self, output: float, period: timedelta, now: datetime) -> int:
        """Record the period start and owe an off edge inside the period."""
        await self.cancel_edge()
        self.calls.append(("duty", output, period, now))
        await self._wait_for_gate()
        if 0 < output < PERCENT:
            self.pending_off = now + period * output / PERCENT
        return int(self.sends and self.available)

    async def hold_setpoint(self, setpoint: float) -> int:
        """Drop the owed off edge, then record the setpoint."""
        await self.cancel_edge()
        self.calls.append(("hold", setpoint))
        await self._wait_for_gate()
        return int(self.sends and self.available)

    async def cancel_edge(self) -> None:
        """Drop the owed off edge."""
        self.pending_off = None

    def fire_edge(self) -> bool:
        """Write the owed off edge as the timer would; return whether one was owed."""
        if self.pending_off is None:
            return False
        self.pending_off = None
        if self.on_edge_written is not None:
            self.on_edge_written(int(self.sends))
        return True


def make_floor_room(
    heaters: Sequence[FakeFloorHeater],
    integral: float = 0.0,
    settings: RoomSettings = SETTINGS,
) -> Room:
    """Create a floor room started at START whose heaters report to it."""
    room = Room(
        settings,
        [],
        PIController(GAINS, integral),
        START,
        name="bathroom",
        heaters=heaters,
    )
    for heater in heaters:
        heater.on_edge_written = room.count_edge_writes
    return room


def clear_heaters(*heaters: FakeFloorHeater) -> None:
    """Forget the calls recorded so far."""
    for heater in heaters:
        heater.calls.clear()


HOLD_FALLBACK: Final[Call] = ("hold", DEFAULT_FALLBACK_FLOOR_SETPOINT)


async def test_direct_step_applies_the_output_to_every_available_heater() -> None:
    """Each available heater starts a period of the PI output over the step."""
    heaters = [
        FakeFloorHeater(),
        FakeFloorHeater(available=False),
        FakeFloorHeater(),
    ]
    room = make_floor_room(heaters)
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.0, START))

    assert room.control_mode is ControlMode.DIRECT
    assert room.last_output == round(KP * 1.0)
    assert heaters[0].calls == [("duty", 30, CONTROL_STEP, START)]
    assert heaters[1].calls == []
    assert heaters[2].calls == [("duty", 30, CONTROL_STEP, START)]
    assert room.device_writes == len(heaters) - 1


async def test_stale_reading_cancels_the_off_edge_and_holds_fallback() -> None:
    """Fallback cancels the period's pending off before writing the fallback."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(START, fresh(20.0, START))
    assert heater.pending_off is not None
    clear_heaters(heater)

    await room.step(at(15), [])

    assert room.control_mode is ControlMode.FALLBACK
    assert heater.calls == [HOLD_FALLBACK]
    assert not heater.fire_edge()


async def test_fallback_holds_the_fallback_setpoint_on_every_step() -> None:
    """The fallback is rewritten each step, for a heater that missed it."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)

    await room.step(at(60), [])
    await room.step(at(75), [])

    assert heater.calls == [HOLD_FALLBACK, HOLD_FALLBACK]
    assert room.device_writes == len(heater.calls)


async def test_fallback_holds_the_rooms_own_fallback_setpoint() -> None:
    """A room's configured fallback replaces the default."""
    heater = FakeFloorHeater()
    settings = replace(SETTINGS, fallback_floor_setpoint=OWN_FALLBACK_SETPOINT)
    room = make_floor_room([heater], settings=settings)
    await room.set_target(TARGET, START)

    await room.step(at(60), [])

    assert heater.calls == [("hold", OWN_FALLBACK_SETPOINT)]


async def test_fallback_cancels_the_edge_of_an_unavailable_heater() -> None:
    """A heater gone unavailable mid-period gets no late off edge either."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(START, fresh(20.0, START))
    heater.available = False
    writes = room.device_writes

    await room.step(at(15), [])

    assert not heater.fire_edge()
    assert room.device_writes == writes


async def test_fresh_reading_in_fallback_starts_a_period_at_once() -> None:
    """The immediate step starts a period off the grid; the next one another."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(at(60), [])
    clear_heaters(heater)
    assert room.needs_immediate_step(at(65), fresh(20.0, at(65)))

    await room.step(at(65), fresh(20.0, at(65)))
    first_off = heater.pending_off
    await room.step(at(75), fresh(20.0, at(75)))

    assert [call[3] for call in heater.calls] == [at(65), at(75)]
    assert first_off is not None
    assert first_off > at(65)
    assert heater.pending_off is not None
    assert heater.pending_off > at(75)


async def test_target_change_in_fallback_writes_nothing_to_heaters() -> None:
    """A floor room's fallback ignores the target; direct uses it next step."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(at(60), [])
    clear_heaters(heater)

    await room.set_target(OTHER_TARGET, at(61))
    assert heater.calls == []
    await room.step(at(65), fresh(20.0, at(65)))

    assert room.last_error == OTHER_TARGET - 20.0
    assert heater.calls == [("duty", round(KP * 2.5), CONTROL_STEP, at(65))]


@pytest.mark.parametrize("stopping", ["hand_back", "stop"])
async def test_stopping_cancels_the_edge_and_holds_fallback(stopping: str) -> None:
    """Hand back and stop leave no timed on running past the room."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(START, fresh(20.0, START))
    clear_heaters(heater)
    writes = room.device_writes

    await getattr(room, stopping)()

    assert room.control_mode is ControlMode.STOPPED
    assert heater.calls == [HOLD_FALLBACK]
    assert room.device_writes == writes + 1
    assert not heater.fire_edge()


@pytest.mark.parametrize("stopping", ["hand_back", "stop"])
async def test_stopping_without_target_holds_fallback(stopping: str) -> None:
    """The heater may be on from before the room; stopping holds the fallback."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])

    await getattr(room, stopping)()

    assert heater.calls == [HOLD_FALLBACK]


async def test_stopped_floor_room_writes_nothing_more() -> None:
    """After hand back, stopping again, steps and holds reach no heater."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.hand_back()
    clear_heaters(heater)

    await room.stop()
    await room.hand_back()
    await room.step(at(15), fresh(20.0, at(15)))
    await room.step(at(120), [])
    await room.hold_fallback()

    assert heater.calls == []


async def test_stop_waits_for_a_period_start_in_progress() -> None:
    """A period start in flight finishes first, so stop cancels its off edge."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    gate = asyncio.Event()
    heater.gate = gate
    step = asyncio.create_task(room.step(START, fresh(20.0, START)))
    await heater.suspended.wait()

    stop = asyncio.create_task(room.stop())
    await settle()
    assert heater.calls == [("duty", 30, CONTROL_STEP, START)]
    gate.set()
    await asyncio.gather(step, stop)

    assert heater.calls == [("duty", 30, CONTROL_STEP, START), HOLD_FALLBACK]
    assert not heater.fire_edge()


async def test_all_heaters_unavailable_freezes_integrator() -> None:
    """With no heater reachable the integrator does not move and nothing is sent."""
    heaters = [FakeFloorHeater(available=False), FakeFloorHeater(available=False)]
    room = make_floor_room(heaters, integral=RESTORED_INTEGRAL)
    await room.set_target(TARGET, START)

    for step in range(4):
        now = at(step * 15)
        await room.step(now, fresh(20.0, now))

    assert room.integrator == RESTORED_INTEGRAL
    assert room.control_mode is ControlMode.DIRECT
    assert all(heater.calls == [] for heater in heaters)
    assert room.device_writes == 0


async def test_off_edge_is_counted_as_device_writes() -> None:
    """A timed off edge written outside a step counts like a step's write."""
    heater = FakeFloorHeater()
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)
    await room.step(START, fresh(20.0, START))
    writes = room.device_writes

    assert heater.fire_edge()

    assert room.device_writes == writes + 1


async def test_hold_fallback_writes_every_heater_at_startup() -> None:
    """A starting room holds the fallback before it has a reading or a target."""
    heaters = [FakeFloorHeater(), FakeFloorHeater(available=False)]
    room = make_floor_room(heaters)

    await room.hold_fallback()

    assert heaters[0].calls == [HOLD_FALLBACK]
    assert room.device_writes == 1
    assert room.control_mode is ControlMode.WAITING


async def test_hold_fallback_in_a_trv_room_writes_nothing() -> None:
    """TRV rooms have no fallback floor setpoint."""
    trv = FakeTrv(mode=TrvMode.DIRECT)
    room = make_room([trv])

    await room.hold_fallback()

    assert trv.calls == []
    assert room.device_writes == 0


async def test_failed_heater_writes_are_not_counted() -> None:
    """Writes a heater did not send add nothing."""
    heater = FakeFloorHeater(sends=False)
    room = make_floor_room([heater])
    await room.set_target(TARGET, START)

    await room.step(START, fresh(20.0, START))
    heater.fire_edge()
    await room.step(at(15), [])

    assert room.device_writes == 0


async def test_room_with_trvs_and_heaters_is_rejected() -> None:
    """A room is driven by TRVs or by floor heaters, never both."""
    with pytest.raises(ValueError, match="both"):
        Room(
            SETTINGS,
            [FakeTrv()],
            PIController(GAINS),
            START,
            name="bathroom",
            heaters=[FakeFloorHeater()],
        )
