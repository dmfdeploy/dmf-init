"""Bounded retention: trim whole segments, oldest first, and remember where the trim ended."""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .segment import ANCHOR_NAME, fsync_dir, read_lines, segments


@dataclass(frozen=True)
class Retention:
    segment_bytes: int = 1024 * 1024
    max_bytes: int = 64 * 1024 * 1024
    # 13 months, counted as 396 days.
    max_age: timedelta = timedelta(days=396)


def _last_entry(path: Path) -> dict | None:
    for line in reversed(read_lines(path)):
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        if isinstance(entry, dict):
            return entry
    return None


def _write_anchor(directory: Path, entry: dict) -> None:
    """Replace the anchor atomically with the last entry about to be trimmed.

    The old anchor stays in place unless the new one was written in full.
    """
    tmp = directory / (ANCHOR_NAME + ".tmp")
    data = json.dumps({"seq": entry["seq"], "hash": entry["hash"]}).encode()
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("short write")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, directory / ANCHOR_NAME)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    fsync_dir(directory)


def trim(directory: Path, policy: Retention, now: datetime | None = None) -> int:
    """Delete old segments while the record is over the size cap or too old.

    The newest segment is never deleted. Returns the number of segments removed.
    """
    now = now or datetime.now(UTC)
    removed = 0
    while True:
        found = segments(directory)
        if len(found) < 2:
            return removed
        oldest = found[0][1]
        last = _last_entry(oldest)
        if last is None or "hash" not in last:
            return removed  # cannot anchor a segment we cannot read; leave it for verify
        total = sum(path.stat().st_size for _, path in found)
        too_old = datetime.fromisoformat(last["time"]) < now - policy.max_age
        if total <= policy.max_bytes and not too_old:
            return removed
        _write_anchor(directory, last)
        oldest.unlink()
        fsync_dir(directory)
        removed += 1
