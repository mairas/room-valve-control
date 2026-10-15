"""A room entry over live fake Spirit TRVs that apply and record their writes."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, override

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    PRESET_NONE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigSubentryDataWithId
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_fire_time_changed,
    mock_integration,
    mock_platform,
)

from custom_components.room_valve_control.config_flow import DEFAULT_ROOM
from custom_components.room_valve_control.const import (
    CONF_SENSORS,
    CONF_TRVS,
    DEFAULT_CONTROL_STEP,
    DOMAIN,
    SUBENTRY_TYPE_ROOM,
)
from custom_components.room_valve_control.spirit import DIRECT_MODE_PRESET

from .helpers import (
    HOME_ID,
    ZWAVE_JS,
    Spirit,
    add_offset_entity,
    add_valve_entity,
    value_unique_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import timedelta

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

COMMAND_CLASS_THERMOSTAT_MODE: Final = 64
ROOM_ID: Final = "living_room_subentry"
ROOM_TITLE: Final = "Living room"
ROOM_CLIMATE: Final = "climate.living_room"
WALL_SENSOR: Final = "sensor.living_room_temperature"
TRV_OWN_TEMPERATURE: Final = 20.5
OFFSET_TENTHS_PER_DEGREE: Final = 10
TRV_SETPOINT: Final = 21.0
# Event loop iterations async_wait_for allows; far more than any write needs.
WAIT_ITERATIONS: Final = 1000

# (service, entity id, value) of a write that reached a TRV.
type TrvCall = tuple[str, str, object]


@dataclass(frozen=True)
class TrvIds:
    """The entity ids of one fake Spirit."""

    node_id: int
    climate: str
    valve: str
    offset: str


def trv_ids(node_id: int, name: str) -> TrvIds:
    """Return the entity ids zwave_js would give a Spirit named name."""
    return TrvIds(
        node_id,
        f"climate.{name}",
        f"number.{name}_valve_control",
        f"number.{name}_temperature_offset",
    )


TRVS: Final = (trv_ids(11, "living_room_trv_1"), trv_ids(12, "living_room_trv_2"))


class FakeSpiritClimate(ClimateEntity):
    """A Spirit's zwave_js climate entity that applies each write at once."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]  # noqa: RUF012
    _attr_preset_modes = [PRESET_NONE, DIRECT_MODE_PRESET]  # noqa: RUF012
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_should_poll = False

    def __init__(self, ids: TrvIds, calls: list[TrvCall]) -> None:
        """Create the TRV in heat mode at TRV_SETPOINT."""
        self._calls = calls
        # While set, heat mode writes hang until it is.
        self.stall: asyncio.Event | None = None
        # While set, writes to the valve number hang until it is.
        self.stall_valve: asyncio.Event | None = None
        self.entity_id = ids.climate
        self._attr_unique_id = value_unique_id(
            ids.node_id, COMMAND_CLASS_THERMOSTAT_MODE, "mode"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(ZWAVE_JS, f"{HOME_ID}-{ids.node_id}")}
        )
        self._attr_hvac_mode = HVACMode.HEAT
        self._attr_preset_mode = PRESET_NONE
        self._attr_target_temperature = TRV_SETPOINT
        self._offset_id = ids.offset

    @property
    @override
    def current_temperature(self) -> float:
        """Return the TRV's own reading plus its parameter 8 offset."""
        offset = self.hass.states.get(self._offset_id)
        tenths = float(offset.state) if offset else 0.0
        return TRV_OWN_TEMPERATURE + tenths / OFFSET_TENTHS_PER_DEGREE

    @override
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._calls.append(("set_preset_mode", self.entity_id, preset_mode))
        self._attr_preset_mode = preset_mode
        self.async_write_ha_state()

    @override
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._calls.append(("set_hvac_mode", self.entity_id, hvac_mode))
        if self.stall is not None:
            await self.stall.wait()
        self._attr_hvac_mode = hvac_mode
        self._attr_preset_mode = PRESET_NONE
        self.async_write_ha_state()

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs[ATTR_TEMPERATURE]
        self._calls.append(("set_temperature", self.entity_id, temperature))
        self._attr_target_temperature = temperature
        self.async_write_ha_state()


