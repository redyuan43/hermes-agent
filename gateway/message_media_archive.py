"""Durable storage for message-owned local media artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_hermes_home


MAX_MESSAGE_MEDIA_BYTES = 512 * 1024 * 1024
DEFAULT_MESSAGE_MEDIA_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
MESSAGE_MEDIA_ARCHIVE_MAX_BYTES_ENV = "HERMES_MESSAGE_MEDIA_ARCHIVE_MAX_BYTES"
STALE_PART_MAX_AGE_SECONDS = 60 * 60
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")


def _archive_root() -> Path:
    return get_hermes_home() / "media" / "messages"


def _archive_max_bytes() -> int:
    raw = os.environ.get(MESSAGE_MEDIA_ARCHIVE_MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MESSAGE_MEDIA_ARCHIVE_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MESSAGE_MEDIA_ARCHIVE_MAX_BYTES


def archived_message_media_path(
    *,
    session_id: str,
    media_id: str,
    name: str,
) -> Path:
    """Return the deterministic path for one archived message attachment."""
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    suffix = Path(name).suffix.lower()
    if not _SAFE_SUFFIX.fullmatch(suffix):
        suffix = ""
    return (
        _archive_root()
        / session_digest
        / f"{media_id}{suffix}"
    )


def find_archived_message_media(
    *,
    session_id: str,
    media_id: str,
    name: str,
) -> Optional[Path]:
    """Return a healthy archived artifact, if one already exists."""
    path = archived_message_media_path(
        session_id=session_id,
        media_id=media_id,
        name=name,
    )
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def prune_message_media_archive(
    *,
    root: Optional[Path] = None,
    max_bytes: Optional[int] = None,
    now: Optional[float] = None,
    preserve: Iterable[Path] = (),
) -> int:
    """Remove stale partial files and oldest artifacts above the byte budget."""
    archive_root = root or _archive_root()
    if not archive_root.is_dir():
        return 0

    current_time = time.time() if now is None else now
    preserved = {path.resolve(strict=False) for path in preserve}
    files: list[tuple[float, int, Path]] = []
    total_bytes = 0
    removed = 0

    for path in archive_root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if path.name.endswith(".part"):
            if current_time - stat.st_mtime >= STALE_PART_MAX_AGE_SECONDS:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            continue
        files.append((stat.st_mtime, stat.st_size, path))
        total_bytes += stat.st_size

    budget = _archive_max_bytes() if max_bytes is None else max(0, max_bytes)
    if budget > 0 and total_bytes > budget:
        for _mtime, size, path in sorted(files):
            if total_bytes <= budget:
                break
            if path.resolve(strict=False) in preserved:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total_bytes -= size
            removed += 1

    for directory in sorted(
        (path for path in archive_root.rglob("*") if path.is_dir()),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed


def archive_message_media_file(
    source: Path,
    *,
    session_id: str,
    media_id: str,
    name: str,
) -> Path:
    """Copy one trusted local artifact into durable message-owned storage."""
    source = source.resolve(strict=True)
    stat = source.stat()
    if not source.is_file() or stat.st_size <= 0:
        raise ValueError("message media source is empty or not a file")
    if stat.st_size > MAX_MESSAGE_MEDIA_BYTES:
        raise ValueError("message media source exceeds the 512 MiB limit")

    destination = archived_message_media_path(
        session_id=session_id,
        media_id=media_id,
        name=name,
    )
    existing = find_archived_message_media(
        session_id=session_id,
        media_id=media_id,
        name=name,
    )
    if existing is not None and existing.stat().st_size == stat.st_size:
        return existing

    archive_root = _archive_root()
    archive_root.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_root, 0o700)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}.part"
    )
    try:
        written = 0
        with source.open("rb") as input_file, temporary.open("wb") as output_file:
            os.chmod(temporary, 0o600)
            while chunk := input_file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_MESSAGE_MEDIA_BYTES:
                    raise ValueError(
                        "message media source exceeds the 512 MiB limit"
                    )
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if written != stat.st_size:
            raise OSError("message media source changed while being archived")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        prune_message_media_archive(preserve=(destination,))
        return destination
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
