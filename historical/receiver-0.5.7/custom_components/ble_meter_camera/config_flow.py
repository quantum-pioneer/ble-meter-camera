"""Manual camera + dedicated proxy configuration; setup performs no BLE traffic."""
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
import re

from .const import DEFAULTS, DOMAIN
from .protocol import FRAME_SIZES, capture_light_duty

MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def schema(existing, advanced=False):
    fields = {
        vol.Required("address", default=existing.get("address", "")): str,
        vol.Required("proxy_source", default=existing.get("proxy_source", "")): str,
    }
    if advanced:
        validators = {
            "interval": vol.All(vol.Coerce(int), vol.In({900: "Every 15 minutes", 1800: "Every 30 minutes", 3600: "Hourly"})),
            "frame_size": vol.All(vol.Coerce(int), vol.In({code: f"{size[0]}x{size[1]} (code {code})" for code, size in FRAME_SIZES.items()})),
            "quality": vol.All(vol.Coerce(int), vol.Range(min=10, max=40)),
            "capture_light_percent": vol.All(int, vol.Range(min=0, max=25)),
            "max_bytes": vol.All(vol.Coerce(int), vol.Range(min=256, max=131072)),
            "chunk_pause": vol.All(vol.Coerce(float), vol.Range(min=0.02, max=1)),
            "history_days": vol.All(vol.Coerce(int), vol.Range(min=7, max=7)),
            "history_max_bytes": vol.All(vol.Coerce(int), vol.Range(min=131072, max=128 * 1024 * 1024)),
        }
        for key, validator in validators.items():
            fields[vol.Optional(key, default=existing.get(key, DEFAULTS[key]))] = validator
    return vol.Schema(fields)


def normalize(user_input):
    if any(not MAC.fullmatch(user_input[key]) for key in ("address", "proxy_source")):
        raise ValueError("invalid_mac")
    if "capture_light_percent" in user_input:
        try:
            capture_light_duty(user_input["capture_light_percent"])
        except ValueError as err:
            raise ValueError("invalid_capture_light") from err
    return {**user_input, "address": user_input["address"].upper(),
            "proxy_source": user_input["proxy_source"].upper()}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data = normalize(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                await self.async_set_unique_id(data["address"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="BLE meter camera", data={**DEFAULTS, **data})
        return self.async_show_form(step_id="user", data_schema=schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data = normalize(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                if data["address"] != self.config_entry.data["address"]:
                    errors["base"] = "address_immutable"
                else:
                    return self.async_create_entry(data=data)
        existing = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=schema(existing, True), errors=errors)
