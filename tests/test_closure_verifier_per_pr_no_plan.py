#!/usr/bin/env python3
"""Per-PR closure reads the dispatch state, never a plan (ADR-047 §2, A5).

The project root in every test has no ``FEATURE_PLAN.md``, ``PR_QUEUE.md`` or
``ROADMAP.md``. A PR exists when a dispatch names it (``pr_has_dispatch``) and
is completed only when that dispatch sits in ``completed/`` AND has a terminal
receipt. A plan file or the ``pr_queue_state.json`` projection that names a PR
changes neither answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier as cv
import queue_reconciler

PLAN_FILES = ("FEATURE_PLAN.md", "PR_QUEUE.md", "ROADMAP.md")
TERMINAL = {"dispatch_id": "d7", "event_type": "task_complete", "status": "success"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    data = root / ".vnx-data"
    dispatch_dir, state_dir = data / "dispatches", data / "state"
    for d in (dispatch_dir, state_dir):
        d.mkdir(parents=True)
    values = {
        "VNX_HOME": str(VNX_ROOT), "PROJECT_ROOT": str(root), "VNX_DATA_DIR": str(data),
        "VNX_STATE_DIR": str(state_dir), "VNX_DISPATCH_DIR": str(dispatch_dir),
        "VNX_LOGS_DIR": str(data / "logs"), "VNX_PIDS_DIR": str(data / "pids"),
        "VNX_LOCKS_DIR": str(data / "locks"), "VNX_REPORTS_DIR": str(data / "unified_reports"),
        "VNX_DB_DIR": str(data / "database"), "VNX_DATA_DIR_GUARD": "off",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert not any((root / name).exists() for name in PLAN_FILES)
    return {"root": root, "dispatch_dir": dispatch_dir, "state_dir": state_dir,
            "receipts": state_dir / "t0_receipts.ndjson", "env": values}


def _dispatch(env, pr_id="PR-7", dispatch_id="d7", state="completed"):
    path = env["dispatch_dir"] / state / f"{dispatch_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[[TARGET:B]]\nManager Block\n\nPR-ID: {pr_id}\nDispatch-ID: {dispatch_id}\n")


def _receipts(env, *lines):
    env["receipts"].write_text("".join(line + "\n" for line in lines))


def _verify(env, pr_id="PR-7"):
    result = cv.verify_pr_closure(
        pr_id=pr_id, project_root=env["root"], dispatch_dir=env["dispatch_dir"],
        receipts_file=env["receipts"], state_dir=env["state_dir"],
    )
    return result, {c["name"]: c["status"] for c in result["checks"]}


def test_dispatch_with_terminal_receipt_passes_without_any_plan(env):
    _dispatch(env)
    _receipts(env, json.dumps(TERMINAL))
    _, checks = _verify(env)
    assert checks["pr_has_dispatch"] == "PASS"
    assert checks["pr_completed"] == "PASS"
    assert "pr_exists_in_plan" not in checks


def test_pr_without_a_dispatch_fails_pr_has_dispatch(env):
    _dispatch(env)
    _receipts(env, json.dumps(TERMINAL))
    result, checks = _verify(env, pr_id="PR-404")
    assert checks == {"pr_has_dispatch": "FAIL"}
    assert result["verdict"] == "fail"
    assert result["reconciled_state"] is None


@pytest.mark.parametrize("receipts", [
    pytest.param(None, id="no-receipt-file"),
    pytest.param([], id="empty-ledger"),
    pytest.param(["{not json"], id="malformed-line"),
    pytest.param([json.dumps({**TERMINAL, "dispatch_id": "other"})], id="other-dispatch"),
    pytest.param([json.dumps({**TERMINAL, "dispatch_id": None})], id="dispatch-id-none"),
    pytest.param([json.dumps({"dispatch_id": "d7", "event_type": "task_started", "status": "running"})],
                 id="non-terminal-event"),
    pytest.param([json.dumps({**TERMINAL, "source": "pytest"})], id="pytest-fixture-receipt"),
])
def test_dispatch_without_terminal_receipt_is_not_completed(env, receipts):
    _dispatch(env)
    if receipts is not None:
        _receipts(env, *receipts)
    result, checks = _verify(env)
    assert checks["pr_has_dispatch"] == "PASS"
    assert checks["pr_completed"] == "FAIL"
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("provenance", [
    pytest.param({}, id="missing"),
    pytest.param({"receipt_confirmed": None}, id="none"),
    pytest.param({"receipt_confirmed": "true"}, id="malformed-string"),
    pytest.param({"receipt_confirmed": 1}, id="unknown-value"),
])
def test_receipt_confirmation_is_only_an_exact_true(env, monkeypatch, provenance):
    """``pr_completed`` passes on ``receipt_confirmed is True`` and nothing else."""
    pr = queue_reconciler.PRReconciled(
        pr_id="PR-7", state="completed", metadata={},
        provenance={"source": "dispatch_filesystem", "dispatch_id": "d7", **provenance},
    )
    fixed = queue_reconciler.ReconcileResult(
        prs=[pr], drift_warnings=[], reconciled_at="2026-10-25T00:00:00+00:00", has_blocking_drift=False,
    )
    monkeypatch.setattr(queue_reconciler.QueueReconciler, "reconcile", lambda self: fixed)
    _, checks = _verify(env)
    assert checks["pr_completed"] == "FAIL"


def test_plan_files_and_projection_naming_a_pr_change_nothing(env):
    """A plan that lists PR-9 does not create it; a projection without PR-7 does not remove it."""
    root = env["root"]
    (root / "FEATURE_PLAN.md").write_text("# Feature\n\n## PR-9: only in the plan\n\n### F1-PR9: same\n")
    (root / "PR_QUEUE.md").write_text("# PR Queue\n\n- PR-9 (completed)\n")
    (env["state_dir"] / "pr_queue_state.json").write_text(json.dumps({
        "prs": [{"id": "PR-9", "status": "completed"}], "completed": ["PR-9"], "active": [], "blocked": [],
    }))
    _dispatch(env)
    _receipts(env, json.dumps(TERMINAL))

    result, checks = _verify(env, pr_id="PR-9")
    assert checks == {"pr_has_dispatch": "FAIL"}
    assert result["verdict"] == "fail"

    _, checks = _verify(env, pr_id="PR-7")
    assert checks["pr_has_dispatch"] == "PASS"
    assert checks["pr_completed"] == "PASS"


def test_cli_per_pr_mode_on_a_real_dispatch_with_a_terminal_receipt(env):
    """The ``--pr-id`` command line itself, as an operator runs it."""
    _dispatch(env)
    _receipts(env, json.dumps(TERMINAL))
    proc = subprocess.run(
        [sys.executable, str(VNX_ROOT / "scripts" / "closure_verifier.py"),
         "--pr-id", "PR-7", "--mode", "post_merge", "--branch", "dispatch/d7", "--json"],
        cwd=env["root"], env={**os.environ, **env["env"]}, capture_output=True, text=True, timeout=60,
    )
    result = json.loads(proc.stdout)
    checks = {c["name"]: c["status"] for c in result["checks"]}
    assert checks["pr_has_dispatch"] == "PASS"
    assert checks["pr_completed"] == "PASS"
    # No review contract was supplied, so the verdict stays fail and the exit code says so.
    assert checks["review_contract"] == "FAIL"
    assert proc.returncode == 1
