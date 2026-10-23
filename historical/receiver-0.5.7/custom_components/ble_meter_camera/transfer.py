"""One transfer at a time with deadlines and unconditional connection cleanup."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import sys
from time import monotonic
from typing import Protocol

from .protocol import (Assembly, CONTROL, DATA, METADATA, MANIFEST, HEALTH, BOOT_DIAGNOSTIC, parse_health, parse_boot_diagnostic, Manifest, configure_command, MAX_IMAGE_BYTES, MAX_DATA_VALUE_BYTES, PAYLOAD_BYTES_BY_VERSION, Metadata,
                       ProtocolError, acknowledge_command, capture_command, capture_light_duty, release_command, retry_command, select_command)


def retryable_gatt_133(error: Exception) -> bool:
    """Only the reviewed ESPHome backend's structured generic GATT133 error.

    bleak-esphome4.0.0 wraps BluetoothGATTAPIError as BleakError with its cause
    preserved. Never infer retryability from a substring/authentication failure.
    """
    cause = error.__cause__
    return (type(error).__module__ == "bleak.exc" and type(error).__name__ == "BleakError"
            and cause is not None and type(cause).__module__ == "aioesphomeapi.core"
            and type(cause).__name__ == "BluetoothGATTAPIError"
            and getattr(getattr(cause, "error", None), "error", None) == 133)


class Transport(Protocol):
    async def connect(self) -> str: ...
    async def write(self, characteristic: str, value: bytes) -> None: ...
    async def read(self, characteristic: str) -> bytes: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class Limits:
    max_bytes: int = MAX_IMAGE_BYTES
    connect_seconds: float = 30
    operation_seconds: float = 5
    capture_seconds: float = 60
    total_seconds: float = 600
    chunk_pause_seconds: float = 0.02
    chunk_retries: int = 1
    capture_light_percent: int = 0

    def __post_init__(self) -> None:
        capture_light_duty(self.capture_light_percent)
        if not 256 <= self.max_bytes <= MAX_IMAGE_BYTES:
            raise ValueError("max_bytes must be 256..131072")
        if not 10 <= self.connect_seconds <= 30 or not 0 < self.operation_seconds <= 5:
            raise ValueError("invalid operation timeout")
        if not 0 < self.capture_seconds <= 60 or not 0 < self.total_seconds <= 600:
            raise ValueError("invalid session timeout")
        if not 0 <= self.chunk_retries <= 1 or not 0.02 <= self.chunk_pause_seconds <= 1:
            raise ValueError("invalid retry/pacing limit")


@dataclass(frozen=True)
class Result:
    data: bytes
    image_id: int
    camera_capture_uptime_ms: int
    capture_window_start: datetime
    capture_window_end: datetime
    received_at: datetime
    duration: float
    source: str
    test: bool
    from_pending: bool = False
    chunk_retries: int = 0
    metadata_retries: int = 0
    protocol_version: int = 1
    chunk_bytes: int = 12
    mtu: int | None = None
    manifest: Manifest | None = None
    health: dict | None = None
    bootdiag: dict | None = None


class Receiver:
    def __init__(self, transport: Transport, guard: Callable[[], None], limits: Limits | None = None) -> None:
        self.transport = transport
        self.guard = guard
        self.limits = limits or Limits()
        self._lock = asyncio.Lock()
        self.cleanup_error: str | None = None
        self.phase = "idle"
        self.failure_phase: str | None = None
        self.failure_connect_subphase: str | None = None
        self.connect_timeout_snapshot: dict = {}
        self.last_manifest: Manifest | None = None
        self.last_health = {}
        self.last_bootdiag = {}
        self.last_attempt_timing = {}
        self._timing_started = None
        self._metadata_timeout_expired = False
        self._capture_write_timeout_expired = False

    def _timing_event(self, event, **fields):
        if self._timing_started is None:
            return
        item = {"event": event, "elapsed_ms": round((monotonic() - self._timing_started) * 1000, 3), **fields}
        if event in ("capture_write_start", "capture_write_end", "capture_wait_start", "capture_wait_end"):
            self.last_attempt_timing[event + "_elapsed_ms"] = item["elapsed_ms"]
        events = self.last_attempt_timing["events"]
        if len(events) == 96:
            events.pop(0)
            self.last_attempt_timing["events_omitted"] += 1
        events.append(item)

    def timing_snapshot(self):
        """Bounded metadata only; never include characteristic bytes or errors' text."""
        return {**self.last_attempt_timing, "events": [dict(item) for item in self.last_attempt_timing.get("events", [])]}

    async def _read_diagnostics(self, *, strict=False):
        """Read both diagnostics through this owned client, each with a deadline."""
        self.last_health, self.last_bootdiag = {}, {}
        errors = []
        for characteristic, parser, target, error_key in (
            (HEALTH, parse_health, self.last_health, "health_read_error_type"),
            (BOOT_DIAGNOSTIC, parse_boot_diagnostic, self.last_bootdiag, "camera_bootdiag_read_error_type"),
        ):
            try:
                async with asyncio.timeout(self.limits.operation_seconds):
                    target.update(parser(await self.transport.read(characteristic)))
                if characteristic == BOOT_DIAGNOSTIC:
                    target["camera_bootdiag_observed_at"] = datetime.now(timezone.utc).isoformat()
                    target[error_key] = None
            except Exception as err:
                target[error_key] = type(err).__name__
                errors.append(err)
        if strict and errors:
            raise errors[0]

    async def _configure(self, context, *, maintenance=False):
        """Clock sync and SD cleanup complete before any new capture or retry."""
        now = context["now"]()
        self.last_manifest = None
        try:
            async with asyncio.timeout(self.limits.operation_seconds):
                await self.transport.write(CONTROL, configure_command(int(now.timestamp()), context["mode"], context["run_code"]))
            async with asyncio.timeout(10):
                while True:
                    if not maintenance:
                        self.guard()
                    async with asyncio.timeout(self.limits.operation_seconds):
                        manifest = Manifest.parse(await self.transport.read(MANIFEST))
                    # Failed/pending context is diagnostic evidence too. In
                    # particular, CLOCK_SYNC is withheld after an SD error.
                    self.last_manifest = manifest
                    if manifest.flags & 4 and manifest.run_code == context["run_code"]:
                        manifest.validate(mode=context["mode"], run_code=context["run_code"],
                                          now_epoch=int(context["now"]().timestamp()), photo=False)
                        break
                    await asyncio.sleep(0.25)
        except (TimeoutError, ProtocolError) as err:
            # Independent bounded reads retain boot/reset evidence even when
            # SD context never becomes ready or one diagnostic read fails.
            await self._read_diagnostics()
            if self.last_manifest and self.last_manifest.sd_error:
                raise ProtocolError("camera clock/SD context blocked: "
                                    f"sd_error={self.last_manifest.sd_error}, flags={self.last_manifest.flags}, "
                                    f"run={self.last_manifest.run_code}") from err
            raise
        await self._read_diagnostics(strict=True)
        return manifest

    async def maintain(self, context):
        """Bounded no-photo SD maintenance remains available after the cutoff."""
        if self._lock.locked():
            raise RuntimeError("transfer already running")
        async with self._lock:
            self.last_bootdiag = {}
            try:
                async with asyncio.timeout(50):
                    async with asyncio.timeout(self.limits.connect_seconds):
                        await self.transport.connect()
                    return await self._configure(context, maintenance=True)
            finally:
                cleanup = asyncio.create_task(self._cleanup(0, True))
                cancelled = False
                while True:
                    try:
                        self.cleanup_error = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                        if cleanup.done():
                            self.cleanup_error = "SD maintenance disconnect was cancelled"
                            break
                if cancelled:
                    raise asyncio.CancelledError

    async def _write(self, value: bytes) -> None:
        self.phase = {1: "capture_command", 2: "select_chunk", 4: "test_command", 5: "acknowledge", 6: "retry_pending_command"}.get(value[0], "control_write")
        self.guard()
        timer = asyncio.timeout(self.limits.operation_seconds)
        capture = value[0] in (1, 4, 6)
        error = None
        if capture:
            self._timing_event("capture_write_start")
        try:
            async with timer:
                await self.transport.write(CONTROL, value)
        except BaseException as err:
            error = type(err).__name__
            raise
        finally:
            if capture:
                self._capture_write_timeout_expired = timer.expired()
                self._timing_event("capture_write_end", operation_timeout_expired=timer.expired(), exception_type=error)

    async def _read(self, characteristic: str) -> bytes:
        self.phase = "metadata_read" if characteristic == METADATA else "data_read"
        self.guard()
        timer = asyncio.timeout(self.limits.operation_seconds)
        metadata = characteristic == METADATA
        error, raw = None, None
        if metadata:
            self.last_attempt_timing["metadata_poll_count"] += 1
            self._timing_event("metadata_read_start", poll=self.last_attempt_timing["metadata_poll_count"])
        try:
            async with timer:
                raw = await self.transport.read(characteristic)
        except BaseException as err:
            error = type(err).__name__
            raise
        finally:
            if metadata:
                self._metadata_timeout_expired = timer.expired()
                self._timing_event("metadata_read_end", poll=self.last_attempt_timing["metadata_poll_count"],
                                   operation_timeout_expired=timer.expired(), exception_type=error,
                                   returned_bytes=len(raw) if raw is not None else None,
                                   returned_state=raw[1] if raw is not None and len(raw) >= 2 else None)
        maximum = MAX_DATA_VALUE_BYTES if characteristic == DATA else 20
        if len(raw) > maximum:
            raise ProtocolError("GATT value exceeds characteristic bound")
        return raw

    async def collect(self, *, frame_size: int = 5, quality: int = 20, test: bool = False,
                      retry_pending: bool = False,
                      on_verified: Callable[[Result], Awaitable[None]] | None = None,
                      context: dict | None = None) -> Result:
        if test and retry_pending:
            raise ValueError("test and pending retry are mutually exclusive")
        if self._lock.locked():
            raise RuntimeError("transfer already running")
        async with self._lock:
            self.guard()
            self.last_bootdiag = {}
            self.cleanup_error = None
            self.failure_phase = None
            self.failure_connect_subphase = None
            self.connect_timeout_snapshot = {}
            self.phase = "connect"
            started = monotonic()
            ident = secrets.randbelow(0xFFFFFFFF) + 1
            self._timing_started = monotonic()
            self._metadata_timeout_expired = self._capture_write_timeout_expired = False
            self.last_attempt_timing = {"version": 1, "image_id": ident,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "operation_seconds": self.limits.operation_seconds, "capture_wait_seconds": self.limits.capture_seconds,
                "outcome": "running", "metadata_poll_count": 0, "events": [], "events_omitted": 0,
                "capture_write_start_elapsed_ms": None, "capture_write_end_elapsed_ms": None,
                "capture_wait_start_elapsed_ms": None, "capture_wait_end_elapsed_ms": None,
                "capture_wait_timeout_expired": False, "timeout_classification": None}
            capture_timer = None
            session_timer = asyncio.timeout(self.limits.total_seconds)
            commanded = False
            retried_chunks = metadata_retries = 0
            try:
                async with session_timer:
                    async with asyncio.timeout(self.limits.connect_seconds):
                        source = await self.transport.connect()
                    self.guard()
                    self.last_manifest = None
                    if context is not None:
                        self.phase = "clock_sd_policy_sync"
                        await self._configure(context)
                    capture_start = datetime.now(timezone.utc)
                    await self._write(retry_command(ident) if retry_pending else capture_command(
                        ident, frame_size, quality, test=test,
                        flash_duty=capture_light_duty(self.limits.capture_light_percent)))
                    commanded = True
                    capture_timer = asyncio.timeout(self.limits.capture_seconds)
                    self._timing_event("capture_wait_start", limit_seconds=self.limits.capture_seconds)
                    try:
                        async with capture_timer:
                            while True:
                                try:
                                    raw_metadata = await self._read(METADATA)
                                except Exception as err:
                                    if not retryable_gatt_133(err) or metadata_retries >= self.limits.chunk_retries:
                                        raise
                                    metadata_retries += 1
                                    await asyncio.sleep(0.25)
                                    continue
                                metadata = Metadata.parse(raw_metadata, ident, self.limits.max_bytes)
                                if metadata.state == 2:
                                    break
                                if metadata.state != 1:
                                    raise ProtocolError("camera is not capturing")
                                await asyncio.sleep(0.25)
                    finally:
                        self.last_attempt_timing["capture_wait_timeout_expired"] = capture_timer.expired()
                        self._timing_event("capture_wait_end", outer_timeout_expired=capture_timer.expired())
                    capture_end = datetime.now(timezone.utc)
                    manifest = None
                    if context is not None and not test:
                        manifest = Manifest.parse(await self._read(MANIFEST))
                        manifest.validate(mode=context["mode"], run_code=context["run_code"],
                                          now_epoch=int(context["now"]().timestamp()))
                        self.last_manifest = manifest
                        await self._read_diagnostics(strict=True)
                    self.phase = "mtu_gate"
                    mtu = getattr(self.transport, "mtu_size", None)
                    if metadata.version == 2 and (type(mtu) is not int or mtu < MAX_DATA_VALUE_BYTES + 1):
                        raise ProtocolError("wire v2 requires reported ATT MTU >=137 before image reads")
                    assembly = Assembly(metadata)
                    while not assembly.complete:
                        # SELECT is idempotent and READ never advances the camera offset.
                        # Retry at most once within this same connection; never recapture.
                        for attempt in range(self.limits.chunk_retries + 1):
                            try:
                                await self._write(select_command(ident, assembly.offset))
                                raw = await self._read(DATA)
                                assembly.append(raw)
                                break
                            except Exception as err:
                                if not isinstance(err, (TimeoutError, ProtocolError)) and not retryable_gatt_133(err):
                                    raise
                                if attempt == self.limits.chunk_retries:
                                    raise
                                retried_chunks += 1
                                await asyncio.sleep(0.25)
                        await asyncio.sleep(self.limits.chunk_pause_seconds)
                    self.phase = "wire_integrity_check"
                    data = assembly.finish(test=test)
                    self.guard()
                    result = Result(data, ident, metadata.capture_uptime_ms, capture_start, capture_end,
                                    datetime.now(timezone.utc), monotonic() - started, source, test, retry_pending, retried_chunks, metadata_retries,
                                    metadata.version, PAYLOAD_BYTES_BY_VERSION[metadata.version], mtu, manifest, dict(self.last_health), dict(self.last_bootdiag))
                    if on_verified is not None:
                        self.phase = "verified_publish"
                        await on_verified(result)
                        if not test:
                            await self._write(acknowledge_command(ident))
                    self.phase = "complete"
                    self.last_attempt_timing["outcome"] = "complete"
                    return result
            except BaseException as error:
                self.last_attempt_timing.update({"outcome": "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                                                "exception_type": type(error).__name__, "failure_phase": self.phase})
                if isinstance(error, TimeoutError):
                    outer = bool(capture_timer and capture_timer.expired())
                    self.last_attempt_timing["timeout_classification"] = (
                        "capture_wait_and_metadata_operation" if outer and self._metadata_timeout_expired else
                        "capture_ready_wait" if outer else "metadata_operation" if self._metadata_timeout_expired else
                        "capture_write_operation" if self._capture_write_timeout_expired else
                        "session" if session_timer.expired() else "backend_or_other_scope")
                self.failure_phase = self.phase
                if self.phase == "connect":
                    self.failure_connect_subphase = getattr(self.transport, "connect_subphase", None)
                    snapshot = getattr(self.transport, "connect_diagnostics", None)
                    if isinstance(error, TimeoutError) and snapshot is not None:
                        try:
                            self.connect_timeout_snapshot = snapshot()
                        except Exception as diagnostic_error:
                            self.connect_timeout_snapshot = {"snapshot_error_type": type(diagnostic_error).__name__}
                raise
            finally:
                pending_error = sys.exception()
                # A second stop/unload cancellation must not skip disconnect.
                # Cleanup runs in its own bounded task and never checks the
                # collection gate, so a safety trip still releases the slot.
                cleanup = asyncio.create_task(self._cleanup(ident, commanded))
                cancelled_during_cleanup = False
                while True:
                    try:
                        self.cleanup_error = await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        if cleanup.done():
                            self.cleanup_error = "disconnect cleanup was cancelled"
                            break
                        cancelled_during_cleanup = True
                self.last_attempt_timing["cleanup_confirmed"] = self.cleanup_error is None
                self.last_attempt_timing["session_timeout_expired"] = session_timer.expired()
                self._timing_event("attempt_finished")
                self._timing_started = None
                if cancelled_during_cleanup:
                    self.last_attempt_timing["outcome"] = "cancelled"
                    raise asyncio.CancelledError
                if self.cleanup_error and pending_error is None:
                    self.last_attempt_timing["outcome"] = "cleanup_failed"
                    raise RuntimeError(self.cleanup_error)

    async def _cleanup(self, ident: int, commanded: bool) -> str | None:
        if commanded:
            try:
                async with asyncio.timeout(1):
                    await self.transport.write(CONTROL, release_command(ident))
            except Exception:
                pass
        try:
            async with asyncio.timeout(5):
                await self.transport.close()
        except Exception as err:
            return f"disconnect unconfirmed: {type(err).__name__}"
        return None
