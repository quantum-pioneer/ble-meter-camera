"""Bounded BLE meter camera v1/v2 wire protocol (no HA dependencies)."""
from __future__ import annotations

from dataclasses import dataclass
import struct
import zlib

SERVICE = "9a8e0001-55d5-4c52-a75b-9dfe0e5f5a01"
CONTROL = "9a8e0002-55d5-4c52-a75b-9dfe0e5f5a01"
METADATA = "9a8e0003-55d5-4c52-a75b-9dfe0e5f5a01"
DATA = "9a8e0004-55d5-4c52-a75b-9dfe0e5f5a01"
MANIFEST = "9a8e0005-55d5-4c52-a75b-9dfe0e5f5a01"
HEALTH = "9a8e0006-55d5-4c52-a75b-9dfe0e5f5a01"
BOOT_DIAGNOSTIC = "9a8e0007-55d5-4c52-a75b-9dfe0e5f5a01"
BOOT_STAGES = {0: "UNKNOWN", 1: "APP_ENTERED", 2: "STATE_INIT", 3: "CAMERA_INIT",
               4: "POLICY_NVS", 5: "SD_INIT", 6: "FLASH_INIT", 7: "BLE_INIT",
               8: "BLE_SERVICE", 9: "ADVERTISING_REQUESTED", 10: "ADVERTISING_ACTIVE",
               11: "ADVERTISING_EXHAUSTED"}
BOOT_DIAGNOSTIC_ATTRIBUTES = (
    "camera_bootdiag_version", "camera_bootdiag_flags", "camera_bootdiag_nvs_ready",
    "camera_bootdiag_prior_valid", "camera_bootdiag_counter_overflow", "camera_bootdiag_write_error",
    "camera_boot_counter", "camera_previous_boot_counter", "camera_previous_boot_stage",
    "camera_previous_boot_stage_name", "camera_previous_reset_reason", "camera_previous_strap_register",
    "camera_bootdiag_observed_at", "camera_bootdiag_read_error_type")
MAX_IMAGE_BYTES = 128 * 1024
PAYLOAD_BYTES = 12  # Legacy v1; v2 keeps the same ID/offset header.
PAYLOAD_BYTES_BY_VERSION = {1: 12, 2: 128}
MAX_DATA_VALUE_BYTES = 8 + 128
FRAME_SIZES = {1: (160, 120), 5: (320, 240), 8: (640, 480), 9: (800, 600),
               10: (1024, 768), 12: (1280, 1024), 13: (1600, 1200)}
META = struct.Struct("<BBHIIII")
CHUNK = struct.Struct("<II")


def configure_command(now_epoch: int, mode: str, run_code: int) -> bytes:
    from .const import TEST_RETENTION_SECONDS
    from .policy import CUTOFF
    if mode not in ("TEST_PHOTO", "METER") or not 0 < run_code <= 0xffffffff:
        raise ValueError("invalid capture mode/run")
    return META.pack(7, 1 if mode == "TEST_PHOTO" else 2, 0,
                     now_epoch, int(CUTOFF.timestamp()), run_code, TEST_RETENTION_SECONDS)


@dataclass(frozen=True)
class Manifest:
    flags: int
    sd_error: int
    captured_epoch: int
    expires_epoch: int
    run_code: int
    sd_sequence: int

    @classmethod
    def parse(cls, raw: bytes):
        if len(raw) != 20:
            raise ProtocolError("camera manifest length is not 20")
        version, flags, error, captured, expires, run, sequence = META.unpack(raw)
        if version != 1:
            raise ProtocolError("unsupported camera manifest version")
        return cls(flags, error, captured, expires, run, sequence)

    def validate(self, *, mode, run_code, now_epoch, photo=True):
        expected_mode = 16 if mode == "TEST_PHOTO" else 32
        if not self.flags & 4 or not self.flags & expected_mode or self.run_code != run_code:
            raise ProtocolError("camera clock/mode/run is not synchronized")
        if self.sd_error or not self.flags & 1:
            raise ProtocolError(f"camera microSD is unavailable (error {self.sd_error})")
        if photo:
            if not self.flags & 2 or not self.sd_sequence:
                raise ProtocolError("camera did not commit JPEG to microSD")
            if mode == "TEST_PHOTO" and (self.flags & 8 or now_epoch >= self.expires_epoch):
                raise ProtocolError("expired camera test work rejected")
            if not self.captured_epoch and not self.flags & 128:
                raise ProtocolError("camera original time is missing without fallback declaration")
            if self.captured_epoch and (self.captured_epoch > now_epoch + 5 or mode == "TEST_PHOTO" and self.expires_epoch != self.captured_epoch + 604800):
                raise ProtocolError("invalid original camera capture/expiry time")


class ProtocolError(Exception):
    """Reject transfer; retain last good picture."""


def parse_health(raw):
    if len(raw) != 20:
        raise ProtocolError("camera health length is not 20")
    version, reason, sd_error, boot, uptime, files, pending = META.unpack(raw)
    if version != 1:
        raise ProtocolError("unsupported camera health version")
    return {"camera_reset_reason": reason, "camera_sd_error": sd_error,
            "camera_boot_id": boot, "camera_uptime_ms": uptime,
            "camera_sd_files": files, "camera_sd_pending": pending}


def parse_boot_diagnostic(raw):
    """Decode persistent application-start evidence, without inferring a cause."""
    if len(raw) != 20:
        raise ProtocolError("camera boot diagnostic length is not 20")
    version, stage, flags, current, previous, reason, strap = META.unpack(raw)
    if version != 1 or flags & ~15 or stage not in BOOT_STAGES:
        raise ProtocolError("unsupported camera boot diagnostic version/stage/flags")
    ready, valid, overflow, write_error = (bool(flags & bit) for bit in (1, 2, 4, 8))
    if valid:
        if not ready or not previous or not stage or current != min(previous + 1, 0xffffffff):
            raise ProtocolError("inconsistent camera boot diagnostic counters/prior record")
    elif stage or previous or reason or strap or current != (1 if ready else 0):
        raise ProtocolError("inconsistent camera boot diagnostic without a valid prior record")
    if overflow != (valid and previous == 0xffffffff):
        raise ProtocolError("inconsistent camera boot diagnostic counter overflow")
    return {"camera_bootdiag_version": version, "camera_bootdiag_flags": flags,
            "camera_bootdiag_nvs_ready": ready, "camera_bootdiag_prior_valid": valid,
            "camera_bootdiag_counter_overflow": overflow, "camera_bootdiag_write_error": write_error,
            # Zero means unavailable; any write error makes this counter tentative.
            "camera_boot_counter": current,
            "camera_previous_boot_counter": previous if valid else None,
            "camera_previous_boot_stage": stage if valid else None,
            "camera_previous_boot_stage_name": BOOT_STAGES[stage] if valid else None,
            "camera_previous_reset_reason": reason if valid else None,
            "camera_previous_strap_register": strap if valid else None}


@dataclass(frozen=True)
class Metadata:
    state: int
    error: int
    image_id: int
    length: int
    crc32: int
    capture_uptime_ms: int
    version: int = 1

    @classmethod
    def parse(cls, raw: bytes, expected_id: int, max_bytes: int) -> Metadata:
        if len(raw) != META.size:
            raise ProtocolError("metadata length is not 20")
        version, state, error, ident, length, crc, uptime = META.unpack(raw)
        if version not in PAYLOAD_BYTES_BY_VERSION or state not in (0, 1, 2, 3):
            raise ProtocolError("unsupported metadata version/state")
        if ident != expected_id:
            raise ProtocolError("metadata image identifier mismatch")
        if state == 3 or error:
            raise ProtocolError(f"camera error {error}")
        if state == 2 and not 0 < length <= min(max_bytes, MAX_IMAGE_BYTES):
            raise ProtocolError("image length outside configured bound")
        return cls(state, error, ident, length, crc, uptime, version)


def capture_light_duty(percent: int) -> int:
    """Map the bounded user setting to the camera's optional raw PWM byte."""
    if type(percent) is not int or not 0 <= percent <= 25:
        raise ValueError("capture light brightness must be an integer 0..25 percent")
    return min(64, round(percent * 255 / 100))


def capture_command(ident: int, frame_size: int, quality: int, *, test: bool = False,
                    flash_duty: int = 0) -> bytes:
    if frame_size not in FRAME_SIZES or not 10 <= quality <= 40:
        raise ValueError("unsupported resolution or JPEG quality")
    if type(flash_duty) is not int or not 0 <= flash_duty <= 64:
        raise ValueError("capture light duty must be an integer 0..64")
    command = struct.pack("<BIBB", 4 if test else 1, ident, frame_size, quality)
    # Zero preserves the legacy packet. TEST never turns on capture lighting.
    return command + bytes([flash_duty]) if flash_duty and not test else command


def select_command(ident: int, offset: int) -> bytes:
    return struct.pack("<BII", 2, ident, offset)


def acknowledge_command(ident: int) -> bytes:
    """Mark optional SD pending image delivered only after durable JPEG publish."""
    return struct.pack("<BI", 5, ident)


def retry_command(ident: int) -> bytes:
    """Load newest optional SD pending capture under a fresh transfer ID."""
    return struct.pack("<BI", 6, ident)


def release_command(ident: int) -> bytes:
    return struct.pack("<BI", 3, ident)


class Assembly:
    """Accept exactly sequential fixed-size chunks; allocation follows validated metadata."""
    def __init__(self, metadata: Metadata) -> None:
        if not 0 < metadata.length <= MAX_IMAGE_BYTES:
            raise ProtocolError("invalid buffer size")
        if metadata.version not in PAYLOAD_BYTES_BY_VERSION:
            raise ProtocolError("unsupported assembly wire version")
        self.payload_bytes = PAYLOAD_BYTES_BY_VERSION[metadata.version]
        self.metadata = metadata
        self._data = bytearray()

    @property
    def offset(self) -> int:
        return len(self._data)

    @property
    def complete(self) -> bool:
        return self.offset == self.metadata.length

    def append(self, raw: bytes) -> None:
        remaining = self.metadata.length - self.offset
        expected = min(self.payload_bytes, remaining)
        if expected <= 0 or len(raw) != CHUNK.size + expected:
            raise ProtocolError("unexpected chunk length")
        ident, offset = CHUNK.unpack_from(raw)
        if ident != self.metadata.image_id or offset != self.offset:
            raise ProtocolError("chunk image identifier or offset mismatch")
        self._data.extend(raw[CHUNK.size:])

    def finish(self, *, test: bool = False) -> bytes:
        if not self.complete:
            raise ProtocolError("incomplete image")
        result = bytes(self._data)
        if zlib.crc32(result) & 0xFFFFFFFF != self.metadata.crc32:
            raise ProtocolError("CRC32 mismatch")
        if test:
            if result != bytes(range(256)):
                raise ProtocolError("test payload mismatch")
        elif not (result.startswith(b"\xff\xd8") and result.endswith(b"\xff\xd9")):
            raise ProtocolError("invalid JPEG markers")
        return result
