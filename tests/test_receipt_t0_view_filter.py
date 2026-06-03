"""Tests for receipt T0-view filter — project_id alignment + recent_receipts builder.

Covers:
- project_root.resolve_project_id() reads .vnx-project-id (Fix 1)
- _build_recent_receipts top-N, project_id filter, T0-view fields (Fix 2)
- _infer_next_action per status mapping
- Integration: build_t0_state produces >5 recent_receipts entries
"""

from __future__ import annotations

import json
import sys
import os
from pathlib import Path

import pytest

# Make scripts/ and scripts/lib importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
for _p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Fix 1 — resolve_project_id reads .vnx-project-id
# ---------------------------------------------------------------------------

class TestResolveProjectId:
    def test_resolve_project_id_returns_vnx_dev(self, tmp_path: Path) -> None:
        """resolve_project_id() reads .vnx-project-id file, returns its content."""
        from project_root import resolve_project_id

        marker = tmp_path / ".vnx-project-id"
        marker.write_text("vnx-dev\n", encoding="utf-8")

        result = resolve_project_id(project_dir=tmp_path)
        assert result == "vnx-dev"

    def test_resolve_project_id_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """VNX_PROJECT_ID env var takes priority over .vnx-project-id file."""
        from project_root import resolve_project_id

        marker = tmp_path / ".vnx-project-id"
        marker.write_text("vnx-dev\n", encoding="utf-8")
        monkeypatch.setenv("VNX_PROJECT_ID", "from-env")

        result = resolve_project_id(project_dir=tmp_path)
        assert result == "from-env"

    def test_resolve_project_id_file_in_ancestor(self, tmp_path: Path) -> None:
        """Walks up to ancestor to find .vnx-project-id."""
        from project_root import resolve_project_id

        (tmp_path / ".vnx-project-id").write_text("parent-project\n", encoding="utf-8")
        subdir = tmp_path / "sub" / "dir"
        subdir.mkdir(parents=True)

        result = resolve_project_id(project_dir=subdir)
        assert result == "parent-project"

    def test_resolve_project_id_real_repo_returns_vnx_dev(self) -> None:
        """For this actual repo, resolve_project_id() returns vnx-dev via .vnx-project-id."""
        from project_root import resolve_project_id

        result = resolve_project_id(project_dir=_REPO_ROOT)
        assert result == "vnx-dev", (
            f"Expected 'vnx-dev' but got {result!r}. "
            "Check .vnx-project-id in the repo root."
        )


# ---------------------------------------------------------------------------
# Fix 2 — _infer_next_action per status mapping
# ---------------------------------------------------------------------------

class TestInferNextAction:
    def _fn(self, r: dict) -> str:
        from build_t0_state import _infer_next_action
        return _infer_next_action(r)

    def test_done_no_pr_returns_merge_ready(self) -> None:
        assert self._fn({"status": "done"}) == "merge_ready"

    def test_done_with_pr_id_returns_review(self) -> None:
        assert self._fn({"status": "done", "pr_id": "PR-123"}) == "review"

    def test_success_no_pr_returns_merge_ready(self) -> None:
        assert self._fn({"status": "success"}) == "merge_ready"

    def test_success_with_pr_returns_review(self) -> None:
        assert self._fn({"status": "success", "pr": "PR-456"}) == "review"

    def test_failed_returns_fix_needed(self) -> None:
        assert self._fn({"status": "failed"}) == "fix_needed"

    def test_failure_returns_fix_needed(self) -> None:
        assert self._fn({"status": "failure"}) == "fix_needed"

    def test_running_returns_wait(self) -> None:
        assert self._fn({"status": "running"}) == "wait"

    def test_queued_returns_wait(self) -> None:
        assert self._fn({"status": "queued"}) == "wait"

    def test_unknown_returns_verify(self) -> None:
        assert self._fn({"status": "unknown"}) == "verify"

    def test_none_status_returns_verify(self) -> None:
        assert self._fn({"status": None}) == "verify"

    def test_empty_status_returns_verify(self) -> None:
        assert self._fn({}) == "verify"


