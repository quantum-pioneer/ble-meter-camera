"""Accelerated S3 SPI-context waits; owned fake BLE clients, no HA/device calls."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import tempfile
from time import monotonic
import types
import unittest
from unittest.mock import patch

from test_runtime import FakeHass, runtime_module
from test_trial_policy import FixtureClock, PolicyTransport
from ble_meter_camera.const import DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL, TEST_RETENTION_SECONDS, TEST_RUN_CODE
from ble_meter_camera.policy import CUTOFF
from ble_meter_camera.protocol import BOOT_DIAGNOSTIC, HEALTH, MANIFEST, META
from ble_meter_camera.transfer import Limits, Receiver

REAL_SLEEP, REAL_TIMEOUT = asyncio.sleep, asyncio.timeout


@contextmanager
def accelerated(requested):
    def timeout(seconds):
        requested.append(seconds)
        return REAL_TIMEOUT(seconds * 0.02)
    async def sleep(seconds):
        await REAL_SLEEP(seconds * 0.02)
    with patch('ble_meter_camera.transfer.asyncio.timeout', timeout), \
            patch('ble_meter_camera.transfer.asyncio.sleep', sleep):
        yield


class ContextTransport(PolicyTransport):
    def __init__(self, *, ready_after=0, mode=None, minimum_polls=0):
        super().__init__()
        self.ready_after, self.mode = ready_after, mode
        self.manifest_entered = asyncio.Event()
        self.configured_at = None
        self.polls, self.minimum_polls = 0, minimum_polls
    async def write(self, characteristic, value):
        await super().write(characteristic, value)
        if value[0] == 7:
            self.configured_at = monotonic()
            if self.mode == 'write_stall':
                await asyncio.Event().wait()
    async def read(self, characteristic):
        if characteristic == MANIFEST and not self.ident:
            self.polls += 1
            self.manifest_entered.set()
            if self.mode == 'manifest_stall':
                await asyncio.Event().wait()
            if self.mode == 'backend_timeout':
                raise TimeoutError('backend context sentinel')
            raw = await super().read(characteristic)
            if monotonic() - self.configured_at < self.ready_after or self.polls <= self.minimum_polls:
                fields = list(META.unpack(raw)); fields[1] &= ~4
                return META.pack(*fields)
            return raw
        if characteristic in (HEALTH, BOOT_DIAGNOSTIC) and self.mode == 'diagnostic_stall':
            await asyncio.Event().wait()
        return await super().read(characteristic)


class ContextTimingTests(unittest.IsolatedAsyncioTestCase):
    def receiver(self, transport, *, context=60, guard=lambda: None):
        return Receiver(transport, guard, Limits(context_seconds=context))
    def context(self):
        return {'mode':'TEST_PHOTO','run_code':TEST_RUN_CODE,'now':lambda:CUTOFF-timedelta(hours=2)}

    def test_only_S3_runtime_gets120_seconds_without_changing_policy_or_other_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            for model, expected, maintenance in ((None,10,50), (DEFAULT_CAMERA_MODEL,10,50), (S3_CAMERA_MODEL,120,170)):
                with self.subTest(model=model):
                    entry = types.SimpleNamespace(entry_id='context', data={'address':'00:11:22:33:44:55',
                        'proxy_source':'AA:BB:CC:DD:EE:FF'}, options={} if model is None else {'camera_model':model})
                    runtime = runtime_module.Runtime(FakeHass(directory),entry)
                    limits = runtime.receiver.limits
                    self.assertEqual((limits.context_seconds,limits.maintenance_seconds),(expected,maintenance))
                    self.assertEqual((limits.operation_seconds,limits.capture_seconds,limits.total_seconds),
                        (5,120 if model == S3_CAMERA_MODEL else 60,600))
                    self.assertEqual(runtime.attributes['receiver_context_limit_seconds'],expected)
                    self.assertEqual(runtime.attributes['receiver_maintenance_limit_seconds'],maintenance)
                    self.assertEqual(runtime.bench_until,CUTOFF)
                    self.assertEqual((runtime.config['history_days'],TEST_RETENTION_SECONDS),(7,604800))
        self.assertEqual(Limits().context_seconds,10)
        for seconds in (0,-1,120.001):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                Limits(context_seconds=seconds)

    async def test_S3_context_beyond_old_ten_second_deadline_succeeds_and_keeps_initial_scalars(self):
        #10s ->200ms;60s ->1.2s. More than48 polls also evicts CONFIG events.
        transport = ContextTransport(ready_after=0.32,minimum_polls=60)
        receiver = self.receiver(transport)
        requested=[]
        with accelerated(requested):
            result = await receiver.collect(context=self.context())
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(result.manifest.expires_epoch-result.manifest.captured_epoch,604800)
        self.assertGreater(diagnostic['context_wait_end_elapsed_ms']-diagnostic['context_wait_start_elapsed_ms'],200)
        self.assertEqual(diagnostic['context_wait_seconds'],60)
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertGreater(diagnostic['events_omitted'],0)
        self.assertNotIn('config_write_start',[e['event'] for e in diagnostic['events']])
        self.assertIsNotNone(diagnostic['config_write_start_elapsed_ms'])
        self.assertLessEqual(diagnostic['config_write_end_elapsed_ms'],diagnostic['context_wait_start_elapsed_ms'])
        ends=[e for e in diagnostic['events'] if e['event']=='manifest_read_end']
        self.assertTrue(ends[-1]['returned_flags'] & 4)
        self.assertTrue(all(e['returned_bytes']==20 and not e['operation_timeout_expired'] for e in ends))
        diagnostics=[e for e in diagnostic['events'] if e['event']=='diagnostic_read_end']
        self.assertEqual({e['diagnostic'] for e in diagnostics},{'health','bootdiag'})
        self.assertTrue(all(e['duration_ms'] >= 0 and e['returned_bytes']==20 for e in diagnostics))
        self.assertIn(600,requested); self.assertTrue(transport.closed)

    async def test_original_default_context_still_stops_at_ten_seconds_before_capture(self):
        transport=ContextTransport(ready_after=0.32)
        receiver=self.receiver(transport,context=Limits().context_seconds)
        requested=[]
        with accelerated(requested), self.assertRaises(TimeoutError):
            await receiver.collect(context=self.context())
        diagnostic=receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'],'context_ready_wait')
        self.assertTrue(diagnostic['context_wait_timeout_expired'])
        self.assertIn(10,requested)
        self.assertFalse(any(c[0] in (1,4,6) for c in transport.writes))
        self.assertEqual({e['reason'] for e in diagnostic['events'] if e['event']=='diagnostic_read_end'},{'context_failure'})
        self.assertTrue(transport.closed)

    async def test_manifest_hang_remains_five_second_operation_not_sixty_second_context(self):
        transport=ContextTransport(mode='manifest_stall'); receiver=self.receiver(transport)
        requested=[]
        with accelerated(requested), self.assertRaises(TimeoutError):
            await receiver.collect(context=self.context())
        diagnostic=receiver.timing_snapshot()
        self.assertEqual(diagnostic['operation_seconds'],5)
        self.assertEqual(diagnostic['timeout_classification'],'context_manifest_operation')
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        end=next(e for e in diagnostic['events'] if e['event']=='manifest_read_end')
        self.assertTrue(end['operation_timeout_expired']); self.assertIsNone(end['returned_bytes'])
        self.assertTrue(transport.closed)

    async def test_outer_context_deadline_cancel_does_not_claim_inner_operation_expired(self):
        transport=ContextTransport(mode='manifest_stall'); receiver=self.receiver(transport,context=0.5)
        with accelerated([]), self.assertRaises(TimeoutError):
            await receiver.collect(context=self.context())
        diagnostic=receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'],'context_ready_wait')
        end=next(e for e in diagnostic['events'] if e['event']=='manifest_read_end')
        self.assertFalse(end['operation_timeout_expired']); self.assertEqual(end['exception_type'],'CancelledError')
        self.assertTrue(transport.closed)

    async def test_CONFIG_write_and_diagnostic_timeouts_are_separate_and_keep_five_second_bounds(self):
        for mode,classification in (('write_stall','config_write_operation'),('diagnostic_stall','diagnostic_operation')):
            with self.subTest(mode=mode):
                transport=ContextTransport(mode=mode); receiver=self.receiver(transport)
                with accelerated([]), self.assertRaises(TimeoutError):
                    await receiver.collect(context=self.context())
                diagnostic=receiver.timing_snapshot()
                self.assertEqual(diagnostic['timeout_classification'],classification)
                self.assertFalse(diagnostic['context_wait_timeout_expired'])
                self.assertFalse(any(c[0] in (1,4,6) for c in transport.writes))
                self.assertTrue(transport.closed)

    async def test_backend_timeout_is_not_mislabeled_as_receiver_deadline(self):
        receiver=self.receiver(ContextTransport(mode='backend_timeout'))
        with accelerated([]), self.assertRaisesRegex(TimeoutError,'backend context sentinel'):
            await receiver.collect(context=self.context())
        self.assertEqual(receiver.timing_snapshot()['timeout_classification'],'backend_or_other_scope')

    async def test_total_session_deadline_still_bounds_context_and_diagnostics(self):
        transport=ContextTransport(mode='manifest_stall'); receiver=self.receiver(transport)
        receiver.limits=replace(receiver.limits,total_seconds=1)
        with accelerated([]), self.assertRaises(TimeoutError):
            await receiver.collect(context=self.context())
        diagnostic=receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'],'session')
        self.assertTrue(diagnostic['session_timeout_expired'])
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assertTrue(transport.closed)

    async def test_external_cancellation_keeps_cancelled_error_and_owned_cleanup(self):
        transport=ContextTransport(mode='manifest_stall'); receiver=self.receiver(transport)
        task=asyncio.create_task(receiver.collect(context=self.context()))
        await transport.manifest_entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        diagnostic=receiver.timing_snapshot()
        self.assertEqual(diagnostic['outcome'],'cancelled')
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertFalse(diagnostic['context_wait_timeout_expired'])
        self.assertTrue(transport.closed)

    async def test_cutoff_and_safety_trip_still_interrupt_long_context_before_capture(self):
        for reason in ('cutoff','safety'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                entry=types.SimpleNamespace(entry_id='guard',data={'address':'00:11:22:33:44:55',
                    'proxy_source':'AA:BB:CC:DD:EE:FF'},options={'camera_model':S3_CAMERA_MODEL})
                runtime=runtime_module.Runtime(FakeHass(directory),entry)
                clock=FixtureClock(CUTOFF-timedelta(seconds=1));runtime.clock=clock.trusted();runtime.enabled=True
                transport=ContextTransport(ready_after=1)
                receiver=self.receiver(transport,guard=runtime.check)
                with accelerated([]):
                    task=asyncio.create_task(receiver.collect(context=runtime.context()))
                    await transport.manifest_entered.wait()
                    if reason=='cutoff':clock.advance(2)
                    else:runtime.safety_trip='isolated safety gate'
                    with self.assertRaisesRegex(RuntimeError,'Bench test ended|isolated safety gate'):
                        await task
                self.assertFalse(any(c[0] in (1,4,6) for c in transport.writes))
                self.assertTrue(transport.closed)

    async def test_maintenance_budget_adapts_only_for_slow_context_and_keeps_poststop_cleanup(self):
        for context,budget in ((10,50),(60,110),(120,170)):
            with self.subTest(context=context):
                transport=ContextTransport(); receiver=self.receiver(transport,context=context,
                    guard=lambda:(_ for _ in ()).throw(RuntimeError('collection off after stop')))
                policy=self.context();policy['now']=lambda:CUTOFF+timedelta(days=1)
                requested=[]
                with accelerated(requested): await receiver.maintain(policy)
                self.assertEqual(requested[0],budget)
                self.assertLess(budget+6,600)
                self.assertEqual([c[0] for c in transport.writes],[7,3])
                self.assertTrue(transport.closed)


if __name__=='__main__':unittest.main()
