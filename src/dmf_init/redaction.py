from __future__ import annotations

from collections.abc import Iterable


def redact_text(text: str, secrets: Iterable[str | None]) -> str:
    scrubbed = text
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed
