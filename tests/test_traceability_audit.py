"""Unit tests for scripts/traceability_audit.py gap-detection logic.

Tests use a synthetic dataset with known gaps and verify the tool detects
exactly those gaps. No subprocess calls, no filesystem reads beyond tmp_path.

Covers:
  1.  Category A: dispatches without completion receipt
  2.  Category A: all dispatches with receipts → 0 gaps
  3.  Category B: receipts without traceable dispatch
  4.  Category B: all receipts with valid dispatch → 0 gaps
  5.  Category C: PRs without receipt/dispatch linkage — internal PR-N match
  6.  Category C: PRs without receipt/dispatch linkage — GitHub numeric in dispatch_id
  7.  Category C: all PRs unlinked → 100% gap
  8.  Category D: completion receipts with no PR and no dispatch
  9.  Category D: all completion receipts linked → 0 gaps
  10. Date range filtering: iter_receipts respects since/until
  11. _is_valid_dispatch_id rejects unknown/free-form IDs
  12. render_markdown_report produces non-empty markdown with category headings
  13. gap_prs_without_receipt: branch-slug heuristic links PR to receipt
  14. gap_dispatches_without_completion_receipt: only completed state counted
  15. Dedup: identical receipts across two NDJSON files counted once
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

# Make sure scripts/lib is importable
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts" / "lib"))
sys.path.insert(0, str(_REPO / "scripts"))

import traceability_audit as ta
from traceability_audit import (
    DispatchRecord,
    GapReport,
    PRRecord,
    ReceiptRecord,
    _in_range,
    _is_valid_dispatch_id,
    gap_dispatches_without_completion_receipt,
    gap_prs_without_receipt,
    gap_receipts_without_dispatch,
    gap_receipts_without_pr_or_dispatch,
    iter_receipts,
    render_markdown_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _receipt(
    dispatch_id: str = "20260101-test-dispatch",
    event_type: str = "task_complete",
    status: str = "success",
    pr_id: str = "",
    timestamp: str = "2026-01-15T10:00:00Z",
    commit_hash: str = "",
    terminal: str = "T1",
) -> ReceiptRecord:
    return ReceiptRecord(
        dispatch_id=dispatch_id,
        pr_id=pr_id,
        event_type=event_type,
        status=status,
        timestamp=timestamp,
        commit_hash=commit_hash,
        terminal=terminal,
        source_file="test.ndjson",
        raw={},
    )


def _dispatch(
    dispatch_id: str = "20260101-test-dispatch",
    state: str = "completed",
    pr_id: str = "",
) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=dispatch_id,
        state=state,
        pr_id=pr_id,
        source_path=f"/fake/{dispatch_id}.md",
    )


def _pr(
    number: int = 100,
    title: str = "feat: some feature",
    branch: str = "feat/some-feature",
    merged_at: str = "2026-01-15T12:00:00Z",
    sha: str = "abc1234",
    internal_pr_ids: list | None = None,
    github_pr_refs: list | None = None,
) -> PRRecord:
    return PRRecord(
        number=number,
        title=title,
        branch=branch,
        merged_at=merged_at,
        sha=sha,
        internal_pr_ids=internal_pr_ids or [],
        github_pr_refs=github_pr_refs or [],
    )


# ---------------------------------------------------------------------------
# 1. Category A: dispatches without completion receipt
# ---------------------------------------------------------------------------

class TestCategoryA:
    def test_dispatch_with_no_receipt_is_gap(self):
        dispatches = [_dispatch("20260101-alpha", state="completed")]
        receipts: list[ReceiptRecord] = []
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.gap_count == 1
        assert report.traced == 0
        assert "20260101-alpha" in report.examples[0]

    def test_dispatch_with_completion_receipt_is_traced(self):
        dispatches = [_dispatch("20260101-alpha", state="completed")]
        receipts = [_receipt("20260101-alpha", event_type="task_complete")]
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.gap_count == 0
        assert report.traced == 1

    def test_subprocess_completion_counts(self):
        dispatches = [_dispatch("20260101-sub", state="completed")]
        receipts = [_receipt("20260101-sub", event_type="subprocess_completion")]
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.gap_count == 0

    def test_task_started_receipt_not_sufficient(self):
        """task_started receipt does not close the gap — needs task_complete."""
        dispatches = [_dispatch("20260101-started", state="completed")]
        receipts = [_receipt("20260101-started", event_type="task_started")]
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.gap_count == 1

    def test_only_completed_state_counted(self):
        """Failed/pending dispatches are excluded from category A."""
        dispatches = [
            _dispatch("20260101-alpha", state="completed"),
            _dispatch("20260101-beta", state="failed"),
            _dispatch("20260101-gamma", state="pending"),
        ]
        receipts = [_receipt("20260101-alpha", event_type="task_complete")]
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.total == 1  # only completed counts
        assert report.gap_count == 0

    def test_mixed_set_exact_gap_count(self):
        """3 completed dispatches, 1 has receipt → 2 gaps."""
        dispatches = [
            _dispatch(f"20260101-d{i}", state="completed") for i in range(3)
        ]
        receipts = [_receipt("20260101-d0", event_type="task_complete")]
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.total == 3
        assert report.gap_count == 2
        assert report.traced == 1


# ---------------------------------------------------------------------------
# 3. Category B: receipts without traceable dispatch
# ---------------------------------------------------------------------------

class TestCategoryB:
    def test_receipt_unknown_dispatch_id_is_gap(self):
        receipts = [_receipt("unknown", event_type="task_complete")]
        report = gap_receipts_without_dispatch(receipts, dispatch_ids=set())
        assert report.gap_count == 1

    def test_receipt_valid_dispatch_id_in_set_is_traced(self):
        receipts = [_receipt("20260101-alpha", event_type="task_complete")]
        report = gap_receipts_without_dispatch(
            receipts, dispatch_ids={"20260101-alpha"}
        )
        assert report.gap_count == 0
        assert report.traced == 1

    def test_empty_dispatch_id_is_gap(self):
        receipts = [_receipt("", event_type="task_started")]
        report = gap_receipts_without_dispatch(receipts, dispatch_ids=set())
        assert report.gap_count == 1

    def test_free_form_dispatch_id_is_gap(self):
        """Non-date-prefixed dispatch IDs are not considered valid structured IDs."""
        receipts = [
            _receipt("(none - direct instruction)", event_type="task_complete"),
            _receipt("(self-initiated)", event_type="task_complete"),
        ]
        report = gap_receipts_without_dispatch(receipts, dispatch_ids=set())
        assert report.gap_count == 2

    def test_non_task_events_excluded(self):
        """context_pressure / context_rotation receipts are not relevant to Category B."""
        receipts = [
            _receipt("unknown", event_type="context_pressure"),
            _receipt("unknown", event_type="context_rotation"),
        ]
        report = gap_receipts_without_dispatch(receipts, dispatch_ids=set())
        assert report.total == 0


# ---------------------------------------------------------------------------
# 5. Category C: PRs without receipt/dispatch linkage
# ---------------------------------------------------------------------------

class TestCategoryC:
    def test_pr_linked_via_internal_pr_id(self):
        """PR with internal PR-N in merge commit → receipt has pr_id=PR-5 → traced."""
        prs = [_pr(number=100, internal_pr_ids=["PR-5"])]
        receipts = [_receipt("20260101-d", pr_id="PR-5")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0
        assert report.traced == 1

    def test_pr_linked_via_github_number_in_dispatch_id(self):
        """PR #650 → receipt dispatch_id contains '650' → traced."""
        prs = [_pr(number=650, title="feat(x): something (#650)")]
        # Receipt whose dispatch_id contains the PR number
        receipts = [_receipt("20260101-pr650-impl", event_type="task_complete")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0

    def test_pr_linked_via_dispatch_pr_id_header(self):
        """PR with internal_pr_ids=['PR-3'] → dispatch file has pr_id=PR-3 → traced."""
        prs = [_pr(number=200, internal_pr_ids=["PR-3"])]
        receipts: list[ReceiptRecord] = []
        dispatches = [_dispatch("20260101-d", state="completed", pr_id="PR-3")]
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0

    def test_pr_linked_via_branch_slug(self):
        """PR branch 'feat/my-feature' → receipt dispatch_id contains 'my-feature' → traced."""
        prs = [_pr(number=300, branch="feat/my-feature")]
        receipts = [_receipt("20260101-my-feature-impl", event_type="task_complete")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0

    def test_unlinked_pr_is_gap(self):
        """PR with no linkage at all → gap."""
        prs = [_pr(number=999, title="chore: cleanup", branch="chore/cleanup")]
        receipts: list[ReceiptRecord] = []
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 1
        assert "#999" in report.examples[0]

    def test_all_prs_unlinked_100pct_gap(self):
        prs = [_pr(number=i) for i in range(5)]
        receipts: list[ReceiptRecord] = []
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 5
        assert report.gap_pct == 100.0

    def test_empty_pr_list_produces_zero_total(self):
        report = gap_prs_without_receipt([], [], [])
        assert report.total == 0
        assert report.gap_count == 0


# ---------------------------------------------------------------------------
# 8. Category D: completion receipts with no PR and no dispatch
# ---------------------------------------------------------------------------

class TestCategoryD:
    def test_completion_receipt_no_pr_no_dispatch_is_gap(self):
        receipts = [_receipt("unknown", event_type="task_complete", pr_id="")]
        report = gap_receipts_without_pr_or_dispatch(receipts, dispatch_ids=set())
        assert report.gap_count == 1

    def test_completion_receipt_with_pr_id_is_traced(self):
        receipts = [_receipt("unknown", event_type="task_complete", pr_id="PR-5")]
        report = gap_receipts_without_pr_or_dispatch(receipts, dispatch_ids=set())
        assert report.gap_count == 0
        assert report.traced == 1

    def test_completion_receipt_with_valid_dispatch_is_traced(self):
        receipts = [_receipt("20260101-alpha", event_type="task_complete", pr_id="")]
        report = gap_receipts_without_pr_or_dispatch(
            receipts, dispatch_ids={"20260101-alpha"}
        )
        assert report.gap_count == 0
        assert report.traced == 1

    def test_task_started_not_in_category_d(self):
        """task_started is not a completion event — excluded from Category D."""
        receipts = [_receipt("unknown", event_type="task_started", pr_id="")]
        report = gap_receipts_without_pr_or_dispatch(receipts, dispatch_ids=set())
        assert report.total == 0

    def test_subprocess_completion_included(self):
        receipts = [_receipt("unknown", event_type="subprocess_completion", pr_id="")]
        report = gap_receipts_without_pr_or_dispatch(receipts, dispatch_ids=set())
        assert report.total == 1
        assert report.gap_count == 1


# ---------------------------------------------------------------------------
# 10. Date range filtering
# ---------------------------------------------------------------------------

class TestDateFilter:
    def test_iter_receipts_since_excludes_older(self, tmp_path: Path):
        ndjson = tmp_path / "receipts.ndjson"
        records = [
            {"dispatch_id": "old", "event_type": "task_complete", "timestamp": "2025-12-01T00:00:00Z"},
            {"dispatch_id": "new", "event_type": "task_complete", "timestamp": "2026-02-01T00:00:00Z"},
        ]
        ndjson.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        since = datetime.date(2026, 1, 1)
        results = list(iter_receipts(ndjson, since=since, until=None))
        assert len(results) == 1
        assert results[0].dispatch_id == "new"

    def test_iter_receipts_until_excludes_newer(self, tmp_path: Path):
        ndjson = tmp_path / "receipts.ndjson"
        records = [
            {"dispatch_id": "early", "event_type": "task_complete", "timestamp": "2026-01-01T00:00:00Z"},
            {"dispatch_id": "late", "event_type": "task_complete", "timestamp": "2026-06-01T00:00:00Z"},
        ]
        ndjson.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        until = datetime.date(2026, 3, 1)
        results = list(iter_receipts(ndjson, since=None, until=until))
        assert len(results) == 1
        assert results[0].dispatch_id == "early"

    def test_iter_receipts_missing_file_yields_nothing(self, tmp_path: Path):
        ndjson = tmp_path / "nonexistent.ndjson"
        results = list(iter_receipts(ndjson, since=None, until=None))
        assert results == []

    def test_in_range_unknown_date_included(self):
        assert _in_range("", since=datetime.date(2026, 1, 1), until=None)

    def test_in_range_exact_boundary_included(self):
        assert _in_range("2026-01-01T00:00:00Z", since=datetime.date(2026, 1, 1), until=None)
        assert _in_range("2026-01-01T00:00:00Z", since=None, until=datetime.date(2026, 1, 1))


# ---------------------------------------------------------------------------
# 11. _is_valid_dispatch_id
# ---------------------------------------------------------------------------

class TestIsValidDispatchId:
    @pytest.mark.parametrize("did", [
        "20260101-test-dispatch",
        "20260526-gov3-traceability-audit-r2",
        "20251201-alpha-A",
    ])
    def test_valid_date_prefix_ids(self, did: str):
        assert _is_valid_dispatch_id(did)

    @pytest.mark.parametrize("did", [
        "",
        "unknown",
        "(none - direct instruction)",
        "(self-initiated)",
        "(user-requested)",
        ".claude/vnx-system/unified_reports/20260208-report.md",
    ])
    def test_invalid_ids(self, did: str):
        assert not _is_valid_dispatch_id(did)


# ---------------------------------------------------------------------------
# 12. render_markdown_report
# ---------------------------------------------------------------------------

class TestRenderMarkdownReport:
    def test_produces_valid_markdown_with_headings(self):
        gaps = [
            GapReport(
                category="A — Test",
                total=10,
                traced=8,
                gap_count=2,
                gap_pct=20.0,
                examples=["ex1", "ex2"],
            ),
        ]
        report = render_markdown_report(
            gaps=gaps,
            since=datetime.date(2026, 1, 1),
            until=datetime.date(2026, 5, 26),
            project_root=Path("/fake/repo"),
            receipt_count=100,
            dispatch_count=50,
            pr_count=30,
            run_ts="2026-05-26T12:00:00+00:00",
            schema_notes=["Note 1"],
        )
        assert "# Traceability Audit" in report
        assert "## Summary" in report
        assert "## Gap Categories" in report
        assert "### A — Test" in report
        assert "Gap examples" in report
        assert "ex1" in report
        assert "Note 1" in report
        assert "## Open Items" in report

    def test_zero_gap_shows_no_gaps_message(self):
        gaps = [
            GapReport(
                category="A — Test",
                total=5,
                traced=5,
                gap_count=0,
                gap_pct=0.0,
                examples=[],
            ),
        ]
        report = render_markdown_report(
            gaps=gaps,
            since=None, until=None,
            project_root=Path("/fake/repo"),
            receipt_count=5, dispatch_count=5, pr_count=2,
            run_ts="2026-05-26T00:00:00Z",
            schema_notes=[],
        )
        assert "No gaps detected." in report


# ---------------------------------------------------------------------------
# 13. Branch-slug heuristic edge cases
# ---------------------------------------------------------------------------

class TestBranchSlugHeuristic:
    def test_branch_slash_normalized_to_dash(self):
        """'feat/my-feature' should match dispatch_id containing 'feat-my-feature'."""
        prs = [_pr(number=400, branch="feat/my-feature", internal_pr_ids=[])]
        receipts = [_receipt("20260101-feat-my-feature-impl", event_type="task_complete")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0

    def test_short_generic_branch_does_not_match(self):
        """Branch with only tokens <4 chars ('fix/x') produces no heuristic match."""
        prs = [_pr(number=500, branch="fix/x", internal_pr_ids=[])]
        receipts = [_receipt("20260101-complex-feature-implementation", event_type="task_complete")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        # Both 'fix' (3) and 'x' (1) are <4 chars → no tokens qualify → gap
        assert report.gap_count == 1


# ---------------------------------------------------------------------------
# 14. Only completed dispatches counted in Category A
# ---------------------------------------------------------------------------

class TestCategoryAStateFilter:
    def test_rejected_dispatch_excluded(self):
        dispatches = [_dispatch("20260101-rej", state="rejected")]
        receipts: list[ReceiptRecord] = []
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.total == 0

    def test_active_dispatch_excluded(self):
        dispatches = [_dispatch("20260101-act", state="active")]
        receipts: list[ReceiptRecord] = []
        report = gap_dispatches_without_completion_receipt(dispatches, receipts)
        assert report.total == 0


# ---------------------------------------------------------------------------
# 15. Dedup in iter_receipts via load_all_receipts (indirect test via iter_receipts)
# ---------------------------------------------------------------------------

class TestIterReceiptsDedup:
    def test_malformed_json_skipped(self, tmp_path: Path):
        ndjson = tmp_path / "bad.ndjson"
        ndjson.write_text(
            '{"dispatch_id":"ok","event_type":"task_complete","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{not json}\n'
            '{"dispatch_id":"ok2","event_type":"task_complete","timestamp":"2026-01-01T00:00:00Z"}\n'
        )
        results = list(iter_receipts(ndjson, since=None, until=None))
        assert len(results) == 2
        assert {r.dispatch_id for r in results} == {"ok", "ok2"}


# ---------------------------------------------------------------------------
# 16. _run_git / _run_gh: exception paths return None, never raise
# ---------------------------------------------------------------------------

class TestRunHelperExceptionPaths:
    """Regression tests for the silent-except fix in _run_git/_run_gh.

    Both helpers are fallback-safe wrappers: when subprocess raises (e.g.
    git/gh not installed, CWD not found), they must return None rather than
    propagating the exception to the caller, which handles None by trying
    alternative approaches. Behavior must be identical before/after the lint fix.
    """

    def test_run_git_returns_none_on_file_not_found(self, tmp_path):
        """_run_git returns None when git binary does not exist."""
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=FileNotFoundError("git: not found"),
        ):
            result = ta._run_git(["log", "--oneline", "-1"], str(tmp_path))
        assert result is None

    def test_run_git_returns_none_on_timeout(self, tmp_path):
        """_run_git returns None when subprocess.TimeoutExpired is raised."""
        import subprocess
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=30),
        ):
            result = ta._run_git(["log"], str(tmp_path))
        assert result is None

    def test_run_gh_returns_none_on_file_not_found(self, tmp_path):
        """_run_gh returns None when gh binary does not exist."""
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=FileNotFoundError("gh: not found"),
        ):
            result = ta._run_gh(["pr", "list"], str(tmp_path))
        assert result is None

    def test_run_gh_returns_none_on_timeout(self, tmp_path):
        """_run_gh returns None when subprocess.TimeoutExpired is raised."""
        import subprocess
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=60),
        ):
            result = ta._run_gh(["pr", "list"], str(tmp_path))
        assert result is None

    def test_run_git_logs_to_stderr_on_exception(self, tmp_path, capsys):
        """_run_git logs the failure to stderr instead of swallowing it silently."""
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            ta._run_git(["log"], str(tmp_path))
        captured = capsys.readouterr()
        assert "traceability-audit" in captured.err
        assert "permission denied" in captured.err

    def test_run_gh_logs_to_stderr_on_exception(self, tmp_path, capsys):
        """_run_gh logs the failure to stderr instead of swallowing it silently."""
        import unittest.mock as mock
        with mock.patch(
            "traceability_audit.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            ta._run_gh(["pr", "list"], str(tmp_path))
        captured = capsys.readouterr()
        assert "traceability-audit" in captured.err
        assert "permission denied" in captured.err


# ---------------------------------------------------------------------------
# 17. Strategy 2b: event_type=pr_merged strictness
# ---------------------------------------------------------------------------

class TestCategoryCStrategy2b:
    """Strategy 2b must only close the gap for receipts with event_type='pr_merged'."""

    def _pr_receipt(self, pr_number: int, event_type: str) -> ReceiptRecord:
        raw = {"pr_number": pr_number, "event_type": event_type}
        return ReceiptRecord(
            dispatch_id="20260101-test",
            pr_id="",
            event_type=event_type,
            status="success",
            timestamp="2026-01-15T10:00:00Z",
            commit_hash="",
            terminal="T1",
            source_file="test.ndjson",
            raw=raw,
        )

    def test_pr_number_with_task_complete_does_not_close_gap(self):
        """Receipt with pr_number but event_type='task_complete' must NOT trace the PR."""
        prs = [_pr(number=700, internal_pr_ids=[], branch="feat/orphan")]
        receipts = [self._pr_receipt(pr_number=700, event_type="task_complete")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 1, "task_complete receipt with pr_number must not close cat-C gap"

    def test_pr_number_with_pr_merged_closes_gap(self):
        """Receipt with pr_number AND event_type='pr_merged' SHOULD trace the PR."""
        prs = [_pr(number=701, internal_pr_ids=[], branch="feat/orphan2")]
        receipts = [self._pr_receipt(pr_number=701, event_type="pr_merged")]
        dispatches: list[DispatchRecord] = []
        report = gap_prs_without_receipt(prs, receipts, dispatches)
        assert report.gap_count == 0, "pr_merged receipt with matching pr_number must close cat-C gap"
        assert report.traced == 1
