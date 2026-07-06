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

_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_PROJECT_ID",
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
        lambda: {"VNX_DATA_DIR": str(data), "VNX_STATE_DIR": str(state)},
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
