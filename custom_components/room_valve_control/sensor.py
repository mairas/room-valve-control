"""Diagnostic sensors showing what a room's control is doing."""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature

from .room import ControlMode, Room

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .runtime import RoomRuntime, RoomValveControlConfigEntry


@dataclass(frozen=True, kw_only=True)
class RoomSensorDescription(SensorEntityDescription):
    """A room sensor and the room value it shows."""

    value: Callable[[Room], StateType]


SENSORS: Final = (
    RoomSensorDescription(
        key="output",
        translation_key="output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda room: room.last_output,
    ),
    RoomSensorDescription(
        key="integrator",
        translation_key="integrator",
        suggested_display_precision=1,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda room: room.integrator,
    ),
    RoomSensorDescription(
        key="error",
        translation_key="error",
        suggested_display_precision=1,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda room: room.last_error,
    ),
    RoomSensorDescription(
        key="control_mode",
        translation_key="control_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[mode.value for mode in ControlMode],
        value=lambda room: room.control_mode.value,
    ),
    RoomSensorDescription(
        key="device_writes",
        translation_key="device_writes",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda room: room.device_writes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: RoomValveControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the diagnostic sensors of every room."""
    for subentry_id, runtime in entry.runtime_data.items():
        async_add_entities(
            [
                RoomSensor(runtime, _for_room(runtime, description))
                for description in SENSORS
            ],
            config_subentry_id=subentry_id,
        )


def _for_room(
    runtime: RoomRuntime, description: RoomSensorDescription
) -> RoomSensorDescription:
    """Name a floor room's output for what it drives, keeping its unique id."""
    if description.key == "output" and runtime.heats_floor:
        return replace(description, translation_key="duty_cycle")
    return description


class RoomSensor(SensorEntity):
    """One diagnostic value of a room, updated after every step."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    entity_description: RoomSensorDescription

    def __init__(
        self, runtime: RoomRuntime, description: RoomSensorDescription
    ) -> None:
        """Create the sensor for one of the room's values."""
        self._runtime = runtime
        self.entity_description = description
        self._attr_unique_id = f"{runtime.subentry_id}_{description.key}"
        self._attr_device_info = runtime.device_info

    @property
    @override
    def native_value(self) -> StateType:
        room = self._runtime.room
        return None if room is None else self.entity_description.value(room)

    @override
    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._runtime.add_listener(self.async_write_ha_state))
