#!/usr/bin/env python3
"""Central-mode path-resolution regression tests.

For each site migrated by the ``central-mode-path-correctness`` track, prove that
in a simulated central install the resolved path is the project store
(``~/.vnx-data/<project>/...``) and NEVER a ``__file__``-derived keystone walk.

Two simulations per site where applicable:
  1. Env set — ``VNX_DATA_DIR`` / ``VNX_STATE_DIR`` point at a central-style dir;
     the site must honor them (not override with a ``__file__`` walk).
  2. Fallback (the actual fix) — env unset; the canonical vnx_paths resolver
     (which is VNX_HOME + project-marker aware) supplies the central path. We
     stub it to a sentinel and assert the site delegates to it, proving the
     ``__file__`` walk is gone.

The roadmap-path fix (track_reconciler) is covered separately: repo-root first,
CWD git-root next, legacy co-located layout last, never crashing on a miss.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402
import cleanup_worker_exit  # noqa: E402
import cost_tracker  # noqa: E402
import dispatch_enricher  # noqa: E402
import event_store  # noqa: E402
import runtime_facade  # noqa: E402
import shadow_logger  # noqa: E402
import subprocess_adapter  # noqa: E402
import t0_decision_reconcile  # noqa: E402
import t0_escalations_log  # noqa: E402
import track_reconciler  # noqa: E402
import wiring_gate  # noqa: E402
from subprocess_dispatch_internals import state_paths  # noqa: E402

sys.path.insert(0, str(VNX_ROOT / "scripts"))
import planning_cli  # noqa: E402

_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_PROJECT_ID",
    "VNX_ROADMAP_PATH",
    "VNX_DISPATCH_DIR",
)


@pytest.fixture
def central(monkeypatch, tmp_path):
    """Simulate a central install where the canonical resolver returns the
    project store (``<tmp>/.vnx-data/proj-x``), with all VNX_* env unset so the
    migrated fallback path is exercised."""
    data = tmp_path / ".vnx-data" / "proj-x"
    state = data / "state"
    state.mkdir(parents=True)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        vnx_paths,
        "resolve_paths",
        lambda: {
            "VNX_DATA_DIR": str(data),
            "VNX_STATE_DIR": str(state),
            "VNX_DISPATCH_DIR": str(data / "dispatches"),
            "PROJECT_ROOT": str(tmp_path / "project-repo"),
        },
    )
    monkeypatch.setattr(
        vnx_paths, "resolve_state_dir", lambda project_root=None: state
    )
    return data, state


# ---------------------------------------------------------------------------
# Fallback (the fix): env unset → canonical resolver → project store
# ---------------------------------------------------------------------------


def test_t0_escalations_log_data_dir(central):
    data, _ = central
    assert t0_escalations_log._data_dir() == data


def test_t0_decision_reconcile_data_and_state(central):
    data, state = central
    assert t0_decision_reconcile._data_dir() == data
    assert t0_decision_reconcile._state_dir() == state


def test_runtime_facade_state_dir(central):
    _, state = central
    assert runtime_facade._resolve_state_dir() == str(state)


def test_cleanup_worker_exit_state_and_register(central):
    _, state = central
    assert cleanup_worker_exit._resolve_state_dir() == state
    assert (
        cleanup_worker_exit._resolve_dispatch_register_path()
        == state / "dispatch_register.ndjson"
    )


def test_dispatch_enricher_intelligence_db(central):
    _, state = central
    (state / "quality_intelligence.db").write_text("", encoding="utf-8")
    resolved = dispatch_enricher.DispatchEnricher._intelligence_db_path()
    assert resolved == state / "quality_intelligence.db"


def test_event_store_events_dir(central):
    data, _ = central
    assert event_store._events_dir() == data / "events"


def test_subprocess_adapter_stderr_log_path(central):
    data, _ = central
    adapter = subprocess_adapter.SubprocessAdapter()
    resolved = adapter._resolve_stderr_log_path("T1", "d1")
    assert resolved == data / "logs" / "subprocess" / "T1_d1.stderr.log"


def test_cost_tracker_receipts_path(central):
    _, state = central
    assert cost_tracker._resolve_receipts_path() == state / "t0_receipts.ndjson"


def test_shadow_logger_ledger_path(central):
    _, state = central
    resolved = shadow_logger._resolve_ledger_path(None)
    assert resolved == state / shadow_logger.LEDGER_FILENAME


def test_wiring_gate_skip_list_reads_project_store(central):
    _, state = central
    (state / "wiring_skip.yaml").write_text(
        "library_exports:\n  - some_symbol\n", encoding="utf-8"
    )
    assert wiring_gate._load_skip_list() == {"some_symbol"}


def test_no_site_returns_keystone(central):
    """Blanket invariant: no migrated site returns a ~/.vnx-system keystone path."""
    resolved = [
        str(t0_escalations_log._data_dir()),
        str(t0_decision_reconcile._data_dir()),
        str(runtime_facade._resolve_state_dir()),
        str(cleanup_worker_exit._resolve_state_dir()),
        str(event_store._events_dir()),
        str(cost_tracker._resolve_receipts_path()),
        str(shadow_logger._resolve_ledger_path(None)),
    ]
    for p in resolved:
        assert ".vnx-system" not in p, p


# ---------------------------------------------------------------------------
# Env-set: VNX_DATA_DIR / VNX_STATE_DIR honored (not overridden by a walk)
# ---------------------------------------------------------------------------


def test_env_vnx_data_dir_honored(monkeypatch, tmp_path):
    data = tmp_path / ".vnx-data" / "proj-x"
    data.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    assert t0_escalations_log._data_dir() == data
    assert t0_decision_reconcile._data_dir() == data
    adapter = subprocess_adapter.SubprocessAdapter()
    assert str(adapter._resolve_stderr_log_path("T2", "d2")).startswith(str(data))


def test_env_vnx_state_dir_honored(monkeypatch, tmp_path):
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    assert runtime_facade._resolve_state_dir() == str(state)
    assert cleanup_worker_exit._resolve_state_dir() == state
    assert cost_tracker._resolve_receipts_path() == state / "t0_receipts.ndjson"


# ---------------------------------------------------------------------------
# state_paths (subprocess_dispatch_internals) — helper-return __file__ sites
# ---------------------------------------------------------------------------


def test_state_paths_default_state_dir(central):
    _, state = central
    assert state_paths._default_state_dir() == state


def test_state_paths_active_dispatch_dir_uses_dispatch_dir(central, monkeypatch):
    data, _ = central
    active = data / "dispatches" / "active"
    active.mkdir(parents=True)
    marker = active / "d-999-foo.md"
    marker.write_text("x", encoding="utf-8")
    assert state_paths._resolve_active_dispatch_file("d-999") == marker


def test_state_paths_manifest_dir_uses_dispatch_dir(central):
    data, _ = central
    resolved = state_paths._dispatch_manifest_dir("pending", "d-123")
    assert resolved == data / "dispatches" / "pending" / "d-123"


def test_state_paths_no_project_root_helper_remains():
    # The __file__-anchored _project_root helper must be gone entirely.
    assert not hasattr(state_paths, "_project_root")


# ---------------------------------------------------------------------------
# planning_cli default resolvers (Gap 1)
# ---------------------------------------------------------------------------


def test_planning_cli_repo_root_none_when_not_explicit():
    # Must return None (not a __file__-derived keystone path) so the reconciler's
    # own CWD/legacy fallback runs. Passing a non-None value is treated as explicit.
    assert planning_cli._resolve_repo_root("") is None


def test_planning_cli_repo_root_explicit_resolved(tmp_path):
    resolved = planning_cli._resolve_repo_root(str(tmp_path))
    assert resolved == tmp_path.resolve()


def test_planning_cli_state_dir_routes_through_resolver(central):
    _, state = central
    assert planning_cli._resolve_state_dir("") == state


def test_planning_cli_roadmap_routes_through_project_root(central, tmp_path):
    resolved = planning_cli._resolve_roadmap_path(None)
    assert resolved == tmp_path / "project-repo" / "ROADMAP.yaml"
    assert ".vnx-system" not in str(resolved)


# ---------------------------------------------------------------------------
# Close-evidence threads repo_root (Gap 3)
# ---------------------------------------------------------------------------


def _minimal_reconciler_db(state_dir: Path, track_id: str, project_id: str, pr_ref: str):
    """Create the minimal tables _close_evidence queries, with one track."""
    import sqlite3

    db = state_dir / track_reconciler.DB_FILENAME
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE dispatches (dispatch_id TEXT, track TEXT, project_id TEXT, state TEXT);"
        "CREATE TABLE tracks (track_id TEXT, project_id TEXT, pr_ref TEXT);"
        "CREATE TABLE coordination_events (event_type TEXT, project_id TEXT, entity_id TEXT);"
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, pr_ref) VALUES (?, ?, ?)",
        (track_id, project_id, pr_ref),
    )
    conn.commit()
    conn.close()


def test_close_evidence_uses_repo_root_roadmap(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_RECONCILE_GIT", "0")
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    state.mkdir(parents=True)
    _minimal_reconciler_db(state, "T-close", "proj-x", "#4242")

    repo = tmp_path / "project-repo"
    repo.mkdir()
    (repo / "ROADMAP.yaml").write_text(
        "features:\n  - pr_queue:\n      - pr_id: '#4242'\n        status: merged\n",
        encoding="utf-8",
    )

    # With repo_root -> reads the repo roadmap -> #4242 merged -> pr_merged True.
    ev = track_reconciler._close_evidence("%s" % state, "T-close", "proj-x", repo_root=repo)
    assert ev["pr_merged"] is True
    assert ev["has_success_signal"] is True

    # Without repo_root, and with the CWD git-root roadmap forced empty, the same
    # evidence renders pr_merged False — proving repo_root is what carried it.
    monkeypatch.setattr(track_reconciler, "_git_toplevel", lambda p: None)
    ev_none = track_reconciler._close_evidence("%s" % state, "T-close", "proj-x")
    assert ev_none["pr_merged"] is False


# ---------------------------------------------------------------------------
# Roadmap-path fix (track_reconciler._resolve_roadmap_path)
# ---------------------------------------------------------------------------


def test_roadmap_explicit_repo_root_wins(tmp_path):
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    repo = tmp_path / "repo"
    resolved = track_reconciler._resolve_roadmap_path(state, repo)
    assert resolved == repo / "ROADMAP.yaml"


def test_roadmap_central_mode_not_two_up_from_state(tmp_path, monkeypatch):
    # Central layout: state = ~/.vnx-data/<proj>/state; the legacy two-up
    # (~/.vnx-data/ROADMAP.yaml) must NOT be used when a repo_root is given.
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    state.mkdir(parents=True)
    repo = tmp_path / "project-repo"
    resolved = track_reconciler._resolve_roadmap_path(state, repo)
    assert resolved == repo / "ROADMAP.yaml"
    assert resolved != state.parent.parent / "ROADMAP.yaml"


def test_roadmap_cwd_git_root_fallback(tmp_path, monkeypatch):
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    fake_repo = tmp_path / "cwd-repo"
    monkeypatch.setattr(track_reconciler, "_git_toplevel", lambda p: fake_repo)
    resolved = track_reconciler._resolve_roadmap_path(state, None)
    assert resolved == fake_repo / "ROADMAP.yaml"


def test_roadmap_legacy_fallback_when_no_git(tmp_path, monkeypatch):
    state = tmp_path / "repo" / ".vnx-data" / "state"
    monkeypatch.setattr(track_reconciler, "_git_toplevel", lambda p: None)
    resolved = track_reconciler._resolve_roadmap_path(state, None)
    assert resolved == state.parent.parent / "ROADMAP.yaml"


def test_load_merged_pr_numbers_never_crashes_on_missing_roadmap(tmp_path, monkeypatch):
    # Source-3 is best-effort: a non-existent state dir / roadmap must not raise.
    monkeypatch.setattr(track_reconciler, "_git_toplevel", lambda p: None)
    result = track_reconciler._load_merged_pr_numbers(tmp_path / "nowhere" / "state")
    assert result == frozenset()


def test_load_merged_pr_numbers_reads_roadmap_from_repo_root(tmp_path, monkeypatch):
    state = tmp_path / ".vnx-data" / "proj-x" / "state"
    state.mkdir(parents=True)
    repo = tmp_path / "project-repo"
    repo.mkdir()
    (repo / "ROADMAP.yaml").write_text(
        "features:\n"
        "  - pr_queue:\n"
        "      - pr_id: '#4242'\n"
        "        status: merged\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VNX_RECONCILE_GIT", "0")
    merged = track_reconciler._load_merged_pr_numbers(state, repo_root=repo)
    assert 4242 in merged
