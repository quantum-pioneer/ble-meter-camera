"""Two sequential fake SD phases within bounded S3 CONFIG readiness policy."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import tempfile
import types
import unittest
from unittest.mock import patch

from test_runtime import FakeHass, runtime_module
from test_trial_policy import FixtureClock, PolicyTransport
from ble_meter_camera.const import DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL
from ble_meter_camera.policy import CUTOFF
from ble_meter_camera.protocol import MANIFEST, META
from ble_meter_camera.transfer import Limits

REAL_SLEEP,REAL_TIMEOUT=asyncio.sleep,asyncio.timeout


@contextmanager
def accelerated(requested):
    def timeout(seconds):
        requested.append(seconds)
        return REAL_TIMEOUT(seconds*0.01)
    async def sleep(seconds):await REAL_SLEEP(seconds*0.01)
    with patch('ble_meter_camera.transfer.asyncio.timeout',timeout), \
            patch('ble_meter_camera.transfer.asyncio.sleep',sleep):
        yield


class ConfigPhasesTransport(PolicyTransport):
    def __init__(self,*,background=0.4,configure=0.4,stall_read=False):
        super().__init__()
        self.background,self.configure=background,configure
        self.stall_read=stall_read
        self.worker=None
        self.ready=False
        self.phases=[]
        self.manifest_entered=asyncio.Event()
    async def write(self,characteristic,value):
        if value[0] in (1,4,6) and not self.ready:
            raise AssertionError('Photo command before both fake SD phases completed')
        await super().write(characteristic,value)
        if value[0]==7:
            async def phases():
                self.phases.append('background_started')
                await REAL_SLEEP(self.background)
                self.phases.extend(('background_finished','config_started'))
                await REAL_SLEEP(self.configure)
                self.phases.append('config_finished')
                self.ready=True
            self.worker=asyncio.create_task(phases())
    async def read(self,characteristic):
        if characteristic==MANIFEST and not self.ident:
            self.manifest_entered.set()
            if self.stall_read:await asyncio.Event().wait()
            raw=await super().read(characteristic)
            if not self.ready:
                fields=list(META.unpack(raw));fields[1]&=~4
                return META.pack(*fields)
            return raw
        return await super().read(characteristic)
    async def close(self):
        # Fixture-only task disposal; no real device work is controlled here.
        if self.worker is not None:
            self.worker.cancel()
            try:await self.worker
            except asyncio.CancelledError:pass
        await super().close()


class S3ContextHeadroomTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.clock=FixtureClock(CUTOFF-timedelta(hours=1))
    def tearDown(self):self.temp.cleanup()
    def runtime(self,transport,*,model=S3_CAMERA_MODEL):
        entry=types.SimpleNamespace(entry_id='context-headroom',data={'address':'00:11:22:33:44:55',
            'proxy_source':'AA:BB:CC:DD:EE:FF'},options={'camera_model':model})
        runtime=runtime_module.Runtime(FakeHass(self.temp.name),entry)
        runtime.clock=self.clock.trusted();runtime.enabled=True
        runtime.receiver.transport=transport
        return runtime
    def assert_no_photo_command(self,transport):
        self.assertFalse(any(command[0] in (1,4,6) for command in transport.writes))
        self.assertTrue(transport.closed)

    async def test_S3_background_then_CONFIG_exceeds60_and_completes_within120_under_runtime_guard(self):
        # Linear scale: two40s phases ->800ms total;60s ->600ms/120s ->1.2s.
        transport=ConfigPhasesTransport();runtime=self.runtime(transport)
        requested=[]
        with accelerated(requested):result=await runtime.receiver.collect(context=runtime.context())
        diagnostic=runtime.receiver.timing_snapshot()
        elapsed=diagnostic['context_wait_end_elapsed_ms']-diagnostic['context_wait_start_elapsed_ms']
        self.assertGreater(elapsed,600);self.assertLess(elapsed,1200)
        self.assertEqual(transport.phases,['background_started','background_finished','config_started','config_finished'])
        self.assertEqual(diagnostic['context_wait_seconds'],120)
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertEqual((runtime.receiver.limits.operation_seconds,runtime.receiver.limits.total_seconds),(5,600))
        self.assertEqual((runtime.attributes['receiver_context_limit_seconds'],runtime.attributes['receiver_maintenance_limit_seconds']),(120,170))
        self.assertEqual(result.manifest.expires_epoch-result.manifest.captured_epoch,604800)
        self.assertTrue(result.manifest.flags&2)
        self.assertTrue(transport.closed and diagnostic['cleanup_confirmed'])
        self.assertEqual(Limits().context_seconds,10)
        self.assertEqual(Limits(context_seconds=120).context_seconds,120)
        with self.assertRaises(ValueError):Limits(context_seconds=120.001)

    async def test_original_camera_still_has10_second_context_for_same_two_phases(self):
        transport=ConfigPhasesTransport();runtime=self.runtime(transport,model=DEFAULT_CAMERA_MODEL)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(context=runtime.context())
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['context_wait_seconds'],10)
        self.assertEqual(diagnostic['timeout_classification'],'context_ready_wait')
        self.assertEqual((runtime.receiver.limits.capture_seconds,runtime.receiver.limits.maintenance_seconds),(60,50))
        self.assert_no_photo_command(transport)

    async def test_S3_context_still_expires_at120_when_both_phases_remain_busy(self):
        transport=ConfigPhasesTransport(background=0.8,configure=0.8);runtime=self.runtime(transport)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(context=runtime.context())
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['context_wait_seconds'],120)
        self.assertTrue(diagnostic['context_wait_timeout_expired'])
        self.assertEqual(diagnostic['timeout_classification'],'context_ready_wait')
        self.assertFalse(transport.ready)
        self.assert_no_photo_command(transport)

    async def test_total_deadline_interrupts_long_S3_context_without_extension(self):
        transport=ConfigPhasesTransport();runtime=self.runtime(transport)
        runtime.receiver.limits=replace(runtime.receiver.limits,total_seconds=1)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(context=runtime.context())
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'],'session')
        self.assertTrue(diagnostic['session_timeout_expired'])
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assert_no_photo_command(transport)

    async def test_cutoff_interrupts_background_plus_CONFIG_before_capture(self):
        transport=ConfigPhasesTransport();runtime=self.runtime(transport)
        self.clock.advance(3599)
        with accelerated([]):
            task=asyncio.create_task(runtime.receiver.collect(context=runtime.context()))
            await transport.manifest_entered.wait();self.clock.advance(2)
            with self.assertRaisesRegex(RuntimeError,'Bench test ended'):await task
        self.assertTrue(runtime.bench_ended)
        self.assertFalse(runtime.receiver.timing_snapshot()['context_wait_timeout_expired'])
        self.assert_no_photo_command(transport)

    async def test_S3_manifest_stall_is_still_per_operation5_seconds(self):
        transport=ConfigPhasesTransport(stall_read=True);runtime=self.runtime(transport)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(context=runtime.context())
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['operation_seconds'],5)
        self.assertEqual(diagnostic['timeout_classification'],'context_manifest_operation')
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assert_no_photo_command(transport)

    async def test_external_cancellation_preserves_CancelledError_and_owned_cleanup(self):
        transport=ConfigPhasesTransport();runtime=self.runtime(transport)
        task=asyncio.create_task(runtime.receiver.collect(context=runtime.context()))
        await transport.manifest_entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['outcome'],'cancelled')
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assert_no_photo_command(transport)


if __name__=='__main__':unittest.main()
