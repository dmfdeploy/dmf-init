"""Segment files and the entry hash: the on-disk format shared by every audit module."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

GENESIS_PREV = "0" * 64
ANCHOR_NAME = "anchor.json"

_SEGMENT_RE = re.compile(r"segment-(\d{8})\.jsonl")


def segment_name(number: int) -> str:
    return f"segment-{number:08d}.jsonl"


def segments(directory: Path) -> list[tuple[int, Path]]:
    """Return (number, path) for every segment file, oldest first."""
    found = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = _SEGMENT_RE.fullmatch(path.name)
            if match:
                found.append((int(match.group(1)), path))
    return sorted(found)


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: dict) -> str:
    """SHA-256 over the canonical JSON of the entry without its `hash` field."""
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def read_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8", errors="replace").splitlines()


def read_entries(directory: Path):
    """Yield every parseable entry in order. Use `verify` to check integrity."""
    for _, path in segments(directory):
        for line in read_lines(path):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def read_anchor(directory: Path) -> dict | None:
    try:
        anchor = json.loads((directory / ANCHOR_NAME).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(anchor, dict) and isinstance(anchor.get("seq"), int):
        return anchor
    return None


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
