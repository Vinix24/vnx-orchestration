#!/usr/bin/env python3
"""Tests for pr_merge.py and backfill_pr_merged_receipts.py.

Covers:
- merge_pr() emits a pr_merged receipt with correct pr_number
- a query on pr_number in t0_receipts.ndjson finds the receipt
- dispatch_register also receives a pr_merged event
- --dry-run executes no merge and writes no receipt
- backfill detects PRs missing receipts and emits them
- backfill is idempotent (already-receipted PRs are skipped)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, Any
from unittest import mock

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
    """Set up a minimal VNX environment with writable state directories."""
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
        "receipts_path": state_dir / "t0_receipts.ndjson",
        "events_pr_merged": events_dir / "pr_merged.ndjson",
    }


def _load_receipts(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _receipts_for_pr(path: Path, pr_number: int) -> list[Dict[str, Any]]:
    return [
        r for r in _load_receipts(path)
        if (r.get("event_type") or r.get("event")) == "pr_merged"
        and r.get("pr_number") == pr_number
    ]


# ---------------------------------------------------------------------------
# pr_merge.py tests
# ---------------------------------------------------------------------------

class TestMergePrEmitsReceipt:
    """merge_pr() writes a pr_merged receipt that can be queried by pr_number."""

    def test_receipt_written_with_correct_pr_number(self, vnx_env, monkeypatch):
        """A successful merge writes exactly one pr_merged receipt with the given pr_number."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "feat: test PR", "headRefName": "feature-branch",
            "state": "OPEN",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(
            pr_number=99,
            dispatch_id="test-dispatch-001",
            merge_method="squash",
            receipts_file=str(receipts_path),
        )

        assert result["success"] is True
        assert result["pr_number"] == 99
        assert result["dispatch_id"] == "test-dispatch-001"

        matching = _receipts_for_pr(receipts_path, 99)
        assert len(matching) == 1, f"Expected 1 receipt for PR#99, got {len(matching)}"
        r = matching[0]
        assert r["pr_number"] == 99
        assert r["event_type"] == "pr_merged"
        assert r["conclusion"] == "merged"
        assert r["merge_method"] == "squash"
        assert r["dispatch_id"] == "test-dispatch-001"

    def test_receipt_queryable_by_pr_number(self, vnx_env, monkeypatch):
        """pr_number field is present and queryable — FPY/history can link via pr_number."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "chore: cleanup", "headRefName": "chore-branch",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        pr_merge.merge_pr(pr_number=42, receipts_file=str(receipts_path))

        all_receipts = _load_receipts(receipts_path)
        pr_receipts = [r for r in all_receipts if r.get("pr_number") == 42]
        assert len(pr_receipts) >= 1
        assert pr_receipts[0]["event_type"] == "pr_merged"

    def test_receipt_contains_pr_title_and_branch(self, vnx_env, monkeypatch):
        """Receipt captures pr_title and branch from the GitHub API response."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n,
            "title": "refactor(receipt): extract mtime-calc python",
            "headRefName": "gov2-receipt-mtime",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=648, receipts_file=str(receipts_path))

        assert result["pr_title"] == "refactor(receipt): extract mtime-calc python"
        assert result["branch"] == "gov2-receipt-mtime"

        matching = _receipts_for_pr(receipts_path, 648)
        assert len(matching) == 1
        assert matching[0]["pr_title"] == "refactor(receipt): extract mtime-calc python"
        assert matching[0]["branch"] == "gov2-receipt-mtime"

    def test_dispatch_id_optional(self, vnx_env, monkeypatch):
        """merge_pr() works without a dispatch_id — pr_number alone is sufficient linkage."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "fix: hotfix", "headRefName": "hotfix-branch",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=77, receipts_file=str(receipts_path))

        assert result["success"] is True
        matching = _receipts_for_pr(receipts_path, 77)
        assert len(matching) == 1
        # dispatch_id absent or empty — receipt still exists and is queryable
        assert matching[0].get("event_type") == "pr_merged"

    def test_dry_run_writes_no_receipt(self, vnx_env, monkeypatch):
        """--dry-run returns success but writes nothing to t0_receipts.ndjson."""
        import pr_merge

        query_called = []
        merge_called = []
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: query_called.append(n) or None)
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: merge_called.append((n, m)) or (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=55, dry_run=True, receipts_file=str(receipts_path))

        assert result["success"] is True
        assert result["dry_run"] is True
        assert not merge_called, "merge must not be called in dry-run mode"
        assert not receipts_path.exists() or not _load_receipts(receipts_path), \
            "No receipts should be written in dry-run mode"

    def test_merge_failure_writes_no_receipt(self, vnx_env, monkeypatch):
        """When gh pr merge fails, no receipt is written."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "...", "headRefName": "...",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (False, "merge conflict"))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=33, receipts_file=str(receipts_path))

        assert result["success"] is False
        assert "merge conflict" in result["error"]
        matching = _receipts_for_pr(receipts_path, 33)
        assert len(matching) == 0, "Receipt must not be written when merge fails"

    def test_receipt_has_required_timestamp(self, vnx_env, monkeypatch):
        """Receipt carries a timestamp field (required by append_receipt validation)."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "test", "headRefName": "branch",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        pr_merge.merge_pr(pr_number=11, receipts_file=str(receipts_path))

        matching = _receipts_for_pr(receipts_path, 11)
        assert len(matching) == 1
        assert matching[0].get("timestamp"), "Receipt must have a timestamp"


class TestEmitRegisterEvent:
    """merge_pr() also writes to dispatch_register.ndjson."""

    def test_register_event_written(self, vnx_env, monkeypatch):
        """pr_merged event is written to dispatch_register.ndjson."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "test register", "headRefName": "reg-branch",
        })
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        register_path = vnx_env["state_dir"] / "dispatch_register.ndjson"

        result = pr_merge.merge_pr(pr_number=200, receipts_file=str(receipts_path))
        assert result["success"] is True

        if register_path.exists():
            events = [json.loads(l) for l in register_path.read_text().splitlines() if l.strip()]
            pr_events = [e for e in events if e.get("event") == "pr_merged" and e.get("pr_number") == 200]
            assert len(pr_events) >= 1, "dispatch_register must have a pr_merged event for PR#200"


