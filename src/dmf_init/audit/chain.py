"""The append-only, hash-chained audit record."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import retention
from .retention import Retention
from .schema import MAX_ENTRY_BYTES, AuditSchemaError, validate
from .segment import (
    ANCHOR_NAME,
    GENESIS_PREV,
    canonical,
    entry_hash,
    fsync_dir,
    read_lines,
    segment_name,
    segments,
)
from .verify import verify


class AuditWriteError(Exception):
    """The entry could not be made durable. The caller must not go on."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _month(path: Path) -> str | None:
    """Return the UTC month (YYYY-MM) of the segment's first entry, or None if it has none."""
    lines = read_lines(path)
    try:
        return json.loads(lines[0])["time"][:7] if lines else None
    except (ValueError, KeyError, TypeError):
        return None


class AuditLog:
    """One chain in one directory. Many processes may append; a file lock orders them.

    A segment rolls at `segment_bytes` or when a new entry falls in a later UTC month
    than the segment's first entry. The monthly roll lets age-based trimming run on a
    record that is too quiet to reach the size limit.
    """

    def __init__(
        self,
        directory: Path,
        policy: Retention | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.directory = Path(directory)
        self.policy = policy or Retention()
        self._clock = clock
        self.dropped = 0

    def require_written(self, entry: dict) -> dict:
        """Append the entry and make it durable, or raise.

        Use this before any action that must not happen unrecorded, such as
        creating a root token. The whole retained chain is verified first; if it is
        broken, nothing is appended and the call raises. Returns the stored entry.
        """
        checked = validate(entry)
        try:
            return self._append(checked, check_integrity=True)
        except OSError as exc:
            raise AuditWriteError(f"audit record not written: {exc}") from exc

    def try_write(self, entry: dict) -> bool:
        """Append the entry if possible. Never raises; a failure is counted in `dropped`.

        Use this on the unseal path, where a full or broken record must not block
        recovery. It skips the integrity check that `require_written` makes.
        """
        try:
            self._append(validate(entry), check_integrity=False)
        except Exception:
            self.dropped += 1
            return False
        return True

    def _append(self, fields: dict, *, check_integrity: bool) -> dict:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(self.directory / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._append_locked(fields, check_integrity)
        finally:
            os.close(lock_fd)  # closing releases the lock

    def _append_locked(self, fields: dict, check_integrity: bool) -> dict:
        with contextlib.suppress(OSError):
            retention.finish_interrupted_trim(self.directory)
        found = segments(self.directory)
        tail = self._tail(found)
        if check_integrity:
            result = verify(self.directory)
            if not result.intact:
                raise AuditWriteError(
                    f"the record is broken ({result.status.value} at seq {result.seq}); "
                    "no gated write until it is restored or rebuilt"
                )
        entries = []
        if tail is None:
            tail = (0, GENESIS_PREV)
            entries.append(self._seal({"event": "genesis"}, tail))
            tail = (entries[0]["seq"], entries[0]["hash"])
        entries.append(self._seal(fields, tail))
        lines = [canonical(entry) for entry in entries]
        if any(len(line) > MAX_ENTRY_BYTES for line in lines):
            raise AuditSchemaError(f"entry is larger than {MAX_ENTRY_BYTES} bytes once sealed")
        data = b"".join(line + b"\n" for line in lines)

        number, path = found[-1] if found else (1, self.directory / segment_name(1))
        rolled = False
        if found and (
            path.stat().st_size >= self.policy.segment_bytes
            or _month(path) not in (None, entries[-1]["time"][:7])
        ):
            number, path = number + 1, self.directory / segment_name(number + 1)
            rolled = True
        self._write(path, data, created=rolled or not found)
        if rolled:
            # Trimming is housekeeping: the entry is already durable, so a failure here
            # must not turn into a failed write.
            with contextlib.suppress(OSError):
                retention.trim(self.directory, self.policy, self._clock())
        return entries[-1]

    def _seal(self, fields: dict, tail: tuple[int, str]) -> dict:
        seq, prev = tail
        body = {"seq": seq + 1, "time": self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"), "prev": prev}
        body.update(fields)
        body["hash"] = entry_hash(body)
        return body

    def _tail(self, found: list[tuple[int, Path]]) -> tuple[int, str] | None:
        """Return (seq, hash) of the newest entry, or None for an empty chain."""
        for _, path in reversed(found):
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                # A crash cut the last write short. It was never acknowledged, so drop it.
                os.truncate(path, data.rfind(b"\n") + 1)
            for line in reversed(read_lines(path)):
                try:
                    entry = json.loads(line)
                    return entry["seq"], entry["hash"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise AuditWriteError(f"{path.name} ends in an unreadable entry") from exc
        if (self.directory / ANCHOR_NAME).exists():
            # Trimming never removes the newest segment, so an anchor with no segment
            # means the record was deleted. Do not resume from the anchor alone.
            raise AuditWriteError("the anchor exists but every segment is missing")
        return None

    def _write(self, path: Path, data: bytes, *, created: bool) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.fstat(fd).st_size
            try:
                if os.write(fd, data) != len(data):
                    raise OSError("short write")
                os.fsync(fd)
                if created:
                    fsync_dir(self.directory)
            except OSError:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, size)  # leave nothing half-written behind
                raise
        finally:
            os.close(fd)
