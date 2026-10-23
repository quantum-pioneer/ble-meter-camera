"""Backup-excluded payload migration and cutoff/thread cancellation fixtures."""
import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path, PurePath
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
from test_runtime import FakeHass, FakeStore, jpeg, runtime_module
from test_trial_policy import FixtureClock
from ble_meter_camera.policy import CUTOFF
from ble_meter_camera.protocol import Manifest
from ble_meter_camera.const import TEST_RUN_CODE
from ble_meter_camera.transfer import Result
from ble_meter_camera.storage import migrate_photo_storage


def envelope(data):
    return json.dumps({'version':1,'data':data}).encode()


class PhotoMigrationTests(unittest.TestCase):
    def test_legacy_payloads_verified_migrated_backup_excluded_metadata_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);entry='migration'
            legacy=root/'.storage';legacy.mkdir()
            received=CUTOFF-timedelta(days=2)
            info={'mode':'TEST_PHOTO','run_id':'calendar-20261021','received_at':received.isoformat(),
                  'captured_at':(received-timedelta(hours=1)).isoformat(),'capture_clock_synchronized':True,
                  'expires_at':(received-timedelta(hours=1)+timedelta(hours=168)).isoformat()}
            latest=legacy/f'ble_meter_camera_{entry}_latest';latest.write_bytes(envelope({'jpeg':base64.b64encode(jpeg()).decode(),'info':info}))
            state=legacy/f'ble_meter_camera_{entry}_state';state.write_bytes(b'metadata stays unchanged')
            history=legacy/'ble_meter_camera'/entry/'history'/'test-calendar-20261021';history.mkdir(parents=True)
            photo=history/'20261022T192500000000Z-00000001.jpg';photo.write_bytes(jpeg())
            manifest=photo.with_suffix('.jpg.json');manifest.write_text(json.dumps(info))
            unrelated=history/'family.jpg';unrelated.write_bytes(b'unrelated')
            corrupt=latest.with_name(latest.name+'.corrupt.fixture');corrupt.write_bytes(latest.read_bytes())
            before={str(p.relative_to(legacy)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (latest,photo,manifest,corrupt)}
            migrate_photo_storage(root,entry)
            target=root/'.cache'/'ble_meter_camera'/entry
            self.assertFalse(latest.exists() or photo.exists() or manifest.exists() or corrupt.exists())
            self.assertEqual(state.read_bytes(),b'metadata stays unchanged')
            self.assertEqual(unrelated.read_bytes(),b'unrelated')
            moved=(target/'latest.json',target/'history'/'test-calendar-20261021'/photo.name,
                   target/'history'/'test-calendar-20261021'/manifest.name,target/'latest.json.corrupt.fixture')
            self.assertEqual(sorted(before.values()),sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in moved))
            self.assertEqual(json.loads(moved[2].read_text())['expires_at'],info['expires_at'])
            for path in moved:
                relative='data/'+str(path.relative_to(root))
                self.assertTrue(any(parent.full_match('data/.cache/*') for parent in (PurePath(relative), *PurePath(relative).parents)))
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertFalse(PurePath('data/'+str(state.relative_to(root))).full_match('data/.cache/*'))
            migrate_photo_storage(root,entry)  # Idempotent restart after migration.
            self.assertEqual(len([p for p in target.rglob('*') if p.is_file()]),4)

    def test_failed_migration_preserves_original_and_cleans_owned_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);legacy=root/'.storage';legacy.mkdir()
            source=legacy/'ble_meter_camera_failure_latest';source.write_bytes(envelope({}))
            with patch('os.link',side_effect=OSError('storage full fixture')):
                with self.assertRaises(OSError):
                    migrate_photo_storage(root,'failure')
            self.assertTrue(source.exists())
            self.assertFalse(list((root/'.cache').rglob('*.tmp')))


class ThreadHass(FakeHass):
    async def async_add_executor_job(self,function,*args):
        return await asyncio.to_thread(function,*args)


class PhotoStoreRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeStore.data.clear()
        self.temp=tempfile.TemporaryDirectory()
        self.hass=ThreadHass(self.temp.name)
        self.entry=types.SimpleNamespace(entry_id='photoio',data={'address':'00:11:22:33:44:55','proxy_source':'AA:BB:CC:DD:EE:FF'},options={})
        self.clock=FixtureClock(CUTOFF-timedelta(hours=1))
        self.runtime=runtime_module.Runtime(self.hass,self.entry);self.runtime.clock=self.clock.trusted()
        await self.runtime.start()
    async def asyncTearDown(self):
        await self.runtime.stop();self.temp.cleanup()
    def result(self,ident):
        now=self.clock.now();epoch=int(now.timestamp())
        return Result(jpeg(),ident,100,now,now,now,1,'AA:BB:CC:DD:EE:FF',False,
                      manifest=Manifest(23,0,epoch,epoch+604800,TEST_RUN_CODE,ident))

    async def test_photo_store_restart_ignores_storage_manager_cache_and_cleans_tmp(self):
        await self.runtime.store.async_save({'proof':'real path contents'})
        self.assertIn('/.cache/ble_meter_camera/photoio/',self.runtime.store.path)
        FakeStore.data[self.runtime.store.key]={'proof':'wrong .storage preload'}
        self.assertEqual(await self.runtime.store.async_load(),{'proof':'real path contents'})
        temporary=Path(self.runtime.store.path+'.tmp');temporary.write_bytes(b'abandoned payload')
        await self.runtime.stop()
        fresh=runtime_module.Runtime(self.hass,self.entry);fresh.clock=self.clock.trusted()
        await fresh.start()
        try:
            self.assertFalse(temporary.exists())
            self.assertEqual(await fresh.store.async_load(),{'proof':'real path contents'})
        finally:
            await fresh.stop()

    async def test_cutoff_repeated_cancellation_waits_real_writer_then_rolls_back(self):
        await self.runtime.set_enabled(True)
        await self.runtime._publish(self.result(123))
        previous=Path(self.runtime.store.path).read_bytes()
        self.clock.advance(1)
        incoming=self.result(456)
        started,release=threading.Event(),threading.Event()
        original=self.runtime.store._save
        def blocking_save(value):
            if value and value.get('info',{}).get('image_id')==456:
                started.set()
                if not release.wait(5):
                    raise TimeoutError('fixture writer not released')
            original(value)
        with patch.object(self.runtime.store,'_save',blocking_save):
            task=asyncio.create_task(self.runtime._publish(incoming))
            self.assertTrue(await asyncio.to_thread(started.wait,2))
            self.clock.advance(3599)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            task.cancel()
            release.set()
            result=await asyncio.gather(task,return_exceptions=True)
            self.assertIsInstance(result[0],asyncio.CancelledError)
        self.assertEqual(Path(self.runtime.store.path).read_bytes(),previous)
        self.assertEqual(self.runtime.image_info['image_id'],123)
        files=list(self.runtime.history.directory.rglob('*.jpg'))
        self.assertEqual(len(files),1)
        self.assertTrue(files[0].name.endswith('-0000007b.jpg'))
        self.assertFalse(list(Path(self.runtime.store.path).parent.rglob('*.tmp')))
