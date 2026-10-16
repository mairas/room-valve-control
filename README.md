# room-valve-control

A Home Assistant custom component that controls radiator valves room by room. Each room gets one `climate` entity; a PI controller reads the room's wall temperature sensor and sets the valve position on the room's thermostatic radiator valves (TRVs) directly. When the wall sensor stops reporting, the TRVs are handed back to their own regulation with a temperature offset kept current while the sensor was live. Disabling a room's thermostat also hands its TRVs back. Nothing else does: a Home Assistant stop or restart, a reload after a room is added or changed, a room removed, a TRV taken out of its room, and the integration disabled or deleted all leave the TRVs as they are, in valve-position mode at their last position if they were under control.

Supported TRVs: Eurotronic Spirit Z-Wave Plus, through the Z-Wave JS integration.

**Status:** under development. The current release only installs the integration; it does not create room entities or control valves yet. The plan is in [issue 1](https://github.com/mairas/room-valve-control/issues/1).

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
