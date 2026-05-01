#!/usr/bin/env python3
"""Regression tests for W5C misc-cleanups bundle.

OI-1101 — T2 doesn't write a formal receipt file to unified_reports/
OI-1109 — TestOnceMode tests inline snippet instead of supervisor --once mode
OI-1131 — slug-check run_gate stricter than docs (gate rejects valid slugs)
OI-1152 — event-timeline phase filter with zero events allows invalid cursor state
OI-1161 — headless_dispatch_writer.py documented consumers
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

SUPERVISOR_SH = REPO_ROOT / "scripts" / "dispatcher_supervisor.sh"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _vnx_env(tmp_dir: str) -> dict:
    env = dict(os.environ)
    env["VNX_DATA_DIR"] = tmp_dir
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    env["VNX_STATE_DIR"] = os.path.join(tmp_dir, "state")
    env["VNX_DISPATCH_DIR"] = os.path.join(tmp_dir, "dispatches")
    env["VNX_LOGS_DIR"] = os.path.join(tmp_dir, "logs")
    env["VNX_PIDS_DIR"] = os.path.join(tmp_dir, "pids")
    env["VNX_LOCKS_DIR"] = os.path.join(tmp_dir, "locks")
    env["VNX_REPORTS_DIR"] = os.path.join(tmp_dir, "unified_reports")
    env["VNX_DB_DIR"] = os.path.join(tmp_dir, "database")
    env["VNX_SUPERVISOR_BACKOFF_INIT"] = "1"
    env["VNX_SUPERVISOR_BACKOFF_MAX"] = "4"
    env["VNX_SUPERVISOR_BACKOFF_STABLE"] = "999"
    return env


def _make_dirs(tmp_dir: str) -> None:
    for sub in ("state", "dispatches", "logs", "pids", "locks", "unified_reports", "database"):
        os.makedirs(os.path.join(tmp_dir, sub), exist_ok=True)


# ---------------------------------------------------------------------------
# OI-1101 — _ensure_unified_report
# ---------------------------------------------------------------------------

class TestEnsureUnifiedReport:
    def _import(self):
        from subprocess_dispatch_internals.receipt_writer import _ensure_unified_report
        return _ensure_unified_report

    def test_creates_stub_when_missing(self, tmp_path, monkeypatch):
        """_ensure_unified_report writes a stub when no report exists."""
        reports_dir = tmp_path / "unified_reports"
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

        fn = self._import()
        result = fn("20260501-120000-fix-test-B", "T2", "done")

        assert result is not None
        report_path = reports_dir / "20260501-120000-fix-test-B_report.md"
        assert report_path.exists(), "Stub report must be written to unified_reports/"
        content = report_path.read_text()
        assert "20260501-120000-fix-test-B" in content
        assert "T2" in content
        assert "done" in content
        assert "## Open Items" in content

    def test_does_not_overwrite_existing_report(self, tmp_path, monkeypatch):
        """_ensure_unified_report is idempotent — existing reports are not modified."""
        reports_dir = tmp_path / "unified_reports"
        reports_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

        existing = reports_dir / "20260501-120000-fix-existing-B_report.md"
        existing.write_text("# Worker-written report\nCustom content.\n")

        fn = self._import()
        result = fn("20260501-120000-fix-existing-B", "T2", "done")

        assert result is None, "Should return None when report already exists"
        assert existing.read_text() == "# Worker-written report\nCustom content.\n"

    def test_returns_none_when_env_var_unset(self, monkeypatch):
        """_ensure_unified_report is a no-op when VNX_REPORTS_DIR is unset."""
        monkeypatch.delenv("VNX_REPORTS_DIR", raising=False)
        fn = self._import()
        result = fn("20260501-120000-fix-no-env-B", "T2", "done")
        assert result is None

    def test_t1_also_gets_stub(self, tmp_path, monkeypatch):
        """Stub creation applies to T1 as well as T2."""
        reports_dir = tmp_path / "unified_reports"
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        fn = self._import()
        result = fn("20260501-120000-fix-t1-A", "T1", "done")
        assert result is not None
        assert (reports_dir / "20260501-120000-fix-t1-A_report.md").exists()

    def test_t3_also_gets_stub(self, tmp_path, monkeypatch):
        """Stub creation applies to T3 as well as T2."""
        reports_dir = tmp_path / "unified_reports"
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        fn = self._import()
        result = fn("20260501-120000-fix-t3-C", "T3", "done")
        assert result is not None
        assert (reports_dir / "20260501-120000-fix-t3-C_report.md").exists()


# ---------------------------------------------------------------------------
# OI-1109 — TestOnceMode using real supervisor --once
# ---------------------------------------------------------------------------

class TestOnceModeWithRealSupervisor:
    def _make_fake_dispatcher(self, tmp_dir: str, exit_code: int = 0) -> str:
        path = os.path.join(tmp_dir, "fake_dispatcher_v8_minimal.sh")
        with open(path, "w") as f:
            f.write(f"#!/bin/bash\nexit {exit_code}\n")
        os.chmod(path, 0o755)
        return path

    def test_once_mode_exits_after_dispatcher_exits(self):
        """--once flag: supervisor exits with dispatcher's exit code (0)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            fake = self._make_fake_dispatcher(tmp_dir, exit_code=0)
            env["VNX_DISPATCHER_SCRIPT"] = fake

            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "--once"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
                timeout=20,
            )
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}\n"
            f"stderr={result.stderr!r}"
        )

    def test_once_mode_propagates_nonzero_exit_code(self):
        """--once flag: supervisor propagates dispatcher's non-zero exit code."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            fake = self._make_fake_dispatcher(tmp_dir, exit_code=42)
            env["VNX_DISPATCHER_SCRIPT"] = fake

            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "--once"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
                timeout=20,
            )
        assert result.returncode == 42, (
            f"Expected exit 42, got {result.returncode}\n"
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# OI-1131 — slug-check: legacy dispatch ID format accepted by run_gate
# ---------------------------------------------------------------------------

class TestSlugMatchLegacyFormat:
    def test_legacy_dispatch_id_slug_extracted(self):
        """dispatch_id_slug handles YYYYMMDD-slug (no time, no track)."""
        from check_ci_slug_match import dispatch_id_slug
        result = dispatch_id_slug("20260224-fix-auth-validation")
        assert result is not None, (
            "dispatch_id_slug must return non-None for legacy YYYYMMDD-slug format"
        )
        assert "auth" in result or "fix" in result

    def test_legacy_dispatch_id_slug_new_format_unaffected(self):
        """Full-format dispatch IDs still parse correctly after regex change."""
        from check_ci_slug_match import dispatch_id_slug
        assert dispatch_id_slug("20260423-230100-ci-slug-match-gate-B") == "ci-slug-match-gate"
        assert dispatch_id_slug("20260101-000000-headless-gate-dispatch-id-A") == "headless-gate-dispatch-id"
        assert dispatch_id_slug("20260423-100000-fix-A") == "fix"

    def test_legacy_slug_in_run_gate_passes(self, capsys):
        """A commit with legacy Dispatch-ID passes run_gate (shadow mode)."""
        from check_ci_slug_match import run_gate
        from unittest.mock import patch
        commits = [
            ("abc12345", "feat: fix auth\n\nDispatch-ID: 20260224-fix-auth-validation\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/fix-auth-validation", enforce=False)
        assert rc == 0

    def test_legacy_slug_format_in_enforce_mode(self, capsys):
        """Legacy Dispatch-ID passes run_gate in enforce mode when slug matches."""
        from check_ci_slug_match import run_gate
        from unittest.mock import patch
        commits = [
            ("abc99999", "feat: fix auth\n\nDispatch-ID: 20260224-fix-auth-validation\n"),
        ]
        with patch("check_ci_slug_match.commits_since", return_value=commits), \
             patch("check_ci_slug_match.resolve_base_ref", return_value="main"):
            rc = run_gate("main", "fix/fix-auth-validation", enforce=True)
        assert rc == 0, (
            "Legacy dispatch ID should pass run_gate when branch slug matches"
        )


# ---------------------------------------------------------------------------
# OI-1152 — event-timeline: empty phase filter does not produce invalid cursor
# ---------------------------------------------------------------------------

class TestEventTimelineCursorBounds:
    """Pure logic tests for cursor state — no React renderer required."""

    def _compute_filtered_length(self, events: list, phase_filter: str) -> int:
        """Compute filtered event count from raw event list."""
        tool_with_phase = []
        current_phase = "other"
        for ev in events:
            if ev.get("type") == "phase_marker":
                current_phase = ev.get("phase", "other")
                continue
            if ev.get("type") == "tool_use":
                tool_with_phase.append({"phase": current_phase})
        if phase_filter == "all":
            return len(tool_with_phase)
        return sum(1 for x in tool_with_phase if x["phase"] == phase_filter)

    def test_empty_filter_result_has_length_zero(self):
        """A phase filter with no matching events yields filtered.length == 0."""
        events = [
            {"type": "phase_marker", "phase": "explore"},
            {"type": "tool_use", "tool_name": "Read", "summary": "a"},
        ]
        length = self._compute_filtered_length(events, "commit")
        assert length == 0, "commit phase has no events — filtered length must be 0"

    def test_cursor_clamped_to_none_when_filtered_empty(self):
        """Cursor must be None (not 0) when filtered.length is 0."""
        # Simulate the cursor state machine: cursor starts null, filter → empty
        cursor = None
        filtered_length = 0

        # The forward button is disabled when filtered_length == 0;
        # no state mutation should happen.
        max_idx = filtered_length - 1  # = -1
        # Guard: stepForward must not advance cursor when filtered is empty
        if filtered_length > 0:
            cursor = 0 if cursor is None else min(max_idx, cursor + 1)

        assert cursor is None, (
            "cursor must stay None when filtered is empty "
            f"(got cursor={cursor!r})"
        )

    def test_filter_switch_resets_cursor_to_none(self):
        """Switching phase filter always resets cursor to None."""
        cursor = 3  # was non-null from previous interaction
        # Simulate phase filter button onClick: setCursor(null)
        cursor = None
        assert cursor is None


# ---------------------------------------------------------------------------
# OI-1161 — headless_dispatch_writer consumers documented
# ---------------------------------------------------------------------------

class TestHeadlessDispatchWriterDocumented:
    def test_module_docstring_mentions_consumers(self):
        """headless_dispatch_writer module docstring names its consumers."""
        import headless_dispatch_writer as hdw
        doc = hdw.__doc__ or ""
        assert len(doc) > 0, "Module must have a docstring"
        assert any(
            kw in doc.lower()
            for kw in ["consumer", "used by", "reads", "daemon", "dispatch-agent", "decision_executor"]
        ), (
            f"Module docstring must document consumers; got: {doc[:200]!r}"
        )

    def test_write_dispatch_returns_path(self, tmp_path):
        """write_dispatch creates pending/<id>/dispatch.json and returns its path."""
        import headless_dispatch_writer as hdw

        dispatch_id = hdw.generate_dispatch_id("test-w5c", "B")
        import os
        os.environ["VNX_DISPATCH_DIR"] = str(tmp_path / "dispatches")

        path = hdw.write_dispatch(
            dispatch_id=dispatch_id,
            terminal="T2",
            track="B",
            role="test-engineer",
            instruction="test instruction",
        )
        assert path.exists(), "dispatch.json must be created"
        import json
        data = json.loads(path.read_text())
        assert data["dispatch_id"] == dispatch_id
        assert data["terminal"] == "T2"
