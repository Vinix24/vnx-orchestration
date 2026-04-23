"""tests/test_gate_status_check.py — OI-1139 + OI-1140 regression tests.

OI-1139: _check_all_gates_passed must inspect result status, not just file presence.
OI-1140: cleanup_orphan_gates.main() must track resolved orphans individually.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_gate_result(results_dir: Path, pr_number: int, gate: str, status: str) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    f = results_dir / f"pr-{pr_number}-{gate}.json"
    f.write_text(json.dumps({"gate": gate, "pr_number": pr_number, "status": status}), encoding="utf-8")
    return f


def _make_event(dispatch_id: str) -> "object":
    """Build a minimal LoopEvent-like object for _check_all_gates_passed."""
    from headless_orchestrator import LoopEvent
    return LoopEvent(
        reason="receipt",
        context={
            "latest_event": "gate_pass",
            "latest_dispatch_id": dispatch_id,
        },
    )


def _make_orchestrator(tmp_path: Path) -> "object":
    """Construct a HeadlessOrchestrator with tmp_path as data/state dir."""
    from headless_orchestrator import HeadlessOrchestrator
    data_dir = tmp_path / ".vnx-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return HeadlessOrchestrator(data_dir=data_dir, state_dir=state_dir, dry_run=True)


# ---------------------------------------------------------------------------
# OI-1139: status inspection
# ---------------------------------------------------------------------------

class TestCheckAllGatesPassed:
    def test_fails_when_result_status_is_failed(self, tmp_path: Path) -> None:
        """Gate file exists but status=failed — must NOT emit feature_gates_complete."""
        orch = _make_orchestrator(tmp_path)
        results_dir = orch.state_dir / "review_gates" / "results"
        _write_gate_result(results_dir, 57, "codex_gate", "failed")
        _write_gate_result(results_dir, 57, "gemini_review", "completed")

        logged: list[dict] = []
        with patch("headless_orchestrator._log_loop_event", side_effect=lambda _d, rec: logged.append(rec)):
            orch._check_all_gates_passed(_make_event("20260424-f42-pr3-some-feature-A"))

        assert not any(r.get("event_type") == "feature_gates_complete" for r in logged), (
            "feature_gates_complete must not fire when a required gate has status=failed"
        )

    def test_passes_when_all_results_are_completed(self, tmp_path: Path) -> None:
        """Both required gates have status=completed — must emit feature_gates_complete."""
        orch = _make_orchestrator(tmp_path)
        results_dir = orch.state_dir / "review_gates" / "results"
        _write_gate_result(results_dir, 57, "codex_gate", "completed")
        _write_gate_result(results_dir, 57, "gemini_review", "completed")

        logged: list[dict] = []
        with patch("headless_orchestrator._log_loop_event", side_effect=lambda _d, rec: logged.append(rec)):
            orch._check_all_gates_passed(_make_event("20260424-f42-pr3-some-feature-A"))

        assert any(r.get("event_type") == "feature_gates_complete" for r in logged), (
            "feature_gates_complete must fire when all required gates have passing status"
        )

    def test_passes_when_all_results_are_pass(self, tmp_path: Path) -> None:
        """Both gates have status=pass (gate_result_parser path) — must emit complete."""
        orch = _make_orchestrator(tmp_path)
        results_dir = orch.state_dir / "review_gates" / "results"
        _write_gate_result(results_dir, 99, "codex_gate", "pass")
        _write_gate_result(results_dir, 99, "gemini_review", "pass")

        logged: list[dict] = []
        with patch("headless_orchestrator._log_loop_event", side_effect=lambda _d, rec: logged.append(rec)):
            orch._check_all_gates_passed(_make_event("20260424-f99-pr1-feature-A"))

        assert any(r.get("event_type") == "feature_gates_complete" for r in logged)

    def test_does_not_fire_when_gate_file_missing(self, tmp_path: Path) -> None:
        """Only one required gate present — must not fire."""
        orch = _make_orchestrator(tmp_path)
        results_dir = orch.state_dir / "review_gates" / "results"
        _write_gate_result(results_dir, 57, "codex_gate", "completed")
        # gemini_review intentionally absent

        logged: list[dict] = []
        with patch("headless_orchestrator._log_loop_event", side_effect=lambda _d, rec: logged.append(rec)):
            orch._check_all_gates_passed(_make_event("20260424-f42-pr3-some-feature-A"))

        assert not logged


class TestLatestPrScopedToCurrentFeature:
    def test_picks_highest_pr_when_multiple_features_present(self, tmp_path: Path) -> None:
        """With gate results for PR #5 (failed) and PR #10 (completed), picks PR #10."""
        orch = _make_orchestrator(tmp_path)
        results_dir = orch.state_dir / "review_gates" / "results"
        _write_gate_result(results_dir, 5, "codex_gate", "failed")
        _write_gate_result(results_dir, 5, "gemini_review", "failed")
        _write_gate_result(results_dir, 10, "codex_gate", "completed")
        _write_gate_result(results_dir, 10, "gemini_review", "completed")

        logged: list[dict] = []
        with patch("headless_orchestrator._log_loop_event", side_effect=lambda _d, rec: logged.append(rec)):
            orch._check_all_gates_passed(_make_event("20260424-f42-pr3-some-feature-A"))

        gates_complete = [r for r in logged if r.get("event_type") == "feature_gates_complete"]
        assert gates_complete, "Should fire for PR #10 (highest, both passing)"
        assert gates_complete[0]["pr_number"] == 10


