"""Absolute-cutoff, original-time retention and mandatory-SD fault fixtures."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from test_runtime import FakeStore, FakeHass, HomeAssistantError, jpeg, runtime_module
from ble_meter_camera.policy import CUTOFF
TrustedClock = runtime_module.TrustedClock
ClockError = runtime_module.ClockError
ProtocolError = runtime_module.ProtocolError
from ble_meter_camera.const import TEST_RUN_CODE, TEST_RUN_ID
from ble_meter_camera.history import History
from ble_meter_camera.protocol import Manifest, META, METADATA, configure_command, ProtocolError as WireProtocolError
from ble_meter_camera.transfer import Result, Receiver
from test_receiver import Version2Transport, FAST


class FixtureClock:
    def __init__(self, value):
        self.value, self.tick = value, 0
    def now(self):
        return self.value
    def elapsed(self):
        return self.tick
    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.tick += seconds
    def trusted(self):
        return TrustedClock(self.now, self.elapsed)


class TrialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeStore.data.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.hass = FakeHass(self.temp.name)
        self.entry = types.SimpleNamespace(entry_id='policy', data={'address':'00:11:22:33:44:55', 'proxy_source':'AA:BB:CC:DD:EE:FF'}, options={})
        self.clock = FixtureClock(CUTOFF-timedelta(hours=1))
        self.runtime = runtime_module.Runtime(self.hass, self.entry)
        self.runtime.clock = self.clock.trusted()
        await self.runtime.start()
    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temp.cleanup()
    def result(self, *, capture=None, flags=23, manifest=True):
        now = self.clock.now()
        captured = int((capture or now).timestamp())
        return Result(jpeg(), 123, 100, now, now, now, 1, 'AA:BB:CC:DD:EE:FF', False,
                      manifest=Manifest(flags,0,captured,captured+604800,TEST_RUN_CODE,9) if manifest else None)

    async def test_immutable_exact_cutoff_overrides_old_saved_relative_deadline(self):
        await self.runtime.set_enabled(True)
        await self.runtime.set_periodic(True)
        state = FakeStore.data[self.runtime.state_store.key]
        state['bench_until'] = '2030-01-01T00:00:00+00:00'
        await self.runtime.stop()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        fresh.clock = self.clock.trusted()
        await fresh.start()
        try:
            self.assertEqual(fresh.bench_until.isoformat(), '2026-10-24T19:25:00+00:00')
            await fresh._save_state()
            self.assertEqual(FakeStore.data[fresh.state_store.key]['bench_until'], CUTOFF.isoformat())
        finally:
            await fresh.stop()

    async def test_cutoff_blocks_manual_fixture_retry_and_reenable(self):
        await self.runtime.set_enabled(True)
        self.clock.advance(3600)
        for kwargs in ({}, {'test':True}, {'retry_pending':True}):
            with self.assertRaises(HomeAssistantError):
                await self.runtime.capture(**kwargs)
        self.assertTrue(self.runtime.bench_ended)
        with self.assertRaises(HomeAssistantError):
            await self.runtime.set_enabled(True)
        self.assertEqual(FakeStore.data[self.runtime.state_store.key]['bench_until'], CUTOFF.isoformat())
        self.assertTrue(FakeStore.data[self.runtime.state_store.key]['bench_ended'])
        with self.assertRaises(HomeAssistantError):
            await self.runtime.set_mode('TEST_PHOTO')

    async def test_last_slot_never_advertises_a_1430_capture(self):
        self.clock.advance(3599)
        await self.runtime.set_enabled(True)
        self.runtime.periodic = True
        sleeps = []
        async def sleep(seconds):
            sleeps.append(seconds)
            self.assertIsNone(self.runtime.next_capture_at)
            raise asyncio.CancelledError
        with patch.object(runtime_module.asyncio, 'sleep', sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime._run_periodic()
        self.assertEqual(sleeps, [1])

    async def test_late_arrival_and_missing_sd_proof_cannot_publish(self):
        await self.runtime.set_enabled(True)
        for result in (self.result(manifest=False), self.result(flags=21)):
            with self.assertRaises((ProtocolError, WireProtocolError)):
                await self.runtime._publish(result)
        result = self.result()
        self.clock.advance(3600)
        with self.assertRaises(RuntimeError):
            await self.runtime._publish(result)
        self.assertIsNone(self.runtime.latest)
        self.assertFalse(list(self.runtime.history.directory.rglob('*.jpg')))

    async def test_sixty_second_ready_wait_cannot_bypass_absolute_cutoff_between_busy_polls(self):
        self.clock.advance(3599)
        await self.runtime.set_enabled(True)
        clock = self.clock
        class CutoffTransport(Version2Transport):
            async def read(self, characteristic):
                raw = await super().read(characteristic)
                if characteristic == METADATA:
                    clock.advance(2)
                    fields = list(META.unpack(raw)); fields[1] = 1
                    return META.pack(*fields)
                return raw
        transport = CutoffTransport()
        receiver = Receiver(transport, self.runtime.check)
        with self.assertRaisesRegex(RuntimeError, 'Bench test ended'):
            await receiver.collect(test=True)
        self.assertEqual(receiver.limits.capture_seconds, 60)
        self.assertTrue(self.runtime.bench_ended)
        self.assertEqual(receiver.timing_snapshot()['metadata_poll_count'], 1)
        self.assertEqual(receiver.timing_snapshot()['outcome'], 'failed')
        self.assertFalse(receiver.timing_snapshot()['capture_wait_timeout_expired'])
        self.assertTrue(transport.closed and transport.released)
        self.assertIsNone(self.runtime.latest)

    async def test_cutoff_crossed_during_store_rolls_back_staged_photo(self):
        self.clock.advance(3599)
        await self.runtime.set_enabled(True)
        original = self.runtime.store.async_save
        async def delayed(value):
            if value:
                self.clock.advance(2)
            await original(value)
        with patch.object(self.runtime.store, 'async_save', delayed):
            with self.assertRaises(RuntimeError):
                await self.runtime._publish(self.result())
        self.assertIsNone(self.runtime.latest)
        self.assertEqual(json.loads(Path(self.runtime.store.path).read_text())['data'], {})
        self.assertFalse(list(self.runtime.history.directory.rglob('*.jpg')))

    async def test_latest_and_history_expire_at_original_168h_after_mode_change(self):
        await self.runtime.set_enabled(True)
        await self.runtime._publish(self.result())
        expiry = datetime.fromisoformat(self.runtime.image_info['expires_at'])
        await self.runtime.set_mode('METER')
        self.clock.advance(604799)
        await self.runtime._prune_history()
        self.assertIsNotNone(self.runtime.visible_image())
        self.clock.advance(1)
        self.assertEqual(self.clock.now(), expiry)
        self.assertIsNone(self.runtime.visible_image())
        await self.runtime._prune_history()
        self.assertIsNone(self.runtime.latest)
        self.assertFalse(list(self.runtime.history.directory.rglob('*.jpg')))
        self.assertEqual(json.loads(Path(self.runtime.store.path).read_text())['data'], {})

    async def test_clock_rollback_monitor_blocks_without_crashing(self):
        await self.runtime.set_enabled(True)
        self.clock.value -= timedelta(minutes=5)
        ticks = 0
        async def tick(_):
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError
        with patch.object(runtime_module.asyncio, 'sleep', tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime._monitor()
        self.assertFalse(self.runtime.enabled)
        self.assertEqual(self.runtime.status, 'clock_untrusted')
        self.assertIsNone(self.runtime.visible_image())

    async def test_monitor_survives_state_disk_failure_and_keeps_cleanup_loop(self):
        await self.runtime.set_enabled(True)
        ticks = 0
        async def tick(_):
            nonlocal ticks
            ticks += 1
            if ticks > 65:
                raise asyncio.CancelledError
        async def failed_save():
            self.runtime.enabled = self.runtime.periodic = False
            raise HomeAssistantError('disk unavailable fixture')
        with patch.object(runtime_module.asyncio, 'sleep', tick), patch.object(self.runtime, '_save_state', failed_save):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime._monitor()
        self.assertEqual(ticks,66)
        self.assertFalse(self.runtime.enabled)

    async def test_photo_age_uses_original_time_on_pending_retry(self):
        await self.runtime.set_enabled(True)
        await self.runtime._publish(self.result(capture=self.clock.now()-timedelta(hours=1)))
        self.assertEqual(self.runtime.attributes['retained_image_age_seconds'],3600)
        self.assertTrue(self.runtime.attributes['retained_image_stale'])
        await self.runtime.set_mode('METER')
        self.assertEqual(self.runtime.attributes['image_mode'],'TEST_PHOTO')
        self.assertEqual(self.runtime.attributes['mode'],'METER')

    async def test_capacity_blocks_before_camera_command_without_early_deletion(self):
        self.runtime.history.max_bytes = len(jpeg())
        self.runtime.history.save(jpeg(), 1, self.clock.now())
        await self.runtime.set_enabled(True)
        with self.assertRaises(HomeAssistantError):
            await self.runtime.capture()
        self.assertEqual(len(list(self.runtime.history.directory.rglob('*.jpg'))), 1)


class PolicyProtocolTests(unittest.TestCase):
    def test_config_wire_layout_and_immutable_cutoff(self):
        fields = META.unpack(configure_command(1792600000, 'TEST_PHOTO', TEST_RUN_CODE))
        self.assertEqual(fields, (7,1,0,1792600000,1792869900,TEST_RUN_CODE,604800))
    def test_manifest_mode_run_sd_expiry_guards_and_meter_after_cutoff(self):
        now = int(CUTOFF.timestamp())+100
        # Global expired-test flag must never apply TEST retention to METER.
        meter = Manifest(1|2|4|8|32,0,now,0,5,9)
        meter.validate(mode='METER',run_code=5,now_epoch=now)
        for flags,error,captured,expires,run,sequence in ((21,0,now,now+604800,5,9), (23,1,now,now+604800,5,9),
                (23,0,now,now+604800,6,9), (23,0,now-604800,now,5,9), (23,0,now,now+604801,5,9)):
            with self.subTest(flags=flags,error=error,run=run,expires=expires), self.assertRaises((ProtocolError, WireProtocolError)):
                Manifest(flags,error,captured,expires,run,sequence).validate(mode='TEST_PHOTO',run_code=5,now_epoch=now)
    def test_clock_unavailable_and_persisted_rollback_reject(self):
        for value in (datetime(1970,1,1,tzinfo=timezone.utc), datetime(2026,10,21)):
            with self.assertRaises(ClockError):
                TrustedClock(now=lambda:value).read()
        clock = TrustedClock(now=lambda: CUTOFF-timedelta(hours=1))
        clock.high_water = CUTOFF
        with self.assertRaises(ClockError):
            clock.read()
    def test_legacy_receipt_fallback_is_not_migration_time_and_orphans_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            legacy = path/'20261021T000000000000Z-00000001.jpg'
            legacy.write_bytes(jpeg())
            unrelated = path/'family.jpg.json'; unrelated.write_text('keep')
            orphan = path/'20261021T000000000001Z-00000002.jpg.json'; orphan.write_text('{}')
            history = History(directory,7,1024*1024)
            history.prune(datetime(2026,10,22,tzinfo=timezone.utc))
            manifest = json.loads(legacy.with_suffix('.jpg.json').read_text())
            self.assertEqual(manifest['expires_at'],'2026-10-28T00:00:00+00:00')
            self.assertFalse(orphan.exists())
            history.prune(datetime(2026,10,28,tzinfo=timezone.utc))
            self.assertFalse(legacy.exists())
            self.assertTrue(unrelated.exists())


class PolicyTransport(Version2Transport):
    def __init__(self, *, committed=True, retry=False):
        super().__init__(b'\xff\xd8'+b'x'*140+b'\xff\xd9')
        self.policy = None
        self.committed, self.retry = committed, retry
    async def write(self, characteristic, value):
        if value[0] == 7:
            self.policy = META.unpack(value)
        await super().write(characteristic, value)
    async def read(self, characteristic):
        from ble_meter_camera.protocol import MANIFEST, HEALTH, BOOT_DIAGNOSTIC
        if characteristic == MANIFEST:
            now, run = self.policy[3], self.policy[5]
            flags = 1|4|16
            if self.ident:
                flags |= 2 if self.committed else 0
                flags |= 64 if self.retry else 0
            captured = now-3600 if self.retry else now
            return META.pack(1,flags,0,captured if self.ident else 0,captured+604800 if self.ident else 0,run,9 if self.ident else 0)
        if characteristic == HEALTH:
            return META.pack(1,1,0,789,123456,10,1)
        if characteristic == BOOT_DIAGNOSTIC:
            return META.pack(1,10,3,3,2,1,0x3a)
        return await super().read(characteristic)


class PolicyTransferTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {'mode':'TEST_PHOTO','run_code':TEST_RUN_CODE,'now':lambda:CUTOFF-timedelta(hours=2)}
    async def test_full_wire_policy_sync_health_and_sd_commit_before_publish(self):
        transport = PolicyTransport()
        receiver = Receiver(transport,lambda:None,FAST)
        published=[]
        async def publish(result):
            published.append(result)
            self.assertTrue(result.manifest.flags & 2)
            self.assertEqual(result.health['camera_boot_id'],789)
            self.assertEqual(result.bootdiag['camera_boot_counter'],3)
            self.assertEqual(result.bootdiag['camera_previous_boot_stage_name'],'ADVERTISING_ACTIVE')
            self.assertIsNotNone(result.bootdiag['camera_bootdiag_observed_at'])
        result = await receiver.collect(context=self.context(),on_verified=publish)
        self.assertEqual(transport.writes[0][0],7)
        self.assertEqual(result.manifest.run_code,TEST_RUN_CODE)
        self.assertEqual(len(published),1)
        self.assertEqual(sum(value[0]==5 for value in transport.writes),1)
        self.assertTrue(transport.closed)
    async def test_missing_sd_commit_rejects_before_image_transfer_or_ack(self):
        transport = PolicyTransport(committed=False)
        with self.assertRaises((ProtocolError,WireProtocolError)):
            await Receiver(transport,lambda:None,FAST).collect(context=self.context())
        self.assertFalse(any(value[0] in (2,5) for value in transport.writes))
        self.assertTrue(transport.closed)
    async def test_pending_retry_keeps_original_capture_and_expiry(self):
        transport = PolicyTransport(retry=True)
        context=self.context()
        result=await Receiver(transport,lambda:None,FAST).collect(context=context,retry_pending=True)
        self.assertEqual(result.manifest.captured_epoch,int(context['now']().timestamp())-3600)
        self.assertEqual(result.manifest.expires_epoch,result.manifest.captured_epoch+604800)
        self.assertTrue(result.from_pending)
    async def test_postcutoff_maintenance_never_requests_photo(self):
        transport=PolicyTransport()
        context=self.context();context['now']=lambda:CUTOFF+timedelta(days=2)
        receiver=Receiver(transport,lambda:(_ for _ in ()).throw(RuntimeError('test ended')),FAST)
        await receiver.maintain(context)
        self.assertEqual([value[0] for value in transport.writes],[7,3])
        self.assertTrue(transport.closed)


class ContextFailureDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_context_retains_raw_manifest_and_reads_health_before_cleanup(self):
        from ble_meter_camera.protocol import HEALTH, MANIFEST
        class FailedContext(PolicyTransport):
            async def read(self, characteristic):
                if characteristic == MANIFEST:
                    return META.pack(1,17,8,0,0,TEST_RUN_CODE,0)
                if characteristic == HEALTH:
                    return META.pack(1,1,8,12345,45678,27,3)
                return await super().read(characteristic)
        transport = FailedContext()
        receiver = Receiver(transport,lambda:None,FAST)
        context = PolicyTransferTests().context()
        async def timeout_poll(_):
            raise TimeoutError('isolated context poll timeout')
        with patch('ble_meter_camera.transfer.asyncio.sleep',timeout_poll):
            with self.assertRaisesRegex(WireProtocolError,'sd_error=8, flags=17'):
                await receiver._configure(context)
        self.assertEqual(receiver.last_manifest.sd_error,8)
        self.assertEqual(receiver.last_manifest.flags,17)
        self.assertEqual(receiver.last_health['camera_boot_id'],12345)
        self.assertEqual(receiver.last_health['camera_reset_reason'],1)
        self.assertEqual(receiver.last_bootdiag['camera_boot_counter'],3)
        self.assertIsNotNone(receiver.last_bootdiag['camera_bootdiag_observed_at'])
        self.assertFalse(any(command[0] in (1,4,6) for command in transport.writes))

    async def test_health_diagnostic_failure_preserves_primary_context_error(self):
        from ble_meter_camera.protocol import HEALTH, MANIFEST
        class FailedContext(PolicyTransport):
            async def read(self, characteristic):
                if characteristic == MANIFEST:
                    return META.pack(1,17,8,0,0,TEST_RUN_CODE,0)
                if characteristic == HEALTH:
                    raise ConnectionError('health read fixture')
                return await super().read(characteristic)
        receiver = Receiver(FailedContext(),lambda:None,FAST)
        async def timeout_poll(_):
            raise TimeoutError('isolated context poll timeout')
        with patch('ble_meter_camera.transfer.asyncio.sleep',timeout_poll):
            with self.assertRaisesRegex(WireProtocolError,'sd_error=8'):
                await receiver._configure(PolicyTransferTests().context())
        self.assertEqual(receiver.last_health['health_read_error_type'],'ConnectionError')
        self.assertEqual(receiver.last_manifest.sd_error,8)
        self.assertEqual(receiver.last_bootdiag['camera_boot_counter'],3)

    async def test_failed_context_bootdiag_failure_keeps_health_and_primary_sd_error(self):
        from ble_meter_camera.protocol import HEALTH, MANIFEST, BOOT_DIAGNOSTIC
        class FailedContext(PolicyTransport):
            async def read(self, characteristic):
                if characteristic == MANIFEST:
                    return META.pack(1,17,8,0,0,TEST_RUN_CODE,0)
                if characteristic == BOOT_DIAGNOSTIC:
                    raise ConnectionError('boot diagnostic fixture')
                return await super().read(characteristic)
        receiver = Receiver(FailedContext(),lambda:None,FAST)
        receiver.last_bootdiag = {'camera_boot_counter':99}
        async def timeout_poll(_):
            raise TimeoutError('isolated context poll timeout')
        with patch('ble_meter_camera.transfer.asyncio.sleep',timeout_poll):
            with self.assertRaisesRegex(WireProtocolError,'sd_error=8'):
                await receiver._configure(PolicyTransferTests().context())
        self.assertEqual(receiver.last_health['camera_boot_id'],789)
        self.assertEqual(receiver.last_bootdiag,{'camera_bootdiag_read_error_type':'ConnectionError'})

    async def test_successful_context_requires_valid_bootdiag_before_capture(self):
        from ble_meter_camera.protocol import BOOT_DIAGNOSTIC
        class BadBootDiagnostic(PolicyTransport):
            async def read(self, characteristic):
                if characteristic == BOOT_DIAGNOSTIC:
                    return META.pack(2,10,3,3,2,1,0x3a)
                return await super().read(characteristic)
        transport = BadBootDiagnostic()
        receiver = Receiver(transport,lambda:None,FAST)
        with self.assertRaisesRegex(WireProtocolError,'boot diagnostic version'):
            await receiver.collect(context=PolicyTransferTests().context())
        self.assertEqual(receiver.last_bootdiag['camera_bootdiag_read_error_type'],'ProtocolError')
        self.assertFalse(any(command[0] in (1,4,6) for command in transport.writes))
        self.assertTrue(transport.closed)

    async def test_new_connection_failure_does_not_keep_old_bootdiag(self):
        class Offline(PolicyTransport):
            async def connect(self):
                raise ConnectionError('cold start unavailable fixture')
        receiver = Receiver(Offline(),lambda:None,FAST)
        receiver.last_bootdiag = {'camera_boot_counter':99}
        with self.assertRaises(ConnectionError):
            await receiver.collect(context=PolicyTransferTests().context())
        self.assertEqual(receiver.last_bootdiag,{})
