"""AG-4: the TEST MODE GUARD refusal names the repo's own slug, never the operator's."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import forge_check_run as fcr  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_opt_in(monkeypatch):
    monkeypatch.delenv(fcr.TEST_POST_OPT_IN_ENV, raising=False)


def _repo(path: Path, origin: str | None) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _refusal(monkeypatch, repo: Path) -> str:
    monkeypatch.chdir(repo)
    with pytest.raises(fcr.ForgeTestPostRefused) as excinfo:
        fcr.publish_check_run("a" * 40, "vnx-gate/review", "success", "test")
    return str(excinfo.value)


def test_refusal_names_the_slug_of_this_checkout(monkeypatch, tmp_path):
    repo = _repo(tmp_path / "r", "https://github.com/other-owner/other-repo.git")
    message = _refusal(monkeypatch, repo)
    assert "other-owner/other-repo" in message
    assert "Vinix24" not in message


def test_refusal_without_origin_says_this_repository(monkeypatch, tmp_path):
    message = _refusal(monkeypatch, _repo(tmp_path / "r", None))
    assert "this repository" in message


def test_refusal_survives_a_resolver_that_raises(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("resolver broke")

    monkeypatch.setattr(fcr, "resolve_owner_repo", boom)
    message = _refusal(monkeypatch, _repo(tmp_path / "r", "https://github.com/a/b.git"))
    assert "this repository" in message