# ---------------------------------------------------------------------------
# OI-1140: orphan tracking
# ---------------------------------------------------------------------------

class TestOrphanLogTracksResolvedItemsNotSlice:
    def _make_orphan(self, tmp_path: Path, stem: str) -> dict:
        req_dir = tmp_path / "requests"
        res_dir = tmp_path / "results"
        req_dir.mkdir(parents=True, exist_ok=True)
        res_dir.mkdir(parents=True, exist_ok=True)
        req_file = req_dir / f"{stem}.json"
        req_file.write_text(json.dumps({"stem": stem}), encoding="utf-8")
        return {
            "request_path": req_file,
            "result_path": res_dir / f"{stem}.json",
            "gate_name": stem.split("-", 2)[2] if stem.count("-") >= 2 else stem,
            "stem": stem,
            "age_hours": 48.0,
            "request_data": {"stem": stem},
        }

    def test_logs_only_succeeded_orphans_when_first_fails(self, tmp_path: Path) -> None:
        """When orphan[0] write fails and orphan[1] succeeds, audit must log orphan[1] only."""
        import cleanup_orphan_gates as m

        orphan_a = self._make_orphan(tmp_path, "pr-10-codex_gate")
        orphan_b = self._make_orphan(tmp_path, "pr-11-gemini_review")

        write_calls: list[str] = []

        def fake_write(orphan: dict, dry_run: bool) -> bool:
            # First call fails, second succeeds.
            if not write_calls:
                write_calls.append(orphan["stem"])
                return False
            write_calls.append(orphan["stem"])
            return True

        audit_calls: list[list[dict]] = []

        with (
            patch.object(m, "_find_orphans", return_value=[orphan_a, orphan_b]),
            patch.object(m, "_write_abandoned_result", side_effect=fake_write),
            patch.object(m, "_log_to_audit", side_effect=lambda lst: audit_calls.append(list(lst))),
        ):
            ret = m.main([])

        assert ret == 1, "Should return 1 because one write failed"
        assert len(audit_calls) == 1, "audit should be called once"
        logged_stems = [o["stem"] for o in audit_calls[0]]
        assert logged_stems == ["pr-11-gemini_review"], (
            f"Only the succeeded orphan should be logged, got: {logged_stems}"
        )
        assert "pr-10-codex_gate" not in logged_stems

    def test_logs_all_orphans_when_all_succeed(self, tmp_path: Path) -> None:
        """When all writes succeed, all orphans are logged."""
        import cleanup_orphan_gates as m

        orphans = [
            self._make_orphan(tmp_path, "pr-20-codex_gate"),
            self._make_orphan(tmp_path, "pr-21-gemini_review"),
        ]

        audit_calls: list[list[dict]] = []

        with (
            patch.object(m, "_find_orphans", return_value=orphans),
            patch.object(m, "_write_abandoned_result", return_value=True),
            patch.object(m, "_log_to_audit", side_effect=lambda lst: audit_calls.append(list(lst))),
        ):
            ret = m.main([])

        assert ret == 0
        assert len(audit_calls) == 1
        assert len(audit_calls[0]) == 2
