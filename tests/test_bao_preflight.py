"""Unit tests for the OpenBao preflight helpers (ADR-0044 facet (d))."""

from __future__ import annotations

import json
from pathlib import Path

from dmf_init.bootstrap_steps import (
    BAO_PREFLIGHT_ID,
    SANDBOX_SINGLE_NODE_PROFILE,
    BootstrapContext,
    build_bao_preflight_step,
    supports_auto_unseal,
)


def test_supports_auto_unseal_sandbox() -> None:
    assert supports_auto_unseal("sandbox-single-node") is True


def test_supports_auto_unseal_multi_node() -> None:
    assert supports_auto_unseal("multi-node-ha") is False


def test_supports_auto_unseal_empty_string() -> None:
    assert supports_auto_unseal("") is False


def test_build_bao_preflight_step_id_and_argv(tmp_path: Path) -> None:
    """Cheap ctx construction via the same layout _make_env_on_disk uses."""
    data_root = tmp_path / "data"
    env_id = "sandbox-alpha"
    env_dir = data_root / "envs" / env_id
    render_dir = data_root / "runs" / env_id
    env_dir.mkdir(parents=True, exist_ok=True)
    render_dir.mkdir(parents=True, exist_ok=True)
    age_key_path = render_dir / "age" / "keys.txt"
    age_key_path.parent.mkdir(parents=True, exist_ok=True)
    age_key_path.write_text("AGE-SECRET-KEY-1TEST\n", encoding="utf-8")
    answers_file_path = render_dir / "answers.yaml"
    answers_file_path.write_text("operator: test\n", encoding="utf-8")
    repos_root = data_root / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)
    (render_dir / "render.json").write_text(
        json.dumps(
            {
                "env_id": env_id,
                "profile": SANDBOX_SINGLE_NODE_PROFILE,
                "schema_version": 1,
                "render_dir": str(render_dir),
                "age_key_path": str(age_key_path),
                "answers_file_path": str(answers_file_path),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    ctx = BootstrapContext.from_data_root(data_root, env_id)
    step = build_bao_preflight_step(ctx)
    assert step.id == BAO_PREFLIGHT_ID
    argv_str = " ".join(step.argv)
    assert "unseal-openbao.sh" in argv_str


def test_canonical_failed_step_id_maps_preflight_to_next() -> None:
    """bao-preflight failure → resolve to the canonical step it guards."""
    from types import SimpleNamespace

    from dmf_init.main import _canonical_failed_step_id

    run = SimpleNamespace(
        failed_step_id="bao-preflight",
        steps=[
            SimpleNamespace(id="bao-preflight"),
            SimpleNamespace(id="configure"),
            SimpleNamespace(id="verify"),
        ],
    )
    assert _canonical_failed_step_id(run) == "configure"


def test_canonical_failed_step_id_passes_through_normal() -> None:
    """Non-preflight failed_step_id is returned as-is."""
    from types import SimpleNamespace

    from dmf_init.main import _canonical_failed_step_id

    run = SimpleNamespace(failed_step_id="configure", steps=[])
    assert _canonical_failed_step_id(run) == "configure"


def test_canonical_failed_step_id_none() -> None:
    """No failure → None."""
    from types import SimpleNamespace

    from dmf_init.main import _canonical_failed_step_id

    run = SimpleNamespace(failed_step_id=None, steps=[])
    assert _canonical_failed_step_id(run) is None


def test_canonical_failed_step_id_preflight_at_end_returns_none() -> None:
    """Preflight is the only step (no next) → None."""
    from types import SimpleNamespace

    from dmf_init.main import _canonical_failed_step_id

    run = SimpleNamespace(
        failed_step_id="bao-preflight",
        steps=[SimpleNamespace(id="bao-preflight")],
    )
    assert _canonical_failed_step_id(run) is None
