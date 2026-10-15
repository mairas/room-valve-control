"""
Entity lookup for room subentries.

A room stores its sensors and TRVs by entity registry id, which survives an
entity being renamed; the entity id is looked up whenever it is needed.
"""

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from collections.abc import Sequence

    from homeassistant.core import HomeAssistant


@callback
def entity_ids(
    hass: HomeAssistant, registry_ids: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Return the entity ids of stored registry ids, and the ids that are gone."""
    entries = er.async_get(hass).entities
    resolved = []
    missing = []
    for registry_id in registry_ids:
        entry = entries.get_entry(registry_id)
        if entry is None:
            missing.append(registry_id)
        else:
            resolved.append(entry.entity_id)
    return resolved, missing
