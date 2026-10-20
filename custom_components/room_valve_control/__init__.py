"""Per-room PI control of radiator valves or floor heating from a wall sensor."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONF_FLOOR_HEATERS,
    CONF_FLOOR_MAX,
    CONF_FLOOR_MIN,
    CONF_MIN_ON_TIME,
    CONF_TRVS,
    DEFAULT_FLOOR_MAX,
    DEFAULT_FLOOR_MIN,
    DEFAULT_MIN_ON_TIME,
    DOMAIN,
    ISSUE_ROOM_WITHOUT_DEVICES,
    ISSUE_UNUSABLE_FLOOR_HEATER,
    ISSUE_UNUSABLE_TRV,
    SUBENTRY_TYPE_ROOM,
)
from .floor_heater import ClimateFloorHeater, FloorHeaterEntityError
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
    Set up a room for every room subentry with a usable TRV or floor heater.

    A device that is not usable is skipped, and left as it is, with a repair
    issue; the rest of its room, and the other rooms, are set up.
    """
    previous = hass.data.setdefault(UNLOADED_TRVS, {}).pop(entry.entry_id, {})
    issues: set[str] = set()
    rooms: dict[str, RoomRuntime] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_ROOM:
            continue
        trvs = _usable_trvs(hass, subentry, previous, issues)
        heaters = _usable_heaters(hass, entry, subentry, issues)
        if trvs or heaters:
            rooms[subentry.subentry_id] = RoomRuntime(
                hass, entry, subentry, trvs, heaters
            )
            continue
        _LOGGER.warning(
            "%s: no usable TRV or floor heater; the room is not set up",
            subentry.title,
        )
        issue_id = f"{ISSUE_ROOM_WITHOUT_DEVICES}_{subentry.subentry_id}"
        _create_issue(hass, issue_id, ISSUE_ROOM_WITHOUT_DEVICES, room=subentry.title)
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
            issues.add(_skip(hass, subentry, ISSUE_UNUSABLE_TRV, registry_id))
            continue
        try:
            # Built even for a reused adapter: building checks the entities.
            fresh = SpiritTrv(hass, resolved[0])
        except SpiritEntityError as err:
            issues.add(
                _skip(
                    hass,
                    subentry,
                    ISSUE_UNUSABLE_TRV,
                    registry_id,
                    device=resolved[0],
                    error=err,
                )
            )
            continue
        trvs[registry_id] = previous.get(registry_id, fresh)
    return trvs


def _usable_heaters(
    hass: HomeAssistant,
    entry: RoomValveControlConfigEntry,
    subentry: ConfigSubentry,
    issues: set[str],
) -> dict[str, ClimateFloorHeater]:
    """
    Return the room's usable floor heaters by registry id, as new adapters.

    Nothing carries over from a previous adapter: stopping it dropped its
    pending edge and wrote the fallback setpoint.
    """
    data = subentry.data
    subentry_id = subentry.subentry_id

    @callback
    def edge_written(sent: int) -> None:
        # The room is set up after its heaters, and stopping it cancels their
        # edges, so it is there whenever one is written.
        entry.runtime_data[subentry_id].async_edge_written(sent)

    heaters: dict[str, ClimateFloorHeater] = {}
    for registry_id in data.get(CONF_FLOOR_HEATERS, []):
        resolved, _ = entity_ids(hass, [registry_id])
        if not resolved:
            issues.add(_skip(hass, subentry, ISSUE_UNUSABLE_FLOOR_HEATER, registry_id))
            continue
        try:
            heaters[registry_id] = ClimateFloorHeater(
                hass,
                resolved[0],
                on_setpoint=data.get(CONF_FLOOR_MAX, DEFAULT_FLOOR_MAX),
                off_setpoint=data.get(CONF_FLOOR_MIN, DEFAULT_FLOOR_MIN),
                min_on_time=timedelta(
                    seconds=data.get(
                        CONF_MIN_ON_TIME, DEFAULT_MIN_ON_TIME.total_seconds()
                    )
                ),
                on_edge_written=edge_written,
            )
        except FloorHeaterEntityError as err:
            issues.add(
                _skip(
                    hass,
                    subentry,
                    ISSUE_UNUSABLE_FLOOR_HEATER,
                    registry_id,
                    device=resolved[0],
                    error=err,
                )
            )
    return heaters


def _skip(  # noqa: PLR0913
    hass: HomeAssistant,
    subentry: ConfigSubentry,
    issue_key: str,
    registry_id: str,
    *,
    device: str | None = None,
    error: Exception | None = None,
) -> str:
    """
    Log the skipped device and raise its repair issue; return the issue id.

    Without a device entity id or an error, its registry entry is gone.
    """
    reason = (
        "its entity registry entry no longer exists" if error is None else str(error)
    )
    device = registry_id if device is None else device
    _LOGGER.warning("%s: %s skipped: %s", subentry.title, device, reason)
    issue_id = f"{issue_key}_{registry_id}"
    _create_issue(
        hass, issue_id, issue_key, room=subentry.title, device=device, reason=reason
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
    Stop the rooms, then unload the platforms.

    Whether a reload, a disable or a delete, nothing is written to the TRVs,
    and the floor heaters hold their fallback setpoint. A disabled entry
    controls nothing, so its repair issues go.
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