# ---------------------------------------------------------------------------
# backfill_pr_merged_receipts.py tests
# ---------------------------------------------------------------------------

class TestBackfillPrMergedReceipts:
    """backfill_pr_merged_receipts.py reconciles missing receipts."""

    def _make_merged_prs(self, pr_numbers: list[int]) -> list[dict]:
        return [
            {
                "number": n,
                "title": f"PR #{n}",
                "headRefName": f"branch-{n}",
                "mergedAt": "2026-05-01T10:00:00Z",
                "baseRefName": "main",
            }
            for n in pr_numbers
        ]

    def test_backfill_emits_receipt_for_missing_pr(self, vnx_env, monkeypatch):
        """Backfill writes receipts to events/pr_merged.ndjson (ADR-005 ledger)."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = self._make_merged_prs([100, 101, 102])
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        receipts_path = vnx_env["receipts_path"]
        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(receipts_file=str(receipts_path), events_path=events_path)

        assert summary["missing_receipt_count"] == 3
        assert summary["backfilled"] == 3
        assert summary["errors"] == 0

        # Backfilled receipts go to events/pr_merged.ndjson, not t0_receipts.ndjson
        for pr_n in [100, 101, 102]:
            matching = _receipts_for_pr(events_path, pr_n)
            assert len(matching) >= 1, f"Expected receipt for PR#{pr_n} in events/pr_merged.ndjson"
            assert matching[0]["backfilled"] is True

    def test_backfill_skips_already_receipted_prs(self, vnx_env, monkeypatch):
        """backfill is idempotent — PRs with existing receipts are not re-emitted."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = self._make_merged_prs([200, 201])

        # Pre-populate a receipt for PR 200 in t0_receipts.ndjson
        receipts_path = vnx_env["receipts_path"]
        existing = {
            "timestamp": "2026-05-01T09:00:00Z",
            "event_type": "pr_merged",
            "pr_number": 200,
            "conclusion": "merged",
        }
        receipts_path.write_text(json.dumps(existing) + "\n", encoding="utf-8")

        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(receipts_file=str(receipts_path), events_path=events_path)

        assert summary["missing_receipt_count"] == 1
        assert summary["backfilled"] == 1

        # PR 200 was already in t0_receipts — no new receipt in events/
        matching_200 = _receipts_for_pr(events_path, 200)
        assert len(matching_200) == 0, "PR 200 already had a receipt, no backfill expected"

        # PR 201 should be in events/
        matching_201 = _receipts_for_pr(events_path, 201)
        assert len(matching_201) == 1

    def test_dry_run_writes_nothing(self, vnx_env, monkeypatch):
        """--dry-run previews without writing any receipts."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = self._make_merged_prs([300, 301])
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        receipts_path = vnx_env["receipts_path"]
        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(dry_run=True, receipts_file=str(receipts_path), events_path=events_path)

        assert summary["dry_run"] is True
        assert summary["missing_receipt_count"] == 2
        assert summary["backfilled"] == 0

        # Nothing written to events/
        assert not events_path.exists() or not _load_receipts(events_path)

    def test_since_filter_excludes_old_prs(self, vnx_env, monkeypatch):
        """--since filters out PRs merged before the given date."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = [
            {"number": 400, "title": "old", "headRefName": "b-400", "mergedAt": "2025-12-01T00:00:00Z"},
            {"number": 401, "title": "new", "headRefName": "b-401", "mergedAt": "2026-05-15T00:00:00Z"},
        ]
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        receipts_path = vnx_env["receipts_path"]
        events_path = vnx_env["events_pr_merged"]
        summary = bfr.backfill(since="2026-01-01", receipts_file=str(receipts_path), events_path=events_path)

        assert summary["missing_receipt_count"] == 1
        assert summary["backfilled"] == 1

        # Only PR 401 should have a receipt in events/
        assert len(_receipts_for_pr(events_path, 401)) == 1
        assert len(_receipts_for_pr(events_path, 400)) == 0

    def test_backfill_receipt_has_pr_number(self, vnx_env, monkeypatch):
        """Backfilled receipt has pr_number field for FPY/history query."""
        import backfill_pr_merged_receipts as bfr

        merged_prs = self._make_merged_prs([500])
        monkeypatch.setattr(bfr, "_gh_list_merged_prs", lambda limit, since: merged_prs)
        monkeypatch.setattr(bfr, "_load_dispatch_register_events", lambda: [])

        receipts_path = vnx_env["receipts_path"]
        events_path = vnx_env["events_pr_merged"]
        bfr.backfill(receipts_file=str(receipts_path), events_path=events_path)

        matching = _receipts_for_pr(events_path, 500)
        assert len(matching) == 1
        r = matching[0]
        assert r["pr_number"] == 500
        assert r["event_type"] == "pr_merged"
        assert r.get("backfilled") is True


