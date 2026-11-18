"""Check a record for corruption, gaps and trims; read intents and accessors from it."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .segment import (
    ANCHOR_NAME,
    GENESIS_PREV,
    entry_hash,
    read_anchor,
    read_entries,
    read_lines,
    segments,
)


class Status(Enum):
    OK = "ok"
    TRIMMED = "trimmed"  # old entries were removed by retention; the rest is intact
    GAP = "gap"  # entries or whole segments are missing
    TAMPER = "tamper"  # an entry changed, or the chain links are broken


@dataclass(frozen=True)
class VerifyResult:
    status: Status
    seq: int | None  # first bad sequence number for GAP and TAMPER
    entries: int
    detail: str = ""

    @property
    def intact(self) -> bool:
        return self.status in (Status.OK, Status.TRIMMED)


def verify(directory: Path) -> VerifyResult:
    directory = Path(directory)
    anchor = read_anchor(directory)
    if not segments(directory) and (directory / ANCHOR_NAME).exists():
        # Trimming never removes the newest segment, so this means the record was deleted.
        seq = anchor["seq"] + 1 if anchor else None
        return VerifyResult(Status.GAP, seq, 0, "every segment is missing")
    status = Status.OK
    expected: int | None = None
    prev = GENESIS_PREV
    count = 0

    for _, path in segments(directory):
        for line in read_lines(path):
            try:
                entry = json.loads(line)
                seq, claimed_hash, claimed_prev = entry["seq"], entry["hash"], entry["prev"]
            except (ValueError, KeyError, TypeError):
                return VerifyResult(Status.TAMPER, expected, count, f"{path.name}: unreadable")
            if entry_hash(entry) != claimed_hash:
                return VerifyResult(Status.TAMPER, seq, count, "hash does not match the entry")
            if expected is None:
                if seq == 1:
                    pass
                elif anchor and anchor["seq"] == seq - 1 and anchor.get("hash") == claimed_prev:
                    status = Status.TRIMMED
                    prev = claimed_prev
                else:
                    return VerifyResult(Status.GAP, 1, count, "record does not start at seq 1")
            elif seq != expected:
                detail = f"expected seq {expected}, found {seq}"
                return VerifyResult(Status.GAP, expected, count, detail)
            if claimed_prev != prev:
                return VerifyResult(Status.TAMPER, seq, count, "prev does not match entry before")
            prev = claimed_hash
            expected = seq + 1
            count += 1
    return VerifyResult(status, None, count)


def open_intents(directory: Path) -> list[dict]:
    """Return intent entries whose request has no `closed` entry yet."""
    entries = list(read_entries(Path(directory)))
    closed = {e.get("request_id") for e in entries if e.get("event") == "closed"}
    return [e for e in entries if e.get("event") == "intent" and e.get("request_id") not in closed]


def recorded_accessors(directory: Path) -> list[str]:
    """Return every accessor named in a `minted` entry, oldest first."""
    return [
        accessor
        for e in read_entries(Path(directory))
        if e.get("event") == "minted"
        for accessor in e.get("accessors", [])
    ]
