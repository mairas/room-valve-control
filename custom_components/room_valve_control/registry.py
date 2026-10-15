"""
Entity lookup for room subentries.

A room stores its sensors and TRVs by entity registry id, which survives an
entity being renamed; the entity id is looked up whenever it is needed.
"""

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@callback
def resolve_entity_id(hass: HomeAssistant, registry_id: str) -> str | None:
    """Return the current entity id of a stored registry id, None if removed."""
    entry = er.async_get(hass).entities.get_entry(registry_id)
    return entry.entity_id if entry is not None else None
