"""Shared fixture helper: give a data root a dmf-infra checkout that lists the
install capabilities dmf-init requires."""
from __future__ import annotations

from pathlib import Path

from dmf_init.repos import INSTALL_CAPABILITIES_FILE, REQUIRED_INSTALL_CAPABILITIES


def write_install_capabilities(
    data_root: Path, capabilities: tuple[str, ...] = REQUIRED_INSTALL_CAPABILITIES
) -> Path:
    """Write the capability file under ``<data_root>/repos`` and return its path."""
    path = data_root / "repos" / INSTALL_CAPABILITIES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test fixture\n" + "".join(f"{c}\n" for c in capabilities), encoding="utf-8")
    return path
