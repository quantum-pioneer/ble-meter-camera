"""Camera replacement with native Core2026.10.2 flow helpers and local HA fakes."""
import ast
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
import importlib.util
import importlib.machinery
import json
from pathlib import Path
import tempfile
import types
import unittest
import zlib
from unittest.mock import patch

from test_runtime import FakeHass, FakeStore, jpeg, runtime_module
from ble_meter_camera.const import DOMAIN, DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL, TEST_RUN_CODE
from ble_meter_camera.protocol import Manifest
from ble_meter_camera.transfer import Result

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'custom_components/ble_meter_camera'
OLD = '02:00:00:00:00:01'
NEW = '00:11:22:33:44:55'
PROXY = '02:00:00:00:00:02'


def native_api_base():
    """Execute only reviewed native flow helpers, not an HA runtime or source imports."""
    source = ROOT / 'tests/fixtures/homeassistant_config_entries_2026_10_2.py'
    tree = ast.parse(source.read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ConfigFlow')
    names = {'unique_id', '_get_reconfigure_entry', '_reconfigure_entry_id',
             '_async_current_entries', 'async_set_unique_id', '__async_update', 'async_update_and_abort'}
    methods = [n for n in original.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(methods) == len(names)
    cls = ast.ClassDef(name='NativeAPI', bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = {'callback': lambda f: f, 'UNDEFINED': object(), 'SOURCE_RECONFIGURE': 'reconfigure',
                 'SOURCE_REAUTH': 'reauth', 'SOURCE_USER': 'user', 'HOMEASSISTANT_DOMAIN': 'homeassistant',
                 'DEFAULT_DISCOVERY_UNIQUE_ID': 'default_discovery_unique_id'}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    class Base(namespace['NativeAPI']):
        def __init_subclass__(cls, **kwargs): pass
        def _async_in_progress(self, **kwargs): return []
        @property
        def source(self): return self.context['source']
        def async_abort(self, *, reason, **kwargs): return {'type': 'abort', 'reason': reason}
        def async_show_form(self, **kwargs): return {'type': 'form', **kwargs}
    return Base


def load_flow():
    entries = types.ModuleType('homeassistant.config_entries')
    entries.ConfigFlow = entries.OptionsFlow = native_api_base()
    ha = types.ModuleType('homeassistant'); ha.config_entries = entries
    core = types.ModuleType('homeassistant.core'); core.callback = lambda f: f
    spec = importlib.util.spec_from_file_location('ble_meter_camera.reconfigure_fixture', PACKAGE / 'config_flow.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', {'homeassistant': ha, 'homeassistant.config_entries': entries, 'homeassistant.core': core}):
        spec.loader.exec_module(module)
    return module


class EntryManager:
    def __init__(self, hass, entry):
        self.hass, self.entries = hass, [entry]
        self.updates, self.reloads, self.tasks = [], [], []
    def async_get_known_entry(self, entry_id):
        return next(e for e in self.entries if e.entry_id == entry_id)
    def async_entries(self, domain, include_ignore=True): return [e for e in self.entries if e.domain == domain]
    def async_entry_for_domain_unique_id(self, domain, unique_id):
        return next((e for e in self.entries if e.domain == domain and e.unique_id == unique_id), None)
    def async_update_entry(self, *, entry, data, options, unique_id, title):
        self.updates.append({'entry_id': entry.entry_id, 'data': data, 'options': options, 'unique_id': unique_id})
        # Match Core's one update: all attributes are changed before listeners run.
        entry.data, entry.options, entry.unique_id = data, options, unique_id
        for listener in entry.update_listeners:
            self.tasks.append(asyncio.create_task(listener(self.hass, entry)))
        return True
    async def async_reload(self, entry_id): self.reloads.append(entry_id); return True


class CameraReconfigureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeStore.data.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.hass = FakeHass(self.temp.name)
        self.entry = types.SimpleNamespace(entry_id='existing-meter-entry', domain=DOMAIN, unique_id=OLD,
            data={'address': OLD, 'proxy_source': PROXY, 'unrelated_preserved': 19},
            options={'address': OLD, 'proxy_source': PROXY, 'frame_size': 13, 'quality': 12,
                     'capture_light_percent': 15, 'history_days': 7, 'history_max_bytes': 134217728})
        self.runtime = runtime_module.Runtime(self.hass, self.entry)
        await self.runtime.start()
        self.runtime.receiver.transport.client = None
        self.hass.data = {DOMAIN: {self.entry.entry_id: self.runtime}}
        self.manager = self.hass.config_entries = EntryManager(self.hass, self.entry)
        # Load the integration's real listener, with only its HA type imports faked.
        entries = types.ModuleType('homeassistant.config_entries'); entries.ConfigEntry = object
        core = types.ModuleType('homeassistant.core'); core.HomeAssistant = object
        spec = importlib.util.spec_from_loader('ble_meter_camera.entry_listener_fixture',
            importlib.machinery.SourceFileLoader('ble_meter_camera.entry_listener_fixture', str(PACKAGE / '__init__.py')),
            is_package=False)
        init = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'homeassistant.config_entries': entries, 'homeassistant.core': core,
                                       'ble_meter_camera.runtime': runtime_module}):
            spec.loader.exec_module(init)
        self.entry.update_listeners = [init._reload]
        self.module = load_flow()
        self.flow = self.module.ConfigFlow()
        self.flow.hass, self.flow.handler = self.hass, DOMAIN
        self.flow.context = {'source': 'reconfigure', 'entry_id': self.entry.entry_id}
        self.values = {'address': NEW.lower(), 'camera_model': S3_CAMERA_MODEL, 'capture_light_percent': 0}
    async def asyncTearDown(self):
        await asyncio.gather(*self.manager.tasks)
        await self.runtime.stop()
        self.temp.cleanup()
    async def submit(self, values=None): return await self.flow.async_step_reconfigure(self.values if values is None else values)
    def result(self):
        now = datetime.now(timezone.utc)
        return Result(jpeg(), 7, 123, now, now, now, 1, PROXY, False,
            manifest=Manifest(23, 0, int(now.timestamp()), int(now.timestamp()) + 604800, TEST_RUN_CODE, 1))

    def entity(self, runtime):
        entity_module = types.ModuleType('homeassistant.helpers.entity'); entity_module.Entity = object
        dispatcher = types.ModuleType('homeassistant.helpers.dispatcher'); dispatcher.async_dispatcher_connect = lambda *args: None
        spec = importlib.util.spec_from_file_location('ble_meter_camera.entity_fixture', PACKAGE / 'entity.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'homeassistant.helpers.entity': entity_module, 'homeassistant.helpers.dispatcher': dispatcher}):
            spec.loader.exec_module(module)
        return module.MeterEntity(runtime, 'latest', 'Latest verified image')

    async def test_native_atomic_update_preserves_entry_options_and_uses_one_existing_reload_listener(self):
        old_data, old_options = dict(self.entry.data), dict(self.entry.options)
        foreign_entity = {'entity_id': 'sensor.unrelated', 'device_id': 'old-device'}
        response = await self.submit()
        await asyncio.gather(*self.manager.tasks)
        self.assertEqual(response, {'type': 'abort', 'reason': 'reconfigure_successful'})
        self.assertEqual(len(self.manager.updates), 1)
        self.assertEqual(self.manager.reloads, ['existing-meter-entry'])
        self.assertEqual(self.entry.entry_id, 'existing-meter-entry')
        self.assertEqual(self.entry.unique_id, NEW)
        for value in (self.entry.data, self.entry.options):
            self.assertEqual(value['address'], NEW)
            self.assertEqual(value['camera_model'], S3_CAMERA_MODEL)
            self.assertEqual(value['capture_light_percent'], 0)
            self.assertEqual(value['device_identifier'], OLD)
        for source, updated in ((old_data, self.entry.data), (old_options, self.entry.options)):
            for key in source.keys() - {'address', 'capture_light_percent'}:
                self.assertEqual(updated[key], source[key])
        self.assertEqual(foreign_entity, {'entity_id': 'sensor.unrelated', 'device_id': 'old-device'})
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)

    async def test_invalid_duplicate_and_proxy_changes_do_not_mutate_entry(self):
        for changes, error in [({'address': 'not-a-mac'}, 'invalid_mac'),
                               ({'address': PROXY}, 'camera_is_proxy'),
                               ({'proxy_source': '00:00:00:00:00:01'}, 'proxy_immutable'),
                               ({'camera_model': 'unknown'}, 'invalid_reconfigure'),
                               ({'capture_light_percent': 15}, 's3_light_must_be_off'),
                               ({'capture_light_percent': True}, 'invalid_capture_light'),
                               ({'device_identifier': 'user-supplied-anchor'}, 'invalid_reconfigure'),
                               ({'quality': 20}, 'invalid_reconfigure')]:
            with self.subTest(changes=changes):
                response = await self.submit({**self.values, **changes})
                self.assertEqual(response['errors']['base'], error)
                self.assertFalse(self.manager.updates)
        self.manager.entries.append(types.SimpleNamespace(entry_id='other', domain=DOMAIN, unique_id=NEW,
                                                         data={}, options={}))
        self.assertEqual((await self.submit())['reason'], 'already_configured')
        self.assertFalse(self.manager.updates)

    async def test_collection_periodic_owned_tasks_and_clients_block_replacement(self):
        targets = [(self.runtime, 'enabled', True), (self.runtime, 'periodic', True),
                   (self.runtime, '_transfer_task', object()), (self.runtime, '_maintenance_task', object()),
                   (self.runtime, 'cleanup_recovery', {}), (self.runtime.receiver, 'cleanup_error', 'unknown'),
                   (self.runtime.receiver.transport, 'client', object()), (self.runtime, '_stopping', True)]
        for target, key, value in targets:
            previous = getattr(target, key)
            try:
                setattr(target, key, value)
                response = await self.submit()
                self.assertEqual(response['type'], 'form')
                self.assertTrue(response['errors'])
                self.assertFalse(self.manager.updates)
            finally: setattr(target, key, previous)

    async def test_state_or_operation_change_after_unique_id_await_aborts_without_overwrite(self):
        original = self.flow.async_set_unique_id
        async def race(unique_id):
            result = await original(unique_id)
            self.entry.options = {**self.entry.options, 'quality': 19}
            return result
        with patch.object(self.flow, 'async_set_unique_id', race):
            self.assertEqual((await self.submit())['errors']['base'], 'entry_changed')
        self.assertFalse(self.manager.updates)
        self.assertEqual(self.entry.options['quality'], 19)

    async def test_client_started_after_unique_id_await_and_missing_loaded_runtime_are_blocked(self):
        original = self.flow.async_set_unique_id
        async def race(unique_id):
            result = await original(unique_id)
            self.runtime.receiver.transport.client = object()
            return result
        with patch.object(self.flow, 'async_set_unique_id', race):
            self.assertEqual((await self.submit())['errors']['base'], 'client_must_be_closed')
        self.runtime.receiver.transport.client = None
        self.hass.data[DOMAIN].clear()
        self.assertEqual((await self.submit())['errors']['base'], 'receiver_not_loaded')
        self.assertFalse(self.manager.updates)

    async def test_normal_options_cannot_reenable_unsupported_s3_flash(self):
        await self.submit()
        flow = self.module.OptionsFlow()
        flow.config_entry = self.entry
        response = await flow.async_step_init({'address': NEW, 'proxy_source': PROXY, 'capture_light_percent': 15})
        self.assertEqual(response['errors']['base'], 's3_light_must_be_off')
        self.assertEqual(self.entry.options['capture_light_percent'], 0)

    async def test_same_entry_reload_preserves_storage_policy_and_unlabeled_old_photo(self):
        await self.runtime.set_enabled(True)
        await self.runtime._publish(self.result())
        await self.runtime.set_enabled(False)
        # Model the old0.5.7 Store, which has no per-image hardware provenance.
        self.runtime.image_info.pop('image_camera_address'); self.runtime.image_info.pop('image_camera_model')
        import base64
        await self.runtime.store.async_save({'jpeg': base64.b64encode(self.runtime.latest).decode(), 'info': self.runtime.image_info})
        paths = [self.runtime.store.path, self.runtime.state_store.path, str(self.runtime.history.directory)]
        old_latest = Path(paths[0]).read_bytes()
        old_cutoff = self.runtime.bench_until
        self.assertEqual((await self.submit())['reason'], 'reconfigure_successful')
        await self.runtime.stop()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertEqual(paths, [fresh.store.path, fresh.state_store.path, str(fresh.history.directory)])
            self.assertEqual(Path(paths[0]).read_bytes(), old_latest)
            self.assertEqual(fresh.bench_until, old_cutoff)
            self.assertEqual(fresh.run_id, self.runtime.run_id)
            self.assertFalse(fresh.enabled or fresh.periodic)
            self.assertEqual(fresh.attributes['configured_camera_address'], NEW)
            self.assertEqual(fresh.attributes['configured_camera_model'], S3_CAMERA_MODEL)
            self.assertIsNone(fresh.attributes['image_camera_address'])
            self.assertIsNone(fresh.attributes['image_camera_model'])
            new_info = fresh._result_info(self.result())
            self.assertEqual(new_info['image_camera_address'], NEW)
            self.assertEqual(new_info['image_camera_model'], S3_CAMERA_MODEL)
        finally: await fresh.stop()

    async def test_model_defaults_old_and_entity_unique_id_remains_entry_scoped(self):
        before = self.entity(self.runtime)
        self.assertEqual(before._attr_device_info['model'], DEFAULT_CAMERA_MODEL)
        await self.submit()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        after = self.entity(fresh)
        self.assertEqual(before._attr_unique_id, after._attr_unique_id)
        self.assertEqual(after._attr_device_info['model'], S3_CAMERA_MODEL)
        self.assertEqual(after._attr_device_info['identifiers'], before._attr_device_info['identifiers'])
        self.assertEqual(after._attr_device_info['identifiers'], {(DOMAIN, OLD)})
        self.assertEqual(after._attr_device_info['manufacturer'], 'Local project')

    async def test_existing_device_identifiers_survive_multiple_camera_reconfigures(self):
        before = self.entity(self.runtime)
        self.assertEqual((await self.submit())['reason'], 'reconfigure_successful')
        await self.runtime.stop()
        self.runtime = runtime_module.Runtime(self.hass, self.entry)
        await self.runtime.start()
        self.runtime.receiver.transport.client = None
        self.hass.data[DOMAIN][self.entry.entry_id] = self.runtime
        first = self.entity(self.runtime)
        self.assertEqual(first._attr_device_info['model'], S3_CAMERA_MODEL)
        next_address = '00:11:22:33:44:66'
        response = await self.submit({'address': next_address, 'camera_model': DEFAULT_CAMERA_MODEL,
                                      'capture_light_percent': 0})
        self.assertEqual(response['reason'], 'reconfigure_successful')
        final_runtime = runtime_module.Runtime(self.hass, self.entry)
        final = self.entity(final_runtime)
        self.assertEqual(final_runtime.config['address'], next_address)
        self.assertEqual(final._attr_device_info['model'], DEFAULT_CAMERA_MODEL)
        for entity in (first, final):
            self.assertEqual(entity._attr_device_info['identifiers'], before._attr_device_info['identifiers'])
            self.assertEqual(entity._attr_unique_id, before._attr_unique_id)
        for mapping in (self.entry.data, self.entry.options):
            self.assertEqual(mapping['device_identifier'], OLD)
        self.assertEqual(self.entry.entry_id, 'existing-meter-entry')

    async def test_existing_device_anchor_uses_runtime_merge_and_survives_options_replacement(self):
        self.entry.data['device_identifier'] = 'lower-priority-data-anchor'
        self.entry.options['device_identifier'] = OLD
        before = self.entity(runtime_module.Runtime(self.hass, self.entry))
        self.assertEqual((await self.submit())['reason'], 'reconfigure_successful')
        for mapping in (self.entry.data, self.entry.options):
            self.assertEqual(mapping['device_identifier'], OLD)
        # A normal options save may replace its mapping with schema-only fields;
        # the persistent data anchor must still retain the same registry identity.
        self.entry.options = {key: value for key, value in self.entry.options.items()
                              if key != 'device_identifier'}
        after = self.entity(runtime_module.Runtime(self.hass, self.entry))
        self.assertEqual(after._attr_device_info['identifiers'], before._attr_device_info['identifiers'])
        self.assertEqual(after._attr_device_info['identifiers'], {(DOMAIN, OLD)})
        self.assertEqual(after._attr_device_info['model'], S3_CAMERA_MODEL)

    async def test_old_pending_photo_preserves_original_time_crc_and_unknown_capture_origin_on_s3_transfer(self):
        await self.submit()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        result = self.result()
        original = int(result.received_at.timestamp()) - 86400
        pending = replace(result, from_pending=True,
            manifest=Manifest(23 | 64, 0, original, original + 604800, TEST_RUN_CODE, 9))
        info = fresh._result_info(pending)
        self.assertEqual(info['captured_at'], datetime.fromtimestamp(original, timezone.utc).isoformat())
        self.assertEqual(info['expires_at'], datetime.fromtimestamp(original + 604800, timezone.utc).isoformat())
        self.assertEqual(info['crc32'], f'{zlib.crc32(pending.data) & 0xffffffff:08x}')
        self.assertIsNone(info['image_camera_address'])
        self.assertIsNone(info['image_camera_model'])
        self.assertEqual(info['image_transfer_camera_address'], NEW)
        self.assertEqual(info['image_transfer_camera_model'], S3_CAMERA_MODEL)
        self.assertIsNone(info['capture_window_start'])
        self.assertIsNone(info['capture_window_end'])
        fresh_info = fresh._result_info(result)
        self.assertEqual(fresh_info['image_camera_address'], NEW)
        self.assertEqual(fresh_info['image_camera_model'], S3_CAMERA_MODEL)


if __name__ == '__main__': unittest.main()
