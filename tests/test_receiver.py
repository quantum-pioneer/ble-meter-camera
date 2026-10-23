"""Fault injection tests; no radios or HA server contacted."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch
import zlib

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "ble_meter_camera"
package = types.ModuleType("ble_meter_camera")
package.__path__ = [str(PACKAGE)]
sys.modules.setdefault("ble_meter_camera", package)
from ble_meter_camera.protocol import (Assembly, CONTROL, DATA, METADATA, META, Metadata,
                                       ProtocolError, capture_command, capture_light_duty, parse_boot_diagnostic, retry_command, select_command)
from ble_meter_camera.transfer import Limits, Receiver, retryable_gatt_133


class FakeTransport:
    mtu_size = 23
    def __init__(self, payload=bytes(range(256)), *, mode="normal"):
        self.payload = payload
        self.mode = mode
        self.ident = 0
        self.offset = 0
        self.closed = False
        self.released = False
        self.connects = 0
        self.reads = 0
        self.writes = []
        self.failed_once = False
        self.selected = asyncio.Event()
        self.stall = asyncio.Event()

    async def connect(self):
        self.connects += 1
        if self.mode == "connect_fail":
            raise ConnectionError("camera disconnected")
        if self.mode == "connect_stall":
            await self.stall.wait()
        return "AA:BB:CC:DD:EE:FF"

    async def write(self, characteristic, value):
        self.writes.append(value)
        if value[0] in (1, 4, 6):
            self.ident = struct.unpack_from("<I", value, 1)[0]
        elif value[0] == 2:
            self.offset = struct.unpack_from("<I", value, 5)[0]
            self.selected.set()
        elif value[0] == 3:
            self.released = True

    async def read(self, characteristic):
        if characteristic == METADATA:
            length = 131073 if self.mode == "oversize" else len(self.payload)
            crc = zlib.crc32(self.payload) & 0xFFFFFFFF
            if self.mode == "bad_crc":
                crc ^= 1
            return META.pack(1, 1 if self.mode == "busy" else 2, 0, self.ident, length, crc, 101)
        self.reads += 1
        if self.mode == "stall":
            await self.stall.wait()
        if self.mode == "once_timeout" and not self.failed_once:
            self.failed_once = True
            raise TimeoutError("interrupted read")
        if self.mode == "disconnect":
            raise ConnectionError("disconnected during image")
        offset = self.offset + 1 if self.mode == "bad_offset" else self.offset
        ident = self.ident + 1 if self.mode == "bad_id" else self.ident
        header = struct.pack("<II", ident, offset)
        return header + self.payload[self.offset:self.offset + 12]

    async def close(self):
        self.closed = True


class Version2Transport(FakeTransport):
    mtu_size = 247
    def __init__(self, payload=bytes(range(256)), *, version=2, mode="normal"):
        super().__init__(payload, mode=mode)
        self.version = version
    async def read(self, characteristic):
        if characteristic == METADATA:
            raw = META.pack(self.version, 2, 0, self.ident, len(self.payload), zlib.crc32(self.payload) & 0xffffffff, 101)
            return raw + b"x" if self.mode == "metadata_oversize" else raw
        self.reads += 1
        count = 129 if self.mode == "data_oversize" else 128
        value = struct.pack("<II", self.ident, self.offset) + self.payload[self.offset:self.offset + count]
        return value


FakeBleakError = type("BleakError", (Exception,), {"__module__": "bleak.exc"})
FakeGattAPIError = type("BluetoothGATTAPIError", (Exception,), {"__module__": "aioesphomeapi.core"})


def gatt_error(code=133):
    cause = FakeGattAPIError("structured proxy error")
    cause.error = types.SimpleNamespace(error=code)
    error = FakeBleakError(f"Bluetooth GATT Error handle=46 error={code}")
    error.__cause__ = cause
    return error


class GattFaultTransport(FakeTransport):
    def __init__(self, phase, *, code=133, permanent=False):
        super().__init__()
        self.phase, self.code, self.permanent = phase, code, permanent
        self.faults = self.meta_reads = 0
    def fail(self):
        if self.permanent or not self.faults:
            self.faults += 1
            raise gatt_error(self.code)
    async def write(self, characteristic, value):
        await super().write(characteristic, value)
        if (self.phase == "capture" and value[0] in (1, 4)) or (self.phase == "select" and value[0] == 2):
            self.fail()
    async def read(self, characteristic):
        if characteristic == METADATA:
            self.meta_reads += 1
        raw = await super().read(characteristic)
        if (self.phase == "metadata" and characteristic == METADATA) or (self.phase == "data" and characteristic == DATA):
            self.fail()
        return raw


FAST = Limits(total_seconds=5, chunk_pause_seconds=0.02)


class ProtocolTests(unittest.TestCase):
    def test_persistent_boot_diagnostic_decodes_prior_stage_and_counter_without_causal_claim(self):
        for stage in range(1, 12):
            with self.subTest(stage=stage):
                result = parse_boot_diagnostic(META.pack(1, stage, 3, 8, 7, 1, 0x3a))
                self.assertEqual(result["camera_boot_counter"], 8)
                self.assertEqual(result["camera_previous_boot_counter"], 7)
                self.assertEqual(result["camera_previous_boot_stage"], stage)
                self.assertEqual(result["camera_previous_reset_reason"], 1)
                self.assertEqual(result["camera_previous_strap_register"], 0x3a)
                self.assertTrue(result["camera_bootdiag_prior_valid"])
        self.assertEqual(parse_boot_diagnostic(META.pack(1, 10, 3, 8, 7, 1, 0x3a))[
            "camera_previous_boot_stage_name"], "ADVERTISING_ACTIVE")

    def test_persistent_boot_diagnostic_keeps_nvs_failure_first_boot_and_saturation_truthful(self):
        for flags, current in ((8, 0), (1, 1), (9, 1)):
            with self.subTest(flags=flags):
                result = parse_boot_diagnostic(META.pack(1, 0, flags, current, 0, 0, 0))
                self.assertEqual(result["camera_boot_counter"], current)
                self.assertIsNone(result["camera_previous_boot_counter"])
                self.assertIsNone(result["camera_previous_boot_stage_name"])
                self.assertEqual(result["camera_bootdiag_write_error"], bool(flags & 8))
        saturated = parse_boot_diagnostic(META.pack(1, 11, 15, 0xffffffff, 0xffffffff, 3, 0x12))
        self.assertTrue(saturated["camera_bootdiag_counter_overflow"])
        self.assertTrue(saturated["camera_bootdiag_write_error"])
        self.assertEqual(saturated["camera_boot_counter"], 0xffffffff)

    def test_persistent_boot_diagnostic_rejects_bad_wire_and_inconsistent_records(self):
        cases = [(2, 10, 3, 8, 7, 1, 0x3a), (1, 12, 3, 8, 7, 1, 0x3a),
                 (1, 10, 19, 8, 7, 1, 0x3a), (1, 0, 3, 8, 7, 1, 0x3a),
                 (1, 10, 1, 1, 0, 0, 0), (1, 10, 3, 9, 7, 1, 0x3a),
                 (1, 10, 7, 8, 7, 1, 0x3a), (1, 0, 8, 0, 0, 1, 0)]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                parse_boot_diagnostic(META.pack(*fields))
        for raw in (b"", bytes(19), bytes(21)):
            with self.subTest(length=len(raw)), self.assertRaises(ProtocolError):
                parse_boot_diagnostic(raw)

    def test_metadata_rejects_wrong_version_id_state_length_and_error(self):
        cases = [(3, 2, 0, 5, 4, 0, 0), (1, 2, 0, 4, 4, 0, 0),
                 (1, 7, 0, 5, 4, 0, 0), (1, 2, 0, 5, 131073, 0, 0),
                 (1, 2, 0, 5, 0, 0, 0), (1, 3, 3, 5, 4, 0, 0)]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                Metadata.parse(META.pack(*fields), 5, 131072)
        with self.assertRaises(ProtocolError):
            Metadata.parse(b"x", 5, 131072)

    def test_wrong_order_duplicate_and_oversized_chunk_rejected_without_progress(self):
        data = b"\xff\xd8" + b"x" * 30 + b"\xff\xd9"
        meta = Metadata(2, 0, 4, len(data), zlib.crc32(data), 0)
        assembly = Assembly(meta)
        assembly.append(struct.pack("<II", 4, 0) + data[:12])
        for packet in (struct.pack("<II", 4, 0) + data[:12],
                       struct.pack("<II", 5, 12) + data[12:24],
                       struct.pack("<II", 4, 12) + data[12:25]):
            with self.assertRaises(ProtocolError):
                assembly.append(packet)
            self.assertEqual(assembly.offset, 12)
        with self.assertRaises(ProtocolError):
            assembly.finish()

    def test_version2_metadata_and_fixed128_payload_with_short_final_chunk(self):
        payload = b"\xff\xd8" + b"x" * 137 + b"\xff\xd9"
        raw = META.pack(2, 2, 0, 9, len(payload), zlib.crc32(payload) & 0xffffffff, 123)
        metadata = Metadata.parse(raw, 9, 131072)
        self.assertEqual(metadata.version, 2)
        assembly = Assembly(metadata)
        assembly.append(struct.pack("<II", 9, 0) + payload[:128])
        self.assertEqual(assembly.offset, 128)
        for bad in (struct.pack("<II", 9, 0) + payload[:13], struct.pack("<II", 9, 128) + payload[128:] + b"x"):
            with self.assertRaises(ProtocolError):
                assembly.append(bad)
            self.assertEqual(assembly.offset, 128)
        assembly.append(struct.pack("<II", 9, 128) + payload[128:])
        self.assertEqual(assembly.finish(), payload)

    def test_command_lengths_fit_default_mtu(self):
        self.assertEqual(len(capture_command(5, 5, 12)), 7)
        self.assertEqual(len(select_command(5, 12)), 9)
        with self.assertRaises(ValueError):
            capture_command(5, 99, 12)

    def test_capture_light_percent_conversion_and_bounds(self):
        for percent, duty in [(0, 0), (1, 3), (5, 13), (10, 26), (20, 51), (25, 64)]:
            self.assertEqual(capture_light_duty(percent), duty)
        for percent in (-1, 26, True, 1.5, "25", None):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                capture_light_duty(percent)
            with self.assertRaises(ValueError):
                Limits(capture_light_percent=percent)

    def test_optional_capture_light_byte_preserves_legacy_and_test_packets(self):
        legacy = struct.pack("<BIBB", 1, 5, 5, 12)
        self.assertEqual(capture_command(5, 5, 12), legacy)
        self.assertEqual(capture_command(5, 5, 12, flash_duty=0), legacy)
        self.assertEqual(capture_command(5, 5, 12, flash_duty=64), legacy + b"\x40")
        self.assertEqual(capture_command(5, 5, 12, flash_duty=1), legacy + b"\x01")
        self.assertEqual(capture_command(5, 5, 12, test=True, flash_duty=64),
                         struct.pack("<BIBB", 4, 5, 5, 12))
        self.assertEqual(len(retry_command(5)), 5)
        for duty in (-1, 65, True, 0.5):
            with self.subTest(duty=duty), self.assertRaises(ValueError):
                capture_command(5, 5, 12, flash_duty=duty)

    def test_gatt133_classifier_requires_exact_reviewed_structured_cause(self):
        self.assertTrue(retryable_gatt_133(gatt_error()))
        for error in (gatt_error(5), RuntimeError("Bluetooth GATT Error error=133"),
                      FakeBleakError("Bluetooth GATT Error error=133")):
            self.assertFalse(retryable_gatt_133(error))

    def test_default_wait_increase_preserves_session_retry_bounds(self):
        limits = Limits()
        self.assertEqual((limits.operation_seconds, limits.capture_seconds, limits.total_seconds), (5, 60, 600))
        self.assertEqual((limits.chunk_retries, limits.connect_seconds), (1, 30))

    def test_no_unbounded_settings(self):
        for options in ({"max_bytes": 131073}, {"chunk_retries": 2}, {"total_seconds": 601},
                        {"chunk_pause_seconds": 0}, {"connect_seconds": 1}, {"operation_seconds": 5.01},
                        {"capture_seconds": 120.001}, {"capture_seconds": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Limits(**options)


class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_light_setting_reaches_only_capture_command(self):
        for percent, length, duty in [(0, 7, None), (25, 8, 64)]:
            with self.subTest(percent=percent):
                transport = FakeTransport(b"\xff\xd8photo\xff\xd9")
                limits = Limits(total_seconds=5, capture_light_percent=percent)
                await Receiver(transport, lambda: None, limits).collect()
                command = transport.writes[0]
                self.assertEqual((command[0], len(command)), (1, length))
                if duty is not None:
                    self.assertEqual(command[7], duty)
                self.assertTrue(transport.released and transport.closed)

    async def test_light_setting_cannot_extend_test_or_pending_retry(self):
        limits = Limits(total_seconds=5, capture_light_percent=25)
        fixture = FakeTransport()
        await Receiver(fixture, lambda: None, limits).collect(test=True)
        self.assertEqual((fixture.writes[0][0], len(fixture.writes[0])), (4, 7))
        pending = FakeTransport(b"\xff\xd8pending\xff\xd9")
        await Receiver(pending, lambda: None, limits).collect(retry_pending=True)
        self.assertEqual((pending.writes[0][0], len(pending.writes[0])), (6, 5))

    async def test_small_transfer_crc_and_release(self):
        transport = FakeTransport()
        result = await Receiver(transport, lambda: None, FAST).collect(test=True)
        self.assertEqual(result.data, bytes(range(256)))
        self.assertEqual(transport.connects, 1)
        self.assertEqual(transport.reads, 22)
        self.assertEqual((result.protocol_version, result.chunk_bytes, result.mtu), (1, 12, 23))
        self.assertTrue(transport.released and transport.closed)
        self.assertLessEqual(result.capture_window_start, result.capture_window_end)
        self.assertLessEqual(result.capture_window_end, result.received_at)

    async def test_version2_fixture_uses_two_reads_and_reports_negotiated_mtu(self):
        transport = Version2Transport()
        result = await Receiver(transport, lambda: None, FAST).collect(test=True)
        self.assertEqual(result.data, bytes(range(256)))
        self.assertEqual(transport.reads, 2)
        self.assertEqual((result.protocol_version, result.chunk_bytes, result.mtu), (2, 128, 247))
        self.assertTrue(transport.released and transport.closed)

    async def test_version2_mtu_gate_prevents_select_or_data_with_too_small_mtu(self):
        for mtu in (None, 23, 136):
            with self.subTest(mtu=mtu):
                transport = Version2Transport()
                transport.mtu_size = mtu
                receiver = Receiver(transport, lambda: None, FAST)
                with self.assertRaisesRegex(ProtocolError, "MTU >=137"):
                    await receiver.collect(test=True)
                self.assertEqual(transport.reads, 0)
                self.assertFalse(any(value[0] == 2 for value in transport.writes))
                self.assertEqual(receiver.failure_phase, "mtu_gate")
                self.assertTrue(transport.released and transport.closed)
        transport = Version2Transport()
        transport.mtu_size = 137
        self.assertEqual((await Receiver(transport, lambda: None, FAST).collect(test=True)).data, bytes(range(256)))

    async def test_version_specific_data_bounds_and_metadata20_bound(self):
        for version, mode in ((1, "normal"), (2, "data_oversize"), (2, "metadata_oversize")):
            with self.subTest(version=version, mode=mode):
                transport = Version2Transport(version=version, mode=mode)
                with self.assertRaises(ProtocolError):
                    await Receiver(transport, lambda: None, FAST).collect(test=True)
                self.assertTrue(transport.released and transport.closed)
                self.assertEqual(transport.reads, 0 if mode == "metadata_oversize" else 2)

    async def test_real_jpeg_wire_bytes_and_partial_final_chunk(self):
        # Actual JPEG decode validation belongs to runtime executor; protocol also
        # enforces JPEG framing in addition to exact byte count and CRC.
        payload = b"\xff\xd8" + b"example" * 5 + b"\xff\xd9"
        transport = FakeTransport(payload)
        result = await Receiver(transport, lambda: None, FAST).collect()
        self.assertEqual(result.data, payload)
        self.assertEqual(transport.reads, 4)

    async def test_faults_reject_release_and_never_reconnect(self):
        for mode in ("oversize", "bad_crc", "bad_offset", "bad_id", "disconnect"):
            with self.subTest(mode=mode):
                transport = FakeTransport(mode=mode)
                with self.assertRaises((ProtocolError, ConnectionError)):
                    await Receiver(transport, lambda: None, FAST).collect(test=True)
                self.assertTrue(transport.released and transport.closed)
                self.assertEqual(transport.connects, 1)
                if mode in ("bad_offset", "bad_id"):
                    self.assertEqual(transport.reads, 2)
                if mode == "oversize":
                    self.assertEqual(transport.reads, 0)

    async def test_idempotent_chunk_retry_recovers_once(self):
        transport = FakeTransport(mode="once_timeout")
        result = await Receiver(transport, lambda: None, FAST).collect(test=True)
        self.assertEqual(result.data, bytes(range(256)))
        selects = [value for value in transport.writes if value[0] == 2]
        self.assertEqual(selects[0], selects[1])
        self.assertEqual(transport.connects, 1)
        self.assertEqual(result.chunk_retries, 1)

    async def test_ack_follows_durable_verification_and_precedes_release(self):
        payload = b"\xff\xd8" + b"camera" * 6 + b"\xff\xd9"
        transport = FakeTransport(payload)
        observed = []
        async def published(result):
            self.assertEqual(result.data, payload)
            self.assertFalse(any(value[0] == 5 for value in transport.writes))
            self.assertFalse(transport.released)
            observed.append("durable")
        await Receiver(transport, lambda: None, FAST).collect(on_verified=published)
        operations = [value[0] for value in transport.writes]
        self.assertEqual(observed, ["durable"])
        self.assertLess(operations.index(5), operations.index(3))

    async def test_publish_error_preserves_pending_no_ack_and_cleans_connection(self):
        transport = FakeTransport(b"\xff\xd8photo\xff\xd9")
        async def failed_publish(result):
            raise OSError("disk full fixture")
        with self.assertRaises(OSError):
            await Receiver(transport, lambda: None, FAST).collect(on_verified=failed_publish)
        self.assertFalse(any(value[0] == 5 for value in transport.writes))
        self.assertTrue(transport.released and transport.closed)

    async def test_retry_command_uses_new_id_and_marks_capture_origin(self):
        class PendingTransport(FakeTransport):
            async def write(self, characteristic, value):
                if value[0] == 6:
                    self.ident = struct.unpack_from("<I", value, 1)[0]
                await super().write(characteristic, value)
        transport = PendingTransport(b"\xff\xd8old-photo\xff\xd9")
        result = await Receiver(transport, lambda: None, FAST).collect(retry_pending=True)
        self.assertTrue(result.from_pending)
        self.assertEqual(transport.writes[0][0], 6)
        self.assertEqual(len(transport.writes[0]), 5)
        self.assertFalse(any(value[0] in (1, 4, 5) for value in transport.writes))

    async def test_structured_gatt133_idempotent_select_or_read_recovers_once(self):
        for phase in ("select", "data", "metadata"):
            with self.subTest(phase=phase):
                transport = GattFaultTransport(phase)
                result = await Receiver(transport, lambda: None, FAST).collect(test=True)
                self.assertEqual(result.data, bytes(range(256)))
                self.assertEqual(result.chunk_retries, 0 if phase == "metadata" else 1)
                self.assertEqual(result.metadata_retries, 1 if phase == "metadata" else 0)
                self.assertEqual(sum(value[0] in (1, 4) for value in transport.writes), 1)
                self.assertTrue(transport.released and transport.closed)
                if phase == "select":
                    selects = [value for value in transport.writes if value[0] == 2]
                    self.assertEqual(selects[0], selects[1])

    async def test_repeated_gatt133_stops_after_one_retry(self):
        for phase in ("select", "data", "metadata"):
            with self.subTest(phase=phase):
                transport = GattFaultTransport(phase, permanent=True)
                receiver = Receiver(transport, lambda: None, FAST)
                with self.assertRaises(FakeBleakError):
                    await receiver.collect(test=True)
                self.assertEqual(transport.faults, 2)
                self.assertEqual(receiver.failure_phase, {"select": "select_chunk", "data": "data_read", "metadata": "metadata_read"}[phase])
                self.assertTrue(transport.released and transport.closed)

    async def test_other_gatt_errors_and_capture_command_never_retry(self):
        for phase, code in (("data", 5), ("metadata", 5), ("capture", 133)):
            with self.subTest(phase=phase, code=code):
                transport = GattFaultTransport(phase, code=code, permanent=True)
                with self.assertRaises(FakeBleakError):
                    await Receiver(transport, lambda: None, FAST).collect(test=True)
                self.assertEqual(transport.faults, 1)
                self.assertEqual(sum(value[0] in (1, 4) for value in transport.writes), 1)
                self.assertTrue(transport.closed)

    async def test_cancellation_mid_transfer_releases_connection(self):
        transport = FakeTransport(mode="stall")
        task = asyncio.create_task(Receiver(transport, lambda: None, FAST).collect(test=True))
        await transport.selected.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport.released and transport.closed)

    async def test_cancellation_during_connect_closes_partial_client(self):
        transport = FakeTransport(mode="connect_stall")
        task = asyncio.create_task(Receiver(transport, lambda: None, FAST).collect(test=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport.closed)
        self.assertFalse(transport.released)

    async def test_repeated_cancellation_cannot_skip_disconnect(self):
        class SlowCleanup(FakeTransport):
            def __init__(self):
                super().__init__(mode="stall")
                self.closing = asyncio.Event()
                self.allow_close = asyncio.Event()

            async def close(self):
                self.closing.set()
                await self.allow_close.wait()
                self.closed = True

        transport = SlowCleanup()
        receiver = Receiver(transport, lambda: None, FAST)
        task = asyncio.create_task(receiver.collect(test=True))
        await transport.selected.wait()
        task.cancel()
        await transport.closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        transport.allow_close.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(transport.released and transport.closed)

    async def test_failed_disconnect_cannot_be_reported_as_success(self):
        class FailedCleanup(FakeTransport):
            async def close(self):
                raise ConnectionError("release not confirmed")

        receiver = Receiver(FailedCleanup(), lambda: None, FAST)
        with self.assertRaisesRegex(RuntimeError, "disconnect unconfirmed"):
            await receiver.collect(test=True)
        self.assertIsNotNone(receiver.cleanup_error)

    async def test_concurrent_collection_rejected(self):
        transport = FakeTransport(mode="stall")
        receiver = Receiver(transport, lambda: None, FAST)
        first = asyncio.create_task(receiver.collect(test=True))
        await transport.selected.wait()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            await receiver.collect(test=True)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    async def test_presence_regression_stops_before_next_chunk(self):
        transport = FakeTransport()
        def guard():
            if transport.reads >= 1:
                raise RuntimeError("presence unavailable")
        with self.assertRaisesRegex(RuntimeError, "presence unavailable"):
            await Receiver(transport, guard, FAST).collect(test=True)
        self.assertEqual(transport.reads, 1)
        self.assertTrue(transport.closed and transport.released)

    async def test_capture_deadline_is_bounded(self):
        transport = FakeTransport(mode="busy")
        limits = Limits(capture_seconds=0.01, total_seconds=1, chunk_pause_seconds=0.02)
        with self.assertRaises(TimeoutError):
            await Receiver(transport, lambda: None, limits).collect(test=True)
        self.assertTrue(transport.closed and transport.released)

    async def test_session_deadline_is_bounded(self):
        transport = FakeTransport(mode="stall")
        limits = Limits(total_seconds=0.02, chunk_pause_seconds=0.02)
        with self.assertRaises(TimeoutError):
            await Receiver(transport, lambda: None, limits).collect(test=True)
        self.assertTrue(transport.closed and transport.released)

    async def test_bad_test_payload_and_bad_jpeg_rejected(self):
        for test in (True, False):
            transport = FakeTransport(b"corrupt content")
            with self.assertRaises(ProtocolError):
                await Receiver(transport, lambda: None, FAST).collect(test=test)
            self.assertTrue(transport.closed and transport.released)


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only import the route policy with explicit dependency fakes. This does
        # not claim compatibility with an installed Home Assistant deployment.
        retry = types.ModuleType("bleak_retry_connector")
        retry.BleakClientWithServiceCache = object
        retry.establish_connection = None
        ha = types.ModuleType("homeassistant")
        components = types.ModuleType("homeassistant.components")
        components.bluetooth = types.SimpleNamespace()
        with patch.dict(sys.modules, {"bleak_retry_connector": retry, "homeassistant": ha,
                                      "homeassistant.components": components}):
            from ble_meter_camera import transport
            cls.module = transport

    def candidate(self, source="AA:BB:CC:DD:EE:FF"):
        return types.SimpleNamespace(scanner=types.SimpleNamespace(source=source),
                                     ble_device=types.SimpleNamespace(details={"source": source}))

    def test_requested_dedicated_proxy_route_allowed_among_stronger_alternates(self):
        candidate = self.candidate()
        self.assertIs(self.module.require_proxy_route([self.candidate("00:11:22:33:44:55"), candidate], "aa:bb:cc:dd:ee:ff"), candidate)

    def test_no_route_alternate_adapter_and_duplicate_sources_blocked(self):
        for routes in ([], [self.candidate("00:11:22:33:44:55")],
                       [self.candidate(), self.candidate()]):
            with self.subTest(routes=routes), self.assertRaises(self.module.RouteError):
                self.module.require_proxy_route(routes, "AA:BB:CC:DD:EE:FF")

    def test_managed_selection_uses_only_dedicated_proxy_and_keeps_manager_hook(self):
        candidate = self.candidate()
        alternate = self.candidate("00:11:22:33:44:55")
        manager = types.SimpleNamespace(async_scanner_by_source=lambda source: candidate.scanner)
        calls = []
        selected = types.SimpleNamespace(source=candidate.scanner.source)
        def choose(manager_arg, scanner_arg, device_arg):
            calls.append((manager_arg, scanner_arg, device_arg))
            return selected
        client = types.SimpleNamespace(_async_get_backend_for_ble_device=choose)
        self.assertIs(self.module.select_managed_backend(client, manager, [alternate, candidate],
                                                       candidate.scanner.source), selected)
        self.assertEqual(calls, [(manager, candidate.scanner, candidate.ble_device)])
        client._async_get_backend_for_ble_device = lambda *args: None
        with self.assertRaisesRegex(self.module.RouteError, "connection slot"):
            self.module.select_managed_backend(client, manager, [candidate], candidate.scanner.source)

    def test_unreviewed_dependency_versions_block_before_connect(self):
        with patch.object(self.module, "version", return_value="0.0.0"):
            with self.assertRaises(self.module.RouteError):
                self.module.check_supported_versions()

    def test_stale_cleanup_requires_disconnected_untracked_and_free_current_proxy(self):
        backend_class = type("ESPHomeClient", (), {"__module__": "bleak_esphome.backend.client"})
        backend = backend_class()
        backend._source = "AA:BB:CC:DD:EE:FF"
        client = types.SimpleNamespace(_backend=backend, is_connected=False, _connected_scanner=None)
        slots = types.SimpleNamespace(adapter=backend._source, slots=1, free=1, allocated=[])
        scanner = types.SimpleNamespace(source=backend._source, connectable=True, scanning=True,
                    connector=types.SimpleNamespace(can_connect=lambda: True), get_allocations=lambda: slots)
        self.assertIsNotNone(self.module.confirmed_stale_cleanup(client, scanner, backend._source))
        for target, name, value in ((client, "is_connected", True), (client, "_connected_scanner", scanner),
                                     (slots, "free", 0), (slots, "allocated", ["00:11:22:33:44:55"]),
                                     (scanner, "scanning", False), (scanner, "source", "00:11:22:33:44:55")):
            previous = getattr(target, name)
            setattr(target, name, value)
            self.assertIsNone(self.module.confirmed_stale_cleanup(client, scanner, backend._source))
            setattr(target, name, previous)
        scanner.connector.can_connect = lambda: False
        self.assertIsNone(self.module.confirmed_stale_cleanup(client, scanner, backend._source))

    def test_actual_backend_source_must_match_and_be_esphome(self):
        backend_class = type("ESPHomeClient", (), {"__module__": "bleak_esphome.backend.client"})
        backend = backend_class()
        backend._source = "AA:BB:CC:DD:EE:FF"
        client = types.SimpleNamespace(_backend=backend)
        self.assertEqual(self.module.verify_connected_route(client, backend._source), backend._source)
        with self.assertRaises(self.module.RouteError):
            self.module.verify_connected_route(client, "00:11:22:33:44:55")
        with self.assertRaises(self.module.RouteError):
            self.module.verify_connected_route(types.SimpleNamespace(_backend=object()), backend._source)
        client._connected_scanner = types.SimpleNamespace(source="00:11:22:33:44:55")
        with self.assertRaises(self.module.RouteError):
            self.module.verify_connected_route(client, backend._source)


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        RouteTests.setUpClass()
        self.module = RouteTests.module
        backend_class = type("ESPHomeClient", (), {"__module__": "bleak_esphome.backend.client"})
        self.backend = backend_class()
        self.backend._source = "AA:BB:CC:DD:EE:FF"

    async def test_pair_none_success_on_verified_route(self):
        calls = []
        async def pair():
            calls.append("paired")
        client = types.SimpleNamespace(_backend=self.backend, pair=pair)
        await self.module.pair_selected_proxy(client, self.backend._source)
        self.assertEqual(calls, ["paired"])

    async def test_wrong_route_never_pairs(self):
        async def pair():
            self.fail("pair called on wrong route")
        client = types.SimpleNamespace(_backend=self.backend, pair=pair)
        with self.assertRaises(self.module.RouteError):
            await self.module.pair_selected_proxy(client, "00:11:22:33:44:55")

    async def test_pair_failure_and_changed_route_block(self):
        for mode in ("false", "exception", "route_change"):
            self.backend._source = "AA:BB:CC:DD:EE:FF"
            async def pair():
                if mode == "exception":
                    raise ConnectionError("pair rejected fixture")
                if mode == "route_change":
                    self.backend._source = "00:11:22:33:44:55"
                return False if mode == "false" else None
            client = types.SimpleNamespace(_backend=self.backend, pair=pair)
            with self.subTest(mode=mode), self.assertRaises((ConnectionError, self.module.RouteError)):
                await self.module.pair_selected_proxy(client, "AA:BB:CC:DD:EE:FF")


class StaleCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_disconnect_then_free_proxy_does_not_resend_stale_api(self):
        RouteTests.setUpClass()
        module = RouteTests.module
        async def executor(function, *args):
            return function(*args)
        transport = module.ManagedTransport(types.SimpleNamespace(async_add_executor_job=executor), "00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF")
        backend_class = type("ESPHomeClient", (), {"__module__": "bleak_esphome.backend.client"})
        backend = backend_class()
        backend._source = transport.source
        calls = []
        async def disconnect():
            calls.append("disconnect")
            raise TimeoutError("old API callback never responds fixture")
        client = types.SimpleNamespace(_backend=backend, is_connected=False, _connected_scanner=None, disconnect=disconnect)
        transport.client = client
        with self.assertRaises(TimeoutError):
            await transport.close()
        self.assertIs(transport.client, client)
        slots = types.SimpleNamespace(adapter=transport.source, slots=1, free=0, allocated=[])
        scanner = types.SimpleNamespace(source=transport.source, connectable=True, scanning=True,
                    connector=types.SimpleNamespace(can_connect=lambda: True), get_allocations=lambda: slots)
        with patch.object(module, "check_supported_versions", return_value=module.SUPPORTED_PACKAGES), \
             patch.object(module.bluetooth, "async_scanner_by_source", lambda hass, source: scanner, create=True):
            for _ in range(2):
                with self.assertRaisesRegex(module.RouteError, "awaits current dedicated proxy"):
                    await transport.close()
                self.assertIs(transport.client, client)
                self.assertEqual(calls, ["disconnect"])
            slots.free = 1
            await transport.close()
        self.assertEqual(calls, ["disconnect"])
        self.assertIsNone(transport.client)
        self.assertEqual(transport.last_cleanup_proof["free"], 1)


    async def test_fresh_runtime_recovery_requires_live_expected_proxy_free_proof(self):
        RouteTests.setUpClass()
        module = RouteTests.module
        async def executor(function, *args):
            return function(*args)
        transport = module.ManagedTransport(types.SimpleNamespace(async_add_executor_job=executor), "00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF")
        slots = types.SimpleNamespace(adapter=transport.source, slots=1, free=0, allocated=[])
        scanner = types.SimpleNamespace(source=transport.source, connectable=True, scanning=True,
                    connector=types.SimpleNamespace(can_connect=lambda: True), get_allocations=lambda: slots)
        with patch.object(module, "check_supported_versions", return_value=module.SUPPORTED_PACKAGES), \
             patch.object(module.bluetooth, "async_scanner_by_source", lambda hass, source: scanner, create=True):
            with self.assertRaises(module.RouteError):
                await transport.recover_close()
            slots.free = 1
            await transport.recover_close()
        self.assertEqual(transport.last_cleanup_proof["method"], "fresh_runtime_no_client_and_current_proxy_all_slots_free")

    async def test_connect_never_overwrites_any_retained_client(self):
        RouteTests.setUpClass()
        module = RouteTests.module
        transport = module.ManagedTransport(object(), "00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF")
        retained = object()
        transport.client = retained
        with self.assertRaises(module.RouteError):
            await transport.connect()
        self.assertIs(transport.client, retained)


if __name__ == "__main__":
    unittest.main()


class ConnectDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        RouteTests.setUpClass()
        self.module = RouteTests.module

    async def test_connect_timeout_preserves_subphase_before_normal_cleanup(self):
        for subphase, services in [("managed_connect_and_services", False), ("pair_request", True)]:
            with self.subTest(subphase=subphase):
                class ConnectTimeout(FakeTransport):
                    connect_subphase = subphase
                    async def connect(self):
                        self.connects += 1
                        raise TimeoutError()
                    def connect_diagnostics(self):
                        return {"subphase": self.connect_subphase, "client_connected": True,
                                "cached_services_available": services}
                    async def close(self):
                        self.connect_subphase = "cleanup"
                        await super().close()
                transport = ConnectTimeout()
                receiver = Receiver(transport, lambda: None, FAST)
                with self.assertRaises(TimeoutError):
                    await receiver.collect(test=True)
                self.assertEqual(receiver.failure_phase, "connect")
                self.assertEqual(receiver.failure_connect_subphase, subphase)
                self.assertEqual(receiver.connect_timeout_snapshot["subphase"], subphase)
                self.assertEqual(receiver.connect_timeout_snapshot["cached_services_available"], services)
                self.assertEqual(transport.connects, 1)
                self.assertEqual(transport.writes, [])
                self.assertTrue(transport.closed)

    async def test_connect_snapshot_contains_only_cached_public_flags(self):
        transport = self.module.ManagedTransport(object(), "00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF")
        calls = []
        services = types.SimpleNamespace(get_characteristic=lambda uuid: calls.append(uuid) or object())
        transport.client = types.SimpleNamespace(is_connected=True, services=services, password="fake-private-value")
        transport.connect_subphase = "pair_request"
        snapshot = transport.connect_diagnostics()
        self.assertEqual(set(snapshot), {"subphase", "client_exists", "client_connected",
                                         "cached_services_available", "cached_expected_characteristics"})
        self.assertTrue(snapshot["client_connected"] and snapshot["cached_services_available"])
        self.assertTrue(all(snapshot["cached_expected_characteristics"].values()))
        self.assertEqual(len(calls), 6)
        class UnavailableClient:
            @property
            def is_connected(self):
                raise RuntimeError("property unavailable")
            @property
            def services(self):
                raise LookupError("not discovered")
        transport.client = UnavailableClient()
        snapshot = transport.connect_diagnostics()
        self.assertIsNone(snapshot["client_connected"])
        self.assertFalse(snapshot["cached_services_available"])
        self.assertEqual(snapshot["services_property_error_type"], "LookupError")

    async def test_new_attempt_clears_stale_handle_and_mtu_diagnostics(self):
        module = self.module
        async def executor(function, *args):
            return function(*args)
        transport = module.ManagedTransport(types.SimpleNamespace(async_add_executor_job=executor),
                                             "00:11:22:33:44:55", "AA:BB:CC:DD:EE:FF")
        transport.characteristic_handles = {"data": 46}
        transport.mtu_size = 247
        with patch.object(module, "check_supported_versions", return_value=module.SUPPORTED_PACKAGES), \
             patch.object(module.bluetooth, "async_scanner_devices_by_address", return_value=[], create=True):
            with self.assertRaises(module.RouteError):
                await transport.connect()
        self.assertEqual(transport.connect_subphase, "route_discovery")
        self.assertEqual(transport.characteristic_handles, {})
        self.assertIsNone(transport.mtu_size)

    async def test_diagnostic_read_failure_does_not_replace_original_timeout(self):
        class ConnectTimeout(FakeTransport):
            connect_subphase = "pair_request"
            async def connect(self):
                raise TimeoutError("original timeout")
            def connect_diagnostics(self):
                raise RuntimeError("diagnostic failure")
        receiver = Receiver(ConnectTimeout(), lambda: None, FAST)
        with self.assertRaisesRegex(TimeoutError, "original timeout"):
            await receiver.collect(test=True)
        self.assertEqual(receiver.connect_timeout_snapshot, {"snapshot_error_type": "RuntimeError"})

    async def test_real_transport_marks_managed_service_setup_versus_explicit_pair(self):
        module = self.module
        expected = 'AA:BB:CC:DD:EE:FF'
        candidate = RouteTests().candidate(expected)
        backend_class = type('ESPHomeClient', (), {'__module__': 'bleak_esphome.backend.client'})
        async def executor(function, *args):
            return function(*args)
        for failure in ['services', 'pair']:
            with self.subTest(failure=failure):
                class FakeBaseClient:
                    def __init__(self, *args, **kwargs):
                        self._backend = backend_class()
                        self._backend._source = expected
                        self.is_connected = True
                        self.services = None
                    async def connect(self, **kwargs):
                        if failure == 'services':
                            raise TimeoutError('service setup fixture')
                        self.services = types.SimpleNamespace(get_characteristic=lambda uuid: object())
                    async def pair(self):
                        raise TimeoutError('pair reply fixture')
                    async def disconnect(self):
                        self.is_connected = False
                async def establish(client_type, *args, **kwargs):
                    client = client_type()
                    await client.connect()
                    return client
                transport = module.ManagedTransport(types.SimpleNamespace(async_add_executor_job=executor),
                                                     '00:11:22:33:44:55', expected)
                receiver = Receiver(transport, lambda: None, FAST)
                with patch.object(module, 'BleakClientWithServiceCache', FakeBaseClient), \
                     patch.object(module, 'establish_connection', establish), \
                     patch.object(module, 'check_supported_versions', return_value=module.SUPPORTED_PACKAGES), \
                     patch.object(module.bluetooth, 'async_scanner_devices_by_address', return_value=[candidate], create=True):
                    with self.assertRaises(TimeoutError):
                        await receiver.collect(test=True)
                self.assertEqual(receiver.failure_connect_subphase,
                                 'managed_connect_and_services' if failure == 'services' else 'pair_request')
                self.assertEqual(receiver.connect_timeout_snapshot['cached_services_available'], failure == 'pair')
                self.assertTrue(receiver.connect_timeout_snapshot['client_connected'])
                self.assertIsNone(transport.client)
                self.assertEqual(receiver.cleanup_error, None)


class ManagedServiceCacheTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real ManagedTransport with a shared old proxy service cache."""
    async def asyncSetUp(self):
        RouteTests.setUpClass()
        self.module = RouteTests.module
        self.expected = "AA:BB:CC:DD:EE:FF"
        self.candidate = RouteTests().candidate(self.expected)

    def fixture(self, *, missing=True, stale_count=3, refresh=True, clear_result=True, clear_failure=None,
                disconnect_failure=False, wrong_route_after_clear=False, wrong_reconnect=False):
        module, expected = self.module, self.expected
        log, clients = [], []
        cache = {"stale": missing, "cleared": False}
        all_uuids = [uuid for _, uuid in module.REQUIRED_CHARACTERISTICS]
        backend_class = type("ESPHomeClient", (), {"__module__": "bleak_esphome.backend.client"})
        class FakeBaseClient:
            def __init__(self, *args, **kwargs):
                self._backend = backend_class()
                self._backend._source = expected
                self._connected_scanner = types.SimpleNamespace(source=expected)
                self.is_connected = False
                self.services = None
                self.mtu_size = 247
                clients.append(self)
            async def connect(self, **kwargs):
                self.is_connected = True
                use_cache = kwargs.get("dangerous_use_bleak_cache", False)
                log.append(("connect", use_cache))
                if not use_cache and cache["cleared"] and refresh:
                    cache["stale"] = False
                available = all_uuids[:stale_count] if cache["stale"] else all_uuids
                self.services = types.SimpleNamespace(get_characteristic=lambda uuid:
                    types.SimpleNamespace(handle=all_uuids.index(uuid) + 40) if uuid in available else None)
                if wrong_reconnect and len(clients) > 1:
                    self._backend._source = "00:11:22:33:44:55"
            async def pair(self):
                log.append(("pair", self.is_connected))
            async def clear_cache(self):
                if not self.is_connected:
                    raise AssertionError("cache clear was sent while disconnected")
                log.append(("clear", self._backend._source))
                if clear_failure:
                    raise clear_failure
                cache["cleared"] = True
                if wrong_route_after_clear:
                    self._backend._source = "00:11:22:33:44:55"
                return clear_result
            async def disconnect(self):
                log.append(("disconnect", self.is_connected))
                if disconnect_failure:
                    raise TimeoutError("disconnect unconfirmed fixture")
                self.is_connected = False
                self._connected_scanner = None
        async def establish(client_type, device, *args, **kwargs):
            self.assertEqual(device.details["source"], expected)
            self.assertEqual(kwargs["max_attempts"], 1)
            log.append(("establish", kwargs["use_services_cache"]))
            client = client_type()
            await client.connect(dangerous_use_bleak_cache=kwargs["use_services_cache"])
            return client
        async def executor(function, *args):
            return function(*args)
        transport = module.ManagedTransport(types.SimpleNamespace(async_add_executor_job=executor),
                                            "00:11:22:33:44:55", expected)
        patches = (
            patch.object(module, "BleakClientWithServiceCache", FakeBaseClient),
            patch.object(module, "establish_connection", establish),
            patch.object(module, "check_supported_versions", return_value=module.SUPPORTED_PACKAGES),
            patch.object(module.bluetooth, "async_scanner_devices_by_address", return_value=[self.candidate], create=True),
        )
        from contextlib import ExitStack
        stack = ExitStack()
        for fixture_patch in patches:
            stack.enter_context(fixture_patch)
        self.addCleanup(stack.close)
        return transport, log, clients

    async def test_complete_characteristics_need_no_refresh_or_reconnect(self):
        transport, log, clients = self.fixture(missing=False)
        self.assertEqual(await transport.connect(), self.expected)
        self.assertFalse(transport.cache_refresh_attempted)
        self.assertEqual(len(clients), 1)
        self.assertFalse(any(event[0] == "clear" for event in log))
        self.assertEqual(set(transport.characteristic_handles), {name for name, _ in self.module.REQUIRED_CHARACTERISTICS})
        await transport.close()

    async def test_old_three_characteristic_cache_cleared_live_then_once_uncached(self):
        transport, log, clients = self.fixture()
        self.assertEqual(await transport.connect(), self.expected)
        self.assertTrue(transport.cache_refresh_attempted)
        self.assertEqual([event for event in log if event[0] == "establish"], [("establish", True), ("establish", False)])
        self.assertEqual([event for event in log if event[0] == "clear"], [("clear", self.expected)])
        clear_index = next(i for i, event in enumerate(log) if event[0] == "clear")
        self.assertEqual(log[clear_index + 1], ("disconnect", True))
        self.assertFalse(clients[0].is_connected)
        self.assertTrue(clients[1].is_connected)
        self.assertEqual(transport.connect_subphase, "ready")
        self.assertIn("manifest", transport.characteristic_handles)
        self.assertIn("health", transport.characteristic_handles)
        self.assertIn("bootdiag", transport.characteristic_handles)
        await transport.close()

    async def test_old_five_characteristic_cache_refreshes_for_bootdiag_once(self):
        transport, log, clients = self.fixture(stale_count=5)
        self.assertEqual(await transport.connect(), self.expected)
        self.assertEqual(len(transport.characteristic_handles), 6)
        self.assertIn("bootdiag", transport.characteristic_handles)
        self.assertEqual(len(clients), 2)
        self.assertEqual(sum(event[0] == "clear" for event in log), 1)
        self.assertEqual([event for event in log if event[0] == "establish"],
                         [("establish", True), ("establish", False)])
        await transport.close()

    async def test_still_missing_after_refresh_fails_before_any_control_command(self):
        transport, log, clients = self.fixture(refresh=False)
        with self.assertRaisesRegex(self.module.RouteError, "still missing after one cache refresh: manifest, health, bootdiag"):
            await transport.connect()
        self.assertEqual(len(clients), 2)
        self.assertEqual(sum(event[0] == "clear" for event in log), 1)
        self.assertNotEqual(transport.connect_subphase, "ready")
        await transport.close()

    async def test_unconfirmed_cache_clear_has_no_reconnect(self):
        transport, log, clients = self.fixture(clear_result=False)
        with self.assertRaisesRegex(self.module.RouteError, "did not confirm"):
            await transport.connect()
        self.assertEqual(len(clients), 1)
        self.assertEqual(transport.connect_subphase, "connected_service_cache_clear")
        await transport.close()

    async def test_cache_clear_timeout_keeps_client_for_normal_receiver_cleanup(self):
        transport, log, clients = self.fixture(clear_failure=TimeoutError("cache response fixture"))
        receiver = Receiver(transport, lambda: None, FAST)
        with self.assertRaises(TimeoutError):
            await receiver.collect(test=True)
        self.assertEqual(len(clients), 1)
        self.assertIsNone(transport.client)
        self.assertIsNone(receiver.cleanup_error)
        self.assertEqual(receiver.failure_connect_subphase, "connected_service_cache_clear")
        self.assertEqual(sum(event[0] == "disconnect" for event in log), 1)

    async def test_unconfirmed_disconnect_prevents_new_client(self):
        transport, log, clients = self.fixture(disconnect_failure=True)
        with self.assertRaises(TimeoutError):
            await transport.connect()
        self.assertEqual(len(clients), 1)
        self.assertIs(transport.client, clients[0])
        self.assertEqual(transport.connect_subphase, "service_cache_disconnect")

    async def test_changed_route_during_refresh_blocks_reconnect(self):
        transport, log, clients = self.fixture(wrong_route_after_clear=True)
        with self.assertRaisesRegex(self.module.RouteError, "actual connected route differs"):
            await transport.connect()
        self.assertEqual(len(clients), 1)
        self.assertEqual(sum(event[0] == "clear" for event in log), 1)
        await transport.close()

    async def test_uncached_reconnect_route_is_verified_again(self):
        transport, log, clients = self.fixture(wrong_reconnect=True)
        with self.assertRaisesRegex(self.module.RouteError, "actual connected route differs"):
            await transport.connect()
        self.assertEqual(len(clients), 2)
        self.assertEqual(sum(event[0] == "pair" for event in log), 1)
        await transport.close()
