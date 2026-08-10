#!/usr/bin/env python3
"""Regression tests for the w19c / OI-934 test-store-isolation class guard.

Incident: tests/test_pr_dispatch_integration.py had zero isolation of its
own (no tmp_path/monkeypatch/VNX_DATA_DIR reference) and, whenever a broad
pytest sweep made ``pr_queue_manager`` importable (sys.path pollution from
an earlier-collected test module), it silently resolved VNX_DATA_DIR through
``vnx_paths._resolve_state_root``'s branch 3 ("existing central install —
keep resolving to ~/.vnx-data/<id>") straight to the REAL production central
store, writing real dispatch-staging files and (via the same resolver,
through ``vnx_mode._mode_file_path``'s fallback) flipping the live
``mode.json`` from operator to starter.

The guard lives at the WRITE surfaces (``PRQueueManager.__init__``,
``vnx_mode._guard_mode_write_target``), not inside the generic path
resolver: ``vnx_paths.resolve_paths()`` / ``_resolve_state_root()`` are pure
computations with plenty of legitimate read-only callers (e.g.
tests/test_path_resolution_regression.py asserting on resolution shape with
a deliberately clean env) that must keep resolving to wherever production
would — including the real ``~/.vnx-data/vnx-dev`` — without failing. Only an
imminent WRITE into that path is the actual hazard; see
``vnx_paths.refuse_real_central_store_write_under_pytest``.

Before this fix, neither write surface refused a completely clean-env
resolution that happened to land on the real central store (OI-911's
divergence guard only catches a DIVERGING env override, not "no override at
all"). After this fix, both refuse loudly instead.
"""

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import vnx_paths
import vnx_mode


def _clean_env(monkeypatch):
    # Full VNX_* path key set (mirrors tests/test_context_rotation.py's
    # _clean_env). Several of these — VNX_STATE_DIR, VNX_DISPATCH_DIR,
    # PROJECT_ROOT, ... — get permanently pinned into os.environ the first
    # time any module calls vnx_paths.ensure_env() at IMPORT time (it uses
    # os.environ.setdefault, so whichever value was active at COLLECTION
    # time sticks for the rest of the session, unaffected by a later test's
    # VNX_DATA_DIR override). Leaving any of these unset here makes this
    # test's resolution depend on test collection order across the whole
    # suite instead of on what this test actually configures.
    for key in (
        "VNX_HOME", "VNX_BIN", "VNX_EXECUTABLE", "PROJECT_ROOT",
        "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR",
        "VNX_DISPATCH_DIR", "VNX_LOGS_DIR", "VNX_PIDS_DIR", "VNX_LOCKS_DIR",
        "VNX_REPORTS_DIR", "VNX_DB_DIR", "VNX_SKILLS_DIR",
        "VNX_CANONICAL_ROOT", "VNX_INTELLIGENCE_DIR",
        "VNX_DATA_HOME", "XDG_DATA_HOME", "VNX_PROJECT_ID", "VNX_PROJECT_ROOT",
        "VNX_OPERATOR_ID", "VNX_ORCHESTRATOR_ID", "VNX_AGENT_ID",
        "VNX_DATA_DIR_GUARD",
    ):
        monkeypatch.delenv(key, raising=False)


