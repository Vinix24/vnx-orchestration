"""tests/test_oi897_bare_repo_data_dir.py — OI-897(b).

In a bare git repo whose only identity signal is an origin remote, the data
dir must default CENTRAL (``~/.vnx-data/<project_id>``) instead of falling back
to the repo-local ``<repo>/.vnx-data``. project_id resolves from the remote
(ADR-007), so the state root must follow it — otherwise ``vnx horizon list``
crashes with ``sqlite3.OperationalError: unable to open database file`` because
the tracks DB is searched at a path that was never created.

On origin/main ``_resolve_state_project_id`` yields None in a bare repo (the
identity chain + marker resolve nothing), so ``_resolve_state_root`` lands on
the repo-local fallback → these tests are RED on origin/main.

The bare-repo dir must stay clean (only ``.git``) after the whole flow: the
system may not be able to work there yet, but it must not pollute the repo.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts" / "lib"
for p in (LIB, ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import project_root  # noqa: E402
import vnx_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _bootstrap_central_store(data_root: Path) -> Path:
    """Bootstrap a migratable runtime store at ``data_root/state`` with a tracks
    table (mirrors ``_bootstrap_store`` in test_horizon_schema_migration_fix.py)."""
    import schema_migration  # type: ignore

    state_dir = data_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    saved_hooks = {k: list(v) for k, v in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    try:
        from coordination_db import init_schema, db_path_from_state_dir  # type: ignore
        init_schema(state_dir)
        db_path = db_path_from_state_dir(state_dir)
        from project_id_migration import run_runtime_coordination_migration  # type: ignore
        run_runtime_coordination_migration(db_path)
        from migrations.auto_apply import auto_apply  # type: ignore
        auto_apply(db_path)
    finally:
        schema_migration._PREFLIGHT_HOOKS.clear()
        schema_migration._PREFLIGHT_HOOKS.update(saved_hooks)
    return db_path


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A git repo with an origin remote resolving to project_id ``vnx-dev`` and
    no VNX files at all."""
    repo = tmp_path / "bare-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Vinix24/vnx-dev.git"],
        cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def central_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin HOME to a tmp dir and pre-create the central store for vnx-dev."""
    home = tmp_path / "home"
    home.mkdir()
    _bootstrap_central_store(home / ".vnx-data" / "vnx-dev")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, h=home: Path(h)))
    monkeypatch.setenv("HOME", str(home))
    for key in ("VNX_PROJECT_ID", "VNX_OPERATOR_ID", "VNX_DATA_DIR",
                "VNX_STATE_DIR", "VNX_DISPATCH_DIR", "VNX_LOGS_DIR",
                "VNX_DATA_DIR_EXPLICIT", "VNX_HOME", "PROJECT_ROOT",
                "VNX_PROJECT_ROOT", "VNX_BIN"):
        monkeypatch.delenv(key, raising=False)
    return home


def _central_db_path(home: Path) -> Path:
    return home / ".vnx-data" / "vnx-dev" / "state" / "runtime_coordination.db"


# ---------------------------------------------------------------------------
# Unit: resolve_data_root in a bare repo
# ---------------------------------------------------------------------------

class TestBareRepoDataDirResolution:
    def test_remote_project_id_resolves(self, bare_repo, central_home):
        pid = vnx_paths._resolve_state_project_id(bare_repo)
        assert pid == "vnx-dev", f"expected remote-derived pid, got {pid!r}"

    def test_data_dir_defaults_central(self, bare_repo, central_home):
        data_dir = vnx_paths.resolve_data_root(bare_repo)
        assert data_dir == (central_home / ".vnx-data" / "vnx-dev").resolve()

    def test_no_remote_stays_repo_local_collision_safe(self, tmp_path, central_home):
        """A dir with no resolvable identity must NOT guess a shared id."""
        plain = tmp_path / "plain"
        plain.mkdir()
        data_dir = vnx_paths.resolve_data_root(plain)
        assert data_dir == (plain / ".vnx-data").resolve()


# ---------------------------------------------------------------------------
# Integration: vnx horizon list in a bare repo
# ---------------------------------------------------------------------------

def _run_horizon_list(repo: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("VNX_PROJECT_ID", "VNX_OPERATOR_ID", "VNX_DATA_DIR",
                "VNX_STATE_DIR", "VNX_DISPATCH_DIR", "VNX_LOGS_DIR",
                "VNX_DATA_DIR_EXPLICIT", "VNX_HOME", "PROJECT_ROOT",
                "VNX_PROJECT_ROOT", "VNX_BIN"):
        env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "vnx_cli.main", "horizon", "list",
         "--project-dir", str(repo)],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo,
    )


class TestBareRepoHorizonList:
    def test_horizon_list_reads_central_store_and_stays_clean(self, bare_repo, central_home):
        result = _run_horizon_list(bare_repo, central_home)
        assert result.returncode == 0, (
            f"vnx horizon list crashed: rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "unable to open database file" not in result.stderr
        # The tracks DB was opened at the CENTRAL path, not a repo-local one.
        assert _central_db_path(central_home).exists()
        # The repo dir must contain only .git — no stray .vnx-data pollution.
        entries = sorted(p.name for p in bare_repo.iterdir())
        assert entries == [".git"], f"bare repo polluted: {entries}"

    def test_mismatch_warning_is_gone(self, bare_repo, central_home):
        """The OI-897(b) VNXDataDirMismatchWarning must no longer fire when the
        remote-derived pid's central store exists."""
        result = _run_horizon_list(bare_repo, central_home)
        assert "VNXDataDirMismatchWarning" not in result.stderr
        assert "does not belong to project" not in result.stderr


