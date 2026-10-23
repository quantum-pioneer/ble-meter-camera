"""Manual camera + dedicated proxy configuration; setup performs no BLE traffic."""
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
import re

from .const import DEFAULTS, DOMAIN, CAMERA_MODELS, DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL
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


def reconfigure_schema(existing):
    """Replace only camera identity; the dedicated proxy and policy stay fixed."""
    return vol.Schema({
        vol.Required("address", default=existing["address"]): str,
        vol.Required("camera_model", default=existing.get("camera_model", DEFAULT_CAMERA_MODEL)): vol.In(CAMERA_MODELS),
        vol.Required("capture_light_percent", default=existing.get("capture_light_percent", 0)): vol.All(int, vol.Range(min=0, max=25)),
    })


def reconfigure_guard(runtime, existing):
    """Only a fully stopped, loaded entry can replace its physical camera."""
    if runtime is None or getattr(runtime, "_stopping", True):
        return "receiver_not_loaded"
    if runtime.enabled is not False or runtime.periodic is not False:
        return "collection_must_be_off"
    if any(getattr(runtime, key, None) is not None for key in (
            "_transfer_task", "_maintenance_task", "cleanup_recovery")):
        return "client_must_be_closed"
    receiver = runtime.receiver
    if (receiver.cleanup_error or getattr(runtime, "safety_trip", None)
            or getattr(receiver.transport, "client", ...) is not None):
        return "client_must_be_closed"
    if (runtime.config["proxy_source"].upper() != existing["proxy_source"].upper()
            or runtime.config["address"].upper() != existing["address"].upper()):
        return "entry_changed"
    return None


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

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        data_before, options_before = dict(entry.data), dict(entry.options)
        unique_id_before = entry.unique_id
        existing = {**data_before, **options_before}
        runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        errors = {}
        if user_input is not None:
            error = reconfigure_guard(runtime, existing)
            try:
                if "proxy_source" in user_input:
                    raise ValueError("proxy_immutable")
                values = reconfigure_schema(existing)(user_input)
                address = values["address"]
                if not MAC.fullmatch(address):
                    raise ValueError("invalid_mac")
                values["address"] = address = address.upper()
                try:
                    capture_light_duty(values["capture_light_percent"])
                except ValueError as err:
                    raise ValueError("invalid_capture_light") from err
                if address == existing["proxy_source"].upper():
                    raise ValueError("camera_is_proxy")
                if values["camera_model"] == S3_CAMERA_MODEL and values["capture_light_percent"] != 0:
                    raise ValueError("s3_light_must_be_off")
            except vol.Invalid:
                errors["base"] = error or "invalid_reconfigure"
            except ValueError as err:
                errors["base"] = error or str(err)
            else:
                if error:
                    errors["base"] = error
                else:
                    duplicate = any(other.entry_id != entry.entry_id and address in {
                        str(other.unique_id).upper(), str(other.data.get("address", "")).upper(),
                        str(other.options.get("address", "")).upper()}
                        for other in self._async_current_entries(include_ignore=True))
                    if duplicate:
                        return self.async_abort(reason="already_configured")
                    configured = await self.async_set_unique_id(address)
                    # Recheck after the only await: never overwrite a concurrent
                    # options update or race newly started maintenance/capture.
                    if configured is not None and configured.entry_id != entry.entry_id:
                        return self.async_abort(reason="already_configured")
                    if (dict(entry.data) != data_before or dict(entry.options) != options_before
                            or entry.unique_id != unique_id_before
                            or self.hass.data.get(DOMAIN, {}).get(entry.entry_id) is not runtime):
                        errors["base"] = "entry_changed"
                    elif error := reconfigure_guard(runtime, existing):
                        errors["base"] = error
                    else:
                        # Keep the existing logical bench device while its BLE
                        # peer changes. Resolve options over data just as Runtime
                        # does, then persist the same anchor in both mappings.
                        values["device_identifier"] = existing.get("device_identifier", existing["address"])
                        # One native entry update covers data/options/unique_id.
                        # The registered update listener owns the scoped reload.
                        return self.async_update_and_abort(
                            entry, unique_id=address,
                            data={**data_before, **values}, options={**options_before, **values})
        return self.async_show_form(step_id="reconfigure", data_schema=reconfigure_schema(existing), errors=errors)

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
                elif (self.config_entry.options.get("camera_model", self.config_entry.data.get("camera_model")) == S3_CAMERA_MODEL
                      and data.get("capture_light_percent", 0) != 0):
                    errors["base"] = "s3_light_must_be_off"
                else:
                    return self.async_create_entry(data=data)
        existing = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=schema(existing, True), errors=errors)
