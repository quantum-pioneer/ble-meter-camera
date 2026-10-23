"""Local BLE meter snapshot receiver through the dedicated XIAO ESP32-C6."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .runtime import Runtime


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = Runtime(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await runtime.start()
    if not hass.services.has_service(DOMAIN, "set_mode"):
        async def set_mode(call):
            from homeassistant.exceptions import HomeAssistantError
            selected = call.data.get("entry_id")
            runtimes = hass.data.get(DOMAIN, {})
            if selected is None and len(runtimes) == 1:
                selected = next(iter(runtimes))
            if selected not in runtimes:
                raise HomeAssistantError("Select the existing meter-camera entry_id")
            await runtimes[selected].set_mode(call.data.get("mode"))
        hass.services.async_register(DOMAIN, "set_mode", set_mode)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data[DOMAIN][entry.entry_id]
    await runtime.stop()
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove obsolete shared-presence dependencies when upgrading the project."""
    from .const import DEFAULTS
    allowed = {*DEFAULTS, "address", "proxy_source"}
    if entry.version == 1:
        if entry.data.get("interval") not in (900, 1800, 3600):
            data = {**entry.data, "interval": 900}
        else:
            data = entry.data
        options = {key: value for key, value in entry.options.items() if key in allowed}
        if "interval" in options and options["interval"] not in (900, 1800, 3600):
            options["interval"] = 900
        hass.config_entries.async_update_entry(
            entry, data={key: value for key, value in data.items() if key in allowed},
            options=options, version=2)
    return entry.version == 2
