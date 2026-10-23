"""Serve only the last verified JPEG; image requests never start BLE transfers."""
from homeassistant.components.camera import Camera
from .const import DOMAIN
from .entity import MeterEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MeterCamera(hass.data[DOMAIN][entry.entry_id])])


class MeterCamera(MeterEntity, Camera):
    _attr_content_type = "image/jpeg"

    def __init__(self, runtime):
        Camera.__init__(self)
        MeterEntity.__init__(self, runtime, "latest", "Latest verified image")

    @property
    def available(self):
        return self.runtime.visible_image() is not None

    @property
    def extra_state_attributes(self):
        return self.runtime.attributes

    async def async_camera_image(self, width=None, height=None):
        return self.runtime.visible_image()
