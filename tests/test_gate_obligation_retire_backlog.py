"""tests/test_gate_obligation_retire_backlog.py — OI-1388: honest one-time
booking of the pending-forever gate-obligation backlog.

Every test here drives ``scripts/gate_obligation_retire_backlog.py`` against
a throwaway store under ``tmp_path`` with hand-built PR/branch fixtures — the
real ``gh`` CLI and the real store are never touched (the dispatch's own
warning: never fire the obligation runner/backlog script against the live
store from a test).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_retire_backlog as backlog  # noqa: E402
from gate_obligations import (  # noqa: E402
    REASON_NO_PR_BRANCH_GONE,
    REASON_PR_CLOSED,
    REASON_PR_MERGED,
    STATUS_FULFILLED,
    STATUS_PENDING,
    STATUS_RETIRED,
    STATUS_UNRESOLVABLE,
    obligation_path,
    register_obligation,
    update_obligation,
)


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    return state_dir


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 0. IO wrapper negative paths — a gh/git failure must degrade to an empty
#    result, never raise and never silently fabricate data.
# ---------------------------------------------------------------------------


def test_fetch_prs_returns_empty_list_on_gh_failure(monkeypatch):
    monkeypatch.setattr(backlog, "_gh_json", lambda *a, **k: None)
    assert backlog.fetch_prs("Vinix24/vnx-orchestration", "merged", "number,headRefName") == []


def test_fetch_prs_returns_empty_list_on_malformed_gh_output(monkeypatch):
    """gh returning a JSON object instead of the expected array must not
    crash the caller — an unexpected shape degrades to empty, same as a
    hard failure."""
    monkeypatch.setattr(backlog, "_gh_json", lambda *a, **k: {"unexpected": "shape"})
    assert backlog.fetch_prs("Vinix24/vnx-orchestration", "merged", "number,headRefName") == []


def test_fetch_existing_branches_returns_empty_set_on_git_failure(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=128, stdout="", stderr="fatal: not a repo")

    monkeypatch.setattr(backlog.subprocess, "run", fake_run)
    assert backlog.fetch_existing_branches(tmp_path) == set()


def test_fetch_existing_branches_parses_ls_remote_output(monkeypatch, tmp_path):
    ls_remote_output = (
        "abc123\trefs/heads/dispatch/20260801-foo\n"
        "def456\trefs/heads/main\n"
        "ghi789\trefs/heads/dispatch/20260802-bar\n"
    )

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=ls_remote_output, stderr="")

    monkeypatch.setattr(backlog.subprocess, "run", fake_run)
    branches = backlog.fetch_existing_branches(tmp_path)
    assert branches == {"dispatch/20260801-foo", "main", "dispatch/20260802-bar"}


# ---------------------------------------------------------------------------
# 1. build_pr_index — the gh "closed includes merged" gotcha (measured
#    2026-08-23: 1650 closed-state PRs, 1537 of them with mergedAt set).
# ---------------------------------------------------------------------------


def test_build_pr_index_separates_merged_from_closed_without_merge():
    merged_raw = [{"number": 100, "headRefName": "dispatch/a", "mergedAt": "2026-08-01T00:00:00Z"}]
    # gh's "closed" state query returns EVERY non-open PR, merged included.
    closed_raw = [
        {"number": 100, "headRefName": "dispatch/a", "mergedAt": "2026-08-01T00:00:00Z", "closedAt": "2026-08-01T00:00:00Z"},
        {"number": 101, "headRefName": "dispatch/b", "mergedAt": None, "closedAt": "2026-08-02T00:00:00Z"},
    ]
    open_raw = [{"number": 102, "headRefName": "dispatch/c"}]

    merged_by_branch, closed_by_branch, open_branches = backlog.build_pr_index(
        merged_raw, closed_raw, open_raw,
    )

    assert set(merged_by_branch) == {"dispatch/a"}
    assert set(closed_by_branch) == {"dispatch/b"}, (
        "a merged PR must never leak into the closed-without-merge bucket, "
        "even though gh's closed-state query includes it"
    )
    assert open_branches == {"dispatch/c"}


# ---------------------------------------------------------------------------
# 2. classify_obligation — pure, no IO.
# ---------------------------------------------------------------------------


def test_classify_pr_merged_returns_retired_with_number_and_date():
    outcome = backlog.classify_obligation(
        "d-merged",
        merged_by_branch={"dispatch/d-merged": {"number": 1234, "mergedAt": "2026-08-01T12:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
    )
    assert outcome is not None
    status, reason, reason_detail = outcome
    assert status == STATUS_RETIRED
    assert reason == REASON_PR_MERGED
    assert reason_detail
    assert "1234" in reason_detail
    assert "2026-08-01" in reason_detail


def test_classify_pr_closed_without_merge_returns_retired():
    outcome = backlog.classify_obligation(
        "d-closed",
        merged_by_branch={},
        closed_by_branch={"dispatch/d-closed": {"number": 5678, "closedAt": "2026-08-05T09:00:00Z"}},
        open_branches=set(),
        existing_branches=set(),
    )
    assert outcome is not None
    status, reason, reason_detail = outcome
    assert status == STATUS_RETIRED
    assert reason == REASON_PR_CLOSED
    assert "5678" in reason_detail
    assert "2026-08-05" in reason_detail
    assert "without merge" in reason_detail


def test_classify_no_pr_branch_gone_returns_retired():
    outcome = backlog.classify_obligation(
        "d-dead",
        merged_by_branch={},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),  # branch not present anywhere
    )
    assert outcome is not None
    status, reason, reason_detail = outcome
    assert status == STATUS_RETIRED
    assert reason == REASON_NO_PR_BRANCH_GONE
    assert "d-dead" in reason_detail


def test_classify_open_pr_returns_none_even_if_also_in_other_buckets():
    """An open PR always wins — the obligation can still be gated."""
    outcome = backlog.classify_obligation(
        "d-open",
        merged_by_branch={},
        closed_by_branch={},
        open_branches={"dispatch/d-open"},
        existing_branches={"dispatch/d-open"},
    )
    assert outcome is None


def test_classify_no_pr_branch_exists_returns_none():
    """Class 3: no PR at all, branch still on origin — left for a human,
    never auto-retired (cannot tell 'still running' from 'dead, uncleaned'
    without an age threshold, which OI-1388 forbids)."""
    outcome = backlog.classify_obligation(
        "d-maybe-running",
        merged_by_branch={},
        closed_by_branch={},
        open_branches=set(),
        existing_branches={"dispatch/d-maybe-running"},
    )
    assert outcome is None


# ---------------------------------------------------------------------------
# 3. run_backlog_retirement — integration against a real tmp_path store.
#    RED test 1 (dispatch requirement): a PR-merged obligation is pending on
#    unfixed main; after this script it must carry the new end-state with a
#    non-empty reason naming the PR number.
# ---------------------------------------------------------------------------


def test_run_backlog_write_retires_pr_merged_obligation(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260801-merged-example", gate="codex_gate", project_id="vnx-dev",
    )

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={"dispatch/20260801-merged-example": {"number": 1401, "mergedAt": "2026-08-10T10:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=True,
    )

    record = _read_obligation(state_dir, "20260801-merged-example")
    # State, reason-presence, and reason-content asserted separately — a
    # composite check would silently pass on the wrong field.
    assert record["status"] == STATUS_RETIRED
    assert record["status"] != STATUS_FULFILLED
    assert record["reason"], "the reason field is required, never empty"
    assert record["reason"] == REASON_PR_MERGED
    assert record["reason_detail"], "reason_detail is required, never empty"
    assert "1401" in record["reason_detail"]
    assert "2026-08-10" in record["reason_detail"]
    assert summary["changed_count"] == 1
    assert summary["retired_by_reason"][REASON_PR_MERGED] == 1


def test_run_backlog_never_backdates_resolved_at(tmp_path):
    """OI-1259 hard boundary: resolved_at is stamped at booking time, never
    backdated to the historical PR merge date."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260801-no-backdate", gate="codex_gate", project_id="vnx-dev",
    )
    backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={"dispatch/20260801-no-backdate": {"number": 1, "mergedAt": "2020-01-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=True,
    )
    record = _read_obligation(state_dir, "20260801-no-backdate")
    assert record["resolved_at"] != "2020-01-01T00:00:00Z"
    assert not str(record["resolved_at"]).startswith("2020")


def test_run_backlog_dry_run_writes_nothing(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260801-dry-run", gate="codex_gate", project_id="vnx-dev",
    )

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={"dispatch/20260801-dry-run": {"number": 1, "mergedAt": "2026-08-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=False,
    )

    record = _read_obligation(state_dir, "20260801-dry-run")
    assert record["status"] == STATUS_PENDING, "a dry run must never write to disk"
    assert summary["changed_count"] == 1, "dry run still reports what WOULD change"
    assert summary["after_by_status"].get(STATUS_RETIRED) == 1


# ---------------------------------------------------------------------------
# 4. Control cases the dispatch requires to keep passing.
# ---------------------------------------------------------------------------


def test_control_open_pr_obligation_is_never_touched(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260801-open-pr", gate="codex_gate", project_id="vnx-dev",
    )

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={},
        closed_by_branch={},
        open_branches={"dispatch/20260801-open-pr"},
        existing_branches={"dispatch/20260801-open-pr"},
        write=True,
    )

    record = _read_obligation(state_dir, "20260801-open-pr")
    assert record["status"] == STATUS_PENDING
    assert summary["changed_count"] == 0


def test_control_fulfilled_obligation_is_never_rebooked(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260801-already-fulfilled", gate="codex_gate", project_id="vnx-dev",
    )
    update_obligation(path, status=STATUS_FULFILLED, resolved_at="2026-08-01T00:00:00Z")

    summary = backlog.run_backlog_retirement(
        state_dir,
        # Even if the bulk PR fetch WOULD classify this as merged, an
        # already-terminal obligation must never be touched.
        merged_by_branch={"dispatch/20260801-already-fulfilled": {"number": 1, "mergedAt": "2026-08-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=True,
    )

    record = _read_obligation(state_dir, "20260801-already-fulfilled")
    assert record["status"] == STATUS_FULFILLED
    assert record["resolved_at"] == "2026-08-01T00:00:00Z"
    assert summary["changed_count"] == 0


def test_control_retired_status_does_not_count_as_reviewed(tmp_path):
    """The new end-state must never inflate a 'how much was actually
    reviewed' count."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(state_dir, dispatch_id="d-1", gate="codex_gate", project_id="vnx-dev")
    fulfilled_path = register_obligation(state_dir, dispatch_id="d-2", gate="codex_gate", project_id="vnx-dev")
    update_obligation(fulfilled_path, status=STATUS_FULFILLED, resolved_at="2026-08-01T00:00:00Z")

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={"dispatch/d-1": {"number": 1, "mergedAt": "2026-08-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=True,
    )

    reviewed = summary["after_by_status"].get(STATUS_FULFILLED, 0)
    assert reviewed == 1, "only the genuinely fulfilled obligation counts as reviewed"
    assert summary["after_by_status"].get(STATUS_RETIRED, 0) == 1


def test_control_running_dispatch_stays_pending(tmp_path):
    """A dispatch that is still running (no PR yet, branch still exists) must
    stay pending — never auto-retired."""
    state_dir = _make_state_dir(tmp_path)
    register_obligation(
        state_dir, dispatch_id="20260823-still-running", gate="codex_gate", project_id="vnx-dev",
    )

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={},
        closed_by_branch={},
        open_branches=set(),
        existing_branches={"dispatch/20260823-still-running"},
        write=True,
    )

    record = _read_obligation(state_dir, "20260823-still-running")
    assert record["status"] == STATUS_PENDING
    assert summary["changed_count"] == 0
    assert "20260823-still-running" in summary["class3_branch_exists_no_pr_dispatch_ids"]
    assert summary["class3_branch_exists_no_pr_count"] == 1


def test_control_unresolvable_obligation_is_still_in_scope(tmp_path):
    """`unresolvable` is not in TERMINAL_STATUSES — the backlog script must
    still classify it once the environment resolves (this script provides a
    real owner_repo by construction, so it's the right place to close these
    out too)."""
    state_dir = _make_state_dir(tmp_path)
    path = register_obligation(
        state_dir, dispatch_id="20260801-was-unresolvable", gate="codex_gate", project_id="vnx-dev",
    )
    update_obligation(path, status=STATUS_UNRESOLVABLE, reason="unresolvable_repo", reason_detail="env was wrong")

    summary = backlog.run_backlog_retirement(
        state_dir,
        merged_by_branch={"dispatch/20260801-was-unresolvable": {"number": 42, "mergedAt": "2026-08-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches=set(),
        write=True,
    )

    record = _read_obligation(state_dir, "20260801-was-unresolvable")
    assert record["status"] == STATUS_RETIRED
    assert summary["changed_count"] == 1


# ---------------------------------------------------------------------------
# 5. Idempotency: running twice must change nothing the second time.
# ---------------------------------------------------------------------------


def test_run_backlog_is_idempotent(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    register_obligation(state_dir, dispatch_id="d-merged", gate="codex_gate", project_id="vnx-dev")
    register_obligation(state_dir, dispatch_id="d-dead", gate="codex_gate", project_id="vnx-dev")
    register_obligation(state_dir, dispatch_id="d-still-running", gate="codex_gate", project_id="vnx-dev")

    kwargs = dict(
        merged_by_branch={"dispatch/d-merged": {"number": 1, "mergedAt": "2026-08-01T00:00:00Z"}},
        closed_by_branch={},
        open_branches=set(),
        existing_branches={"dispatch/d-still-running"},
        write=True,
    )

    first = backlog.run_backlog_retirement(state_dir, **kwargs)
    assert first["changed_count"] == 2  # d-merged retired, d-dead retired; d-still-running left alone

    second = backlog.run_backlog_retirement(state_dir, **kwargs)
    assert second["changed_count"] == 0, "a second run must change nothing"

    merged_record = _read_obligation(state_dir, "d-merged")
    dead_record = _read_obligation(state_dir, "d-dead")
    running_record = _read_obligation(state_dir, "d-still-running")
    assert merged_record["status"] == STATUS_RETIRED
    assert dead_record["status"] == STATUS_RETIRED
    assert running_record["status"] == STATUS_PENDING
