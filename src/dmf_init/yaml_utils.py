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


def rewrite_yaml_scalar(path: Path, key: str, value: str) -> None:
    if not path.is_file():
        return
    mode = path.stat().st_mode & 0o777
    text = path.read_text(encoding="utf-8")
    rewritten = re.sub(
        rf"^({re.escape(key)}:\s*).*$",
        rf"\1{yaml_scalar(value)}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if rewritten != text:
        path.write_text(rewritten, encoding="utf-8")
        path.chmod(mode)
