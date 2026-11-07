"""Shadow copy of role skills: ADR-050 contract 5 (agents-als-map PR-2a)."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

ROLES = ("backend-developer", "quality-engineer", "research-analyst")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _make_repo(tmp_path: Path, ignore: bool = True) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    skill = main / "skills" / "alpha"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\n---\nbody\n")
    (skill / "references" / "r.md").write_text("ref\n")
    (main / "skills" / "beta").mkdir()
    (main / "skills" / "beta" / "SKILL.md").write_text("beta\n")
    (main / ".gitignore").write_text(".vnx-agent-skills/\n" if ignore else "other\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "dispatch/x", str(wt))
    return main, wt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("VNX_AGENT_LAYOUT", "VNX_OVERRIDE_AGENT_LAYOUT"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setenv("VNX_AGENT_LAYOUT", "shadow")


def _run(wt, slugs=("alpha", "beta"), role="backend-developer"):
    try:
        import agent_skill_materialize as m
    except ImportError:
        m = None
    assert m is not None, "scripts/lib/agent_skill_materialize.py does not exist"
    return m.materialize_role_skills(wt, role, list(slugs))


def test_shadow_copies_skills_with_equal_hashes_and_leaves_git_clean(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    result = _run(wt)
    assert result["status"] == "ok"
    role_dir = wt / ".vnx-agent-skills" / "backend-developer"
    manifest = json.loads((role_dir / "manifest.json").read_text())
    assert manifest["role"] == "backend-developer"
    assert [s["slug"] for s in manifest["skills"]] == ["alpha", "beta"]
    alpha = manifest["skills"][0]
    assert alpha["source_dir"] == "skills/alpha"
    assert {f["path"] for f in alpha["files"]} == {"SKILL.md", "references/r.md"}
    for skill in manifest["skills"]:
        for f in skill["files"]:
            copy = (role_dir / skill["slug"] / f["path"]).read_bytes()
            orig = (wt / "skills" / skill["slug"] / f["path"]).read_bytes()
            assert hashlib.sha256(copy).hexdigest() == f["sha256"] == hashlib.sha256(orig).hexdigest()
            assert f["length"] == len(orig)
    assert _git(wt, "status", "--porcelain") == ""


def test_off_creates_no_directory(tmp_path):
    _, wt = _make_repo(tmp_path)
    result = _run(wt)
    assert result["status"] == "skipped"
    assert not (wt / ".vnx-agent-skills").exists()


@pytest.mark.parametrize("slug", ["../x", "/etc", "a/b", "", "alpha/../beta"])
def test_bad_slug_is_refused_and_nothing_is_copied(tmp_path, shadow, slug, caplog):
    _, wt = _make_repo(tmp_path)
    with caplog.at_level(logging.WARNING):
        result = _run(wt, slugs=("alpha", slug))
    assert result["status"] == "error"
    assert result["reason"].startswith("slug_refused:")
    assert not (wt / ".vnx-agent-skills" / "backend-developer" / "alpha").exists()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    manifest = json.loads((wt / ".vnx-agent-skills" / "backend-developer" / "manifest.json").read_text())
    assert manifest["status"] == "error" and manifest["reason"] == result["reason"]


def test_symlink_slug_pointing_outside_is_refused(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("evil\n")
    (wt / "skills" / "evil").symlink_to(outside)
    result = _run(wt, slugs=("evil",))
    assert result["status"] == "error"
    assert result["reason"] == "slug_outside_skill_trees:evil"
    assert not (wt / ".vnx-agent-skills" / "backend-developer" / "evil").exists()


def test_symlinked_file_pointing_outside_is_refused(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("s\n")
    (wt / "skills" / "alpha" / "leak.md").symlink_to(secret)
    result = _run(wt, slugs=("alpha",))
    assert result["status"] == "error"
    assert result["reason"] == "symlink_outside_skill:leak.md"


def test_unknown_slug_and_unsafe_role_are_errors_not_raises(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    assert _run(wt, slugs=("nope",))["reason"] == "slug_unknown:nope"
    assert _run(wt, role="../evil")["reason"] == "role_refused:../evil"
    assert not (tmp_path / "evil").exists()


def test_not_ignored_writes_nothing(tmp_path, shadow, caplog):
    _, wt = _make_repo(tmp_path, ignore=False)
    with caplog.at_level(logging.WARNING):
        result = _run(wt)
    assert result == {"status": "error", "reason": "not_ignored", "role": "backend-developer"}
    assert not (wt / ".vnx-agent-skills").exists()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_leftover_directory_is_replaced(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    stale = wt / ".vnx-agent-skills" / "backend-developer" / "old"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("x")
    other_role = wt / ".vnx-agent-skills" / "quality-engineer"
    other_role.mkdir()
    (other_role / "keep.txt").write_text("k")
    assert _run(wt)["status"] == "ok"
    assert not stale.exists()
    assert (other_role / "keep.txt").exists()


def test_leftover_symlink_to_outside_is_unlinked_and_target_survives(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("p")
    (wt / ".vnx-agent-skills").mkdir()
    (wt / ".vnx-agent-skills" / "backend-developer").symlink_to(outside)
    assert _run(wt)["status"] == "ok"
    assert (outside / "precious.txt").read_text() == "p"
    assert list(outside.iterdir()) == [outside / "precious.txt"]
    assert not (wt / ".vnx-agent-skills" / "backend-developer").is_symlink()


def test_shadow_dir_symlinked_outside_the_worktree_is_refused(tmp_path, shadow):
    _, wt = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (wt / ".vnx-agent-skills").symlink_to(outside)
    result = _run(wt)
    assert result["status"] == "error"
    assert list(outside.iterdir()) == []


def test_autocommit_scope_never_includes_the_shadow_dir():
    from dispatch_paths import filter_paths

    files = [".vnx-agent-skills/backend-developer/alpha/SKILL.md", "scripts/a.py"]
    for allowed in (["scripts/", "tests/"], ["."], [".vnx-agent-skills-not"], ["scripts"]):
        assert not [f for f in filter_paths(files, allowed) if f.startswith(".vnx-agent-skills/")]


def test_repo_gitignore_ignores_the_shadow_dir():
    lines = (REPO / ".gitignore").read_text().splitlines()
    assert ".vnx-agent-skills/" in lines


@pytest.mark.parametrize("role", ROLES)
def test_role_config_declares_resolvable_skills(role):
    from agent_resolver import resolve_agent
    from skill_prefix import resolve_skill_dir

    cfg = resolve_agent(role, REPO)
    assert cfg is not None and cfg.skills, f"{role} declares no skills"
    assert len(set(cfg.skills)) == len(cfg.skills)
    assert "fabric-reference" in cfg.skills
    for slug in cfg.skills:
        assert resolve_skill_dir(REPO, slug) is not None, slug
