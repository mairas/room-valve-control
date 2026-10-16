"""Per-room PI control of radiator valves from a wall temperature sensor."""

import asyncio
from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util.hass_dict import HassKey

from .const import CONF_TRVS, DOMAIN, SUBENTRY_TYPE_ROOM
from .registry import entity_ids
from .runtime import RoomRuntime, RoomValveControlConfigEntry
from .spirit import SpiritEntityError, SpiritTrv

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant

PLATFORMS: Final = [Platform.CLIMATE, Platform.SENSOR]

# The TRV adapters of an unloaded entry by entry id, then by registry id, for
# its next setup to reuse.
UNLOADED_TRVS: Final[HassKey[dict[str, dict[str, SpiritTrv]]]] = HassKey(
    f"{DOMAIN}_unloaded_trvs"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """Set up Room Valve Control from a config entry."""
    previous = hass.data.setdefault(UNLOADED_TRVS, {}).pop(entry.entry_id, {})
    rooms: dict[str, RoomRuntime] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        trvs = _trvs(hass, subentry, previous)
        rooms[subentry.subentry_id] = RoomRuntime(hass, entry, subentry, trvs)
    entry.runtime_data = rooms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # HA has no per-subentry reload, so adding, changing or removing a room
    # reloads the whole entry.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _trvs(
    hass: HomeAssistant, subentry: ConfigSubentry, previous: Mapping[str, SpiritTrv]
) -> dict[str, SpiritTrv]:
    """
    Return the room's TRVs by registry id, reusing previous adapters.

    The previous adapter also knows whether its last offset write has reached
    the reported temperature yet; a new one would subtract the offset from a
    report that does not include it.
    """
    registry_ids = subentry.data[CONF_TRVS]
    trv_ids, missing = entity_ids(hass, registry_ids)
    if missing:
        msg = (
            f"{subentry.title}: TRV {', '.join(missing)} no longer exists; "
            "reconfigure the room"
        )
        raise ConfigEntryNotReady(msg)
    trvs: dict[str, SpiritTrv] = {}
    for registry_id, trv in zip(registry_ids, trv_ids, strict=True):
        try:
            # Built even for a reused adapter: building checks the entities.
            fresh = SpiritTrv(hass, trv)
        except SpiritEntityError as err:
            msg = f"{subentry.title}: {err}"
            raise ConfigEntryNotReady(msg) from err
        trvs[registry_id] = previous.get(registry_id, fresh)
    return trvs


async def async_unload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """
    Stop the rooms, leaving every TRV as it is, then unload the platforms.

    Whether a reload, a disable or a delete, nothing is written to the TRVs.
    """
    rooms = entry.runtime_data
    # Before the platforms, so that no step runs while they unload and the
    # thermostats save the state of a stopped room.
    await asyncio.gather(*(room.async_stop() for room in rooms.values()))
    hass.data.setdefault(UNLOADED_TRVS, {})[entry.entry_id] = {
        registry_id: trv
        for room in rooms.values()
        for registry_id, trv in room.trvs.items()
    }
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> None:
    """Drop the deleted entry's adapters."""
    hass.data.get(UNLOADED_TRVS, {}).pop(entry.entry_id, None)


async def _async_reload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)
