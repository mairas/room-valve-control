# room-valve-control

A Home Assistant custom component that controls radiator valves, or electric floor heating (see [Floor heating](#floor-heating)), room by room. Each room gets one `climate` entity; a PI controller reads the room's wall temperature sensor and sets the valve position on the room's thermostatic radiator valves (TRVs) directly. When the wall sensor stops reporting, the TRVs are handed back to their own regulation with a temperature offset kept current while the sensor was live. A setpoint set by hand on one of a room's TRVs becomes the room's target at the next control step, and the room's other TRVs follow it. Disabling a room's thermostat also hands its TRVs back. Nothing else does: a Home Assistant stop or restart, a reload after a room is added or changed, a room removed, a TRV taken out of its room, and the integration disabled or deleted all leave the TRVs as they are, in valve-position mode at their last position if they were under control. A TRV that is not usable at setup, because its registry entry is gone or one of its entities is disabled, is left out of its room, and left as it is, with a repair issue; the rest of the room and the other rooms are set up, and a room left with no usable TRV is not.

Supported devices:

- TRVs: Eurotronic Spirit Z-Wave Plus, through the Z-Wave JS integration.
- Floor-heating thermostats: any `climate` entity with a heat mode and settable setpoint, written for the Moes BHT-002 (Zigbee, through Zigbee2MQTT).

**Status:** under development, with no release published yet; build the zip with `./run archive` (see [Development](#development)). The plan is in [issue 1](https://github.com/mairas/room-valve-control/issues/1).

## Floor heating

A room is heated either by TRVs or by electric floor heating, never both. A floor room's PI output becomes the share of each control step its floor heaters heat: at 40 % and a 30 min control step, the floor heats for 12 min and then rests for 18. A 30 min control step suits floor heating; every direct step starts a new period. The thermostat is switched by its setpoint alone: "on" writes the room's floor maximum (default 26 °C) and "off" its floor minimum (default 17 °C). Because "on" is a floor setpoint, the thermostat itself cuts the heat when the floor reaches the maximum, whether or not Home Assistant is still running to write "off".

Each thermostat has to regulate on its floor probe alone. On the Moes BHT-002 that is the external sensor setting "OU"; in "IN" or "AL" it would switch on its air reading, which in a thermostat mounted outside the room means nothing. The component puts the thermostat in heat mode and, where the entity offers it, the hold preset before each write, since the program preset's weekly schedule would overwrite the setpoint. It cannot see the sensor setting, so that is configuration to get right on the device.

Without the room, the thermostat holds the room's fallback floor setpoint (default 22 °C) on its own. It is written when the wall sensor goes stale, on every step while it stays stale, when the room's thermostat is disabled, when the integration reloads, is disabled or is deleted, and when the room starts, before its first step, since a thermostat may still be on from before. In fallback the room's target is not used. A Home Assistant stop writes nothing: a thermostat left on holds at most the floor maximum until the next start writes the fallback setpoint.

The floor minimum is a floor-temperature minimum in every state: at 0 % output the thermostat still heats a floor colder than it. An on-time or off-time shorter than the minimum on-time (default 5 min, at most half the control step) is dropped from its period, which is then all off or all on. At low demand this gives a limit cycle: the output stays below the minimum share, the integral term rises over several steps until one period heats, and the room cools again. Setpoint presses on a floor thermostat are not taken as the room's target, and a floor heater that is not usable at setup is left out with a repair issue, as a TRV is.

## Installation

1. Download `room_valve_control.zip` from the [latest release](https://github.com/mairas/room-valve-control/releases/latest).
2. Extract it into your Home Assistant configuration directory, so that the files end up in `custom_components/room_valve_control/`.
3. Restart Home Assistant.
4. Add the integration under **Settings → Devices & services → Add integration → Room Valve Control**.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and a `run` script for the common tasks:

```bash
./run sync    # create the virtualenv with the dev dependencies
./run help    # list all tasks
```

`./run lint`, `./run typecheck` and `./run test` are what CI runs. `./run archive` builds the release zip into `dist/`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
