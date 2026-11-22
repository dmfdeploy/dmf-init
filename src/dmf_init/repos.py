from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger("dmf_init")

REPO_NAMES = (
    "dmf-env",
    "dmf-infra",
    "dmf-runbooks",
    "dmf-cms",
    "dmf-media",
    "dmf-promsd",
)
DEFAULT_REFS = {
    "dmf-env": "main",
    "dmf-infra": "main",
    "dmf-runbooks": "main",
    "dmf-cms": "main",
    "dmf-media": "main",
    "dmf-promsd": "main",
}


# What dmf-init needs from the dmf-infra checkout. dmf-infra lists the
# capabilities its install provides in INSTALL_CAPABILITIES_FILE. A checkout that
# predates a capability does not list it, and dmf-init refuses to run against it.
# "openbao-seed-in-install": the install seeds OpenBao with its own token, so no
# step after it seeds. Without it, an install finishes with OpenBao unseeded.
REQUIRED_INSTALL_CAPABILITIES = ("openbao-seed-in-install",)
INSTALL_CAPABILITIES_FILE = Path("dmf-infra") / "k3s-lab-bootstrap" / "install-capabilities.txt"
_CAPABILITY_TOKEN = re.compile(r"[a-z0-9-]+")


class StaleRuntimeReposError(RuntimeError):
    """The cached dmf-infra checkout lacks a capability dmf-init requires."""


def missing_install_capabilities(repos_root: Path) -> list[str]:
    """Return the required capabilities the dmf-infra checkout does not list.

    A missing or unreadable file counts as missing every capability. Blank lines
    and lines that start with ``#`` are ignored; any other line is one capability.
    """
    try:
        text = (repos_root / INSTALL_CAPABILITIES_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return list(REQUIRED_INSTALL_CAPABILITIES)
    listed = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and _CAPABILITY_TOKEN.fullmatch(line):
            listed.add(line)
    return [cap for cap in REQUIRED_INSTALL_CAPABILITIES if cap not in listed]


def _checkout_ref(checkout: Path) -> str:
    """Name the branch or commit the checkout is on, for the refusal message."""
    for args in (["rev-parse", "--abbrev-ref", "HEAD"], ["rev-parse", "--short", "HEAD"]):
        try:
            out = subprocess.run(
                ["git", "-C", str(checkout), *args],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if out and out != "HEAD":
            return out
    return "unknown"


def require_install_capabilities(repos_root: Path) -> None:
    """Raise StaleRuntimeReposError if the dmf-infra checkout lacks a needed capability."""
    missing = missing_install_capabilities(repos_root)
    if not missing:
        return
    checkout = repos_root / "dmf-infra"
    raise StaleRuntimeReposError(
        f"The dmf-infra checkout at {checkout} (ref {_checkout_ref(checkout)}) does "
        f"not declare the install capability {', '.join(missing)}. The selected ref "
        f"must include it. Remove {repos_root} to fetch the current refs, or select "
        "a ref that includes it, then start again."
    )


class RepoFetchRequest(BaseModel):
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    refs: dict[str, str] = Field(default_factory=dict)


class RepoProvenance(BaseModel):
    name: str
    ref: str
    sha: str
    source_url: str
    destination: str
    dirty: str = ""  # P1: git status --porcelain output, empty = clean


class RepoFetchResult(BaseModel):
    provenance_path: str
    repos: list[RepoProvenance]


def _repo_url(base_url: str, repo_name: str) -> str:
    return f"{base_url.rstrip('/')}/{repo_name}.git"


def _write_askpass_script(directory: Path) -> Path:
    script = directory / "git-askpass.sh"
    script.write_text(
        """#!/bin/sh
case "$1" in
  *Username*) printf '%s' "${DMF_INIT_GIT_USERNAME:-}" ;;
  *) printf '%s' "${DMF_INIT_GIT_PASSWORD:-}" ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _sanitize(text: str, *secrets: str | None) -> str:
    scrubbed = text
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed


def fetch_runtime_repos(data_root: Path, request: RepoFetchRequest) -> RepoFetchResult:
    base_url = (request.base_url or "").strip()
    if not base_url:
        raise ValueError("repo base URL is required")

    repos_root = data_root / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)

    provenance: list[RepoProvenance] = []
    logger.info(
        "repo fetch requested",
        extra={"event": "repo_fetch_requested", "repos": list(REPO_NAMES)},
    )

    helper_context = (
        tempfile.TemporaryDirectory(dir=data_root)
        if request.username is not None or request.password is not None
        else nullcontext(None)
    )
    with helper_context as helper_dir:
        git_env = os.environ.copy()
        git_env["GIT_TERMINAL_PROMPT"] = "0"
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"
        if helper_dir is not None:
            helper = _write_askpass_script(Path(helper_dir))
            git_env["GIT_ASKPASS"] = str(helper)
            git_env["DMF_INIT_GIT_USERNAME"] = request.username or ""
            git_env["DMF_INIT_GIT_PASSWORD"] = request.password or ""

        for repo_name in REPO_NAMES:
            ref = request.refs.get(repo_name, DEFAULT_REFS[repo_name])
            source_url = _repo_url(base_url, repo_name)
            destination = repos_root / repo_name
            if destination.exists():
                shutil.rmtree(destination)

            try:
                _run_git(
                    [
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        ref,
                        "--single-branch",
                        source_url,
                        str(destination),
                    ],
                    cwd=data_root,
                    env=git_env,
                )
                sha_result = subprocess.run(
                    ["git", "-C", str(destination), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=git_env,
                )
            except subprocess.CalledProcessError as exc:
                stderr = _sanitize((exc.stderr or "").strip(), request.username, request.password)
                stdout = _sanitize((exc.stdout or "").strip(), request.username, request.password)
                detail = stderr or stdout or "git clone failed"
                raise RuntimeError(f"{repo_name} fetch failed: {detail}") from exc

            provenance.append(
                RepoProvenance(
                    name=repo_name,
                    ref=ref,
                    sha=sha_result.stdout.strip(),
                    source_url=source_url,
                    destination=str(destination),
                )
            )

    provenance_path = data_root / "provenance" / "repos.json"
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(
        json.dumps(
            {"repos": [item.model_dump() for item in provenance]},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info(
        "repo fetch complete",
        extra={"event": "repo_fetch_complete", "repo_count": len(provenance)},
    )
    return RepoFetchResult(
        provenance_path=str(provenance_path),
        repos=provenance,
    )
