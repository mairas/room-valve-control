# room-valve-control

A Home Assistant custom component that controls radiator valves room by room. Each room gets one `climate` entity; a PI controller reads the room's wall temperature sensor and sets the valve position on the room's thermostatic radiator valves (TRVs) directly. When the wall sensor stops reporting, or when the component stops, the TRVs are handed back to their own regulation with a temperature offset kept current while the sensor was live.

Supported TRVs: Eurotronic Spirit Z-Wave Plus, through the Z-Wave JS integration.

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
