from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {
    "authorization",
    "cookie",
    "passphrase",
    "password",
    "secret",
    "session",
    "token",
}

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|auth(?:orization)?|cookie|passphrase|password|"
        r"secret|token)\s*[:=]\s*([^,\s]+)"
    ),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
)


def scrub_token_from_url(url: str) -> str:
    split = urlsplit(url)
    params = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if key != "token"
    ]
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(params, doseq=True), split.fragment)
    )


def _is_secret_key(key: object) -> bool:
    return str(key).lower() in _SECRET_KEYS


def _redact_value(value: object, key: object | None = None) -> object:
    if key is not None and _is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {inner_key: _redact_value(inner, inner_key) for inner_key, inner in value.items()}
    if isinstance(value, list | tuple):
        redacted = [_redact_value(item) for item in value]
        return type(value)(redacted)
    if isinstance(value, str):
        scrubbed = scrub_token_from_url(value)
        for pattern in _SECRET_PATTERNS:
            scrubbed = pattern.sub(
                lambda match: match.group(1) + "= [REDACTED]"
                if match.groups()
                else "[REDACTED]",
                scrubbed,
            )
        return scrubbed
    return value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in logging.LogRecord.__dict__:
                continue
            record.__dict__[key] = _redact_value(value, key)
        if isinstance(record.msg, str):
            record.msg = _redact_value(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_value(item) for item in record.args)
            elif isinstance(record.args, dict):
                record.args = {key: _redact_value(value) for key, value in record.args.items()}
        return True


class AccessLogTokenScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) < 3:
            return True
        args = list(record.args)
        args[2] = scrub_token_from_url(str(args[2]))
        record.args = tuple(args)
        return True


class JSONLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": _redact_value(record.getMessage()),
        }
        if record.created:
            payload["timestamp"] = self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")

        for key, value in record.__dict__.items():
            if key in {
                "args",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            if key.startswith("_"):
                continue
            payload[key] = _redact_value(value, key)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
