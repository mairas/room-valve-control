"""Fixtures for the Room Valve Control tests."""

from typing import TYPE_CHECKING

import pytest
from homeassistant.config_entries import ConfigFlow
from pytest_homeassistant_custom_component.common import mock_config_flow

from custom_components.room_valve_control import runtime

from .helpers import ZWAVE_JS
from .live_room import TrvCall, async_setup_spirits

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration under test."""


class FixedRandom:
    """A random source that returns value, which a test may change."""

    def __init__(self) -> None:
        """Start at 0: a full control step to the first periodic step."""
        self.value = 0.0

    def random(self) -> float:
        """Return value."""
        return self.value


@pytest.fixture(autouse=True)
def step_random(monkeypatch: pytest.MonkeyPatch) -> FixedRandom:
    """Fix the randomness of the step schedule, so steps come when tests expect."""
    fixed = FixedRandom()
    monkeypatch.setattr(runtime, "step_random", fixed)
    return fixed


class FakeZwaveJsFlow(ConfigFlow):
    """Lets a mocked zwave_js entry set up; never shown."""

    VERSION = 1


@pytest.fixture
def zwave_js_flow() -> Iterator[None]:
    """Register a config flow for the fake zwave_js."""
    with mock_config_flow(ZWAVE_JS, FakeZwaveJsFlow):
        yield


@pytest.fixture
async def calls(hass: HomeAssistant, zwave_js_flow: None) -> list[TrvCall]:  # noqa: ARG001
    """Set up the fake TRVs and return the writes they receive, in order."""
    recorded: list[TrvCall] = []
    await async_setup_spirits(hass, recorded)
    return recorded
