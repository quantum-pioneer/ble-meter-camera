"""Owned TEST PHOTO archive: 168-hour expiry, no unexpired capacity eviction."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import re
from threading import RLock
import zlib
from .const import TEST_RETENTION_SECONDS, TEST_RUN_ID
from .policy import expiration

OWNED_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}\.jpg")
RUN_NAME = re.compile(r"test-[a-zA-Z0-9_-]{1,64}")


class History:
    def __init__(self, directory: str, days: int, max_bytes: int):
        self.directory = Path(directory)
        self.days = 7
        self.max_bytes = max_bytes
        self._lock = RLock()
        self.next_expiry = None

    def _directories(self):
        if not self.directory.exists():
            return []
        if self.directory.is_symlink():
            raise OSError("photo archive directory must not be a symlink")
        return [self.directory] + [p for p in self.directory.iterdir()
                                  if not p.is_symlink() and p.is_dir() and RUN_NAME.fullmatch(p.name)]

    def _owned(self):
        files = []
        for directory in self._directories():
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if path.name.endswith(".tmp") and (OWNED_NAME.fullmatch(path.name[:-4]) or
                        path.name.endswith(".json.tmp") and OWNED_NAME.fullmatch(path.name[:-9])):
                    path.unlink()
                    continue
                if path.name.endswith(".jpg.json") and OWNED_NAME.fullmatch(path.name[:-5]):
                    photo = path.with_name(path.name[:-5])
                    if not photo.exists() and not photo.is_symlink():
                        # A manifest without its committed JPEG is owned failed
                        # staging, never a separate archival record.
                        path.unlink()
                    continue
                if not OWNED_NAME.fullmatch(path.name):
                    continue
                received = datetime.strptime(path.name[:22], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
                manifest = path.with_suffix(".jpg.json")
                if manifest.is_symlink():
                    raise OSError("owned photo manifest is a symlink")
                if manifest.exists():
                    if manifest.stat().st_size > 16384:
                        raise OSError("owned photo manifest exceeds bound")
                    try:
                        info = json.loads(manifest.read_text())
                    except (ValueError, UnicodeDecodeError) as err:
                        raise OSError("owned photo manifest is unreadable; photograph preserved") from err
                    if info.get("mode") != "TEST_PHOTO" or not info.get("run_id"):
                        raise OSError("owned test manifest has invalid mode/run")
                    expires = expiration(info)
                else:
                    # Exact legacy v0.4.1 history names are the documented calendar
                    # bench. Preserve their original receipt fallback across migration.
                    info = {"mode": "TEST_PHOTO", "run_id": TEST_RUN_ID,
                            "received_at": received.isoformat(), "captured_at": None,
                            "capture_clock_synchronized": False,
                            "retention_basis": "original_legacy_receipt_time_fallback",
                            "expires_at": (received + timedelta(seconds=TEST_RETENTION_SECONDS)).isoformat()}
                    self._write(manifest, json.dumps(info).encode())
                    expires = expiration(info)
                files.append((expires, path, path.stat().st_size, manifest, info))
        return sorted(files, key=lambda item: (item[0], str(item[1])))

    def prune(self, now: datetime, reserve_bytes: int = 0) -> int:
        with self._lock:
            total = 0
            next_expiry = None
            for expires, path, size, manifest, _ in self._owned():
                if now >= expires:
                    path.unlink()
                    manifest.unlink(missing_ok=True)
                else:
                    total += size
                    next_expiry = min(expires, next_expiry or expires)
            self.next_expiry = next_expiry
            if total + reserve_bytes > self.max_bytes:
                raise OSError("test archive capacity reached; unexpired photographs preserved")
            return total

    @staticmethod
    def _write(path, data):
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, data: bytes, image_id: int, received_at: datetime, info=None) -> int:
        with self._lock:
            if len(data) > self.max_bytes:
                raise OSError("JPEG exceeds photo archive cap")
            info = dict(info or {"mode": "TEST_PHOTO", "run_id": TEST_RUN_ID,
                                "received_at": received_at.isoformat(), "captured_at": None,
                                "capture_clock_synchronized": False,
                                "retention_basis": "receipt_time_fallback"})
            if info.get("mode") != "TEST_PHOTO" or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", info.get("run_id", "")):
                raise OSError("test photo lacks explicit mode/run ownership")
            expires = expiration(info)
            if received_at >= expires:
                raise OSError("expired test image cannot enter archive")
            info["expires_at"] = expires.isoformat()
            info["crc32"] = f"{zlib.crc32(data) & 0xffffffff:08x}"
            directory = self.directory / ("test-" + info["run_id"])
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink():
                raise OSError("test run directory is a symlink")
            self.prune(received_at, len(data))
            name = received_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{image_id:08x}.jpg"
            path = directory / name
            manifest = path.with_suffix(".jpg.json")
            self._write(manifest, json.dumps(info, separators=(",", ":")).encode())
            try:
                self._write(path, data)
            except BaseException:
                manifest.unlink(missing_ok=True)
                raise
            return self.prune(received_at)

    def remove(self, image_id, received_at, run_id):
        """Rollback one just-staged late result, never scan/delete other images."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", run_id):
            raise OSError("invalid run ownership")
        directory = self.directory / ("test-" + run_id)
        if directory.is_symlink():
            raise OSError("run directory is a symlink")
        name = received_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{image_id:08x}.jpg"
        path = directory / name
        if not path.is_symlink():
            path.unlink(missing_ok=True)
        manifest = path.with_suffix(".jpg.json")
        if not manifest.is_symlink():
            manifest.unlink(missing_ok=True)
