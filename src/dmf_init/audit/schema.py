"""The closed set of audit events and the rules that keep secrets out of the record."""

from __future__ import annotations

import re

COMMON = ("actor", "role", "request_id", "reason")

# Each event lists the fields it accepts. Anything else is rejected. The chain writes
# its own `genesis` entry; callers cannot.
EVENTS: dict[str, tuple[str, ...]] = {
    "install": (*COMMON, "initial_token_revoked"),
    "unseal": (*COMMON, "asserted_at", "credential_id", "uv", "claimed"),
    "checkin": (*COMMON, "claimed"),
    "intent": (*COMMON, "session", "catalogue_entry"),
    "minted": ("request_id", "session", "accessors"),
    "closed": ("request_id", "outcome"),
    "change": (*COMMON, "target", "detail"),
    "enroll": (*COMMON, "credential_id", "claimed"),
    "remove": (*COMMON, "credential_id"),
    "restore": (*COMMON, "previous_seq", "previous_hash", "unlinked"),
    "snapshot_download": COMMON,
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "intent": ("request_id",),
    "minted": ("request_id", "accessors"),
    "closed": ("request_id", "outcome"),
}

# OpenBao service, batch and recovery tokens start with "s.", "b." or "r."
# (https://openbao.org/docs/concepts/tokens/). The "hvs.", "hvb." and "hvr."
# forms are the HashiCorp Vault equivalents; they are rejected as well.
# Accessors have no prefix and pass.
_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:hv[sbr]|[sbr])\.[A-Za-z0-9_-]{20,}")


class AuditSchemaError(ValueError):
    """The entry is not an allowed audit event."""


def _check_text(where: str, text: str) -> None:
    if "biometric" in text.lower():
        raise AuditSchemaError(f"{where}: the record never says 'biometric'")
    if _TOKEN.search(text):
        raise AuditSchemaError(f"{where}: value looks like a token")


def _check_scalar(where: str, value: object) -> None:
    if isinstance(value, str):
        _check_text(where, value)
    elif value is not None and not isinstance(value, bool | int | float):
        raise AuditSchemaError(f"{where}: unsupported value type {type(value).__name__}")


def _check_field(name: str, value: object) -> None:
    if name == "claimed":
        if not isinstance(value, dict):
            raise AuditSchemaError("claimed: must be an object")
        for key, item in value.items():
            _check_text(f"claimed key {key!r}", str(key))
            _check_scalar(f"claimed.{key}", item)
    elif isinstance(value, list):
        for item in value:
            _check_scalar(name, item)
    else:
        _check_scalar(name, value)


def validate(entry: dict) -> dict:
    """Return a checked copy of the entry, or raise AuditSchemaError."""
    event = entry.get("event")
    if event not in EVENTS:
        raise AuditSchemaError(f"unknown event: {event!r}")
    allowed = ("event", *EVENTS[event])
    for name, value in entry.items():
        if name not in allowed:
            raise AuditSchemaError(f"{event}: unknown field {name!r}")
        _check_text("field name", name)
        _check_field(name, value)
    for name in REQUIRED.get(event, ()):
        if name not in entry:
            raise AuditSchemaError(f"{event}: missing field {name!r}")
    if event == "minted":
        accessors = entry["accessors"]
        if not (isinstance(accessors, list) and len(accessors) == 2 and all(accessors)):
            raise AuditSchemaError("minted: accessors must list exactly two accessors")
    if event == "restore":
        linked = "previous_seq" in entry and "previous_hash" in entry
        if linked == bool(entry.get("unlinked")):
            raise AuditSchemaError("restore: give previous_seq and previous_hash, or unlinked")
    return dict(entry)
