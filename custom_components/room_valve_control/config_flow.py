"""Config flow for the Room Valve Control integration."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class RoomValveControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single Room Valve Control entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(title="Room Valve Control", data={})
