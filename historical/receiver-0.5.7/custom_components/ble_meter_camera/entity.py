"""Shared push-update entity base."""
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from .const import DOMAIN


class MeterEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, runtime, key: str, name: str) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = {"identifiers": {(DOMAIN, runtime.config["address"])},
                                  "name": "BLE meter camera", "manufacturer": "Local project",
                                  "model": "ESP32-CAM OV2640"}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self.async_write_ha_state))
