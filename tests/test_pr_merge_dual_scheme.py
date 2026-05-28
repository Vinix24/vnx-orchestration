#!/usr/bin/env python3
"""Tests for dual-scheme pr_id/pr_number storage in pr_merge.py.

Covers:
- Receipt carries both pr_id and pr_number when commit subject has (PR-X) prefix
- pr_id_resolution='unmatched' when no PR-X prefix found in title
- Alphanumeric PR labels (PR-HYG-1, PR-TMUX-3) are extracted correctly
- Pure numeric PR labels (PR-42) are extracted correctly
- dispatch_id is included in receipt when known
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Any

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
    state_dir.mkdir(parents=True, exist_ok=True)

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
    }


def _load_receipts(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _pr_merged_receipts(path: Path, pr_number: int) -> list[Dict[str, Any]]:
    return [
        r for r in _load_receipts(path)
        if (r.get("event_type") or r.get("event")) == "pr_merged"
        and r.get("pr_number") == pr_number
    ]


# ---------------------------------------------------------------------------
# _extract_pr_id unit tests
# ---------------------------------------------------------------------------

class TestExtractPrId:
    """Unit tests for _extract_pr_id helper."""

    def test_pure_numeric_label(self):
        import pr_merge
        assert pr_merge._extract_pr_id("feat: something (PR-42)") == "PR-42"

    def test_alphanumeric_label_hyg(self):
        import pr_merge
        assert pr_merge._extract_pr_id("chore(hygiene): remove dead code (PR-HYG-1)") == "PR-HYG-1"

    def test_alphanumeric_label_tmux(self):
        import pr_merge
        assert pr_merge._extract_pr_id("feat(tmux): worktree isolation (PR-TMUX-3)") == "PR-TMUX-3"

    def test_alphanumeric_label_route(self):
        import pr_merge
        assert pr_merge._extract_pr_id("feat(route): enforce constraints (PR-ROUTE-1)") == "PR-ROUTE-1"

    def test_returns_none_when_no_label(self):
        import pr_merge
        assert pr_merge._extract_pr_id("feat(misc): cleanup without label") is None

    def test_returns_none_for_empty_string(self):
        import pr_merge
        assert pr_merge._extract_pr_id("") is None

    def test_first_match_wins(self):
        import pr_merge
        result = pr_merge._extract_pr_id("something PR-HYG-1 then PR-42")
        assert result == "PR-HYG-1"

    def test_uppercase_normalisation(self):
        import pr_merge
        result = pr_merge._extract_pr_id("feat: title (pr-hyg-1)")
        assert result == "PR-HYG-1"


# ---------------------------------------------------------------------------
# merge_pr dual-scheme receipt tests
# ---------------------------------------------------------------------------

class TestMergePrDualScheme:
    """merge_pr() emits receipt with both pr_id and pr_number."""

    def _make_pr_data(self, number: int, title: str, branch: str = "feature-branch") -> Dict[str, Any]:
        return {"number": number, "title": title, "headRefName": branch, "state": "OPEN"}

    def test_receipt_has_both_pr_id_and_pr_number(self, vnx_env, monkeypatch):
        """When title contains (PR-HYG-1), receipt carries both pr_id='PR-HYG-1' and pr_number."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: self._make_pr_data(
            n, "chore(hygiene): remove dead code (PR-HYG-1)", "feat/hyg-1-dead-code"
        ))
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=668, receipts_file=str(receipts_path))

        assert result["success"] is True

        matching = _pr_merged_receipts(receipts_path, 668)
        assert len(matching) == 1, f"Expected 1 receipt, got {len(matching)}"
        r = matching[0]
        assert r["pr_number"] == 668
        assert r.get("pr_id") == "PR-HYG-1", f"Expected pr_id='PR-HYG-1', got {r.get('pr_id')!r}"
        assert "pr_id_resolution" not in r, "pr_id_resolution should be absent when pr_id is known"

    def test_receipt_has_pr_id_resolution_unmatched_when_no_label(self, vnx_env, monkeypatch):
        """When title has no PR-N label, receipt has pr_id_resolution='unmatched'."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: self._make_pr_data(
            n, "docs(readme): update installation instructions", "feat/docs-readme"
        ))
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        result = pr_merge.merge_pr(pr_number=674, receipts_file=str(receipts_path))

        assert result["success"] is True

        matching = _pr_merged_receipts(receipts_path, 674)
        assert len(matching) == 1
        r = matching[0]
        assert r["pr_number"] == 674
        assert r.get("pr_id_resolution") == "unmatched", (
            f"Expected pr_id_resolution='unmatched', got {r.get('pr_id_resolution')!r}"
        )
        assert not r.get("pr_id"), f"pr_id should be absent/empty, got {r.get('pr_id')!r}"

    def test_receipt_has_pr_number_regardless_of_pr_id(self, vnx_env, monkeypatch):
        """pr_number is always present whether or not pr_id is found."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: self._make_pr_data(
            n, "fix: hotfix with no label", "fix/hotfix"
        ))
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        pr_merge.merge_pr(pr_number=999, receipts_file=str(receipts_path))

        matching = _pr_merged_receipts(receipts_path, 999)
        assert len(matching) == 1
        assert matching[0]["pr_number"] == 999

    def test_numeric_label_extracted(self, vnx_env, monkeypatch):
        """Pure numeric labels like (PR-42) are also extracted into pr_id."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: self._make_pr_data(
            n, "feat: implement schema versioning (PR-42)", "feat/schema-versioning"
        ))
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        pr_merge.merge_pr(pr_number=642, receipts_file=str(receipts_path))

        matching = _pr_merged_receipts(receipts_path, 642)
        assert len(matching) == 1
        assert matching[0].get("pr_id") == "PR-42"

    def test_dispatch_id_included_when_provided(self, vnx_env, monkeypatch):
        """dispatch_id is stored in receipt when explicitly provided."""
        import pr_merge

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: self._make_pr_data(
            n, "feat(trace): fix traceability gap (PR-B-TRACE)", "feat/trace-gap-fix"
        ))
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m: (True, ""))

        receipts_path = vnx_env["receipts_path"]
        pr_merge.merge_pr(
            pr_number=677,
            dispatch_id="20260528-b-trace-gap-fix",
            receipts_file=str(receipts_path),
        )

        matching = _pr_merged_receipts(receipts_path, 677)
        assert len(matching) == 1
        assert matching[0].get("dispatch_id") == "20260528-b-trace-gap-fix"
        assert matching[0].get("pr_id") == "PR-B-TRACE"
