"""Tests for ADR-0044 tmpfs fail-closed enforcement of DMF_DATA_ROOT (facet c)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from dmf_init import manage
from dmf_init.main import create_app
from dmf_init.manage import (
    DataRootNotTmpfsError,
    ManageRestoreRequest,
    assert_data_root_tmpfs,
    run_manage_restore,
)
from dmf_init.settings import Settings, load_settings


@pytest.fixture
def fake_fs(monkeypatch):
    """Force _resolve_mount_entry to a chosen fs_type + options (platform-independent)."""

    def _set(fs_type: str | None, opts: set[str] | None = None) -> None:
        if fs_type is None:
            monkeypatch.setattr(manage, "_resolve_mount_entry", lambda _p: None)
        else:
            monkeypatch.setattr(
                manage, "_resolve_mount_entry", lambda _p: (fs_type, opts or set())
            )

    return _set


# --- the shared check ---------------------------------------------------------


def test_assert_enforces_only_on_determinable_non_tmpfs(fake_fs, tmp_path: Path) -> None:
    fake_fs("ext4")
    with pytest.raises(DataRootNotTmpfsError):
        assert_data_root_tmpfs(tmp_path, enforce=True)


def test_assert_warns_but_allows_when_not_enforced(fake_fs, tmp_path: Path, caplog) -> None:
    fake_fs("overlay")
    with caplog.at_level(logging.WARNING):
        assert_data_root_tmpfs(tmp_path, enforce=False)  # no raise
    assert any("tmpfs-backed" in r.message for r in caplog.records)


def test_assert_accepts_tmpfs_even_when_enforced(fake_fs, tmp_path: Path) -> None:
    fake_fs("tmpfs")
    assert_data_root_tmpfs(tmp_path, enforce=True)  # no raise


def test_undeterminable_non_linux_allows_even_when_enforced(fake_fs, tmp_path, monkeypatch) -> None:
    fake_fs(None)
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    assert_data_root_tmpfs(tmp_path, enforce=True)  # can't introspect off-Linux → allow


def test_undeterminable_on_linux_fails_closed_when_enforced(fake_fs, tmp_path, monkeypatch) -> None:
    fake_fs(None)
    monkeypatch.setattr(manage.sys, "platform", "linux")
    with pytest.raises(DataRootNotTmpfsError):
        assert_data_root_tmpfs(tmp_path, enforce=True)  # can't prove RAM-backing → refuse


# --- noexec guard (issue #162) -----------------------------------------------


def test_tmpfs_noexec_raises_when_enforced(fake_fs, tmp_path: Path) -> None:
    """DISCRIMINATOR: tmpfs + noexec must be refused (old guard ignores options)."""
    fake_fs("tmpfs", {"rw", "nosuid", "nodev", "noexec"})
    with pytest.raises(DataRootNotTmpfsError, match="noexec"):
        assert_data_root_tmpfs(tmp_path, enforce=True)


def test_tmpfs_noexec_warns_when_not_enforced(fake_fs, tmp_path: Path, caplog) -> None:
    fake_fs("tmpfs", {"rw", "nosuid", "nodev", "noexec"})
    with caplog.at_level(logging.WARNING):
        assert_data_root_tmpfs(tmp_path, enforce=False)  # no raise
    assert any("noexec" in r.message for r in caplog.records)


def test_tmpfs_exec_passes_when_enforced(fake_fs, tmp_path: Path) -> None:
    """tmpfs with exec (no noexec in options) passes cleanly."""
    fake_fs("tmpfs", {"rw", "nosuid", "nodev"})
    assert_data_root_tmpfs(tmp_path, enforce=True)  # no raise


# --- startup gate -------------------------------------------------------------


def test_create_app_refuses_to_start_on_non_tmpfs(fake_fs, tmp_path: Path) -> None:
    fake_fs("ext4")
    with pytest.raises(DataRootNotTmpfsError):
        create_app(Settings(data_root=tmp_path / "data", require_tmpfs_data_root=True))


def test_create_app_starts_on_non_tmpfs_with_override(fake_fs, tmp_path: Path) -> None:
    fake_fs("ext4")
    app = create_app(Settings(data_root=tmp_path / "data", require_tmpfs_data_root=False))
    assert app is not None


# --- settings default (safe-by-default in the real runtime) -------------------


def test_load_settings_enforces_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DMF_ALLOW_NON_TMPFS_DATA_ROOT", raising=False)
    assert load_settings().require_tmpfs_data_root is True


def test_load_settings_override_disables_enforcement(monkeypatch) -> None:
    monkeypatch.setenv("DMF_ALLOW_NON_TMPFS_DATA_ROOT", "true")
    assert load_settings().require_tmpfs_data_root is False


# --- restore path defense-in-depth --------------------------------------------


def test_restore_refuses_non_tmpfs_when_enforced(fake_fs, tmp_path: Path) -> None:
    fake_fs("ext4")
    req = ManageRestoreRequest(artifact_path=tmp_path / "backup.tar.age", passphrase="pw")
    with pytest.raises(DataRootNotTmpfsError):
        run_manage_restore(tmp_path / "data", req, enforce_tmpfs=True)
