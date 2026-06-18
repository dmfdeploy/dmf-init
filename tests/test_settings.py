from __future__ import annotations

import pytest

from dmf_init.settings import DEFAULT_REPO_BASE_URL, load_settings


def test_repo_base_url_defaults_to_public_org(monkeypatch: pytest.MonkeyPatch) -> None:
    # A first-time / stranger run passes no DMF_REPO_BASE_URL. It must resolve to
    # the public dmfdeploy org so the documented `docker run` one-liner fetches
    # repos without any extra env (regression: this used to be None -> ValueError
    # "repo base URL is required" on the first fetch). dmfdeploy/dmfdeploy#86.
    monkeypatch.delenv("DMF_REPO_BASE_URL", raising=False)
    assert load_settings().repo_base_url == DEFAULT_REPO_BASE_URL
    assert DEFAULT_REPO_BASE_URL == "https://github.com/dmfdeploy"


def test_repo_base_url_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Private-mirror path: a non-empty env var still overrides the default.
    monkeypatch.setenv("DMF_REPO_BASE_URL", "https://git.example.com/mirror")
    assert load_settings().repo_base_url == "https://git.example.com/mirror"


def test_repo_base_url_blank_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty / whitespace env var is treated as unset, not as an empty base URL.
    monkeypatch.setenv("DMF_REPO_BASE_URL", "   ")
    assert load_settings().repo_base_url == DEFAULT_REPO_BASE_URL