async def async_setup_spirits(
    hass: HomeAssistant,
    calls: list[TrvCall],
    *,
    offset_disabled_by: er.RegistryEntryDisabler | None = None,
) -> list[FakeSpiritClimate]:
    """
    Set up a fake zwave_js with the TRVS, their numbers and services.

    Also registers WALL_SENSOR, before any test sets its state: a state already
    there would make the registry pick another entity id.
    """
    register_sensor(hass, WALL_SENSOR)
    entities = [FakeSpiritClimate(ids, calls) for ids in TRVS]

    async def setup_climate(
        _hass: HomeAssistant, _entry: ConfigEntry, add_entities: AddEntitiesCallback
    ) -> None:
        add_entities(entities)

    async def setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, ["climate"])
        return True

    mock_integration(hass, MockModule(ZWAVE_JS, async_setup_entry=setup_entry))
    mock_platform(hass, f"{ZWAVE_JS}.config_flow")
    mock_platform(
        hass, f"{ZWAVE_JS}.climate", MockPlatform(async_setup_entry=setup_climate)
    )
    zwave_entry = MockConfigEntry(domain=ZWAVE_JS)
    zwave_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zwave_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    devices = dr.async_get(hass)
    offsets = {}
    by_valve = {ids.valve: entity for ids, entity in zip(TRVS, entities, strict=True)}
    for ids in TRVS:
        device = devices.async_get_device_by_identifier(
            (ZWAVE_JS, f"{HOME_ID}-{ids.node_id}"), zwave_entry.entry_id
        )
        assert device is not None
        spirit = Spirit(ids.node_id, device.id, er.async_get(hass))
        add_valve_entity(spirit, ids.valve)
        add_offset_entity(spirit, ids.offset, disabled_by=offset_disabled_by)
        hass.states.async_set(ids.valve, "0")
        hass.states.async_set(ids.offset, "0")
        offsets[ids.climate] = ids.offset

    async def set_value(call: ServiceCall) -> None:
        entity_id = call.data[ATTR_ENTITY_ID]
        calls.append(("set_value", entity_id, call.data["value"]))
        trv = by_valve.get(entity_id)
        if trv is not None and trv.stall_valve is not None:
            await trv.stall_valve.wait()
        hass.states.async_set(entity_id, str(float(call.data["value"])))

    async def set_config_parameter(call: ServiceCall) -> None:
        climate_id = call.data[ATTR_ENTITY_ID]
        calls.append(("set_config_parameter", climate_id, call.data["value"]))
        hass.states.async_set(offsets[climate_id], str(float(call.data["value"])))

    hass.services.async_register("number", "set_value", set_value)
    hass.services.async_register(ZWAVE_JS, "set_config_parameter", set_config_parameter)
    return entities


def register_sensor(hass: HomeAssistant, entity_id: str) -> str:
    """Register a wall sensor under entity_id and return its registry id."""
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "test", entity_id, suggested_object_id=entity_id.split(".", 1)[1]
    )
    assert entry.entity_id == entity_id
    return entry.id


def registry_ids(hass: HomeAssistant, entity_ids: Sequence[str]) -> list[str]:
    """Return the registry ids of registered entities, as a room stores them."""
    registry = er.async_get(hass)
    entries = [registry.async_get(entity_id) for entity_id in entity_ids]
    assert None not in entries
    return [entry.id for entry in entries if entry is not None]


async def async_setup_room(
    hass: HomeAssistant,
    *,
    sensors: Sequence[str] | None = None,
    trvs: Sequence[str] | None = None,
) -> MockConfigEntry:
    """
    Add and set up the entry with the living room.

    sensors and trvs are the registry ids the room stores, by default those of
    WALL_SENSOR and the TRVS.
    """
    if sensors is None:
        sensors = registry_ids(hass, [WALL_SENSOR])
    if trvs is None:
        trvs = registry_ids(hass, [ids.climate for ids in TRVS])
    entry = MockConfigEntry(
        domain=DOMAIN,
        subentries_data=[
            ConfigSubentryDataWithId(
                data=DEFAULT_ROOM
                | {
                    CONF_SENSORS: list(sensors),
                    CONF_TRVS: list(trvs),
                },
                subentry_id=ROOM_ID,
                subentry_type=SUBENTRY_TYPE_ROOM,
                title=ROOM_TITLE,
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def async_set_room_target(hass: HomeAssistant, target: float) -> None:
    """Set the room thermostat's target through the climate service."""
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ROOM_CLIMATE, ATTR_TEMPERATURE: target},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def async_advance(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    duration: timedelta = DEFAULT_CONTROL_STEP,
) -> None:
    """Move time on by duration and run what came due."""
    freezer.tick(duration)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def async_wait_for(condition: Callable[[], bool]) -> None:
    """Run the event loop until condition holds, without waiting on stalled tasks."""
    for _ in range(WAIT_ITERATIONS):
        if condition():
            return
        await asyncio.sleep(0)
    msg = "condition never held"
    raise AssertionError(msg)
