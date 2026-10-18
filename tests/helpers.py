"""Registry entries shaped like zwave_js's for a Eurotronic Spirit TRV."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

ZWAVE_JS: Final = "zwave_js"
HOME_ID: Final = 3245146787
COMMAND_CLASS_THERMOSTAT_MODE: Final = 64
COMMAND_CLASS_SWITCH_MULTILEVEL: Final = 38
COMMAND_CLASS_CONFIGURATION: Final = 112


def value_unique_id(node_id: int, command_class: int, property_: str) -> str:
    """Return the unique id zwave_js gives an entity for a Z-Wave value."""
    return f"{HOME_ID}.{node_id}-{command_class}-0-{property_}"


@dataclass
class Spirit:
    """Registry entries of one mocked Spirit TRV."""

    node_id: int
    device_id: str
    entity_registry: er.EntityRegistry


def add_entity(
    spirit: Spirit,
    domain: str,
    unique_id: str,
    entity_id: str,
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> None:
    """Register a zwave_js entity on the Spirit's device under entity_id."""
    entry = spirit.entity_registry.async_get_or_create(
        domain,
        ZWAVE_JS,
        unique_id,
        device_id=spirit.device_id,
        suggested_object_id=entity_id.split(".", 1)[1],
        disabled_by=disabled_by,
    )
    assert entry.entity_id == entity_id


def add_spirit_device(hass: HomeAssistant, node_id: int, climate_id: str) -> Spirit:
    """Register a Spirit device with only its climate entity."""
    config_entry = MockConfigEntry(domain=ZWAVE_JS)
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(ZWAVE_JS, f"{HOME_ID}-{node_id}")},
        manufacturer="Eurotronic",
        model="Spirit Z-Wave Plus",
    )
    spirit = Spirit(node_id, device.id, er.async_get(hass))
    add_entity(
        spirit,
        "climate",
        value_unique_id(node_id, COMMAND_CLASS_THERMOSTAT_MODE, "mode"),
        climate_id,
    )
    return spirit


def add_valve_entity(spirit: Spirit, entity_id: str) -> None:
    """Register the Spirit's valve position number."""
    add_entity(
        spirit,
        "number",
        value_unique_id(
            spirit.node_id, COMMAND_CLASS_SWITCH_MULTILEVEL, "currentValue"
        ),
        entity_id,
    )


def add_offset_entity(
    spirit: Spirit,
    entity_id: str,
    disabled_by: er.RegistryEntryDisabler | None = None,
) -> None:
    """Register the Spirit's parameter 8 (temperature offset) number."""
    add_entity(
        spirit,
        "number",
        value_unique_id(spirit.node_id, COMMAND_CLASS_CONFIGURATION, "8"),
        entity_id,
        disabled_by=disabled_by,
    )
