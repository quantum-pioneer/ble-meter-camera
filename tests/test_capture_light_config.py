"""Options validation uses the real form schema, without Home Assistant or radios."""
from pathlib import Path
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch
try:
    import voluptuous as vol
except ImportError:
    vol = None

PACKAGE = Path(__file__).resolve().parents[1] / 'custom_components' / 'ble_meter_camera'
package = types.ModuleType('ble_meter_camera')
package.__path__ = [str(PACKAGE)]
sys.modules.setdefault('ble_meter_camera', package)


def load_config_flow():
    ha = types.ModuleType('homeassistant')
    entries = types.ModuleType('homeassistant.config_entries')
    core = types.ModuleType('homeassistant.core')
    class BaseFlow:
        def __init_subclass__(cls, **kwargs):
            pass
    entries.ConfigFlow = BaseFlow
    entries.OptionsFlow = BaseFlow
    ha.config_entries = entries
    core.callback = lambda function: function
    spec = importlib.util.spec_from_file_location('ble_meter_camera.config_flow', PACKAGE / 'config_flow.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'homeassistant': ha, 'homeassistant.config_entries': entries,
                                  'homeassistant.core': core}):
        spec.loader.exec_module(module)
    return module


@unittest.skipIf(vol is None, 'Options schema needs the existing development environment voluptuous package')
class CaptureLightConfigTests(unittest.TestCase):
    def setUp(self):
        self.flow = load_config_flow()
        self.identity = {'address': '00:11:22:33:44:55', 'proxy_source': 'AA:BB:CC:DD:EE:FF'}

    def test_options_default_off_and_bound_integer_input(self):
        schema = self.flow.schema(self.identity, advanced=True)
        self.assertEqual(schema(self.identity)['capture_light_percent'], 0)
        self.assertEqual(schema({**self.identity, 'capture_light_percent': 25})['capture_light_percent'], 25)
        for value in (-1, 26, 2.5, '25'):
            with self.subTest(value=value), self.assertRaises(vol.Invalid):
                schema({**self.identity, 'capture_light_percent': value})

    def test_flow_normalization_rejects_non_percentage_even_if_supplied_directly(self):
        for value in (True, -1, 26, 2.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_capture_light'):
                self.flow.normalize({**self.identity, 'capture_light_percent': value})
        self.assertEqual(self.flow.normalize({**self.identity, 'capture_light_percent': 25})['capture_light_percent'], 25)
