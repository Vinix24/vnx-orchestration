#!/usr/bin/env python3
"""Tests for `vnx role sync` — provider-agnostic fleet-wide T0 role sync.

Dispatch-ID: 20260708-090921-rolesync-provider-agnostic

Covers the two blocking bugs fixed here and the new provider-agnostic feature:
  - Bug 1 (dual-CLI gap): `role sync` now exists on both the bash `bin/vnx` and
    the Python `vnx_cli` entry points, and both must agree.
  - Bug 2 (project resolution): `cmd_role_sync` no longer trusts the ambient
    $PROJECT_ROOT (which a standalone dev checkout of vnx-orchestration always
    collapses onto $VNX_HOME) — it resolves the target repo fresh from cwd's
    git root (or an explicit --project-dir) on every invocation.
  - Feature: --apply also mirrors the canonical role into a marked block in
    AGENTS.md (Codex) and GEMINI.md (Gemini/Kimi), alongside role-orchestrator.md,
    so a Codex/Kimi orchestrator session gets the same role as Claude.

Isolation: every test targets a throwaway tmp_path project via --project-dir or
an isolated cwd; a regression guard asserts the repo's own
.claude/terminals/T0/ is never touched.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VNX = REPO / "bin" / "vnx"
SHIPPED_ROLE = REPO / ".claude" / "terminals" / "T0" / "role-orchestrator.md"

ROLE_MARKER_BEGIN = "<!-- VNX:BEGIN T0-ROLE -->"
ROLE_MARKER_END = "<!-- VNX:END T0-ROLE -->"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_bash(*args: str, cwd: Path, env: dict = None):
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(VNX), "role", "sync", *args],
        capture_output=True, text=True, cwd=str(cwd), env=run_env,
    )


def _run_python(*args: str, cwd: Path, env: dict = None):
    run_env = dict(os.environ)
    run_env["PYTHONPATH"] = str(REPO) + os.pathsep + run_env.get("PYTHONPATH", "")
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "vnx_cli.main", "role", "sync", *args],
        capture_output=True, text=True, cwd=str(cwd), env=run_env,
    )


def _init_git(path: Path):
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(path), check=True)


def _make_project(tmp_path: Path, name: str = "project") -> Path:
    """A bare project with a T0 terminal dir + a thin CLAUDE.md import, no role yet."""
    root = tmp_path / name
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n")
    return root


def _backups(t0_dir: Path, basename: str):
    return sorted(t0_dir.glob(f"{basename}.bak.*"))


# ---------------------------------------------------------------------------
# --dry-run default / --apply writes
# ---------------------------------------------------------------------------

class TestDryRunDefault:
    def test_dry_run_is_default_and_writes_nothing(self, tmp_path):
        project = _make_project(tmp_path)
        r = _run_bash("--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "dry-run" in (r.stdout + r.stderr)
        t0 = project / ".claude" / "terminals" / "T0"
        assert not (t0 / "role-orchestrator.md").exists()
        assert not (t0 / "AGENTS.md").exists()
        assert not (t0 / "GEMINI.md").exists()

    def test_explicit_dry_run_flag_writes_nothing(self, tmp_path):
        project = _make_project(tmp_path)
        r = _run_bash("--dry-run", "--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        t0 = project / ".claude" / "terminals" / "T0"
        assert not (t0 / "role-orchestrator.md").exists()

    def test_apply_writes_all_three_provider_surfaces(self, tmp_path):
        project = _make_project(tmp_path)
        r = _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        t0 = project / ".claude" / "terminals" / "T0"
        assert (t0 / "role-orchestrator.md").read_text() == SHIPPED_ROLE.read_text()
        for provider_file in ("AGENTS.md", "GEMINI.md"):
            content = (t0 / provider_file).read_text()
            assert ROLE_MARKER_BEGIN in content
            assert ROLE_MARKER_END in content
            assert "T0 - VNX Master Orchestrator" in content
        # CLAUDE.md (the thin import) must never be touched by role sync.
        assert (t0 / "CLAUDE.md").read_text() == "@role-orchestrator.md\n"


# ---------------------------------------------------------------------------
# Tri-file write: AGENTS.md + GEMINI.md get the marked role block, idempotent
# ---------------------------------------------------------------------------

class TestTriFileWrite:
    def test_marked_block_contains_full_canonical_role(self, tmp_path):
        project = _make_project(tmp_path)
        _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        agents = (t0 / "AGENTS.md").read_text()
        start = agents.index(ROLE_MARKER_BEGIN) + len(ROLE_MARKER_BEGIN) + 1
        end = agents.index(ROLE_MARKER_END)
        block_body = agents[start:end]
        assert block_body.strip() == SHIPPED_ROLE.read_text().strip()

    def test_idempotent_on_reapply_no_new_backups_no_content_change(self, tmp_path):
        project = _make_project(tmp_path)
        _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        agents_before = (t0 / "AGENTS.md").read_text()
        gemini_before = (t0 / "GEMINI.md").read_text()

        r = _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "already current" in (r.stdout + r.stderr).lower()
        assert (t0 / "AGENTS.md").read_text() == agents_before
        assert (t0 / "GEMINI.md").read_text() == gemini_before
        assert _backups(t0, "AGENTS.md") == []
        assert _backups(t0, "GEMINI.md") == []
        assert _backups(t0, "role-orchestrator.md") == []

    def test_preserves_content_outside_marker_block(self, tmp_path):
        """A hand-written AGENTS.md may carry other project instructions —
        role sync must only touch its own marked block."""
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "AGENTS.md").write_text("# Project Codex notes\n\nSome custom guidance.\n")
        _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        content = (t0 / "AGENTS.md").read_text()
        assert "# Project Codex notes" in content
        assert "Some custom guidance." in content
        assert ROLE_MARKER_BEGIN in content


# ---------------------------------------------------------------------------
# Backup-first + atomic replace
# ---------------------------------------------------------------------------

class TestBackupAndAtomicReplace:
    def test_backup_made_before_role_overwrite_and_holds_old_content(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "role-orchestrator.md").write_text("OUTDATED ROLE CONTENT\n")

        r = _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr

        backups = _backups(t0, "role-orchestrator.md")
        assert len(backups) == 1
        assert backups[0].read_text() == "OUTDATED ROLE CONTENT\n"
        assert (t0 / "role-orchestrator.md").read_text() == SHIPPED_ROLE.read_text()

    def test_provider_file_backup_made_only_on_real_drift(self, tmp_path):
        project = _make_project(tmp_path)
        _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"

        # Corrupt the block body (not just append after it) to force real drift.
        agents = (t0 / "AGENTS.md").read_text()
        corrupted = agents.replace("T0 - VNX Master Orchestrator", "CORRUPTED TITLE")
        (t0 / "AGENTS.md").write_text(corrupted)

        r = _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        backups = _backups(t0, "AGENTS.md")
        assert len(backups) == 1
        assert "CORRUPTED TITLE" in backups[0].read_text()
        assert "CORRUPTED TITLE" not in (t0 / "AGENTS.md").read_text()

    def test_no_temp_files_left_behind(self, tmp_path):
        project = _make_project(tmp_path)
        _run_bash("--apply", "--project-dir", str(project), cwd=tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        leftover = [p for p in t0.iterdir() if p.name.startswith(".role-orchestrator.")]
        assert leftover == []


# ---------------------------------------------------------------------------
# Consumer-repo resolution (Bug 2): targets cwd's repo, not VNX_HOME
# ---------------------------------------------------------------------------

class TestConsumerRepoResolution:
    def test_resolves_git_root_from_a_subdirectory_no_project_dir_flag(self, tmp_path):
        """The core Bug-2 regression: invoking with no --project-dir from a
        sub-directory of a consumer repo must resolve the repo's git ROOT, not
        the sub-directory and not VNX_HOME."""
        project = _make_project(tmp_path, "consumer")
        _init_git(project)
        subdir = project / "some" / "nested" / "dir"
        subdir.mkdir(parents=True)

        r = _run_bash("--apply", cwd=subdir)
        assert r.returncode == 0, r.stderr

        t0 = project / ".claude" / "terminals" / "T0"
        assert (t0 / "role-orchestrator.md").read_text() == SHIPPED_ROLE.read_text()
        # Must not have been written into the subdirectory itself.
        assert not (subdir / ".claude").exists()

    def test_absolute_path_invocation_from_consumer_repo_never_touches_vnx_home(self, tmp_path):
        """Reproduces the exact reported bug: `bash <vnx-checkout>/bin/vnx role
        sync` invoked by absolute path from within a consumer repo must target
        the consumer repo, never VNX_HOME (this repo's own checkout)."""
        project = _make_project(tmp_path, "consumer")
        _init_git(project)

        before = subprocess.run(
            ["git", "status", "--short", ".claude/terminals/T0/"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout

        r = _run_bash("--apply", cwd=project)
        assert r.returncode == 0, r.stderr

        after = subprocess.run(
            ["git", "status", "--short", ".claude/terminals/T0/"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout
        assert before == after, "role sync must never write into VNX_HOME's own checkout"
        assert not list(REPO.glob(".claude/terminals/T0/*.bak.*")), "no backup leak into VNX_HOME"

        t0 = project / ".claude" / "terminals" / "T0"
        assert (t0 / "role-orchestrator.md").exists()

    def test_project_dir_flag_overrides_cwd(self, tmp_path):
        project = _make_project(tmp_path, "consumer")
        unrelated_cwd = tmp_path / "elsewhere"
        unrelated_cwd.mkdir()

        r = _run_bash("--apply", "--project-dir", str(project), cwd=unrelated_cwd)
        assert r.returncode == 0, r.stderr

        t0 = project / ".claude" / "terminals" / "T0"
        assert (t0 / "role-orchestrator.md").exists()
        assert not (unrelated_cwd / ".claude").exists()

    def test_missing_project_t0_dir_errors(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        r = _run_bash("--project-dir", str(empty), cwd=tmp_path)
        assert r.returncode != 0
        assert "bootstrap-terminals" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# t0-orchestrator invocability NOTE (F1 prevention): the shipped role's
# Mandatory Startup step needs the skill EITHER in-context (CLAUDE.md import)
# OR model-invocable (SKILL.md without disable-model-invocation: true). Role
# sync warns, never blocks, when a target project has neither.
# ---------------------------------------------------------------------------

class TestT0OrchestratorInvocabilityNote:
    def test_warns_when_target_has_neither_import_nor_invocable_skill(self, tmp_path):
        project = _make_project(tmp_path)
        r = _run_bash("--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        out = r.stdout + r.stderr
        assert "does not import the t0-orchestrator skill body" in out
        assert "t0_role_audit.sh --static" in out

    def test_silent_when_target_imports_the_skill_body(self, tmp_path):
        project = _make_project(tmp_path)
        t0 = project / ".claude" / "terminals" / "T0"
        (t0 / "CLAUDE.md").write_text(
            "@role-orchestrator.md\n@../../skills/t0-orchestrator/SKILL.md\n"
        )
        r = _run_bash("--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "does not import the t0-orchestrator skill body" not in (r.stdout + r.stderr)

    def test_silent_when_target_skill_is_model_invocable(self, tmp_path):
        project = _make_project(tmp_path)
        skill_dir = project / ".claude" / "skills" / "t0-orchestrator"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: t0-orchestrator\ndescription: test\n---\n\nplaybook body\n"
        )
        r = _run_bash("--project-dir", str(project), cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "does not import the t0-orchestrator skill body" not in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# VNX_HOME guard still refuses in-place (central install only)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_central_vnx_home(tmp_path):
    """A minimal, self-contained central-install VNX_HOME: real copies (not
    symlinks — vnx_paths.sh resolves via `pwd -P`, which would follow a symlink
    straight back to the real repo) of just what cmd_role_sync needs, marked
    with .vnx-install-mode=central so `_guard_not_vnx_home` engages. Deliberately
    omits scripts/lib/vnx_paths.sh so bin/vnx's inline fallback resolver (which
    mirrors the same central-install detection) is exercised instead.
    """
    home = tmp_path / "fake-vnx-home"
    (home / "bin").mkdir(parents=True)
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / ".claude" / "terminals" / "T0").mkdir(parents=True)

    (home / "bin" / "vnx").write_text(VNX.read_text())
    (home / "bin" / "vnx").chmod(0o755)
    (home / "scripts" / "lib" / "vnx_marked_blocks.sh").write_text(
        (REPO / "scripts" / "lib" / "vnx_marked_blocks.sh").read_text()
    )
    (home / ".claude" / "terminals" / "T0" / "role-orchestrator.md").write_text(
        SHIPPED_ROLE.read_text()
    )
    (home / ".vnx-install-mode").write_text("central\n")
    # VNX_HOME must be its own git repo root for the central-install branch of
    # the resolver to engage at all (mirrors a real central install, which is
    # a git clone per scripts/lib update.py) — otherwise the resolver falls
    # through to a different branch entirely and VNX_CANONICAL_ROOT collapses
    # onto VNX_HOME regardless of cwd, a resolver quirk unrelated to role sync.
    _init_git(home)
    return home


class TestVnxHomeGuard:
    def test_refuses_when_target_equals_vnx_home(self, fake_central_vnx_home, tmp_path):
        neutral_cwd = tmp_path / "neutral"
        neutral_cwd.mkdir()
        r = subprocess.run(
            ["bash", str(fake_central_vnx_home / "bin" / "vnx"), "role", "sync",
             "--apply", "--project-dir", str(fake_central_vnx_home)],
            capture_output=True, text=True, cwd=str(neutral_cwd),
        )
        assert r.returncode != 0
        assert "immutable central install" in (r.stdout + r.stderr)
        assert not (fake_central_vnx_home / ".claude" / "terminals" / "T0" / "AGENTS.md").exists()

    def test_allows_a_different_target_under_the_same_central_install(self, fake_central_vnx_home, tmp_path):
        consumer = _make_project(tmp_path, "consumer")
        r = subprocess.run(
            ["bash", str(fake_central_vnx_home / "bin" / "vnx"), "role", "sync",
             "--apply", "--project-dir", str(consumer)],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert r.returncode == 0, r.stderr
        assert (consumer / ".claude" / "terminals" / "T0" / "role-orchestrator.md").exists()


# ---------------------------------------------------------------------------
# Dual-CLI parity: bash and Python (vnx_cli) produce the same result
# ---------------------------------------------------------------------------

class TestDualCliParity:
    def test_python_cli_delegates_and_agrees_with_bash(self, tmp_path):
        bash_project = _make_project(tmp_path, "bash_target")
        python_project = _make_project(tmp_path, "python_target")

        r_bash = _run_bash("--apply", "--project-dir", str(bash_project), cwd=tmp_path)
        assert r_bash.returncode == 0, r_bash.stderr

        r_python = _run_python("--apply", "--project-dir", str(python_project), cwd=tmp_path)
        assert r_python.returncode == 0, r_python.stderr

        bash_t0 = bash_project / ".claude" / "terminals" / "T0"
        python_t0 = python_project / ".claude" / "terminals" / "T0"
        for name in ("role-orchestrator.md", "AGENTS.md", "GEMINI.md"):
            assert (bash_t0 / name).read_text() == (python_t0 / name).read_text(), (
                f"{name} diverged between bash bin/vnx and python vnx_cli"
            )

    def test_python_cli_dry_run_default_matches_bash(self, tmp_path):
        bash_project = _make_project(tmp_path, "bash_target")
        python_project = _make_project(tmp_path, "python_target")

        r_bash = _run_bash("--project-dir", str(bash_project), cwd=tmp_path)
        r_python = _run_python("--project-dir", str(python_project), cwd=tmp_path)

        assert r_bash.returncode == 0 and r_python.returncode == 0
        assert "dry-run" in (r_bash.stdout + r_bash.stderr)
        assert "dry-run" in (r_python.stdout + r_python.stderr)
        assert not (bash_project / ".claude" / "terminals" / "T0" / "role-orchestrator.md").exists()
        assert not (python_project / ".claude" / "terminals" / "T0" / "role-orchestrator.md").exists()

    def test_python_cli_errors_gracefully_without_a_vnx_home(self, tmp_path, monkeypatch):
        """If bin/vnx cannot be found from the resolved engine root, the Python
        CLI must fail loud with a clear message, never silently no-op."""
        project = _make_project(tmp_path, "consumer")
        fake_engine_root = tmp_path / "no-bin-here"
        fake_engine_root.mkdir()

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        script = (
            "import sys; from unittest.mock import patch; "
            "from vnx_cli import _engine; "
            f"patch.object(_engine, 'engine_root', return_value=__import__('pathlib').Path({str(fake_engine_root)!r})).start(); "
            "from vnx_cli.main import main; "
            f"sys.argv = ['vnx', 'role', 'sync', '--project-dir', {str(project)!r}]; "
            "main()"
        )
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, cwd=str(tmp_path), env=env,
        )
        assert r.returncode != 0
        assert "not found" in (r.stdout + r.stderr).lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
