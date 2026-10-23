"""Transfer state and explicit capture/receipt timestamps."""
from datetime import datetime
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from .const import DOMAIN
from .entity import MeterEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MeterStatus(runtime), TimeSensor(runtime, "capture_window_start", "Capture earliest time"),
                        TimeSensor(runtime, "capture_window_end", "Capture latest time"),
                        TimeSensor(runtime, "received_at", "Last verified receipt")])


class MeterStatus(MeterEntity, SensorEntity):
    def __init__(self, runtime):
        super().__init__(runtime, "status", "Transfer status")

    @property
    def native_value(self):
        return self.runtime.status

    @property
    def extra_state_attributes(self):
        return self.runtime.attributes


class TimeSensor(MeterEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, runtime, key, name):
        super().__init__(runtime, key, name)
        self.key = key

    @property
    def native_value(self):
        value = self.runtime.image_info.get(self.key)
        return datetime.fromisoformat(value) if value else None
