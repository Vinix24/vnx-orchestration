#!/usr/bin/env python3
"""Tests for backfill_pr_merged_receipts.py — dual-scheme + dispatch_id lookup + idempotency.

Covers:
- Backfill emits receipts to events/pr_merged.ndjson (ADR-005 ledger)
- Receipt carries pr_id when title has (PR-X) prefix
- Receipt carries pr_id_resolution='unmatched' when no (PR-X) found
- dispatch_id is looked up from dispatch_register events by pr_number
- dispatch_id is looked up by branch-slug when pr_number has no match
- Backfill is idempotent: re-run emits 0 duplicates
- _extract_pr_id_from_subject and _lookup_dispatch_id_for_pr unit tests
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def vnx_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    events_dir = data_dir / "events"
    state_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    (data_dir / "dispatches").mkdir(parents=True, exist_ok=True)
    return {
        "state_dir": state_dir,
        "data_dir": data_dir,
        "events_dir": events_dir,
        "receipts_path": state_dir / "t0_receipts.ndjson",
        "events_pr_merged": events_dir / "pr_merged.ndjson",
    }


def _load_ndjson(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _make_merged_pr(
    number: int,
    title: str,
    branch: str = "feature-branch",
    merged_at: str = "2026-05-20T10:00:00Z",
) -> Dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "mergedAt": merged_at,
        "baseRefName": "main",
        "mergeCommit": {"oid": "abc123", "committedDate": merged_at},
    }


# ---------------------------------------------------------------------------
# Unit: _extract_pr_id_from_subject
# ---------------------------------------------------------------------------

class TestExtractPrIdFromSubject:
    def test_alphanumeric_label(self):
        import backfill_pr_merged_receipts as bfr
        assert bfr._extract_pr_id_from_subject("feat(hyg): remove dead code (PR-HYG-2B)") == "PR-HYG-2B"

    def test_pure_numeric_label(self):
        import backfill_pr_merged_receipts as bfr
        assert bfr._extract_pr_id_from_subject("fix: something (PR-42)") == "PR-42"

    def test_no_label_returns_none(self):
        import backfill_pr_merged_receipts as bfr
        assert bfr._extract_pr_id_from_subject("docs: update readme") is None

    def test_tmux_label(self):
        import backfill_pr_merged_receipts as bfr
        assert bfr._extract_pr_id_from_subject("feat(tmux): worktree isolation (PR-TMUX-3)") == "PR-TMUX-3"


# ---------------------------------------------------------------------------
# Unit: _lookup_dispatch_id_for_pr
# ---------------------------------------------------------------------------

class TestLookupDispatchIdForPr:
    def _make_register_event(self, pr_number: int, dispatch_id: str) -> Dict[str, Any]:
        return {
            "event": "dispatch_completed",
            "dispatch_id": dispatch_id,
            "pr_number": pr_number,
            "timestamp": "2026-05-20T10:00:00Z",
        }

    def test_pr_number_exact_match(self):
        import backfill_pr_merged_receipts as bfr
        events = [
            self._make_register_event(675, "20260528-route-1-enforce"),
            self._make_register_event(668, "20260528-hyg-2b-deadmodules"),
        ]
        result = bfr._lookup_dispatch_id_for_pr(675, "feat/route-1-enforce", events)
        assert result == "20260528-route-1-enforce"

    def test_different_pr_number_no_match(self):
        import backfill_pr_merged_receipts as bfr
        events = [self._make_register_event(100, "20260520-some-dispatch")]
        result = bfr._lookup_dispatch_id_for_pr(999, "feat/unknown-branch", events)
        assert result == ""

    def test_branch_slug_fallback(self):
        import backfill_pr_merged_receipts as bfr
        events = [
            {
                "event": "dispatch_created",
                "dispatch_id": "20260520-trace-gap-fix",
                "timestamp": "2026-05-20T10:00:00Z",
            }
        ]
        result = bfr._lookup_dispatch_id_for_pr(677, "feat/trace-gap-fix", events)
        assert result == "20260520-trace-gap-fix"

    def test_empty_events_returns_empty(self):
        import backfill_pr_merged_receipts as bfr
        result = bfr._lookup_dispatch_id_for_pr(500, "feat/some-feature", [])
        assert result == ""

    def test_pr_number_wins_over_branch_slug(self):
        import backfill_pr_merged_receipts as bfr
        events = [
            {
                "event": "dispatch_created",
                "dispatch_id": "20260520-branch-match-dispatch",
                "timestamp": "2026-05-20T09:00:00Z",
            },
            self._make_register_event(675, "20260520-exact-pr-dispatch"),
        ]
        result = bfr._lookup_dispatch_id_for_pr(675, "feat/branch-match", events)
        assert result == "20260520-exact-pr-dispatch"


# ---------------------------------------------------------------------------
# Integration: backfill() with fixture data
# ---------------------------------------------------------------------------

class TestBackfillIntegration:
    """Backfill emits receipts to events/pr_merged.ndjson with dual-scheme fields."""

    def test_emits_receipts_to_events_file(self, vnx_env, monkeypatch):
        """Backfill writes receipts to events/pr_merged.ndjson, not t0_receipts.ndjson."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            _make_merged_pr(675, "feat(route): enforce constraints (PR-ROUTE-1)", "feat/route-1-enforce"),
            _make_merged_pr(674, "docs(readme): update docs", "feat/doc-readme"),
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(
            receipts_file=str(vnx_env["receipts_path"]),
            events_path=events_path,
        )

        assert summary["missing_receipt_count"] == 2
        assert summary["backfilled"] == 2
        assert summary["errors"] == 0

        receipts = _load_ndjson(events_path)
        assert len(receipts) == 2

    def test_pr_id_extracted_from_title(self, vnx_env, monkeypatch):
        """Receipt has pr_id='PR-ROUTE-1' when title contains (PR-ROUTE-1)."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            _make_merged_pr(675, "feat(route): enforce constraints (PR-ROUTE-1)", "feat/route-1-enforce"),
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        bfr.backfill(receipts_file=str(vnx_env["receipts_path"]), events_path=events_path)

        receipts = _load_ndjson(events_path)
        r675 = next((r for r in receipts if r.get("pr_number") == 675), None)
        assert r675 is not None
        assert r675.get("pr_id") == "PR-ROUTE-1"
        assert "pr_id_resolution" not in r675

    def test_pr_id_resolution_unmatched_when_no_label(self, vnx_env, monkeypatch):
        """Receipt has pr_id_resolution='unmatched' when title has no PR-X label."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            _make_merged_pr(674, "docs(readme): update docs without label", "feat/doc-readme"),
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        bfr.backfill(receipts_file=str(vnx_env["receipts_path"]), events_path=events_path)

        receipts = _load_ndjson(events_path)
        r674 = next((r for r in receipts if r.get("pr_number") == 674), None)
        assert r674 is not None
        assert r674.get("pr_id_resolution") == "unmatched"
        assert not r674.get("pr_id")

    def test_dispatch_id_from_register(self, vnx_env, monkeypatch):
        """dispatch_id is resolved from dispatch_register when pr_number matches."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            _make_merged_pr(675, "feat(route): enforce constraints (PR-ROUTE-1)", "feat/route-1-enforce"),
        ]
        register_events = [
            {
                "event": "dispatch_completed",
                "dispatch_id": "20260528-route-1-enforce",
                "pr_number": 675,
                "timestamp": "2026-05-28T12:00:00Z",
            }
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: register_events)

        events_path = vnx_env["events_pr_merged"]
        bfr.backfill(receipts_file=str(vnx_env["receipts_path"]), events_path=events_path)

        receipts = _load_ndjson(events_path)
        r675 = next((r for r in receipts if r.get("pr_number") == 675), None)
        assert r675 is not None
        assert r675.get("dispatch_id") == "20260528-route-1-enforce"

    def test_idempotent_no_duplicates_on_rerun(self, vnx_env, monkeypatch):
        """Running backfill twice emits each receipt exactly once."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            _make_merged_pr(675, "feat(route): enforce constraints (PR-ROUTE-1)", "feat/route-1-enforce"),
            _make_merged_pr(674, "docs: update readme", "feat/doc-readme"),
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        receipts_path = vnx_env["receipts_path"]

        # First run
        s1 = bfr.backfill(receipts_file=str(receipts_path), events_path=events_path)
        assert s1["backfilled"] == 2

        # Second run — nothing new should be emitted
        s2 = bfr.backfill(receipts_file=str(receipts_path), events_path=events_path)
        assert s2["backfilled"] == 0
        assert s2["missing_receipt_count"] == 0

        # events/pr_merged.ndjson has exactly 2 receipts (no duplicates)
        all_receipts = _load_ndjson(events_path)
        pr_numbers = [r.get("pr_number") for r in all_receipts]
        assert sorted(pr_numbers) == [674, 675]

    def test_dry_run_writes_nothing(self, vnx_env, monkeypatch):
        """--dry-run reports would_backfill but writes nothing."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [_make_merged_pr(675, "feat(route): constraints (PR-ROUTE-1)")]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(
            dry_run=True,
            receipts_file=str(vnx_env["receipts_path"]),
            events_path=events_path,
        )

        assert summary["dry_run"] is True
        assert summary["missing_receipt_count"] == 1
        assert summary["backfilled"] == 0
        assert not events_path.exists() or _load_ndjson(events_path) == []

    def test_receipt_pr_number_field_always_present(self, vnx_env, monkeypatch):
        """pr_number is always in every backfilled receipt."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [_make_merged_pr(700, "chore: no label")]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        bfr.backfill(receipts_file=str(vnx_env["receipts_path"]), events_path=events_path)

        receipts = _load_ndjson(events_path)
        assert len(receipts) == 1
        assert receipts[0]["pr_number"] == 700
        assert receipts[0]["event_type"] == "pr_merged"
        assert receipts[0]["backfilled"] is True
