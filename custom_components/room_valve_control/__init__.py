"""Per-room PI control of radiator valves from a wall temperature sensor."""

from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.core import HassJob
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_TRVS, SUBENTRY_TYPE_ROOM
from .registry import entity_ids
from .runtime import (
    RoomRuntime,
    RoomValveControlConfigEntry,
    async_hand_back_all,
    async_hand_back_all_at_stop,
)
from .spirit import SpiritEntityError, SpiritTrv

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: Final = [Platform.CLIMATE, Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """Set up Room Valve Control from a config entry."""
    rooms: dict[str, RoomRuntime] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        trv_ids, missing = entity_ids(hass, subentry.data[CONF_TRVS])
        if missing:
            msg = (
                f"{subentry.title}: TRV {', '.join(missing)} no longer exists; "
                "reconfigure the room"
            )
            raise ConfigEntryNotReady(msg)
        try:
            trvs = [SpiritTrv(hass, trv) for trv in trv_ids]
        except SpiritEntityError as err:
            msg = f"{subentry.title}: {err}"
            raise ConfigEntryNotReady(msg) from err
        rooms[subentry.subentry_id] = RoomRuntime(hass, entry, subentry, trvs)
    entry.runtime_data = rooms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # A shutdown job rather than a stop event listener: zwave_js disconnects on
    # the stop event, and shutdown jobs finish before it fires.
    entry.async_on_unload(
        hass.async_add_shutdown_job(
            HassJob(async_hand_back_all_at_stop, "room_valve_control hand back"),
            rooms.values(),
        )
    )
    # HA has no per-subentry reload, so adding, changing or removing a room
    # reloads the whole entry.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """Hand the TRVs back, then unload the entry's platforms."""
    # Before the platforms, so that the thermostats save their state after the
    # hand back; they skip their own hand back while the entry unloads. Without
    # a deadline: unlike at Core stop, nothing disconnects zwave_js meanwhile.
    await async_hand_back_all(entry.runtime_data.values())
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)
