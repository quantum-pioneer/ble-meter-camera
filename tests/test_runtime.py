"""Receiver lifecycle/storage tests with HA interfaces faked; no live radio proof."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from PIL import Image

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "ble_meter_camera"
package = types.ModuleType("ble_meter_camera")
package.__path__ = [str(PACKAGE)]
sys.modules.setdefault("ble_meter_camera", package)


class HomeAssistantError(Exception):
    pass


class FakeStore:
    data = {}
    def __init__(self, hass, version, key, private=False, *, atomic_writes=False):
        self.key = key
        self.private = private
        self.atomic_writes = atomic_writes
        self.path = hass.config.path(".storage", key)
        self.swallow_writes = False
    async def async_load(self):
        return self.data.get(self.key)
    async def async_save(self, data):
        self.data[self.key] = data
        if self.swallow_writes:
            # Real HA Store can cache data then log/suppress a disk write error.
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "data": data}))
        os.chmod(path, 0o600 if self.private else 0o644)


class FakeTransport:
    def __init__(self, *args):
        self.closed = False
    async def close(self):
        self.closed = True
    async def recover_close(self):
        await self.close()


def load_runtime():
    names = ["homeassistant", "homeassistant.exceptions", "homeassistant.helpers",
             "homeassistant.helpers.dispatcher", "homeassistant.helpers.storage", "ble_meter_camera.transport"]
    stubs = {name: types.ModuleType(name) for name in names}
    stubs["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError
    stubs["homeassistant.helpers.dispatcher"].async_dispatcher_send = lambda *args: None
    stubs["homeassistant.helpers.storage"].Store = FakeStore
    stubs["ble_meter_camera.transport"].ManagedTransport = FakeTransport
    with patch.dict(sys.modules, stubs):
        from ble_meter_camera import runtime
        return runtime


runtime_module = load_runtime()
from ble_meter_camera.transfer import Result
from ble_meter_camera.protocol import Manifest
from ble_meter_camera.policy import CUTOFF, TrustedClock, ClockError
from ble_meter_camera.const import TEST_RUN_CODE
from ble_meter_camera.history import History
from ble_meter_camera.storage import MAX_STORE_BYTES, verify_store_file


def jpeg():
    output = BytesIO()
    Image.new("RGB", (320, 240), (100, 120, 130)).save(output, "JPEG")
    return output.getvalue()


class FakeHass:
    def __init__(self, directory):
        self.config = types.SimpleNamespace(path=lambda *parts: str(Path(directory).joinpath(*parts)))
    def async_create_task(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)
    def async_create_background_task(self, coroutine, name):
        return asyncio.create_task(coroutine, name=name)
    async def async_add_executor_job(self, function, *args):
        return function(*args)


class ResultReceiver:
    def __init__(self, data, *, failure=None, delay=False):
        self.data, self.failure, self.delay = data, failure, delay
        self.cleanup_error = None
        self.transport = FakeTransport()
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()
        self.acked = False
    async def collect(self, **kwargs):
        self.started.set()
        try:
            if self.delay:
                await asyncio.Event().wait()
            if self.failure:
                raise self.failure
            now = datetime.now(timezone.utc)
            result = Result(self.data, 5, 0xffffffff if kwargs.get("retry_pending") else 1234,
                            now, now, now, 1.2, "AA:BB:CC:DD:EE:FF", kwargs["test"], kwargs.get("retry_pending", False),
                            manifest=Manifest(1|2|4|16|(64 if kwargs.get("retry_pending") else 0), 0,
                                              int(now.timestamp()), int(now.timestamp())+604800, TEST_RUN_CODE, 1))
            if kwargs.get("on_verified"):
                await kwargs["on_verified"](result)
                self.acked = True
            return result
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class RecoveryTransport(FakeTransport):
    def __init__(self, failures=0):
        super().__init__()
        self.failures, self.close_calls = failures, 0
        self.before_close = None
    async def close(self):
        self.close_calls += 1
        if self.before_close:
            self.before_close()
        if self.failures:
            self.failures -= 1
            raise ConnectionError("proxy still unavailable fixture")
        self.closed = True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeStore.data.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.hass = FakeHass(self.temp.name)
        self.entry = types.SimpleNamespace(entry_id="test", data={"address": "00:11:22:33:44:55",
            "proxy_source": "AA:BB:CC:DD:EE:FF"}, options={})
        self.runtime = runtime_module.Runtime(self.hass, self.entry)
        await self.runtime.start()
    async def asyncTearDown(self):
        await self.runtime.stop()
        self.temp.cleanup()

    async def test_latest_and_state_stores_request_private_permissions(self):
        self.assertTrue(self.runtime.store.private)
        self.assertTrue(self.runtime.state_store.private)
        self.assertTrue(self.runtime.store.atomic_writes)
        self.assertTrue(self.runtime.state_store.atomic_writes)

    async def test_capture_light_option_reaches_limits_and_legacy_defaults_off(self):
        self.assertEqual(self.runtime.receiver.limits.capture_light_percent, 0)
        entry = types.SimpleNamespace(entry_id="lit", data=self.entry.data,
                                      options={"capture_light_percent": 25})
        lit = runtime_module.Runtime(self.hass, entry)
        self.assertEqual(lit.receiver.limits.capture_light_percent, 25)

    async def test_timing_diagnostic_uses_existing_status_attributes_without_storage_changes(self):
        await self.runtime._save_state()
        before = Path(self.runtime.state_store.path).read_bytes()
        self.runtime.receiver.last_attempt_timing = {
            "version": 1, "outcome": "failed", "timeout_classification": "metadata_operation",
            "events": [{"event": "metadata_read_end", "operation_timeout_expired": True}]}
        exposed = self.runtime.attributes["last_attempt_timing_diagnostic"]
        self.assertEqual(exposed["timeout_classification"], "metadata_operation")
        self.assertEqual(self.runtime.attributes["receiver_capture_ready_limit_seconds"], 60)
        self.runtime.receiver.limits = replace(self.runtime.receiver.limits, capture_seconds=12)
        self.assertEqual(self.runtime.attributes["receiver_capture_ready_limit_seconds"], 12)
        exposed["events"][0]["event"] = "snapshot only"
        self.assertEqual(self.runtime.receiver.last_attempt_timing["events"][0]["event"], "metadata_read_end")
        self.assertEqual(Path(self.runtime.state_store.path).read_bytes(), before)

    async def prepare_cleanup_recovery(self, failures=0, periodic=True):
        await self.runtime.set_enabled(True)
        if periodic:
            await self.runtime.set_periodic(True)
        fake = ResultReceiver(b"", failure=TimeoutError("C6 interrupted fixture"))
        fake.cleanup_error = "disconnect unconfirmed: TimeoutError"
        fake.transport = RecoveryTransport(failures)
        self.runtime.receiver = fake
        with self.assertRaises(HomeAssistantError):
            await self.runtime.capture()
        self.assertEqual(self.runtime.status, "recovering_cleanup")
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)
        return fake

    async def test_cleanup_confirmed_resumes_original_intent_deadline_and_backoff(self):
        fake = await self.prepare_cleanup_recovery()
        deadline, backoff = self.runtime.bench_until, self.runtime.retry_after
        def budget_persisted_first():
            state = FakeStore.data[self.runtime.state_store.key]
            self.assertEqual(state["cleanup_recovery"]["attempts"], 1)
            self.assertTrue(state["cleanup_recovery"]["desired_enabled"])
        fake.transport.before_close = budget_persisted_first
        await self.runtime._recover_cleanup()
        self.assertEqual(fake.transport.close_calls, 1)
        self.assertTrue(self.runtime.enabled and self.runtime.periodic)
        self.assertEqual(self.runtime.bench_until, deadline)
        self.assertEqual(self.runtime.retry_after, backoff)
        self.assertIsNone(self.runtime.safety_trip)
        self.assertIsNone(fake.cleanup_error)
        self.assertEqual(self.runtime.last_cleanup_recovery["outcome"], "confirmed")
        self.assertEqual(self.runtime.failures, 1)

    async def test_cleanup_waits_for_100_second_proxy_reconnect_and_resumes_at_120(self):
        start = datetime.now(timezone.utc)
        class Clock(datetime):
            current = start
            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
        attempts = []
        class ReconnectingProxy(RecoveryTransport):
            async def close(self):
                attempts.append(int((Clock.current - start).total_seconds()))
                if Clock.current < start + timedelta(seconds=100):
                    raise ConnectionError("current proxy free-slot proof not ready fixture")
                self.closed = True
        with patch.object(runtime_module, "datetime", Clock):
            self.runtime.clock = TrustedClock(now=lambda: Clock.current, elapsed=lambda: (Clock.current-start).total_seconds())
            fake = await self.prepare_cleanup_recovery()
            fake.transport = ReconnectingProxy()
            deadline, backoff = self.runtime.bench_until, self.runtime.retry_after
            self.assertEqual(datetime.fromisoformat(self.runtime.cleanup_recovery["next_attempt_at"]), start + timedelta(seconds=60))
            for elapsed in (59, 60, 99, 119, 120):
                Clock.current = start + timedelta(seconds=elapsed)
                self.runtime._launch_cleanup_recovery()
                if task := self.runtime._recovery_task:
                    await task
                if elapsed < 120:
                    self.assertFalse(self.runtime.enabled or self.runtime.periodic)
                    self.assertEqual(self.runtime.bench_until, deadline)
            self.assertEqual(attempts, [60, 120])
            self.assertTrue(self.runtime.enabled and self.runtime.periodic)
            self.assertEqual(self.runtime.bench_until, deadline)
            self.assertEqual(self.runtime.retry_after, backoff)
            self.assertEqual(self.runtime.last_cleanup_recovery["attempts"], 2)
            self.assertIsNone(self.runtime.cleanup_recovery)
            state = FakeStore.data[self.runtime.state_store.key]
            self.assertTrue(state["enabled"] and state["periodic"])
            self.assertEqual(state["bench_until"], deadline.isoformat())

    async def test_three_60_second_checks_preserve_budget_across_restart_then_exhaust(self):
        start = datetime.now(timezone.utc)
        class Clock(datetime):
            current = start
            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)
        fresh = None
        with patch.object(runtime_module, "datetime", Clock):
            self.runtime.clock = TrustedClock(now=lambda: Clock.current, elapsed=lambda: (Clock.current-start).total_seconds())
            fake = await self.prepare_cleanup_recovery(failures=3)
            deadline = self.runtime.bench_until
            Clock.current = start + timedelta(seconds=60)
            self.runtime._launch_cleanup_recovery()
            await self.runtime._recovery_task
            self.assertEqual(FakeStore.data[self.runtime.state_store.key]["cleanup_recovery"]["attempts"], 1)
            await self.runtime.stop()
            Clock.current = start + timedelta(seconds=70)
            fresh = runtime_module.Runtime(self.hass, self.entry)
            fresh.clock = TrustedClock(now=lambda: Clock.current, elapsed=lambda: (Clock.current-start).total_seconds())
            await fresh.start()
            try:
                restarted_transport = RecoveryTransport(failures=2)
                fresh.receiver.transport = restarted_transport
                self.assertEqual(fresh.cleanup_recovery["attempts"], 1)
                self.assertEqual(datetime.fromisoformat(fresh.cleanup_recovery["next_attempt_at"]), start + timedelta(seconds=120))
                for elapsed in (119, 120, 179, 180):
                    Clock.current = start + timedelta(seconds=elapsed)
                    fresh._launch_cleanup_recovery()
                    if task := fresh._recovery_task:
                        await task
                    self.assertFalse(fresh.enabled or fresh.periodic)
                    self.assertEqual(fresh.bench_until, deadline)
                self.assertEqual(restarted_transport.close_calls, 2)
                self.assertIsNone(fresh.cleanup_recovery)
                self.assertEqual(fresh.last_cleanup_recovery["attempts"], 3)
                self.assertEqual(fresh.status, "blocked")
                self.assertIsNotNone(fresh.safety_trip)
                self.assertIsNone(FakeStore.data[fresh.state_store.key]["cleanup_recovery"])
            finally:
                await fresh.stop()

    async def test_cleanup_recovery_three_failures_exhausts_and_latches_off(self):
        fake = await self.prepare_cleanup_recovery(failures=3)
        for expected in (1, 2, 3):
            await self.runtime._recover_cleanup()
            self.assertEqual(fake.transport.close_calls, expected)
        await self.runtime._recover_cleanup()
        self.assertEqual(fake.transport.close_calls, 3)
        self.assertIsNone(self.runtime.cleanup_recovery)
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)
        self.assertEqual(self.runtime.status, "blocked")
        self.assertEqual(self.runtime.last_cleanup_recovery["attempts"], 3)
        self.assertIsNotNone(self.runtime.safety_trip)

    async def test_cleanup_launch_honors_delay_and_never_overlaps(self):
        fake = await self.prepare_cleanup_recovery(failures=1)
        self.runtime._launch_cleanup_recovery()
        self.assertIsNone(self.runtime._recovery_task)
        self.runtime.cleanup_recovery["next_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.runtime._launch_cleanup_recovery()
        first = self.runtime._recovery_task
        self.runtime._launch_cleanup_recovery()
        self.assertIs(self.runtime._recovery_task, first)
        await first
        self.assertEqual(fake.transport.close_calls, 1)
        self.runtime._launch_cleanup_recovery()
        self.assertIsNone(self.runtime._recovery_task)

    async def test_cleanup_budget_intent_and_backoff_survive_core_restart(self):
        fake = await self.prepare_cleanup_recovery(failures=1)
        await self.runtime._recover_cleanup()
        deadline, old_backoff = self.runtime.bench_until, self.runtime.retry_after
        await self.runtime.stop()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertEqual(fresh.cleanup_recovery["attempts"], 1)
            self.assertTrue(fresh.cleanup_recovery["desired_periodic"])
            self.assertAlmostEqual(fresh.retry_after, old_backoff, delta=2)
            self.assertEqual(fresh.bench_until, deadline)
            self.assertFalse(fresh.enabled or fresh.periodic)
            await fresh._recover_cleanup()
            self.assertTrue(fresh.enabled and fresh.periodic)
            self.assertEqual(fresh.last_cleanup_recovery["attempts"], 2)
            self.assertEqual(fresh.bench_until, deadline)
        finally:
            await fresh.stop()

    async def test_user_disable_cancels_cleanup_resume_intent(self):
        fake = await self.prepare_cleanup_recovery()
        await self.runtime.set_enabled(False)
        await self.runtime._recover_cleanup()
        self.assertEqual(fake.transport.close_calls, 0)
        self.assertIsNone(self.runtime.cleanup_recovery)
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)
        self.assertIsNone(FakeStore.data[self.runtime.state_store.key]["cleanup_recovery"])

    async def test_periodic_off_during_recovery_preserves_manual_enable_intent(self):
        await self.prepare_cleanup_recovery()
        await self.runtime.set_periodic(False)
        await self.runtime._recover_cleanup()
        self.assertTrue(self.runtime.enabled)
        self.assertFalse(self.runtime.periodic)

    async def test_deadline_during_cleanup_recovery_stops_without_close(self):
        fake = await self.prepare_cleanup_recovery()
        self.runtime.bench_ended = True
        ticks = 0
        async def tick(seconds):
            nonlocal ticks
            ticks += 1
            if ticks > 1:
                raise asyncio.CancelledError
        with patch.object(runtime_module.asyncio, "sleep", tick):
            with self.assertRaises(asyncio.CancelledError):
                await self.runtime._monitor()
        self.assertEqual(fake.transport.close_calls, 0)
        self.assertIsNone(self.runtime.cleanup_recovery)
        self.assertEqual(self.runtime.status, "bench_complete")

    async def test_user_off_with_failed_cancel_cleanup_blocks_future_enable(self):
        fake = ResultReceiver(jpeg(), delay=True)
        fake.cleanup_error = "disconnect unconfirmed: TimeoutError"
        self.runtime.receiver = fake
        await self.runtime.set_enabled(True)
        capture = asyncio.create_task(self.runtime.capture())
        await fake.started.wait()
        await self.runtime.set_enabled(False)
        await asyncio.gather(capture, return_exceptions=True)
        self.assertIsNone(self.runtime.cleanup_recovery)
        self.assertIsNotNone(self.runtime.safety_trip)
        with self.assertRaises(HomeAssistantError):
            await self.runtime.set_enabled(True)

    async def test_user_off_during_active_recovery_close_never_resumes(self):
        fake = await self.prepare_cleanup_recovery()
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class BlockingTransport(RecoveryTransport):
            async def close(self):
                self.close_calls += 1
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        fake.transport = BlockingTransport()
        self.runtime.cleanup_recovery["next_attempt_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.runtime._launch_cleanup_recovery()
        await started.wait()
        await self.runtime.set_enabled(False)
        release.set()
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.runtime.cleanup_recovery)
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)

    async def test_restart_after_third_budget_reserved_latches_without_fourth_attempt(self):
        await self.prepare_cleanup_recovery()
        self.runtime.cleanup_recovery["attempts"] = 3
        await self.runtime._save_state()
        await self.runtime.stop()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertIsNone(fresh.cleanup_recovery)
            self.assertFalse(fresh.enabled or fresh.periodic)
            self.assertEqual(fresh.status, "blocked")
            self.assertEqual(fresh.last_cleanup_recovery["attempts"], 3)
        finally:
            await fresh.stop()

    async def test_new_config_disabled_qvga_and_manual_cannot_bypass(self):
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)
        self.assertEqual(self.runtime.config["frame_size"], 5)
        self.assertEqual(self.runtime.config["interval"], 900)
        with self.assertRaises(HomeAssistantError):
            await self.runtime.capture()

    async def test_success_keeps_capture_receipt_distinct_and_ack_after_durable_store(self):
        receiver = ResultReceiver(jpeg())
        self.runtime.receiver = receiver
        await self.runtime.set_enabled(True)
        await self.runtime.capture()
        self.assertTrue(receiver.acked)
        self.assertEqual(self.runtime.latest, jpeg())
        info = self.runtime.image_info
        self.assertIsNotNone(info["captured_at"])
        self.assertTrue(info["capture_clock_synchronized"])
        self.assertTrue(info["sd_committed"])
        self.assertIsNotNone(info["capture_window_start"])
        self.assertEqual(datetime.fromisoformat(info["received_at_local"]), datetime.fromisoformat(info["received_at"]))
        self.assertEqual(info["display_timezone"], "America/Chicago")
        self.assertEqual(len(list(self.runtime.history.directory.rglob("*.jpg"))), 1)

    async def test_capture_precision_describes_manifest_utc_and_discovery_fallback(self):
        now = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)
        epoch = int(now.timestamp())
        result = Result(jpeg(), 5, 1234, now, now, now, 1.2,
                        "AA:BB:CC:DD:EE:FF", False,
                        manifest=Manifest(1|2|4|16, 0, epoch, epoch+604800, TEST_RUN_CODE, 1))
        for pending in (False, True):
            with self.subTest(pending=pending, clock="synchronized"):
                info = self.runtime._result_info(replace(result, from_pending=pending))
                self.assertEqual(info["captured_at"], now.isoformat())
                self.assertTrue(info["capture_clock_synchronized"])
                self.assertEqual(info["capture_time_precision"],
                                 "camera UTC seconds synchronized from Home Assistant; subsecond precision unavailable")
            with self.subTest(pending=pending, clock="discovery_fallback"):
                info = self.runtime._result_info(replace(result, from_pending=pending,
                    manifest=Manifest(1|2|4|16|128, 0, 0, epoch+604800, TEST_RUN_CODE, 1)))
                self.assertIsNone(info["captured_at"])
                self.assertFalse(info["capture_clock_synchronized"])
                self.assertEqual(info["capture_time_precision"],
                                 "original capture time unknown; retention uses first trusted SD discovery/receipt time")
            with self.subTest(pending=pending, clock="historical_no_manifest"):
                info = self.runtime._result_info(replace(result, from_pending=pending, manifest=None))
                self.assertEqual(info["capture_time_precision"], "unknown_original_capture_time" if pending
                                 else "bounded_by_command_and_ready; camera has no UTC clock")

    async def test_photo_boot_diagnostic_snapshot_is_not_relabelled_by_current_boot(self):
        now = datetime.now(timezone.utc)
        result = Result(jpeg(), 5, 1234, now, now, now, 1.2,
                        "AA:BB:CC:DD:EE:FF", False,
                        bootdiag={"camera_boot_counter":3, "camera_previous_boot_counter":2,
                                  "camera_bootdiag_observed_at":now.isoformat()})
        self.runtime.image_info = self.runtime._result_info(result)
        self.assertNotIn("camera_boot_counter",self.runtime.image_info)
        self.runtime.receiver.last_bootdiag = {"camera_boot_counter":4,
                                             "camera_previous_boot_counter":3}
        attrs = self.runtime.attributes
        self.assertEqual(attrs["image_camera_boot_counter"],3)
        self.assertEqual(attrs["camera_boot_counter"],4)
        self.runtime.receiver.last_bootdiag = {"camera_bootdiag_read_error_type":"TimeoutError"}
        attrs = self.runtime.attributes
        self.assertEqual(attrs["image_camera_boot_counter"],3)
        self.assertIsNone(attrs["camera_boot_counter"])
        self.assertIsNone(attrs["camera_bootdiag_observed_at"])
        self.assertEqual(attrs["camera_bootdiag_read_error_type"],"TimeoutError")

    async def test_last_good_retained_after_failure_and_enabled_restored(self):
        self.runtime.receiver = ResultReceiver(jpeg())
        await self.runtime.set_enabled(True)
        await self.runtime.capture()
        original = self.runtime.latest
        self.runtime.retry_after = 0
        self.runtime.receiver = ResultReceiver(b"", failure=ConnectionError("camera unplugged"))
        with self.assertRaises(HomeAssistantError):
            await self.runtime.capture()
        self.assertEqual(self.runtime.latest, original)
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertEqual(fresh.latest, original)
            self.assertTrue(fresh.enabled)
            self.assertEqual(fresh.successes, 1)
            self.assertEqual(fresh.failures, 1)
        finally:
            await fresh.stop()

    async def test_bad_jpeg_no_publish_or_ack(self):
        self.runtime.receiver = ResultReceiver(jpeg())
        await self.runtime.set_enabled(True)
        await self.runtime.capture()
        original = self.runtime.latest
        self.runtime.retry_after = 0
        bad = ResultReceiver(b"\xff\xd8not-a-jpeg\xff\xd9")
        self.runtime.receiver = bad
        with self.assertRaises(HomeAssistantError):
            await self.runtime.capture()
        self.assertEqual(self.runtime.latest, original)
        self.assertFalse(bad.acked)

    async def test_pending_retry_has_unknown_original_capture_time(self):
        self.runtime.receiver = ResultReceiver(jpeg())
        await self.runtime.set_enabled(True)
        await self.runtime.capture(retry_pending=True)
        self.assertTrue(self.runtime.image_info["from_camera_sd_pending"])
        self.assertIsNotNone(self.runtime.image_info["captured_at"])
        for key in ("capture_window_start", "capture_window_end", "camera_capture_uptime_ms"):
            self.assertIsNone(self.runtime.image_info[key])

    async def test_small_payload_does_not_replace_photo(self):
        self.runtime.receiver = ResultReceiver(bytes(range(256)))
        await self.runtime.set_enabled(True)
        await self.runtime.capture(test=True)
        self.assertIsNone(self.runtime.latest)
        self.assertEqual(self.runtime.test_successes, 1)
        self.assertEqual(self.runtime.last_test["bytes"], 256)

    async def test_controls_and_original_bench_deadline_survive_unload_restart(self):
        await self.runtime.set_enabled(True)
        await self.runtime.set_periodic(True)
        deadline = self.runtime.bench_until
        self.assertEqual(deadline, CUTOFF)
        await self.runtime.stop()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertTrue(fresh.enabled and fresh.periodic)
            self.assertEqual(fresh.bench_until, deadline)
        finally:
            await fresh.stop()

    async def test_restart_after_deadline_disables_collection(self):
        self.runtime.receiver = ResultReceiver(jpeg())
        await self.runtime.set_enabled(True)
        await self.runtime.capture()
        await self.runtime.set_periodic(True)
        self.runtime.bench_ended = True
        await self.runtime._save_state()
        fresh = runtime_module.Runtime(self.hass, self.entry)
        await fresh.start()
        try:
            self.assertFalse(fresh.enabled or fresh.periodic)
            self.assertEqual(fresh.status, "bench_complete")
            self.assertEqual(fresh.latest, jpeg())
        finally:
            await fresh.stop()

    async def test_unload_during_transfer_preserves_collection_intent(self):
        fake = ResultReceiver(jpeg(), delay=True)
        self.runtime.receiver = fake
        await self.runtime.set_enabled(True)
        await self.runtime.set_periodic(True)
        capture = asyncio.create_task(self.runtime.capture())
        await fake.started.wait()
        await self.runtime.stop()
        await asyncio.gather(capture, return_exceptions=True)
        state = FakeStore.data[self.runtime.state_store.key]
        self.assertTrue(state["enabled"] and state["periodic"])
        self.assertTrue(fake.cancelled.is_set())

    async def test_disable_cancels_transfer_and_persists_off(self):
        fake = ResultReceiver(jpeg(), delay=True)
        self.runtime.receiver = fake
        await self.runtime.set_enabled(True)
        capture = asyncio.create_task(self.runtime.capture())
        await fake.started.wait()
        await self.runtime.set_enabled(False)
        await asyncio.gather(capture, return_exceptions=True)
        self.assertTrue(fake.cancelled.is_set())
        self.assertFalse(FakeStore.data[self.runtime.state_store.key]["enabled"])

    async def test_history_write_error_rejects_photo_and_preserves_sd_pending(self):
        fake = ResultReceiver(jpeg())
        self.runtime.receiver = fake
        await self.runtime.set_enabled(True)
        with patch.object(self.runtime.history, "save", side_effect=OSError("disk cap fixture")):
            with self.assertRaises(HomeAssistantError):
                await self.runtime.capture()
        self.assertIsNone(self.runtime.latest)
        self.assertFalse(fake.acked)
        self.assertIn("disk cap fixture", self.runtime.last_error)

    async def test_latest_store_error_prevents_publish_and_ack(self):
        fake = ResultReceiver(jpeg())
        self.runtime.receiver = fake
        await self.runtime.set_enabled(True)
        with patch.object(self.runtime.store, "async_save", side_effect=OSError("full disk fixture")):
            with self.assertRaises(HomeAssistantError):
                await self.runtime.capture()
        self.assertIsNone(self.runtime.latest)
        self.assertFalse(fake.acked)

    async def test_swallowed_latest_write_error_no_ack_and_preserves_prior_image(self):
        self.runtime.receiver = ResultReceiver(jpeg())
        await self.runtime.set_enabled(True)
        await self.runtime.capture()
        original, original_info = self.runtime.latest, self.runtime.image_info.copy()
        original_file = Path(self.runtime.store.path).read_bytes()
        self.runtime.retry_after = 0
        async def swallow_latest_write(_):
            return
        fake = ResultReceiver(jpeg())
        self.runtime.receiver = fake
        with patch.object(self.runtime.store, "async_save", swallow_latest_write):
            with self.assertRaises(HomeAssistantError):
                await self.runtime.capture()
        self.assertFalse(fake.acked)
        self.assertEqual(self.runtime.latest, original)
        self.assertEqual(self.runtime.image_info, original_info)
        self.assertEqual(Path(self.runtime.store.path).read_bytes(), original_file)

    async def test_swallowed_state_write_error_blocks_enabling_and_reports_failure(self):
        await self.runtime.set_enabled(False)
        saved = Path(self.runtime.state_store.path).read_bytes()
        self.runtime.state_store.swallow_writes = True
        with self.assertRaises(HomeAssistantError):
            await self.runtime.set_enabled(True)
        self.assertFalse(self.runtime.enabled or self.runtime.periodic)
        self.assertEqual(self.runtime.status, "storage_failed")
        self.assertEqual(Path(self.runtime.state_store.path).read_bytes(), saved)

    async def test_age_staleness_updates_while_disabled(self):
        self.runtime.image_info = {"received_at": (datetime.now(timezone.utc) - timedelta(seconds=1801)).isoformat()}
        self.assertTrue(self.runtime.attributes["retained_image_stale"])
        self.assertFalse(self.runtime.enabled)


class StorageVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store"
        self.expected = {"enabled": True, "periodic": True, "bench_until": "2026-10-23T12:00:00+00:00"}
    def tearDown(self):
        self.temp.cleanup()
    def write(self, expected=None, mode=0o600):
        self.path.write_text(json.dumps({"version": 1, "data": expected or self.expected}))
        os.chmod(self.path, mode)
    def test_exact_current_private_store_is_confirmed(self):
        self.write()
        verify_store_file(str(self.path), self.expected)
    def test_old_contents_or_public_mode_rejected(self):
        self.write({"enabled": False})
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
        self.write(mode=0o644)
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
    def test_missing_corrupt_oversized_or_symlink_rejected(self):
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
        self.path.write_bytes(b"{" * (MAX_STORE_BYTES + 1))
        os.chmod(self.path, 0o600)
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
        self.path.write_bytes(b"{")
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
        target = self.path.with_name("other")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            verify_store_file(str(self.path), self.expected)
    def test_maximum_image_base64_plus_metadata_fits_bound(self):
        expected = {"jpeg": "A" * (4 * ((131072 + 2) // 3)), "info": {"received_at": "2026-10-20T20:00:00+00:00", "image_id": 123}}
        self.write(expected)
        verify_store_file(str(self.path), expected)
        self.assertLess(self.path.stat().st_size, MAX_STORE_BYTES)


class ScheduleTests(unittest.TestCase):
    def test_quarter_hour_boundaries_and_no_catchup(self):
        for value, expected in (("2026-10-20T13:07:20-05:00", "2026-10-20T18:15:00+00:00"),
                                ("2026-10-20T13:15:00-05:00", "2026-10-20T18:30:00+00:00"),
                                ("2026-10-20T23:59:59-05:00", "2026-10-21T05:00:00+00:00")):
            self.assertEqual(runtime_module.next_capture_slot(datetime.fromisoformat(value)).isoformat(), expected)
    def test_dst_fall_repeated_hour_and_spring_skipped_hour(self):
        for value, expected in (("2026-11-01T01:59:30-05:00", "2026-11-01T07:00:00+00:00"),
                                ("2026-03-08T01:59:30-06:00", "2026-03-08T08:00:00+00:00")):
            self.assertEqual(runtime_module.next_capture_slot(datetime.fromisoformat(value)).isoformat(), expected)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "history"
        self.history = History(str(self.directory), 3, 20)
        self.now = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
    def tearDown(self):
        self.temp.cleanup()
    def test_168hour_expiry_and_capacity_preserves_unexpired(self):
        self.history.save(b"a"*8, 1, self.now - timedelta(days=8))
        self.history.save(b"b"*8, 2, self.now - timedelta(hours=1))
        self.history.save(b"c"*8, 3, self.now)
        self.assertEqual(self.history.prune(self.now), 16)
        with self.assertRaises(OSError):
            self.history.save(b"d"*8, 4, self.now + timedelta(minutes=1))
        files = sorted(self.directory.rglob("*.jpg"))
        self.assertEqual([path.read_bytes() for path in files], [b"b"*8, b"c"*8])
        self.assertEqual(self.history.prune(self.now + timedelta(hours=168)), 0)
        self.assertFalse(list(self.directory.rglob("*.jpg.json")))
    def test_unrelated_files_and_symlinks_preserved(self):
        self.directory.mkdir()
        unrelated = self.directory / "vacation.jpg"
        unrelated.write_bytes(b"unrelated")
        target = Path(self.temp.name) / "outside.jpg"
        target.write_bytes(b"do not delete")
        link = self.directory / "20260101T000000000000Z-00000001.jpg"
        link.symlink_to(target)
        self.history.save(b"ok", 2, self.now)
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"do not delete")
    def test_oversize_rejected_and_duplicate_does_not_overwrite(self):
        with self.assertRaises(OSError):
            self.history.save(b"x"*21, 1, self.now)
        self.history.save(b"first", 1, self.now)
        with self.assertRaises(FileExistsError):
            self.history.save(b"second", 1, self.now)
        self.assertEqual(next(self.directory.rglob("*.jpg")).read_bytes(), b"first")
