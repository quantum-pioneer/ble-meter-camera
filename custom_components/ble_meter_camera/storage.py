"""Confirm HA Store writes from the filesystem before accepting durable state."""
from __future__ import annotations

import json
import os
import stat
import hashlib
import re
from pathlib import Path
from .policy import expiration
from .history import OWNED_NAME, RUN_NAME

# 128KiB JPEG becomes174764 base64 bytes; metadata/envelope fit below180KiB.
MAX_STORE_BYTES = 180 * 1024


def prepare_photo_directory(directory):
    """Create only the dedicated backup-excluded payload namespace."""
    directory = Path(directory)
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise OSError("photo storage path must not contain symlinks")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def _migrate_file(source, destination, *, conflict_copy=False):
    for parent in source.parents:
        if parent.is_symlink():
            raise OSError("legacy photo source path contains a symlink")
    if source.is_symlink() or not source.is_file():
        return
    if source.stat().st_size > MAX_STORE_BYTES:
        raise OSError("owned legacy photo file exceeds migration bound")
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).digest()
    prepare_photo_directory(destination.parent)
    if destination.is_symlink():
        raise OSError("photo migration destination is a symlink")
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).digest() != digest:
            if not conflict_copy:
                raise OSError("conflicting owned photo migration preserved both sources")
            destination = destination.with_name(destination.name + ".corrupt.legacy-" + digest.hex()[:16])
            if destination.exists() and hashlib.sha256(destination.read_bytes()).digest() != digest:
                raise OSError("conflicting legacy latest preservation file")
    if not destination.exists():
        temporary = destination.with_name(destination.name + ".migration.tmp")
        if temporary.is_symlink():
            raise OSError("photo migration staging is a symlink")
        if temporary.exists():
            temporary.unlink()  # Exact migration-only interrupted staging.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    if destination.is_symlink() or hashlib.sha256(destination.read_bytes()).digest() != digest:
        raise OSError("photo migration verification failed; original preserved")
    destination.chmod(0o600)
    source.unlink()  # Only after the durable excluded copy is verified.


def migrate_photo_storage(config_directory, entry_id):
    """Move exact owned legacy payloads out of HA's ordinary backup scope.

    Controls/state remain in .storage. Unknown files, other entries and symlinks
    are preserved. Re-running after interruption verifies existing destinations.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", entry_id):
        raise OSError("invalid meter-camera storage entry identifier")
    root = Path(config_directory)
    target = root / ".cache" / "ble_meter_camera" / entry_id
    prepare_photo_directory(target)
    abandoned_latest = target / "latest.json.tmp"
    if abandoned_latest.is_symlink():
        raise OSError("owned latest staging is a symlink")
    abandoned_latest.unlink(missing_ok=True)
    legacy_latest = root / ".storage" / f"ble_meter_camera_{entry_id}_latest"
    _migrate_file(legacy_latest, target / "latest.json", conflict_copy=True)
    for path in legacy_latest.parent.glob(legacy_latest.name + ".corrupt.*"):
        suffix = path.name[len(legacy_latest.name):]
        _migrate_file(path, target / ("latest.json" + suffix), conflict_copy=True)
    legacy_history = root / ".storage" / "ble_meter_camera" / entry_id / "history"
    if legacy_history.is_symlink():
        raise OSError("legacy test history is a symlink; migration blocked")
    if not legacy_history.exists():
        return
    directories = [legacy_history] + [p for p in legacy_history.iterdir()
                                    if not p.is_symlink() and p.is_dir() and RUN_NAME.fullmatch(p.name)]
    for directory in directories:
        for source in directory.iterdir():
            name = source.name
            owned = OWNED_NAME.fullmatch(name) or name.endswith(".jpg.json") and OWNED_NAME.fullmatch(name[:-5])
            staged = name.endswith(".tmp") and (OWNED_NAME.fullmatch(name[:-4]) or
                                               name.endswith(".jpg.json.tmp") and OWNED_NAME.fullmatch(name[:-9]))
            if owned:
                _migrate_file(source, target / "history" / source.relative_to(legacy_history))
            elif staged and not source.is_symlink() and source.is_file():
                source.unlink()  # Never-committed exact owned staging.


def verify_store_file(path: str, expected: dict) -> None:
    """Bypass HA Store caches; a swallowed write error must never produce ACK.

    Do not include stored data in errors: the latest store contains a room photo.
    The caller awaits HA Store's private atomic/fsync writer first.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STORE_BYTES:
            raise OSError("project storage file is not regular or exceeds bound")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError("project storage file lacks private permissions")
        raw = source.read(MAX_STORE_BYTES + 1)
    if len(raw) > MAX_STORE_BYTES:
        raise OSError("project storage file exceeds read bound")
    try:
        saved = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise OSError("project storage file failed JSON verification") from err
    if not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("data") != expected:
        raise OSError("project storage write was not durably confirmed")


def prune_owned_corrupt_latest(path: str, now) -> None:
    """Clean only this entry's HA-generated corrupt latest-store siblings."""
    original = Path(path)
    for candidate in original.parent.glob(original.name + ".corrupt.*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.stat().st_size > MAX_STORE_BYTES:
            raise OSError("owned corrupt latest store exceeds read bound")
        try:
            value = json.loads(candidate.read_text())["data"]["info"]
            if value.get("mode", "TEST_PHOTO") == "TEST_PHOTO" and now >= expiration(value):
                candidate.unlink()
        except (ValueError, KeyError, TypeError):
            # This exact owned corruption artifact is failed staging, not a
            # valid photo archive. No arbitrary HA files are touched.
            candidate.unlink()