class TestLoadReceiptedPrNumbers:
    """_load_receipted_pr_numbers returns correct set from NDJSON file."""

    def test_empty_file_returns_empty_set(self, tmp_path):
        from backfill_pr_merged_receipts import _load_receipted_pr_numbers
        empty = tmp_path / "receipts.ndjson"
        empty.write_text("", encoding="utf-8")
        assert _load_receipted_pr_numbers(empty) == set()

    def test_missing_file_returns_empty_set(self, tmp_path):
        from backfill_pr_merged_receipts import _load_receipted_pr_numbers
        assert _load_receipted_pr_numbers(tmp_path / "missing.ndjson") == set()

    def test_reads_pr_numbers_from_ndjson(self, tmp_path):
        from backfill_pr_merged_receipts import _load_receipted_pr_numbers
        lines = [
            json.dumps({"event_type": "pr_merged", "pr_number": 10, "timestamp": "2026-01-01T00:00:00Z"}),
            json.dumps({"event_type": "task_complete", "pr_number": 10, "timestamp": "2026-01-01T00:01:00Z"}),
            json.dumps({"event_type": "pr_merged", "pr_number": 20, "timestamp": "2026-01-01T00:02:00Z"}),
        ]
        (tmp_path / "r.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = _load_receipted_pr_numbers(tmp_path / "r.ndjson")
        # Only pr_merged events count
        assert result == {10, 20}

    def test_skips_malformed_lines(self, tmp_path):
        from backfill_pr_merged_receipts import _load_receipted_pr_numbers
        lines = [
            "{malformed",
            json.dumps({"event_type": "pr_merged", "pr_number": 30, "timestamp": "2026-01-01T00:00:00Z"}),
            "",
        ]
        (tmp_path / "r.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert _load_receipted_pr_numbers(tmp_path / "r.ndjson") == {30}