class TestRefuseRealCentralStoreWriteUnderPytest:
    """The low-level guard function itself: refuses a write target under the
    real (as resolved via HOME) ~/.vnx-data, allows everything else."""

    def test_refuses_write_under_home_central_store(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        target = fake_home / ".vnx-data" / "some-project"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            vnx_paths.refuse_real_central_store_write_under_pytest(target)

    def test_allows_write_under_tmp_path(self, tmp_path, monkeypatch):
        target = tmp_path / "isolated" / "some-project"
        target.mkdir(parents=True)
        # Must not raise.
        vnx_paths.refuse_real_central_store_write_under_pytest(target)

    def test_read_only_resolution_is_unaffected(self, tmp_path, monkeypatch):
        """resolve_paths()/_resolve_state_root() must keep resolving to the
        real store when asked to — the guard only fires at write surfaces,
        never inside the resolver itself (that was the over-broad first
        version of this fix, reverted after it broke
        tests/test_path_resolution_regression.py's read-only assertions)."""
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        (fake_home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        result = vnx_paths._resolve_state_root("vnx-dev", tmp_path / "proj")
        assert result == (fake_home / ".vnx-data" / "vnx-dev").resolve()

        paths = vnx_paths.resolve_paths()
        assert paths["VNX_DATA_DIR"] == str((fake_home / ".vnx-data" / "vnx-dev").resolve())


class TestPrQueueManagerWriteGuard:
    """PRQueueManager() construction is the earliest reliable signal of an
    imminent write — the guard fires there, mirroring the w19c incident."""

    def test_construction_refuses_when_it_would_land_in_real_central_store(
        self, tmp_path, monkeypatch
    ):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        (fake_home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        import pr_queue_manager
        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            pr_queue_manager.PRQueueManager()

    def test_construction_allowed_when_properly_isolated(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        import pr_queue_manager
        manager = pr_queue_manager.PRQueueManager()
        assert str(manager.vnx_state_dir).startswith(str(tmp_path))


class TestWriteModeGuard:
    """vnx_mode.write_mode()'s target guard must refuse a completely clean
    env that resolves (correctly, per production semantics) to the real
    central store — the exact gap OI-911's divergence check cannot see,
    since a clean env has nothing to diverge from."""

    def test_write_mode_refuses_clean_env_real_store_resolution(
        self, tmp_path, monkeypatch
    ):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        central = fake_home / ".vnx-data" / "vnx-dev"
        central.mkdir(parents=True)
        (central / "mode.json").write_text(
            '{"mode": "operator", "schema_version": 1}\n', encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            vnx_mode.write_mode(vnx_mode.VNXMode.STARTER)

        # The (fake) production mode.json must be untouched.
        assert '"operator"' in (central / "mode.json").read_text(encoding="utf-8")

    def test_write_mode_allowed_with_explicit_data_dir(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        vnx_mode.write_mode(vnx_mode.VNXMode.OPERATOR, str(data_dir))
        assert (data_dir / "mode.json").exists()


class TestRefuseRealLaunchAgentsWriteUnderPytest:
    """OI-1117: the launchd-test-isolation guard refuses writes to the real
    ``~/Library/LaunchAgents`` while running under pytest, following the
    same pattern as ``refuse_real_central_store_write_under_pytest``."""

    def test_refuses_write_under_launch_agents(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        target = fake_home / "Library" / "LaunchAgents" / "subdir"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            vnx_paths.refuse_real_launch_agents_write_under_pytest(target)

    def test_refuses_write_directly_in_launch_agents(self, tmp_path, monkeypatch):
        """The guard must also fire when the target IS the LaunchAgents dir
        itself, not just a subdir."""
        fake_home = tmp_path / "home"
        target = fake_home / "Library" / "LaunchAgents"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            vnx_paths.refuse_real_launch_agents_write_under_pytest(target)

    def test_allows_write_under_tmp_path(self, tmp_path, monkeypatch):
        """A target outside ~/Library/LaunchAgents must not trigger the guard."""
        target = tmp_path / "isolated" / "LaunchAgents"
        target.mkdir(parents=True)
        # Must not raise.
        vnx_paths.refuse_real_launch_agents_write_under_pytest(target)

    def test_allows_write_to_other_home_subdir(self, tmp_path, monkeypatch):
        """A target elsewhere under HOME (e.g. ~/Documents) must not trigger
        the guard — the guard only protects LaunchAgents specifically."""
        fake_home = tmp_path / "home"
        target = fake_home / "Documents"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))
        # Must not raise.
        vnx_paths.refuse_real_launch_agents_write_under_pytest(target)

    def test_guard_noop_outside_pytest(self, tmp_path, monkeypatch):
        """Outside pytest the guard is a no-op: the write surface check
        relies on ``PYTEST_CURRENT_TEST`` or ``pytest`` in ``sys.modules``.
        When neither signal is present, the guard returns silently and the
        write proceeds — this is the correct production behaviour."""
        import sys
        fake_home = tmp_path / "home"
        target = fake_home / "Library" / "LaunchAgents"
        target.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        # Remove pytest from sys.modules to simulate production.
        saved = sys.modules.pop("pytest", None)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        try:
            # Must NOT raise — the guard thinks we are not under pytest.
            vnx_paths.refuse_real_launch_agents_write_under_pytest(target)
        finally:
            if saved is not None:
                sys.modules["pytest"] = saved