# ---------------------------------------------------------------------------
# Fix 2 — _build_recent_receipts
# ---------------------------------------------------------------------------

def _make_ndjson(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "state" / "t0_receipts.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = "\n".join(json.dumps(e) for e in entries)
    p.write_text(lines + "\n", encoding="utf-8")
    return p


def _make_receipt(
    *,
    dispatch_id: str = "disp-001",
    status: str = "done",
    project_id: str = "vnx-dev",
    terminal: str = "T1",
    timestamp: str = "2026-06-03T10:00:00Z",
    pr_id: str | None = None,
    commit_hash: str | None = None,
    token_usage: int | None = None,
    contract_hash: str | None = None,
) -> dict:
    r: dict = {
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "status": status,
        "project_id": project_id,
        "terminal": terminal,
        "timestamp": timestamp,
    }
    if pr_id is not None:
        r["pr_id"] = pr_id
    if commit_hash is not None:
        r["commit_hash"] = commit_hash
    if token_usage is not None:
        r["token_usage"] = token_usage
    if contract_hash is not None:
        r["contract_hash"] = contract_hash
    return r


class TestBuildRecentReceipts:
    def _fn(self, state_dir: Path, project_id: str = "vnx-dev", limit: int = 20) -> list:
        from build_t0_state import _build_recent_receipts
        # Ensure no central DB interference
        os.environ.pop("VNX_USE_CENTRAL_DB", None)
        return _build_recent_receipts(state_dir, project_id=project_id, limit=limit)

    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        assert self._fn(state_dir) == []

    def test_returns_top_20_from_50_entries(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id=f"d-{i:03d}", timestamp=f"2026-06-03T{i:02d}:00:00Z")
            for i in range(50)
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir, limit=20)
        assert len(result) == 20

    def test_returns_sorted_desc_by_timestamp(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id="early", timestamp="2026-06-03T01:00:00Z"),
            _make_receipt(dispatch_id="late", timestamp="2026-06-03T23:00:00Z"),
            _make_receipt(dispatch_id="mid", timestamp="2026-06-03T12:00:00Z"),
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir, limit=20)
        assert result[0]["dispatch_id"] == "late"
        assert result[1]["dispatch_id"] == "mid"
        assert result[2]["dispatch_id"] == "early"

    def test_filters_receipts_with_wrong_project_id(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id="match", project_id="vnx-dev"),
            _make_receipt(dispatch_id="other", project_id="some-other-project"),
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir, project_id="vnx-dev")
        ids = [r["dispatch_id"] for r in result]
        assert "match" in ids
        assert "other" not in ids

    def test_accepts_no_project_id_for_backward_compat(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id="no-pid", project_id=""),
            _make_receipt(dispatch_id="has-pid", project_id="vnx-dev"),
        ]
        # Remove project_id key entirely from first entry
        entries[0].pop("project_id")
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir, project_id="vnx-dev")
        ids = [r["dispatch_id"] for r in result]
        assert "no-pid" in ids
        assert "has-pid" in ids

    def test_t0_view_fields_present(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(
                dispatch_id="d-001",
                pr_id="PR-42",
                commit_hash="abc1234",
                token_usage=9999,
                contract_hash="deadbeef",
            )
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir)
        assert len(result) == 1
        r = result[0]
        # Required T0-view fields
        assert "timestamp" in r
        assert "dispatch_id" in r
        assert "status" in r
        assert "terminal" in r
        assert "commit_hash" in r
        assert r["commit_hash"] == "abc1234"
        assert "pr_id" in r
        assert r["pr_id"] == "PR-42"
        assert "report_evidence_path" in r
        assert "next_action" in r
        # Excluded fields
        assert "token_usage" not in r
        assert "contract_hash" not in r

    def test_skips_state_mutation_events(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id="real"),
            {"event_type": "state_mutation", "project_id": "vnx-dev", "timestamp": "2026-06-03T09:00:00Z"},
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir)
        ids = [r["dispatch_id"] for r in result if r.get("dispatch_id")]
        assert "real" in ids
        assert len(result) == 1

    def test_commit_fallback_to_commit_field(self, tmp_path: Path) -> None:
        entry = _make_receipt(dispatch_id="d-002")
        entry["commit"] = "fallback-hash"
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, [entry])
        result = self._fn(state_dir)
        assert result[0]["commit_hash"] == "fallback-hash"

    def test_pr_fallback_to_pr_field(self, tmp_path: Path) -> None:
        entry = _make_receipt(dispatch_id="d-003")
        entry["pr"] = "PR-fallback"
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, [entry])
        result = self._fn(state_dir)
        assert result[0]["pr_id"] == "PR-fallback"

    def test_next_action_derived_from_status(self, tmp_path: Path) -> None:
        entries = [
            _make_receipt(dispatch_id="done-no-pr", status="done"),
            _make_receipt(dispatch_id="done-with-pr", status="done", pr_id="PR-1", timestamp="2026-06-03T02:00:00Z"),
            _make_receipt(dispatch_id="failed", status="failed", timestamp="2026-06-03T03:00:00Z"),
        ]
        state_dir = tmp_path / "state"
        _make_ndjson(tmp_path, entries)
        result = self._fn(state_dir)
        by_id = {r["dispatch_id"]: r for r in result}
        assert by_id["done-no-pr"]["next_action"] == "merge_ready"
        assert by_id["done-with-pr"]["next_action"] == "review"
        assert by_id["failed"]["next_action"] == "fix_needed"

    def test_malformed_lines_skipped_gracefully(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        ndjson = state_dir / "t0_receipts.ndjson"
        ndjson.write_text(
            "not-json\n"
            + json.dumps(_make_receipt(dispatch_id="good")) + "\n"
            + "{broken\n",
            encoding="utf-8",
        )
        result = self._fn(state_dir)
        assert len(result) == 1
        assert result[0]["dispatch_id"] == "good"


# ---------------------------------------------------------------------------
# Integration — build_t0_state end-to-end produces >5 recent_receipts
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBuildT0StateIntegration:
    def test_produces_more_than_5_recent_receipts(self, tmp_path: Path) -> None:
        """End-to-end: build_t0_state produces >5 recent_receipts from an NDJSON with 10."""
        import importlib
        import build_t0_state as bts

        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        dispatch_dir.mkdir(parents=True)

        # Write 10 receipts for vnx-dev
        entries = [
            _make_receipt(
                dispatch_id=f"int-{i:02d}",
                status="done",
                timestamp=f"2026-06-03T{i:02d}:00:00Z",
            )
            for i in range(10)
        ]
        ndjson = state_dir / "t0_receipts.ndjson"
        ndjson.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n",
            encoding="utf-8",
        )

        # Monkey-patch _central_state_dir_for to return None (disable central mode)
        orig = bts._central_state_dir_for
        bts._central_state_dir_for = lambda _sd: None  # type: ignore[attr-defined]

        # Patch project_id_from_state_dir to return vnx-dev
        import vnx_paths
        orig_pid = vnx_paths.project_id_from_state_dir
        vnx_paths.project_id_from_state_dir = lambda _: "vnx-dev"  # type: ignore[attr-defined]

        try:
            state = bts.build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        finally:
            bts._central_state_dir_for = orig  # type: ignore[attr-defined]
            vnx_paths.project_id_from_state_dir = orig_pid  # type: ignore[attr-defined]

        receipts = state.get("recent_receipts", [])
        assert len(receipts) > 5, (
            f"Expected >5 recent_receipts but got {len(receipts)}: {receipts}"
        )
