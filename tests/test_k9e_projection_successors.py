"""ADR-045 K-9e (OI-2556): a row that moved on after its projection is no conflict.

Dispatch-ID: dlv-42e2f1410f6a

The projection check used to call every state outside the from-state table a
conflict. An attempt that ended ``timed_out``, was projected, and was then
expired by the reconciler (same ``attempt_count``) is a legal move, not a
conflict — so the count reported a defect that is not one. The walk over the
legal hops stops at ``claimed``: a claim starts a new attempt.

The store is built the way ``tests/test_k9_lifecycle_count.py`` builds it (a
real ``v3`` start and end line, a real ``runtime_coordination.db``); its
fixtures and helpers are reused here by import.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_k9_lifecycle_count as k9  # noqa: E402  (bootstraps ``scripts/lib`` too)

import coordination_db  # noqa: E402
import lifecycle_count  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402

PROJECT = k9.PROJECT


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "vnx-data"


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    sd = data_dir / "state"
    sd.mkdir(parents=True)
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        k9._mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def git_repo(tmp_path: Path) -> dict:
    return k9.git_repo.__wrapped__(tmp_path)


@pytest.fixture()
def writer_sha(monkeypatch, git_repo) -> dict:
    state = {"value": git_repo["new"]}
    monkeypatch.setattr(k9.append_lifecycle, "writer_sha", lambda: state["value"])
    return state


def _conflicts(state_dir: Path, git_repo: dict, dispatch_id: str, *, end_state: str,
               row_state: str) -> int:
    """One real start and end line for one attempt, and a row already at ``row_state``."""
    k9._insert_row(state_dir, dispatch_id, state=row_state, attempt_count=1)
    k9._finish(state_dir, k9._begin(state_dir, dispatch_id), end_state=end_state)
    return k9._count(state_dir, git_repo)["counts"]["projection_conflict"]


# ---------------------------------------------------------------------------
# 1. a legal move after the projection is no conflict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("end_state,row_state", [
    ("timed_out", "expired"),
    ("timed_out", "dead_letter"),
    ("timed_out", "recovered"),
    ("failed", "expired"),
])
def test_a_row_that_moved_on_after_the_projection_is_no_conflict(
        state_dir, git_repo, writer_sha, lifecycle_on, end_state, row_state):
    """Red on origin/main: ``expired`` after a ``timed_out`` projection counted as one."""
    assert _conflicts(state_dir, git_repo, f"d-k9e-{end_state}-{row_state}",
                      end_state=end_state, row_state=row_state) == 0


# ---------------------------------------------------------------------------
# 2. every other state of the same attempt stays a conflict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("end_state,row_state", [
    ("completed", "timed_out"),
    ("timed_out", "completed"),
])
def test_a_state_the_attempt_cannot_reach_is_still_a_conflict(
        state_dir, git_repo, writer_sha, lifecycle_on, end_state, row_state):
    assert _conflicts(state_dir, git_repo, f"d-k9e-bad-{end_state}-{row_state}",
                      end_state=end_state, row_state=row_state) == 1


# ---------------------------------------------------------------------------
# 3. not projected yet stays as it is today
# ---------------------------------------------------------------------------

def test_a_row_still_before_the_projection_is_no_conflict(state_dir, git_repo, writer_sha,
                                                          lifecycle_on):
    assert _conflicts(state_dir, git_repo, "d-k9e-pending",
                      end_state="timed_out", row_state="running") == 0


# ---------------------------------------------------------------------------
# 4. the walk stops at claimed
# ---------------------------------------------------------------------------

def test_the_successor_walk_stops_at_the_next_claim():
    assert lifecycle_count._projection_successors("timed_out") == frozenset(
        {"recovered", "expired", "dead_letter", "queued"})
    assert lifecycle_count._projection_successors("completed") == frozenset()
