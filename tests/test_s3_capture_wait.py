"""Accelerated model-specific READY deadlines; fake BLE and local runtime only."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import tempfile
from time import monotonic
import types
import unittest
from unittest.mock import patch

from test_receiver import Version2Transport
from test_runtime import FakeHass, runtime_module
from test_trial_policy import FixtureClock
from ble_meter_camera.const import DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL
from ble_meter_camera.policy import CUTOFF
from ble_meter_camera.protocol import METADATA, META
from ble_meter_camera.transfer import Limits

REAL_SLEEP, REAL_TIMEOUT = asyncio.sleep, asyncio.timeout


@contextmanager
def accelerated(requested):
    def timeout(seconds):
        requested.append(seconds)
        return REAL_TIMEOUT(seconds * 0.01)
    async def sleep(seconds):
        await REAL_SLEEP(seconds * 0.01)
    with patch('ble_meter_camera.transfer.asyncio.timeout',timeout), \
            patch('ble_meter_camera.transfer.asyncio.sleep',sleep):
        yield


class CaptureWaitTransport(Version2Transport):
    def __init__(self, *, ready_after=0.75, stall=False):
        super().__init__()
        self.ready_after,self.stall=ready_after,stall
        self.metadata_entered=asyncio.Event()
        self.commanded_at=None
    async def write(self,characteristic,value):
        await super().write(characteristic,value)
        if value[0] in (1,4,6):self.commanded_at=monotonic()
    async def read(self,characteristic):
        if characteristic==METADATA:
            self.metadata_entered.set()
            if self.stall:await asyncio.Event().wait()
            raw=await super().read(characteristic)
            if monotonic()-self.commanded_at < self.ready_after:
                fields=list(META.unpack(raw));fields[1]=1
                return META.pack(*fields)
            return raw
        return await super().read(characteristic)


class S3CaptureWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.clock=FixtureClock(CUTOFF-timedelta(hours=1))
    def tearDown(self):self.temp.cleanup()
    def runtime(self,*,model=S3_CAMERA_MODEL,transport=None):
        entry=types.SimpleNamespace(entry_id='ready',data={'address':'00:11:22:33:44:55',
            'proxy_source':'AA:BB:CC:DD:EE:FF'},options={} if model is None else {'camera_model':model})
        runtime=runtime_module.Runtime(FakeHass(self.temp.name),entry)
        runtime.clock=self.clock.trusted();runtime.enabled=True
        if transport is not None:runtime.receiver.transport=transport
        return runtime

    def test_only_S3_ready_limit_is120_and_original_default_and_other_deadlines_stay_intact(self):
        for model,ready,context,maintenance in ((None,60,10,50),(DEFAULT_CAMERA_MODEL,60,10,50),(S3_CAMERA_MODEL,120,120,170)):
            with self.subTest(model=model):
                runtime=self.runtime(model=model);limits=runtime.receiver.limits
                self.assertEqual((limits.capture_seconds,limits.context_seconds,limits.maintenance_seconds),(ready,context,maintenance))
                self.assertEqual((limits.operation_seconds,limits.total_seconds),(5,600))
                self.assertEqual(runtime.attributes['receiver_capture_ready_limit_seconds'],ready)
                self.assertEqual(runtime.bench_until,CUTOFF)
        self.assertEqual(Limits().capture_seconds,60)
        self.assertEqual(Limits(capture_seconds=120).capture_seconds,120)
        with self.assertRaises(ValueError):Limits(capture_seconds=120.001)

    async def test_S3_becomes_READY_after_scaled_old60_and_before120_with_normal_result_cleanup(self):
        #60s ->600ms,120s ->1.2s; the fake camera returns READY after750ms.
        transport=CaptureWaitTransport();runtime=self.runtime(transport=transport)
        requested=[]
        with accelerated(requested):result=await runtime.receiver.collect(test=True)
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(result.data,bytes(range(256)))
        self.assertGreater(diagnostic['capture_wait_end_elapsed_ms']-diagnostic['capture_wait_start_elapsed_ms'],600)
        self.assertEqual(diagnostic['capture_wait_seconds'],120)
        self.assertIn(120,requested);self.assertIn(600,requested);self.assertIn(5,requested)
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(diagnostic['cleanup_confirmed'])
        self.assertTrue(transport.released and transport.closed)

    async def test_original_camera_keeps_sixty_second_deadline_for_same_late_READY(self):
        transport=CaptureWaitTransport();runtime=self.runtime(model=DEFAULT_CAMERA_MODEL,transport=transport)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(test=True)
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['capture_wait_seconds'],60)
        self.assertEqual(diagnostic['timeout_classification'],'capture_ready_wait')
        self.assertTrue(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.released and transport.closed)

    async def test_S3_outer_READY_wait_still_ends_at120_for_continued_BUSY(self):
        transport=CaptureWaitTransport(ready_after=10);runtime=self.runtime(transport=transport)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(test=True)
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['capture_wait_seconds'],120)
        self.assertEqual(diagnostic['timeout_classification'],'capture_ready_wait')
        self.assertTrue(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(diagnostic['cleanup_confirmed'])

    async def test_cutoff_interrupts_S3_long_READY_wait_before_result_or_publish(self):
        transport=CaptureWaitTransport(ready_after=10);runtime=self.runtime(transport=transport)
        self.clock.advance(3599)
        with accelerated([]):
            task=asyncio.create_task(runtime.receiver.collect(test=True))
            await transport.metadata_entered.wait();self.clock.advance(2)
            with self.assertRaisesRegex(RuntimeError,'Bench test ended'):await task
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['outcome'],'failed')
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(runtime.bench_ended)
        self.assertTrue(transport.released and transport.closed)

    async def test_total_session_deadline_still_stops_S3_before_long_READY_limit(self):
        transport=CaptureWaitTransport(ready_after=10);runtime=self.runtime(transport=transport)
        runtime.receiver.limits=replace(runtime.receiver.limits,total_seconds=1)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(test=True)
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'],'session')
        self.assertTrue(diagnostic['session_timeout_expired'])
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.released and transport.closed)

    async def test_stalled_S3_metadata_read_still_has_five_second_operation_limit(self):
        transport=CaptureWaitTransport(stall=True);runtime=self.runtime(transport=transport)
        with accelerated([]),self.assertRaises(TimeoutError):await runtime.receiver.collect(test=True)
        diagnostic=runtime.receiver.timing_snapshot()
        self.assertEqual(diagnostic['operation_seconds'],5)
        self.assertEqual(diagnostic['timeout_classification'],'metadata_operation')
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.released and transport.closed)


if __name__=='__main__':unittest.main()
