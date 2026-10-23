"""Manual small transport test, snapshot, and explicit safety reset."""
from homeassistant.components.button import ButtonEntity
from .const import DOMAIN
from .entity import MeterEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MeterButton(runtime, key, name) for key, name in
                        [("test", "Test BLE transport"), ("capture", "Capture one snapshot"),
                         ("retry", "Retry pending SD snapshot"), ("reset", "Reset collection safety latch")]])


class MeterButton(MeterEntity, ButtonEntity):
    def __init__(self, runtime, key, name):
        super().__init__(runtime, key, name)
        self.key = key

    async def async_press(self):
        if self.key == "reset":
            await self.runtime.reset_safety()
        else:
            await self.runtime.capture(test=self.key == "test", retry_pending=self.key == "retry")
