"""Durable local collection, quarter-hour scheduling and bounded private storage."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import logging
import zlib
from time import monotonic
from zoneinfo import ZoneInfo

from PIL import Image
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DEFAULTS, DOMAIN, DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL, LOCAL_TIMEZONE, TEST_RUN_ID, TEST_RUN_CODE, TEST_RETENTION_SECONDS
from .policy import CUTOFF, ClockError, TrustedClock, expiration
from .history import History
from .photo_store import PhotoStore
from .protocol import BOOT_DIAGNOSTIC_ATTRIBUTES, MAX_IMAGE_BYTES, ProtocolError
from .storage import verify_store_file, prune_owned_corrupt_latest, migrate_photo_storage
from .transfer import Limits, Receiver
from .transport import ManagedTransport

_LOGGER = logging.getLogger(__name__)
LOCAL = ZoneInfo(LOCAL_TIMEZONE)
CLEANUP_RETRY_SECONDS = 60
CLEANUP_MAX_ATTEMPTS = 3
CLEANUP_TIMEOUT_SECONDS = 5


def verify_jpeg(data: bytes) -> None:
    """Decode in executor before publishing; enforce the existing pixel/memory bound."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ProtocolError("JPEG exceeds maximum size")
    with Image.open(BytesIO(data)) as image:
        if image.format != "JPEG" or image.width > 1600 or image.height > 1200:
            raise ProtocolError("unsupported JPEG geometry")
        if image.width <= 0 or image.height <= 0:
            raise ProtocolError("invalid JPEG geometry")
        image.load()


