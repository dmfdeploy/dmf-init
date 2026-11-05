from __future__ import annotations

import re
from pathlib import Path


def yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def parse_yaml_scalar_anywhere(directory: Path, key: str) -> str | None:
    if not directory.is_dir():
        return None
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$")
    for yml_path in sorted(directory.glob("*.yml")):
        try:
            text = yml_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                value = match.group(1).strip()
                if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
                    value = value[1:-1]
                return value or None
    return None


class YamlPathError(ValueError):
    """A path line is in a form the line editor cannot rewrite exactly."""


def _parse_scalar_line(key: str, raw: str) -> tuple[str, bool]:
    """Return ``(value, has_comment)`` for the text after ``key:``.

    Supported forms are a plain scalar, a single-quoted scalar and a
    double-quoted scalar without backslash escapes, each with an optional
    trailing comment. Anything else raises YamlPathError.
    """
    raw = raw.strip()
    if raw[:1] == "'":
        match = re.fullmatch(r"'((?:[^']|'')*)'(?:[ \t]+(#.*))?", raw)
        if not match:
            raise YamlPathError(f"{key}: unsupported YAML value {raw!r}")
        return match.group(1).replace("''", "'"), match.group(2) is not None
    if raw[:1] == '"':
        match = re.fullmatch(r'"((?:[^"\\]|\\.)*)"(?:[ \t]+(#.*))?', raw)
        if not match or "\\" in match.group(1):
            raise YamlPathError(f"{key}: unsupported YAML value {raw!r}")
        return match.group(1), match.group(2) is not None
    parts = re.split(r"[ \t]+#", raw, maxsplit=1)
    return parts[0], len(parts) > 1


def _key_line(text: str, key: str) -> re.Match[str] | None:
    return re.search(rf"^({re.escape(key)}:[ \t]*)(.*)$", text, flags=re.MULTILINE)


def read_yaml_scalar(path: Path, key: str) -> str | None:
    """Return the top-level scalar ``key`` holds in ``path``, unquoted.

    Returns None when the file or the key is absent; raises YamlPathError for
    a form the line editor does not support.
    """
    if not path.is_file():
        return None
    match = _key_line(path.read_text(encoding="utf-8"), key)
    if not match:
        return None
    return _parse_scalar_line(key, match.group(2))[0] or None


def rewrite_yaml_scalar(path: Path, key: str, value: str) -> None:
    if not path.is_file():
        return
    mode = path.stat().st_mode & 0o777
    text = path.read_text(encoding="utf-8")
    rewritten = re.sub(
        rf"^({re.escape(key)}:\s*).*$",
        lambda match: match.group(1) + yaml_scalar(value),
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if rewritten != text:
        path.write_text(rewritten, encoding="utf-8")
        path.chmod(mode)


def relocate_yaml_paths(path: Path, keys: tuple[str, ...], old_root: str, new_root: str) -> None:
    """Move each of ``keys`` from under ``old_root`` to under ``new_root``.

    A value changes only when it equals ``old_root`` or starts with
    ``old_root`` followed by a slash; any other value is left as it is. A
    value under ``old_root`` with a trailing comment raises YamlPathError, as
    does any value in an unsupported form. Nothing is written unless every key
    can be rewritten.
    """
    text = path.read_text(encoding="utf-8")
    for key in keys:
        match = _key_line(text, key)
        if not match:
            continue
        value, has_comment = _parse_scalar_line(key, match.group(2))
        if not (value == old_root or value.startswith(old_root + "/")):
            continue
        if has_comment:
            raise YamlPathError(f"{key}: value {match.group(2).strip()!r} has a trailing comment")
        text = (
            text[: match.start()]
            + match.group(1)
            + yaml_scalar(new_root + value[len(old_root):])
            + text[match.end():]
        )
    if text != path.read_text(encoding="utf-8"):
        mode = path.stat().st_mode & 0o777
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
