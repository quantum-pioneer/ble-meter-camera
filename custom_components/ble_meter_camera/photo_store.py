"""Private payload Store outside HA's .storage preload and backup namespace."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .storage import MAX_STORE_BYTES, prepare_photo_directory


class PhotoStore:
    """Explicit path I/O; never consult HA's .storage-only preload manager."""
    private = atomic_writes = True

    def __init__(self, hass, entry_id):
        self.hass = hass
        self.key = f"ble_meter_camera_{entry_id}_latest"
        self.path = hass.config.path(".cache", "ble_meter_camera", entry_id, "latest.json")
        self._lock = asyncio.Lock()

    def _load(self):
        path = Path(self.path)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STORE_BYTES:
            raise OSError("private photo Store path is invalid or exceeds bound")
        try:
            value = json.loads(path.read_bytes())
            if value.get("version") != 1 or not isinstance(value.get("data"), dict):
                raise ValueError("invalid photo Store envelope")
            return value["data"]
        except (ValueError, UnicodeDecodeError):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            os.replace(path, path.with_name(path.name + ".corrupt." + stamp))
            return None

    async def async_load(self):
        async with self._lock:
            return await self._io(self._load)

    async def _io(self, function, *args):
        # Cancelling an executor Future does not stop its filesystem thread.
        # Hold the Store lock until real completion so cutoff rollback always
        # follows the last staged write, even under repeated cancellation.
        work = asyncio.ensure_future(self.hass.async_add_executor_job(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                cancelled = True
                if work.cancelled():
                    raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _save(self, data):
        path = Path(self.path)
        prepare_photo_directory(path.parent)
        if path.is_symlink():
            raise OSError("private photo Store path is a symlink")
        raw = json.dumps({"version": 1, "data": data}, separators=(",", ":")).encode()
        if len(raw) > MAX_STORE_BYTES:
            raise OSError("private photo Store exceeds bound")
        temporary = path.with_name(path.name + ".tmp")
        if temporary.is_symlink():
            raise OSError("private photo staging path is a symlink")
        temporary.unlink(missing_ok=True)  # Exact owned interrupted staging.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    async def async_save(self, data):
        async with self._lock:
            await self._io(self._save, data)