# ---------------------------------------------------------------------------
# Unit: local-filesystem origins are not project identities (OI-1253)
# ---------------------------------------------------------------------------

def _repo_with_origin(tmp_path: Path, origin: str, name: str = "repo") -> Path:
    """A git repo whose ``origin`` remote is set to ``origin`` (no clone needed —
    ``git remote add`` does not validate local paths)."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
    return repo


class TestGitRemoteOriginRefusal:
    """A central install's git origin is not a project identity.

    The release route clones the install from a temp checkout dir
    (``vnx release publish`` passes it as ``--source``), so the install's
    ``origin`` is a dangling path like ``/var/folders/.../checkout``. Its
    basename must never become a project_id — otherwise every script running
    from the install resolves to the fabricated store ``~/.vnx-data/checkout``.
    """

    def test_temp_dir_origin_yields_no_project_id(self, tmp_path):
        temp_origin = tmp_path / "vnx-release-nnksecu" / "checkout"
        repo = _repo_with_origin(tmp_path, str(temp_origin), name="temp-repo")
        assert vnx_paths._project_id_from_git_remote(repo) is None

    def test_nonexistent_path_origin_yields_no_project_id(self, tmp_path):
        gone = tmp_path / "gone" / "checkout"
        repo = _repo_with_origin(tmp_path, str(gone), name="gone-repo")
        assert vnx_paths._project_id_from_git_remote(repo) is None

    def test_ssh_project_remote_still_resolves(self, tmp_path):
        repo = _repo_with_origin(
            tmp_path, "git@github.com:Vinix24/vnx-dev.git", name="ssh-repo",
        )
        assert vnx_paths._project_id_from_git_remote(repo) == "vnx-dev"

    def test_https_project_remote_still_resolves(self, tmp_path):
        repo = _repo_with_origin(
            tmp_path, "https://github.com/Vinix24/seocrawler-v2.git", name="https-repo",
        )
        assert vnx_paths._project_id_from_git_remote(repo) == "seocrawler-v2"

    def test_file_url_origin_yields_no_project_id(self, tmp_path):
        repo = _repo_with_origin(
            tmp_path, f"file://{tmp_path}/source/checkout", name="fileurl-repo",
        )
        assert vnx_paths._project_id_from_git_remote(repo) is None

    def test_resolve_project_id_refuses_local_path_origin(self, tmp_path, monkeypatch):
        """project_root.resolve_project_id (the data_dir_guard fallback) must
        refuse the same local-path origin, not derive ``checkout``."""
        temp_origin = tmp_path / "vnx-release-nnksecu" / "checkout"
        repo = _repo_with_origin(tmp_path, str(temp_origin), name="pid-temp-repo")
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.chdir(repo)
        with pytest.raises(RuntimeError):
            project_root.resolve_project_id()

    def test_resolve_project_id_ssh_remote_still_resolves(self, tmp_path, monkeypatch):
        repo = _repo_with_origin(
            tmp_path, "git@github.com:Vinix24/vnx-dev.git", name="pid-ssh-repo",
        )
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.chdir(repo)
        assert project_root.resolve_project_id() == "vnx-dev"