def next_capture_slot(now: datetime, interval: int = 900) -> datetime:
    """Return next local wall-clock boundary, including both DST repeated hours."""
    if interval not in (900, 1800, 3600):
        raise ValueError("interval must be 900, 1800 or 3600 seconds")
    candidate = now.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    while candidate.astimezone(LOCAL).minute % (interval // 60):
        candidate += timedelta(minutes=1)
    return candidate


class Runtime:
    def __init__(self, hass, entry) -> None:
        self.hass, self.entry = hass, entry
        self.config = {**DEFAULTS, **entry.data, **entry.options}
        self.config["interval"] = 900
        self.config["history_days"] = 7
        self.config["history_max_bytes"] = max(128 * 1024 * 1024, self.config["history_max_bytes"])
        conf = self.config
        self.signal = f"{DOMAIN}_{entry.entry_id}"
        self.receiver = Receiver(ManagedTransport(hass, conf["address"], conf["proxy_source"]),
                                 self.check, Limits(max_bytes=conf["max_bytes"],
                                                    context_seconds=120 if conf.get("camera_model") == S3_CAMERA_MODEL else 10,
                                                    capture_seconds=120 if conf.get("camera_model") == S3_CAMERA_MODEL else 60,
                                                    chunk_pause_seconds=conf["chunk_pause"],
                                                    capture_light_percent=conf["capture_light_percent"]))
        self.store = PhotoStore(hass, entry.entry_id)
        self.state_store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_state", private=True, atomic_writes=True)
        self.history = History(hass.config.path(".cache", DOMAIN, entry.entry_id, "history"),
                               conf["history_days"], conf["history_max_bytes"])
        self.enabled = self.periodic = False
        self.status = "disabled"
        self.last_error = self.safety_trip = self.history_error = None
        self.failures = self.successes = self.test_successes = self.failure_streak = 0
        self.latest: bytes | None = None
        self.image_info: dict = {}
        self.last_test: dict = {}
        self.bench_until: datetime | None = CUTOFF
        self.mode = conf.get("mode", "TEST_PHOTO")
        self.run_id, self.run_code = TEST_RUN_ID, TEST_RUN_CODE
        self.bench_ended = False
        self.clock = TrustedClock()
        self.sd_cleanup_last_at = self.sd_cleanup_error = None
        self._maintenance_task = None
        self.next_capture_at: datetime | None = None
        self.retry_after = 0.0
        self._transfer_task = self._monitor_task = self._periodic_task = None
        self._stopping = False
        self._state_save_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovery_task = None
        self.cleanup_recovery: dict | None = None
        self.last_cleanup_recovery: dict = {}
        self.history_bytes = 0

    def now(self):
        return self.clock.read()

    def context(self):
        return {"mode": self.mode, "run_code": self.run_code, "now": self.now}

    def check(self) -> None:
        now = self.now()
        if not self.enabled:
            raise RuntimeError("collection is disabled")
        if self.safety_trip:
            raise RuntimeError(self.safety_trip)
        if self.mode == "TEST_PHOTO" and (self.bench_ended or now >= CUTOFF):
            self.bench_ended = True
            raise RuntimeError("Bench test ended—awaiting meter installation")

    def changed(self) -> None:
        async_dispatcher_send(self.hass, self.signal)

    async def _save_state(self) -> None:
        if self._stopping:
            return
        async with self._state_save_lock:
            if self._stopping:
                return
            state = {
                "enabled": self.enabled, "periodic": self.periodic,
                "mode": self.mode, "run_id": self.run_id, "run_code": self.run_code,
                "bench_ended": self.bench_ended,
                "clock_high_water": self.clock.high_water.isoformat() if self.clock.high_water else None,
                "sd_cleanup_transport_error": self.receiver.cleanup_error if self.sd_cleanup_error and not self.cleanup_recovery else None,
                "sd_cleanup_error": self.sd_cleanup_error,
                "bench_until": self.bench_until.isoformat() if self.bench_until else None,
                "successes": self.successes, "failures": self.failures,
                "test_successes": self.test_successes, "safety_trip": self.safety_trip,
                "cleanup_recovery": dict(self.cleanup_recovery) if self.cleanup_recovery else None,
                "last_cleanup_recovery": dict(self.last_cleanup_recovery),
                "retry_not_before": ((self.clock.high_water or datetime.now(timezone.utc)) + timedelta(seconds=max(0, self.retry_after - monotonic()))).isoformat()
                    if self.retry_after > monotonic() else None,
            }
            try:
                await self.state_store.async_save(state)
                await self.hass.async_add_executor_job(verify_store_file, self.state_store.path, state)
            except Exception as err:
                self.enabled = self.periodic = False
                self.status, self.last_error = "storage_failed", "Collection state could not be durably saved"
                self.changed()
                raise HomeAssistantError(self.last_error) from err

    async def start(self) -> None:
        # Payload migration precedes any load/exposure or background task. The
        # persistent metadata/control Store remains under .storage as before.
        await self.hass.async_add_executor_job(migrate_photo_storage, self.hass.config.path(), self.entry.entry_id)
        saved = await self.store.async_load()
        if saved:
            try:
                encoded = saved["jpeg"]
                if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                    raise ValueError("stored JPEG exceeds bound")
                data = base64.b64decode(encoded, validate=True)
                await self.hass.async_add_executor_job(verify_jpeg, data)
                if not isinstance(saved["info"], dict):
                    raise ValueError("invalid image metadata")
                info = saved["info"]
                # The old calendar run lacked explicit mode and capture UTC.
                if "mode" not in info:
                    info = {**info, "mode": "TEST_PHOTO", "run_id": TEST_RUN_ID,
                            "retention_basis": "original_legacy_receipt_time_fallback"}
                    info["expires_at"] = expiration(info).isoformat()
                if info.get("mode") == "TEST_PHOTO":
                    expiration(info)
                self.latest, self.image_info = data, info
            except (ValueError, TypeError, KeyError, OSError, ProtocolError):
                _LOGGER.warning("Stored BLE meter image failed validation; discarded")
        saved = await self.state_store.async_load()
        if saved:
            try:
                self.bench_until = CUTOFF
                self.bench_ended = saved.get("bench_ended") is True
                self.mode = saved.get("mode", self.mode)
                if self.mode not in ("TEST_PHOTO", "METER"):
                    raise ValueError("invalid saved mode")
                self.run_id = saved.get("run_id", TEST_RUN_ID)
                self.run_code = int(saved.get("run_code", TEST_RUN_CODE))
                if self.mode == "TEST_PHOTO":
                    self.run_id, self.run_code = TEST_RUN_ID, TEST_RUN_CODE
                if saved.get("clock_high_water"):
                    self.clock.high_water = datetime.fromisoformat(saved["clock_high_water"])
                if self.bench_until and self.bench_until.tzinfo is None:
                    raise ValueError("bench deadline must include UTC offset")
                self.safety_trip = saved.get("safety_trip")
                self.sd_cleanup_error = saved.get("sd_cleanup_error")
                if saved.get("sd_cleanup_transport_error") and self.safety_trip == saved["sd_cleanup_transport_error"]:
                    self.receiver.cleanup_error = saved["sd_cleanup_transport_error"]
                self.successes, self.failures, self.test_successes = (
                    max(0, int(saved.get(key, 0))) for key in ("successes", "failures", "test_successes"))
                expired = self.mode == "TEST_PHOTO" and (self.bench_ended or self.now() >= CUTOFF)
                self.bench_ended = self.bench_ended or bool(expired)
                self.enabled = saved.get("enabled") is True and not self.safety_trip and not expired
                self.periodic = self.enabled and saved.get("periodic") is True and self.bench_until is not None
                self.status = "bench_complete" if expired else "idle" if self.enabled else "blocked" if self.safety_trip else "disabled"
                retry_at = datetime.fromisoformat(saved["retry_not_before"]) if saved.get("retry_not_before") else None
                if retry_at:
                    if retry_at.tzinfo is None:
                        raise ValueError("backoff timestamp must include offset")
                    self.retry_after = monotonic() + min(3600, max(0, (retry_at - self.now()).total_seconds()))
                self.last_cleanup_recovery = saved.get("last_cleanup_recovery") or {}
                if not isinstance(self.last_cleanup_recovery, dict):
                    raise ValueError("invalid cleanup recovery report")
                recovery = saved.get("cleanup_recovery")
                exhausted = False
                if recovery is not None:
                    if not isinstance(recovery, dict) or type(recovery.get("attempts")) is not int or not 0 <= recovery["attempts"] <= CLEANUP_MAX_ATTEMPTS:
                        raise ValueError("invalid cleanup recovery attempts")
                    next_at = datetime.fromisoformat(recovery["next_attempt_at"])
                    if next_at.tzinfo is None or recovery.get("desired_enabled") is not True or type(recovery.get("desired_periodic")) is not bool:
                        raise ValueError("invalid cleanup recovery intent/deadline")
                    self.enabled = self.periodic = False
                    if not expired and recovery["attempts"] < CLEANUP_MAX_ATTEMPTS:
                        self.cleanup_recovery = recovery
                        self.safety_trip = self.safety_trip or "disconnect cleanup remains unconfirmed"
                        self.status = "recovering_cleanup"
                    elif not expired:
                        exhausted = True
                        self.status = "blocked"
                        self.last_cleanup_recovery = {"outcome": "exhausted", "attempts": recovery["attempts"], "ended_at": self.now().isoformat()}
                if expired or exhausted:
                    await self._save_state()
            except (ValueError, TypeError, ClockError):
                self.enabled = self.periodic = False
                self.status, self.last_error = "disabled", "invalid saved collection state"
        await self._prune_history()
        if self.periodic:
            self._start_periodic_task()
        self._monitor_task = self.hass.async_create_background_task(self._monitor(), "BLE meter monitor")

    async def _prune_history(self):
        try:
            now = self.now()
            self.history_bytes = await self.hass.async_add_executor_job(self.history.prune, now)
            await self.hass.async_add_executor_job(prune_owned_corrupt_latest, self.store.path, now)
            if self.image_info.get("mode") == "TEST_PHOTO" and now >= expiration(self.image_info):
                await self.store.async_save({})
                await self.hass.async_add_executor_job(verify_store_file, self.store.path, {})
                self.latest, self.image_info = None, {}
                self.changed()
            self.history_error = None
        except (OSError, ValueError, ClockError) as err:
            self.history_error = f"{type(err).__name__}: {err}"

    async def _maintain_sd(self):
        try:
            if self.receiver.cleanup_error:
                # A maintenance disconnect may outlast a C6 reboot. One bounded
                # close-only recovery per maintenance interval can restore SD
                # cleanup without re-enabling image collection or test work.
                previous_error = self.receiver.cleanup_error
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await self.receiver.transport.recover_close()
                self.receiver.cleanup_error = None
                if self.safety_trip == previous_error:
                    self.safety_trip = None
            await self.receiver.maintain(self.context())
            self.sd_cleanup_last_at = self.now().isoformat()
            self.sd_cleanup_error = None
        except Exception as err:
            self.sd_cleanup_error = f"{type(err).__name__}: {err}"
            if self.receiver.cleanup_error:
                self.safety_trip = self.receiver.cleanup_error
        finally:
            self._maintenance_task = None
            try:
                await self._save_state()
            except HomeAssistantError:
                pass
            self.changed()

    async def _monitor(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(1)
            try:
                now = self.now()
            except ClockError as err:
                if self.enabled or self.cleanup_recovery:
                    try:
                        await self.set_enabled(False)
                    except HomeAssistantError:
                        pass  # Collection remains closed; keep retention alive.
                self.status, self.last_error = "clock_untrusted", str(err)
                self.changed()
                continue
            if self.mode == "TEST_PHOTO" and (self.bench_ended or now >= CUTOFF):
                newly_ended = not self.bench_ended
                self.bench_ended = True
                if self.enabled or self.cleanup_recovery:
                    try:
                        await self.set_enabled(False)
                    except HomeAssistantError:
                        pass
                self.status = "bench_complete"
                if newly_ended:
                    try:
                        await self._save_state()
                    except HomeAssistantError:
                        pass
                self.changed()
            self._launch_cleanup_recovery()
            ticks += 1
            try:
                expired_latest = self.image_info.get("mode") == "TEST_PHOTO" and now >= expiration(self.image_info)
            except (TypeError, ValueError) as err:
                expired_latest = False
                self.history_error = f"Invalid retained-image expiry: {type(err).__name__}"
            if ticks % 1800 == 0 or expired_latest or self.history.next_expiry and now >= self.history.next_expiry:
                await self._prune_history()
            if ticks % 1800 == 0 and not self._transfer_task and not self._maintenance_task and not self.cleanup_recovery and (not self.safety_trip or self.safety_trip == self.receiver.cleanup_error):
                self._maintenance_task = self.hass.async_create_background_task(self._maintain_sd(), "BLE meter SD maintenance")
            if ticks % 60 == 0:
                try:
                    await self._save_state()
                except HomeAssistantError:
                    pass
            if ticks % 30 == 0:
                self.changed()

    def _begin_cleanup_recovery(self, reason: str) -> None:
        self.cleanup_recovery = {
            "desired_enabled": self.enabled, "desired_periodic": self.periodic, "attempts": 0,
            "next_attempt_at": (self.now() + timedelta(seconds=CLEANUP_RETRY_SECONDS)).isoformat(),
        }
        self.safety_trip = reason
        self.enabled = self.periodic = False
        self.status = "recovering_cleanup"
        self.last_cleanup_recovery = {"outcome": "pending", "attempts": 0}

    def _launch_cleanup_recovery(self) -> None:
        recovery = self.cleanup_recovery
        if (self._stopping or not recovery or (self._recovery_task and not self._recovery_task.done())
                or self.now() < datetime.fromisoformat(recovery["next_attempt_at"])):
            return
        self._recovery_task = self.hass.async_create_background_task(self._recover_cleanup(), "BLE meter cleanup recovery")

    async def _cancel_cleanup_recovery(self) -> None:
        self.cleanup_recovery = None
        task = self._recovery_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._recovery_task = None

    async def _recover_cleanup(self) -> None:
        """Retry only retained-client disconnect, never connect/capture traffic."""
        try:
            async with self._recovery_lock:
                recovery = self.cleanup_recovery
                if self._stopping or not recovery:
                    return
                if self.mode == "TEST_PHOTO" and (self.bench_ended or self.now() >= CUTOFF):
                    return  # The monitor cancels desired intent at the original deadline.
                if recovery["attempts"] >= CLEANUP_MAX_ATTEMPTS:
                    return
                recovery["attempts"] += 1
                recovery["next_attempt_at"] = (self.now() + timedelta(seconds=CLEANUP_RETRY_SECONDS)).isoformat()
                # Persist the budget before issuing a close, including across HA restart.
                try:
                    await self._save_state()
                except HomeAssistantError:
                    self.cleanup_recovery = None
                    return  # State verification already disabled collection and exposed error.
                self.changed()
                error = None
                try:
                    async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                        await self.receiver.transport.recover_close()
                except Exception as err:
                    error = err
                if self._stopping or self.cleanup_recovery is not recovery:
                    return
                if error is None:
                    expired = self.mode == "TEST_PHOTO" and (self.bench_ended or self.now() >= CUTOFF)
                    self.cleanup_recovery = None
                    self.safety_trip = None
                    self.receiver.cleanup_error = None
                    self.enabled = recovery["desired_enabled"] and not expired
                    self.periodic = self.enabled and recovery["desired_periodic"]
                    self.status, self.last_error = ("bench_complete" if expired else "idle"), None
                    self.last_cleanup_recovery = {"outcome": "confirmed", "attempts": recovery["attempts"], "ended_at": self.now().isoformat(),
                                                 "disconnect_proof": dict(getattr(self.receiver.transport, "last_cleanup_proof", {}))}
                    await self._save_state()
                    if self.periodic and (not self._periodic_task or self._periodic_task.done()):
                        self._start_periodic_task()
                else:
                    exhausted = recovery["attempts"] >= CLEANUP_MAX_ATTEMPTS
                    self.last_cleanup_recovery = {"outcome": "exhausted" if exhausted else "retry_pending", "attempts": recovery["attempts"], "error_type": type(error).__name__}
                    if exhausted:
                        self.cleanup_recovery = None
                        self.status, self.last_error = "blocked", self.safety_trip
                    await self._save_state()
        except HomeAssistantError:
            # Storage failure is visible and leaves current collection disabled.
            self.cleanup_recovery = None
        finally:
            if self._recovery_task is asyncio.current_task():
                self._recovery_task = None
            self.changed()

    async def set_enabled(self, enabled: bool) -> None:
        if enabled:
            try:
                self.now()
            except ClockError as err:
                raise HomeAssistantError(str(err)) from err
            if self.safety_trip:
                raise HomeAssistantError(self.safety_trip)
            if self.mode == "TEST_PHOTO" and (self.bench_ended or self.now() >= CUTOFF):
                self.bench_ended = True
                await self._save_state()
                raise HomeAssistantError("Bench test ended—awaiting meter installation")
            self.enabled, self.status = True, "idle"
        else:
            await self._cancel_cleanup_recovery()
            self.enabled = self.periodic = False
            self.status = "disabled"
            tasks = [task for task in (self._periodic_task, self._transfer_task)
                     if task and task is not asyncio.current_task()]
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._periodic_task = None
            self.next_capture_at = None
        await self._save_state()
        self.changed()

    async def set_mode(self, mode):
        """An explicit service call changes mode; it never enables collection."""
        if mode not in ("TEST_PHOTO", "METER"):
            raise HomeAssistantError("Mode must be TEST_PHOTO or METER")
        if mode == "TEST_PHOTO" and (self.bench_ended or self.now() >= CUTOFF):
            raise HomeAssistantError("The calendar bench has permanently ended")
        await self.set_enabled(False)
        self.mode = mode
        if mode == "TEST_PHOTO":
            self.run_id, self.run_code = TEST_RUN_ID, TEST_RUN_CODE
        else:
            import secrets
            self.run_code = secrets.randbelow(0xfffffffe) + 1
            self.run_id = f"meter-{self.run_code:08x}"
        self.status = "disabled"
        await self._save_state()
        self.changed()

    def _start_periodic_task(self):
        self._periodic_task = self.hass.async_create_background_task(self._run_periodic(), "BLE meter periodic")

    async def set_periodic(self, enabled: bool) -> None:
        if enabled:
            try:
                self.check()
            except RuntimeError as err:
                raise HomeAssistantError(str(err)) from err
            if not self._periodic_task:
                self.bench_until = CUTOFF
                self.periodic = True
                await self._save_state()
                self._start_periodic_task()
        else:
            self.periodic = False
            if self.cleanup_recovery:
                self.cleanup_recovery["desired_periodic"] = False
            if self._periodic_task:
                self._periodic_task.cancel()
                await asyncio.gather(self._periodic_task, return_exceptions=True)
                self._periodic_task = None
            self.next_capture_at = None
            await self._save_state()
        self.changed()

    async def _run_periodic(self) -> None:
        try:
            while self.enabled and self.periodic:
                now = self.now()
                self.next_capture_at = next_capture_slot(now, self.config["interval"])
                if self.mode == "TEST_PHOTO" and self.next_capture_at >= CUTOFF:
                    self.next_capture_at = None
                    self.changed()
                    await asyncio.sleep(max(0, (CUTOFF - now).total_seconds()))
                    return
                self.changed()
                await asyncio.sleep((self.next_capture_at - now).total_seconds())
                if monotonic() < self.retry_after or self._transfer_task:
                    continue
                try:
                    await self.capture()
                except HomeAssistantError:
                    pass
        finally:
            self.next_capture_at = None
            if self._periodic_task is asyncio.current_task():
                self._periodic_task = None
            self.changed()

    async def capture(self, *, test: bool = False, retry_pending: bool = False) -> None:
        try:
            self.check()
        except RuntimeError as err:
            raise HomeAssistantError(str(err)) from err
        if self._transfer_task or self._maintenance_task:
            raise HomeAssistantError("A transfer or SD maintenance is already running")
        if monotonic() < self.retry_after:
            raise HomeAssistantError("Collection is in bounded failure backoff")
        if not test and self.mode == "TEST_PHOTO":
            try:
                # Reserve maximum JPEG bytes before the camera command. Storage
                # pressure stops new captures instead of shortening retention.
                await self.hass.async_add_executor_job(self.history.prune, self.now(), self.config["max_bytes"])
            except OSError as err:
                self.history_error = str(err)
                raise HomeAssistantError(self.history_error) from err
        task = self.hass.async_create_task(self._collect(test, retry_pending), "BLE meter transfer")
        self._transfer_task = task
        try:
            await task
        finally:
            self._transfer_task = None
            self.changed()

    def _result_info(self, result):
        pending = result.from_pending
        info = {"image_id": result.image_id, "bytes": len(result.data),
                "image_camera_address": None if pending else self.config["address"],
                "image_camera_model": None if pending else self.config.get("camera_model", DEFAULT_CAMERA_MODEL),
                "image_transfer_camera_address": self.config["address"],
                "image_transfer_camera_model": self.config.get("camera_model", DEFAULT_CAMERA_MODEL),
                "mode": self.mode, "run_id": self.run_id,
                "crc32": f"{zlib.crc32(result.data) & 0xffffffff:08x}", "chunk_retries": result.chunk_retries, "metadata_retries": result.metadata_retries,
                "protocol_version": result.protocol_version, "chunk_bytes": result.chunk_bytes,
                "mtu": result.mtu,
                "captured_at": None, "capture_clock_synchronized": False,
                "capture_window_start": None if pending else result.capture_window_start.isoformat(),
                "capture_window_end": None if pending else result.capture_window_end.isoformat(),
                "capture_time_precision": "unknown_original_capture_time" if pending else "bounded_by_command_and_ready; camera has no UTC clock",
                "camera_capture_uptime_ms": None if result.camera_capture_uptime_ms == 0xffffffff else result.camera_capture_uptime_ms,
                "from_camera_sd_pending": pending,
                "received_at": result.received_at.isoformat(),
                "received_at_local": result.received_at.astimezone(LOCAL).isoformat(),
                "display_timezone": LOCAL_TIMEZONE,
                "duration_seconds": round(result.duration, 3), "verified_proxy_source": result.source}
        manifest = result.manifest
        if manifest is not None:
            info.update({"sd_ready": bool(manifest.flags & 1), "sd_committed": bool(manifest.flags & 2),
                         "sd_error": manifest.sd_error, "sd_sequence": manifest.sd_sequence,
                         "captured_at": datetime.fromtimestamp(manifest.captured_epoch, timezone.utc).isoformat() if manifest.captured_epoch else None,
                         "capture_clock_synchronized": bool(manifest.captured_epoch),
                         "capture_time_precision": "camera UTC seconds synchronized from Home Assistant; subsecond precision unavailable" if manifest.captured_epoch else "original capture time unknown; retention uses first trusted SD discovery/receipt time",
                         "expires_at": datetime.fromtimestamp(manifest.expires_epoch, timezone.utc).isoformat() if self.mode == "TEST_PHOTO" else None,
                         "retention_basis": "camera_capture_utc" if manifest.captured_epoch else "original_camera_receipt_time_fallback"})
        info.update(result.health or {})
        # Photo-bound evidence remains distinct from diagnostics of a later boot.
        info.update({f"image_{key}": value for key, value in (result.bootdiag or {}).items()})
        return info

    async def _publish(self, result):
        """Callback runs while BLE remains connected; ACK follows durable validation."""
        await self.hass.async_add_executor_job(verify_jpeg, result.data)
        self.check()
        if result.manifest is None:
            raise ProtocolError("JPEG lacks mandatory camera microSD/UTC manifest")
        result.manifest.validate(mode=self.mode, run_code=self.run_code, now_epoch=int(self.now().timestamp()))
        info = self._result_info(result)
        if self.mode == "TEST_PHOTO" and self.now() >= expiration(info):
            raise ProtocolError("expired test arrival rejected")
        stored = {"jpeg": base64.b64encode(result.data).decode("ascii"), "info": info}
        previous = {"jpeg": base64.b64encode(self.latest).decode("ascii"), "info": self.image_info} if self.latest else {}
        try:
            if self.mode == "TEST_PHOTO":
                self.history_bytes = await self.hass.async_add_executor_job(
                    self.history.save, result.data, result.image_id, result.received_at, info)
            self.check()
            await self.store.async_save(stored)
            await self.hass.async_add_executor_job(verify_store_file, self.store.path, stored)
            self.check()
        except BaseException:
            # A cutoff/cancellation while filesystem I/O was pending never
            # publishes or ACKs late work. Restore the previous durable view.
            async def rollback():
                await self.store.async_save(previous)
                if self.mode == "TEST_PHOTO":
                    await self.hass.async_add_executor_job(self.history.remove, result.image_id, result.received_at, self.run_id)
            cleanup = asyncio.create_task(rollback())
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
            raise
        self.latest, self.image_info = result.data, info
        self.history_error = None
        self.changed()

    async def _collect(self, test: bool, retry_pending: bool = False) -> None:
        self.status = "transferring_test" if test else "retrying_pending" if retry_pending else "capturing"
        self.changed()
        try:
            result = await self.receiver.collect(frame_size=self.config["frame_size"], quality=self.config["quality"],
                                                 test=test, retry_pending=retry_pending,
                                                 on_verified=None if test else self._publish, context=self.context())
            if test:
                self.test_successes += 1
                self.last_test = self._result_info(result)
            else:
                self.successes += 1
            self.status, self.last_error = "idle", None
            self.failure_streak = 0
            self.retry_after = monotonic() + 10
        except asyncio.CancelledError:
            if self.receiver.cleanup_error:
                if self.enabled and not self._stopping:
                    self._begin_cleanup_recovery(self.receiver.cleanup_error)
                else:
                    # A user-off request cancels automatic resume, not the
                    # requirement to confirm the old client is closed.
                    self.safety_trip = self.last_error = self.receiver.cleanup_error
                    self.enabled = self.periodic = False
                    self.status = "blocked"
            else:
                self.status = "disabled" if not self.enabled else "cancelled"
            raise
        except Exception as err:
            self.failures += 1
            self.failure_streak += 1
            self.last_error = f"{type(err).__name__}: {err}"
            self.status = "failed"
            self.retry_after = monotonic() + min(3600, 60 * 2 ** min(self.failure_streak - 1, 6))
            if self.receiver.cleanup_error:
                if self.enabled and not self._stopping:
                    self._begin_cleanup_recovery(self.receiver.cleanup_error)
                else:
                    self.safety_trip = self.receiver.cleanup_error
                    self.enabled = self.periodic = False
                    self.status = "blocked"
            self.changed()
            raise HomeAssistantError(self.last_error) from err
        finally:
            await self._save_state()
            self.changed()

    async def reset_safety(self) -> None:
        if self._transfer_task or self._maintenance_task:
            raise HomeAssistantError("Wait for transfer/SD maintenance to finish")
        await self._cancel_cleanup_recovery()
        try:
            async with self._recovery_lock:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    if self.safety_trip:
                        await self.receiver.transport.recover_close()
                    else:
                        await self.receiver.transport.close()
        except Exception as err:
            raise HomeAssistantError("Cannot reset until receiver disconnect is confirmed") from err
        self.safety_trip = None
        self.receiver.cleanup_error = None
        await self.set_enabled(False)
        self.last_error = None
        self.changed()

    async def stop(self) -> None:
        # Unload does not save disabled state: user controls/deadline survive restart.
        self._stopping = True
        self.enabled = self.periodic = False
        tasks = [task for task in (self._transfer_task, self._monitor_task, self._periodic_task, self._recovery_task, self._maintenance_task) if task]
        for task in reversed(tasks):
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            async with self._recovery_lock:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await self.receiver.transport.close()
        except Exception:
            _LOGGER.warning("BLE meter receiver final disconnect was not confirmed")

    @property
    def attributes(self) -> dict:
        age = None
        age_basis = None
        if self.image_info.get("received_at"):
            try:
                if self.image_info.get("mode") == "TEST_PHOTO":
                    original = expiration(self.image_info) - timedelta(seconds=TEST_RETENTION_SECONDS)
                    age_basis = self.image_info.get("retention_basis")
                else:
                    original = datetime.fromisoformat(self.image_info.get("captured_at") or self.image_info["received_at"])
                    age_basis = "capture_utc" if self.image_info.get("captured_at") else "receipt_time_fallback"
                age = max(0, (self.now() - original).total_seconds())
            except (TypeError, ValueError, ClockError):
                pass
        manifest = getattr(self.receiver, "last_manifest", None)
        return {**self.image_info, **getattr(self.receiver, "last_health", {}),
                **dict.fromkeys(BOOT_DIAGNOSTIC_ATTRIBUTES), **getattr(self.receiver, "last_bootdiag", {}),
                "collection_enabled": self.enabled, "periodic_enabled": self.periodic,
                "image_mode": self.image_info.get("mode"), "image_run_id": self.image_info.get("run_id"),
                "image_camera_address": self.image_info.get("image_camera_address"),
                "image_camera_model": self.image_info.get("image_camera_model"),
                "image_transfer_camera_address": self.image_info.get("image_transfer_camera_address"),
                "image_transfer_camera_model": self.image_info.get("image_transfer_camera_model"),
                "configured_camera_address": self.config["address"],
                "configured_camera_model": self.config.get("camera_model", DEFAULT_CAMERA_MODEL),
                "mode": self.mode, "run_id": self.run_id,
                "bench_ended": self.bench_ended,
                "status_message": "Bench test ended—awaiting meter installation" if self.mode == "TEST_PHOTO" and self.bench_ended else "TEST PHOTO" if self.mode == "TEST_PHOTO" else "METER",
                "clock_trusted": self.clock.error is None, "clock_error": self.clock.error,
                "sd_ready": bool(manifest.flags & 1) if manifest else self.image_info.get("sd_ready"),
                "sd_committed": self.image_info.get("sd_committed"),
                "sd_active_record_committed": bool(manifest.flags & 2) if manifest else None,
                "sd_error": manifest.sd_error if manifest else self.image_info.get("sd_error"),
                "sd_cleanup_last_at": self.sd_cleanup_last_at, "sd_cleanup_error": self.sd_cleanup_error,
                "retention_hours": 168, "processing_status": "pending_meter_installation",
                "ocr_enabled": False, "drive_archiving_enabled": False,
                "failures": self.failures, "successful_images": self.successes, "successful_tests": self.test_successes,
                "last_test": self.last_test, "last_error": self.last_error, "safety_trip": self.safety_trip,
                "cleanup_error": self.receiver.cleanup_error, "retained_image_age_seconds": round(age) if age is not None else None,
                "image_age_basis": age_basis,
                "retained_image_stale": age is None or age > 2 * self.config["interval"],
                "configured_proxy_source": self.config["proxy_source"],
                "bench_until": self.bench_until.isoformat() if self.bench_until else None,
                "next_capture_at_local": self.next_capture_at.astimezone(LOCAL).isoformat() if self.next_capture_at else None,
                "history_bytes": self.history_bytes, "history_max_bytes": self.config["history_max_bytes"],
                "history_days": 7, "history_error": self.history_error,
                "reviewed_installed_dependencies": getattr(self.receiver.transport, "dependencies", {}),
                "last_ble_pairing_completed": getattr(self.receiver.transport, "pairing_completed", False),
                "last_negotiated_mtu": getattr(self.receiver.transport, "mtu_size", None),
                "gatt_characteristic_handles": getattr(self.receiver.transport, "characteristic_handles", {}),
                "last_transfer_phase": getattr(self.receiver, "phase", None),
                "last_attempt_timing_diagnostic": getattr(self.receiver, "timing_snapshot", lambda: {})(),
                "receiver_capture_ready_limit_seconds": getattr(getattr(self.receiver, "limits", None), "capture_seconds", None),
                "receiver_context_limit_seconds": getattr(getattr(self.receiver, "limits", None), "context_seconds", None),
                "receiver_maintenance_limit_seconds": getattr(getattr(self.receiver, "limits", None), "maintenance_seconds", None),
                "last_failure_phase": getattr(self.receiver, "failure_phase", None),
                "last_connect_subphase": getattr(self.receiver.transport, "connect_subphase", None),
                "last_failure_connect_subphase": getattr(self.receiver, "failure_connect_subphase", None),
                "last_connect_timeout_snapshot": dict(getattr(self.receiver, "connect_timeout_snapshot", {})),
                "retained_client_connected": getattr(self.receiver.transport, "retained_client_connected", None),
                "last_disconnect_proof": getattr(self.receiver.transport, "last_cleanup_proof", {}),
                "cleanup_recovery": dict(self.cleanup_recovery) if self.cleanup_recovery else None,
                "last_cleanup_recovery": dict(self.last_cleanup_recovery),
                "failure_backoff_remaining_seconds": round(max(0, self.retry_after - monotonic()), 1)}

    def visible_image(self):
        try:
            now = self.now()
            if self.image_info.get("mode") == "TEST_PHOTO" and now >= expiration(self.image_info):
                return None
            return self.latest
        except (ClockError, TypeError, ValueError):
            return None
