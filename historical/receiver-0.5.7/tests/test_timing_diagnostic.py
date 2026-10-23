"""Accelerated timing fault injection; no HA server, Bluetooth or image files."""
import asyncio
from time import monotonic
import unittest
from unittest.mock import patch

from test_receiver import Version2Transport
from ble_meter_camera.protocol import METADATA, META
from ble_meter_camera.transfer import Limits, Receiver


class TimedTransport(Version2Transport):
    def __init__(self, mode='ready'):
        super().__init__()
        self.timing_mode = mode
        self.metadata_entered = asyncio.Event()
    async def write(self, characteristic, value):
        await super().write(characteristic, value)
        if value[0] in (1, 4, 6):
            if self.timing_mode == 'write_stall':
                await self.stall.wait()
            await asyncio.sleep(0.005)
    async def read(self, characteristic):
        if characteristic == METADATA:
            self.metadata_entered.set()
            if self.timing_mode == 'metadata_stall':
                await self.stall.wait()
            if self.timing_mode == 'backend_timeout':
                raise TimeoutError('backend sentinel preserved')
            raw = await super().read(characteristic)
            if self.timing_mode == 'busy':
                fields = list(META.unpack(raw));fields[1] = 1
                return META.pack(*fields)
            return raw
        return await super().read(characteristic)


class TimingDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def receiver(self, mode='ready', *, operation=0.03, capture=0.2):
        transport = TimedTransport(mode)
        receiver = Receiver(transport, lambda: None,
            Limits(operation_seconds=operation, capture_seconds=capture,
                   total_seconds=1, chunk_pause_seconds=0.02))
        return receiver, transport

    async def test_default_sixty_second_wait_can_reach_ready_after_scaled_old_deadline_and_evict_events(self):
        class LateReadyTransport(TimedTransport):
            async def read(self, characteristic):
                raw = await super().read(characteristic)
                if characteristic == METADATA:
                    self.polls = getattr(self, 'polls', 0) + 1
                    if self.polls <= 50:
                        fields = list(META.unpack(raw)); fields[1] = 1
                        return META.pack(*fields)
                return raw
        transport = LateReadyTransport()
        receiver = Receiver(transport, lambda: None)
        original_timeout, original_sleep = asyncio.timeout, asyncio.sleep
        requested = []
        def scaled_timeout(seconds):
            requested.append(seconds)
            return original_timeout(seconds * 0.002 if seconds in (10, 60) else seconds)
        async def accelerated_poll_sleep(seconds):
            await original_sleep(0.001 if seconds == 0.25 else seconds)
        with patch('ble_meter_camera.transfer.asyncio.timeout', scaled_timeout), \
                patch('ble_meter_camera.transfer.asyncio.sleep', accelerated_poll_sleep):
            result = await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(result.data, bytes(range(256)))
        self.assertIn(60, requested)
        self.assertEqual(diagnostic['metadata_poll_count'], 51)
        self.assertGreater(diagnostic['events_omitted'], 0)
        self.assertNotIn('capture_write_start', [item['event'] for item in diagnostic['events']])
        markers = [diagnostic[name + '_elapsed_ms'] for name in (
            'capture_write_start', 'capture_write_end', 'capture_wait_start', 'capture_wait_end')]
        self.assertEqual(markers, sorted(markers))
        self.assertGreater(markers[-1] - markers[-2], 20)  # Scaled old10s deadline.
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.closed and transport.released)

    async def test_default_sixty_second_wait_is_bounded_without_changing_operation_or_session_limits(self):
        transport = TimedTransport('busy')
        receiver = Receiver(transport, lambda: None)
        original_timeout = asyncio.timeout
        requested = []
        def accelerated_timeout(seconds):
            requested.append(seconds)
            return original_timeout(0.03 if seconds == 60 else seconds)
        with patch('ble_meter_camera.transfer.asyncio.timeout', accelerated_timeout):
            with self.assertRaises(TimeoutError):
                await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['capture_wait_seconds'], 60)
        self.assertEqual(diagnostic['operation_seconds'], 5)
        self.assertIn(600, requested)
        self.assertEqual(diagnostic['timeout_classification'], 'capture_ready_wait')
        self.assertTrue(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.closed and transport.released)

    async def test_external_cancellation_with_long_default_wait_still_releases_immediately(self):
        transport = TimedTransport('metadata_stall')
        receiver = Receiver(transport, lambda: None)
        task = asyncio.create_task(receiver.collect(test=True))
        await transport.metadata_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['capture_wait_seconds'], 60)
        self.assertEqual(diagnostic['outcome'], 'cancelled')
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertIsNotNone(diagnostic['capture_wait_end_elapsed_ms'])
        self.assertTrue(transport.closed and transport.released)

    async def test_inner_metadata_operation_timeout_is_distinct_and_cleanup_preserved(self):
        receiver, transport = self.receiver('metadata_stall')
        with self.assertRaises(TimeoutError):
            await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'], 'metadata_operation')
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        end = next(event for event in diagnostic['events'] if event['event']=='metadata_read_end')
        self.assertTrue(end['operation_timeout_expired'])
        self.assertEqual(end['exception_type'], 'TimeoutError')
        self.assertTrue(transport.released and transport.closed)

    async def test_outer_capture_timeout_cancels_read_without_expiring_inner_operation(self):
        receiver, transport = self.receiver('metadata_stall', operation=0.2, capture=0.03)
        with self.assertRaises(TimeoutError):
            await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'], 'capture_ready_wait')
        self.assertTrue(diagnostic['capture_wait_timeout_expired'])
        end = next(event for event in diagnostic['events'] if event['event']=='metadata_read_end')
        self.assertFalse(end['operation_timeout_expired'])
        self.assertEqual(end['exception_type'], 'CancelledError')
        self.assertTrue(transport.released and transport.closed)

    async def test_outer_timeout_while_waiting_between_returned_busy_polls(self):
        receiver, transport = self.receiver('busy', operation=0.2, capture=0.03)
        with self.assertRaises(TimeoutError):
            await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'], 'capture_ready_wait')
        end = next(event for event in diagnostic['events'] if event['event']=='metadata_read_end')
        self.assertEqual(end['returned_state'], 1)
        self.assertIsNone(end['exception_type'])
        self.assertFalse(end['operation_timeout_expired'])
        self.assertTrue(transport.closed)

    async def test_backend_timeout_is_not_falsely_labeled_as_an_expired_receiver_timer(self):
        receiver, transport = self.receiver('backend_timeout')
        with self.assertRaisesRegex(TimeoutError, 'backend sentinel preserved'):
            await receiver.collect(test=True)
        self.assertEqual(receiver.timing_snapshot()['timeout_classification'], 'backend_or_other_scope')
        self.assertTrue(transport.closed)

    async def test_capture_write_timeout_keeps_original_exception_and_cleanup(self):
        receiver, transport = self.receiver('write_stall')
        with self.assertRaises(TimeoutError):
            await receiver.collect(test=True)
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['timeout_classification'], 'capture_write_operation')
        self.assertEqual(diagnostic['metadata_poll_count'], 0)
        self.assertTrue(transport.closed)

    async def test_external_cancel_remains_cancelled_error_without_timeout_classification(self):
        receiver, transport = self.receiver('metadata_stall')
        task = asyncio.create_task(receiver.collect(test=True))
        await transport.metadata_entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['outcome'], 'cancelled')
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertFalse(diagnostic['capture_wait_timeout_expired'])
        self.assertTrue(transport.released and transport.closed)

    async def test_normal_ready_result_preserves_id_payload_command_order_and_records_split_spans(self):
        receiver, transport = self.receiver()
        with patch('ble_meter_camera.transfer.secrets.randbelow', return_value=123):
            result = await receiver.collect(test=True)
        self.assertEqual(result.image_id, 124)
        self.assertEqual(result.data, bytes(range(256)))
        self.assertEqual([value[0] for value in transport.writes], [4, 2, 2, 3])
        diagnostic = receiver.timing_snapshot()
        self.assertEqual(diagnostic['outcome'], 'complete')
        self.assertEqual(diagnostic['metadata_poll_count'], 1)
        names = [event['event'] for event in diagnostic['events']]
        self.assertLess(names.index('capture_write_end'), names.index('capture_wait_start'))
        self.assertEqual(next(event for event in diagnostic['events'] if event['event']=='metadata_read_end')['returned_state'], 2)
        self.assertIsNone(diagnostic['timeout_classification'])
        self.assertTrue(diagnostic['cleanup_confirmed'])

    async def test_diagnostic_ring_is_bounded_and_snapshot_does_not_alias_events(self):
        receiver, transport = self.receiver()
        await receiver.collect(test=True)
        markers = {name: value for name, value in receiver.timing_snapshot().items() if name.endswith('_elapsed_ms')}
        receiver._timing_started = monotonic()
        for count in range(150):receiver._timing_event('fixture', poll=count)
        snapshot = receiver.timing_snapshot()
        self.assertEqual(len(snapshot['events']), 96)
        self.assertGreater(snapshot['events_omitted'], 0)
        snapshot['events'][0]['event'] = 'changed snapshot only'
        self.assertNotEqual(receiver.last_attempt_timing['events'][0]['event'], 'changed snapshot only')
        self.assertEqual({name: snapshot[name] for name in markers}, markers)
        self.assertNotIn('capture_wait_end', [event['event'] for event in snapshot['events']])
        self.assertNotIn('payload', str(snapshot))


if __name__ == '__main__':unittest.main()
