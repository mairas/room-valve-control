"""Per-room PI control of radiator valves from a wall temperature sensor."""

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONF_TRVS,
    DOMAIN,
    ISSUE_ROOM_WITHOUT_TRVS,
    ISSUE_UNUSABLE_TRV,
    SUBENTRY_TYPE_ROOM,
)
from .registry import entity_ids
from .runtime import RoomRuntime, RoomValveControlConfigEntry
from .spirit import SpiritEntityError, SpiritTrv

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final = [Platform.CLIMATE, Platform.SENSOR]

# The TRV adapters of an unloaded entry by entry id, then by registry id, for
# its next setup to reuse.
UNLOADED_TRVS: Final[HassKey[dict[str, dict[str, SpiritTrv]]]] = HassKey(
    f"{DOMAIN}_unloaded_trvs"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """
    Set up a room for every room subentry with a usable TRV.

    A TRV that is not usable is skipped, and left as it is, with a repair issue;
    the rest of its room, and the other rooms, are set up.
    """
    previous = hass.data.setdefault(UNLOADED_TRVS, {}).pop(entry.entry_id, {})
    issues: set[str] = set()
    rooms: dict[str, RoomRuntime] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        trvs = _usable_trvs(hass, subentry, previous, issues)
        if trvs:
            rooms[subentry.subentry_id] = RoomRuntime(hass, entry, subentry, trvs)
            continue
        _LOGGER.warning("%s: no usable TRV; the room is not set up", subentry.title)
        issue_id = f"{ISSUE_ROOM_WITHOUT_TRVS}_{subentry.subentry_id}"
        _create_issue(hass, issue_id, ISSUE_ROOM_WITHOUT_TRVS, room=subentry.title)
        issues.add(issue_id)
    _delete_issues_except(hass, issues)
    entry.runtime_data = rooms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # HA has no per-subentry reload, so adding, changing or removing a room
    # reloads the whole entry.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _usable_trvs(
    hass: HomeAssistant,
    subentry: ConfigSubentry,
    previous: Mapping[str, SpiritTrv],
    issues: set[str],
) -> dict[str, SpiritTrv]:
    """
    Return the room's usable TRVs by registry id, reusing previous adapters.

    The previous adapter also knows whether its last offset write has reached
    the reported temperature yet; a new one would subtract the offset from a
    report that does not include it.
    """
    trvs: dict[str, SpiritTrv] = {}
    for registry_id in subentry.data[CONF_TRVS]:
        resolved, _ = entity_ids(hass, [registry_id])
        if not resolved:
            reason = "its entity registry entry no longer exists"
            issues.add(_skip_trv(hass, subentry, registry_id, registry_id, reason))
            continue
        try:
            # Built even for a reused adapter: building checks the entities.
            fresh = SpiritTrv(hass, resolved[0])
        except SpiritEntityError as err:
            issues.add(_skip_trv(hass, subentry, registry_id, resolved[0], str(err)))
            continue
        trvs[registry_id] = previous.get(registry_id, fresh)
    return trvs


def _skip_trv(
    hass: HomeAssistant,
    subentry: ConfigSubentry,
    registry_id: str,
    trv: str,
    reason: str,
) -> str:
    """Log the skipped TRV and raise its repair issue; return the issue id."""
    _LOGGER.warning("%s: TRV %s skipped: %s", subentry.title, trv, reason)
    issue_id = f"{ISSUE_UNUSABLE_TRV}_{registry_id}"
    _create_issue(
        hass, issue_id, ISSUE_UNUSABLE_TRV, room=subentry.title, trv=trv, reason=reason
    )
    return issue_id


def _create_issue(
    hass: HomeAssistant, issue_id: str, translation_key: str, **placeholders: str
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=translation_key,
        translation_placeholders=placeholders,
    )


def _delete_issues_except(hass: HomeAssistant, keep: set[str]) -> None:
    """Delete the integration's repair issues not in keep: those since fixed."""
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id not in keep:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> bool:
    """
    Stop the rooms, leaving every TRV as it is, then unload the platforms.

    Whether a reload, a disable or a delete, nothing is written to the TRVs.
    A disabled entry controls nothing, so its repair issues go.
    """
    if entry.disabled_by is not None:
        _delete_issues_except(hass, set())
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
    """Drop what the deleted entry leaves behind: its adapters and issues."""
    hass.data.get(UNLOADED_TRVS, {}).pop(entry.entry_id, None)
    _delete_issues_except(hass, set())


async def _async_reload_entry(
    hass: HomeAssistant, entry: RoomValveControlConfigEntry
) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)
