"""Explicit local enable and independently controlled periodic collection."""
from homeassistant.components.switch import SwitchEntity
from .const import DOMAIN
from .entity import MeterEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CollectionSwitch(runtime, False), CollectionSwitch(runtime, True)])


class CollectionSwitch(MeterEntity, SwitchEntity):
    def __init__(self, runtime, periodic):
        super().__init__(runtime, "periodic" if periodic else "enabled",
                         "Periodic collection" if periodic else "Image collection")
        self.periodic_control = periodic

    @property
    def is_on(self):
        return self.runtime.periodic if self.periodic_control else self.runtime.enabled

    async def async_turn_on(self, **kwargs):
        await (self.runtime.set_periodic(True) if self.periodic_control else self.runtime.set_enabled(True))

    async def async_turn_off(self, **kwargs):
        await (self.runtime.set_periodic(False) if self.periodic_control else self.runtime.set_enabled(False))
