"""The room thermostat: its target drives the room's control."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self, override

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .runtime import RoomRuntime, RoomValveControlConfigEntry

# The calendar writes targets in tenths; any finer value is still kept exactly.
TARGET_TEMPERATURE_STEP: Final = 0.1

_TARGET_KEY: Final = "target"
_CONTROLLER_KEY: Final = "controller"
_DEVICE_WRITES_KEY: Final = "device_writes"


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: RoomValveControlConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a thermostat for every room."""
    for subentry_id, runtime in entry.runtime_data.items():
        async_add_entities([RoomClimate(runtime)], config_subentry_id=subentry_id)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True)
class RoomStoredData(ExtraStoredData):
    """The room state kept across restarts."""

    target: float | None
    controller: Mapping[str, object] | None
    device_writes: int

    @override
    def as_dict(self) -> dict[str, Any]:
        return {
            _TARGET_KEY: self.target,
            _CONTROLLER_KEY: self.controller,
            _DEVICE_WRITES_KEY: self.device_writes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        """Read stored data, dropping values of the wrong type."""
        controller = data.get(_CONTROLLER_KEY)
        writes = data.get(_DEVICE_WRITES_KEY)
        return cls(
            target=_number(data.get(_TARGET_KEY)),
            controller=controller if isinstance(controller, Mapping) else None,
            device_writes=(
                writes
                if isinstance(writes, int) and not isinstance(writes, bool)
                else 0
            ),
        )


class RoomClimate(ClimateEntity, RestoreEntity):
    """A heat-only thermostat whose target is the room's target."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_hvac_modes = [HVACMode.HEAT]  # noqa: RUF012
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_target_temperature_step = TARGET_TEMPERATURE_STEP

    def __init__(self, runtime: RoomRuntime) -> None:
        """Create the thermostat of a room."""
        self._runtime = runtime
        self._attr_unique_id = runtime.subentry_id
        self._attr_device_info = runtime.device_info
        self._attr_min_temp = runtime.min_target
        self._attr_max_temp = runtime.max_target

    @property
    @override
    def current_temperature(self) -> float | None:
        return self._runtime.current_temperature

    @property
    @override
    def target_temperature(self) -> float | None:
        room = self._runtime.room
        return None if room is None else room.target

    @property
    @override
    def extra_restore_state_data(self) -> RoomStoredData | None:
        room = self._runtime.room
        controller = self._runtime.controller
        if room is None or controller is None:
            return None
        return RoomStoredData(
            target=room.target,
            controller=controller.as_dict(),
            device_writes=room.device_writes,
        )

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.add_listener(self.async_write_ha_state))
        if self._runtime.room is not None:
            # Re-added under a new entity id: the room never stopped.
            return
        extra = await self.async_get_last_extra_data()
        stored = RoomStoredData.from_dict(extra.as_dict() if extra else {})
        await self._runtime.async_start(
            target=stored.target,
            controller_state=stored.controller,
            device_writes=stored.device_writes,
        )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Hand the room back unless the entity is only getting a new entity id."""
        entry = self.registry_entry
        renamed = (
            entry is not None
            and not entry.disabled
            and entry.entity_id != self.entity_id
        )
        if not renamed:
            await self._runtime.async_hand_back()
        await super().async_will_remove_from_hass()

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Accept heat, the only mode."""

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return
        await self._runtime.async_set_target(float(target))
        self.async_write_ha_state()
