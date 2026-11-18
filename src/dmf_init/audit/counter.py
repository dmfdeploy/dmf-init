"""Failed-attempt counter. Separate from the chain, so a flood can never touch the record."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

RING_SIZE = 1000


class FailedAttempts:
    """A running total plus the last `ring` attempts, kept in `directory`.

    One process owns the directory at a time; a second open raises BlockingIOError.
    The file holds up to twice the ring before it is compacted, so a record costs one
    appended line rather than a rewrite.
    """

    def __init__(self, directory: Path, ring: int = RING_SIZE):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._ring = ring
        self._lock = os.open(self.directory / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._path = self.directory / "attempts.jsonl"
        self._base_path = self.directory / "counter.json"
        try:
            self._base = json.loads(self._base_path.read_text())["dropped"]
        except (OSError, ValueError, KeyError):
            self._base = 0
        self._lines = self._load()
        self._file = open(self._path, "a")

    def _load(self) -> list[str]:
        try:
            lines = self._path.read_text().splitlines()
        except OSError:
            return []
        return [line for line in lines if _parses(line)]

    @property
    def total(self) -> int:
        return self._base + len(self._lines)

    def recent(self) -> list[dict]:
        return [json.loads(line) for line in self._lines[-self._ring :]]

    def record(self, source: str, reason: str) -> bool:
        """Count one failed attempt. Never raises; returns False if it could not be saved."""
        line = json.dumps({"time": int(time.time()), "source": source, "reason": reason})
        try:
            self._file.write(line + "\n")
            self._file.flush()
            self._lines.append(line)
            if len(self._lines) > 2 * self._ring:
                self._compact()
        except OSError:
            return False
        return True

    def _compact(self) -> None:
        keep = self._lines[-self._ring :]
        dropped = self._base + len(self._lines) - len(keep)
        tmp = self.directory / "attempts.jsonl.tmp"
        tmp.write_text("".join(line + "\n" for line in keep))
        self._base_path.write_text(json.dumps({"dropped": dropped}))
        os.replace(tmp, self._path)
        self._file.close()
        self._file = open(self._path, "a")
        self._base, self._lines = dropped, keep

    def close(self) -> None:
        self._file.close()
        os.close(self._lock)


def _parses(line: str) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True
