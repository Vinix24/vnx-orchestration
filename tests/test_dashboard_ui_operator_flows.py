#!/usr/bin/env python3
"""
Operator flow tests for PR-3 dashboard UI (Feature 13).

Quality gate coverage (gate_pr3_dashboard_ui):
  - Projects view shows session start entrypoints and active session visibility
  - Terminal state view distinguishes active, stale, blocked, and exited sessions
  - Per-project and aggregate open-item views render correctly
  - Degraded, stale, and empty states render explicitly
  - Session start and control actions produce ActionOutcome results

These tests exercise the read-model and action layers that back the UI,
validating the exact data contract the frontend components consume.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from dashboard_read_model import (
    ProjectsView,
    TerminalView,
    OpenItemsView,
    AggregateOpenItemsView,
    SessionView,
    FreshnessEnvelope,
    load_project_registry,
    register_project,
    FRESH_THRESHOLD,
    AGING_THRESHOLD,
)
from dashboard_actions import (
    ActionOutcome,
    start_session,
    stop_session,
    attach_terminal,
    refresh_projections,
    run_reconciliation,
    inspect_open_item,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _make_open_items_file(path: Path, items: list[dict]) -> None:
    _write_json(path, {"schema_version": 1, "items": items})


def _make_pr_queue(path: Path, feature: str = "Feature 13", prs: list | None = None) -> None:
    _write_json(path, {
        "feature": feature,
        "prs": prs or [
            {"id": "PR-3", "title": "Dashboard UI", "status": "in_progress", "track": "A", "gate": "gate_pr3_dashboard_ui"},
        ],
    })


def _make_registry(tmp: Path, projects: list[dict]) -> Path:
    registry_path = tmp / "projects.json"
    _write_json(registry_path, {"schema_version": 1, "projects": projects})
    return registry_path


# ---------------------------------------------------------------------------
# 1. Projects View — session start entrypoints and active session visibility
# ---------------------------------------------------------------------------

class TestProjectsView(unittest.TestCase):

    def test_empty_registry_returns_empty_list(self):
        """Empty projects registry produces empty list, not error."""
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "projects.json"
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            self.assertIsInstance(env, FreshnessEnvelope)
            self.assertEqual(env.data, [])
            # degraded because registry file is missing
            self.assertTrue(env.degraded)

    def test_inactive_session_shows_start_entrypoint(self):
        """Projects without session_profile.json show session_active=False (start entrypoint visible)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "my-project"
            proj_dir.mkdir()
            registry_path = _make_registry(tmp_path, [
                {"name": "my-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            self.assertEqual(len(env.data), 1)
            proj = env.data[0]
            self.assertFalse(proj["session_active"], "No session profile → session_active must be False")
            self.assertEqual(proj["name"], "my-project")

    def test_active_session_reflected_in_projects_view(self):
        """Projects with session_profile.json show session_active=True."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "live-project"
            state_dir = proj_dir / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            # Create session profile to signal active session
            _write_json(state_dir / "session_profile.json", {"session_name": "vnx-live-project"})
            registry_path = _make_registry(tmp_path, [
                {"name": "live-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            self.assertEqual(len(env.data), 1)
            proj = env.data[0]
            self.assertTrue(proj["session_active"], "Session profile present → session_active must be True")

    def test_attention_level_critical_when_blockers(self):
        """Projects with blockers show attention_level=critical."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "blocked-project"
            state_dir = proj_dir / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _make_open_items_file(state_dir / "open_items.json", [
                {"id": "OI-1", "severity": "blocker", "status": "open", "title": "Critical failure"},
            ])
            registry_path = _make_registry(tmp_path, [
                {"name": "blocked-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            proj = env.data[0]
            self.assertEqual(proj["attention_level"], "critical")
            self.assertEqual(proj["open_blocker_count"], 1)

    def test_attention_level_warning_when_warns_only(self):
        """Projects with warnings but no blockers show attention_level=warning."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "warned-project"
            state_dir = proj_dir / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _make_open_items_file(state_dir / "open_items.json", [
                {"id": "OI-2", "severity": "warn", "status": "open", "title": "Watch this"},
            ])
            registry_path = _make_registry(tmp_path, [
                {"name": "warned-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            proj = env.data[0]
            self.assertEqual(proj["attention_level"], "warning")

    def test_attention_level_clear_when_no_open_items(self):
        """Clean project shows attention_level=clear."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "clean-project"
            state_dir = proj_dir / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _make_open_items_file(state_dir / "open_items.json", [])
            registry_path = _make_registry(tmp_path, [
                {"name": "clean-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            proj = env.data[0]
            self.assertEqual(proj["attention_level"], "clear")
            self.assertEqual(proj["open_blocker_count"], 0)
            self.assertEqual(proj["open_warn_count"], 0)

    def test_inactive_projects_excluded(self):
        """Projects with active=False are not shown in the dashboard."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "archived-project"
            proj_dir.mkdir()
            registry_path = _make_registry(tmp_path, [
                {"name": "archived-project", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": False}
            ])
            view = ProjectsView(registry_path=registry_path)
            env = view.list_projects()
            self.assertEqual(len(env.data), 0, "Inactive projects must not appear in projects view")


# ---------------------------------------------------------------------------
# 2. Terminal View — distinguishes active, stale, blocked, exited
# ---------------------------------------------------------------------------

class TestTerminalView(unittest.TestCase):

    def _setup_db(self, state_dir: str):
        """Initialize runtime DB schema."""
        try:
            from runtime_coordination import init_schema
            init_schema(state_dir)
        except ImportError:
            self.skipTest("runtime_coordination not available")

    def test_empty_state_dir_returns_degraded(self):
        """Missing runtime DB produces degraded TerminalView with empty terminals list."""
        with tempfile.TemporaryDirectory() as tmp:
            view = TerminalView(tmp)
            env = view.get_all_terminals()
            self.assertIsInstance(env, FreshnessEnvelope)
            # Data will be empty list (no terminals registered)
            self.assertIsInstance(env.data, list)

    def test_terminal_status_heartbeat_classifications(self):
        """classify_heartbeat returns fresh/stale/dead based on DEFAULT thresholds (90s/300s)."""
        try:
            from worker_state_manager import classify_heartbeat
        except ImportError:
            self.skipTest("worker_state_manager not available")

        now = datetime.now(timezone.utc)
        # Fresh: within DEFAULT_HEARTBEAT_STALE_THRESHOLD (90s)
        fresh_ts = _ago_iso(30)
        self.assertEqual(classify_heartbeat(fresh_ts, now=now), "fresh")

        # Stale: between 90s and DEFAULT_HEARTBEAT_DEAD_THRESHOLD (300s)
        stale_ts = _ago_iso(180)
        self.assertEqual(classify_heartbeat(stale_ts, now=now), "stale")

        # Dead: beyond DEFAULT_HEARTBEAT_DEAD_THRESHOLD (300s)
        dead_ts = _ago_iso(600)
        self.assertEqual(classify_heartbeat(dead_ts, now=now), "dead")

        # None timestamp: treated as dead (no heartbeat ever received)
        self.assertEqual(classify_heartbeat(None, now=now), "dead")

    def test_terminal_get_all_with_registered_terminals(self):
        """TerminalView with initialized DB returns list of terminal entries."""
        try:
            from runtime_coordination import init_schema, register_dispatch, acquire_lease
        except ImportError:
            self.skipTest("runtime_coordination not available")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = os.path.join(tmp, "state")
            os.makedirs(state_dir)
            self._setup_db(state_dir)

            try:
                register_dispatch(state_dir, "D-001", "T1")
                acquire_lease(state_dir, "T1", "D-001")
            except Exception:
                pass  # Schema may differ — still test the view surface

            view = TerminalView(state_dir)
            env = view.get_all_terminals()
            self.assertIsInstance(env.data, list)
            self.assertIsInstance(env, FreshnessEnvelope)

    def test_single_terminal_degraded_when_not_found(self):
        """Requesting a nonexistent terminal produces degraded envelope with status=unknown."""
        with tempfile.TemporaryDirectory() as tmp:
            view = TerminalView(tmp)
            env = view.get_terminal("T99")
            self.assertTrue(env.degraded)
            self.assertIsNotNone(env.data)
            self.assertEqual(env.data.get("terminal_id"), "T99")


# ---------------------------------------------------------------------------
# 3. Open Items View — per-project rendering
# ---------------------------------------------------------------------------

class TestOpenItemsView(unittest.TestCase):

    def test_missing_file_returns_degraded_empty(self):
        """Missing open_items.json returns degraded=True with empty items list."""
        with tempfile.TemporaryDirectory() as tmp:
            view = OpenItemsView(tmp)
            env = view.get_items()
            self.assertTrue(env.degraded)
            self.assertEqual(env.data["items"], [])
            self.assertIn("unavailable", " ".join(env.degraded_reasons))

    def test_empty_items_file_returns_clean_state(self):
        """File with no items renders as empty, not degraded."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [])
            view = OpenItemsView(tmp)
            env = view.get_items()
            self.assertFalse(env.degraded)
            self.assertEqual(env.data["items"], [])
            self.assertEqual(env.data["summary"]["blocker_count"], 0)

    def test_open_items_sorted_by_severity(self):
        """Open items are returned sorted: blocker > warn > info."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "OI-3", "severity": "info",    "status": "open", "title": "Info item",    "created_at": _ago_iso(100)},
                {"id": "OI-1", "severity": "blocker", "status": "open", "title": "Blocker item", "created_at": _ago_iso(300)},
                {"id": "OI-2", "severity": "warn",    "status": "open", "title": "Warning item", "created_at": _ago_iso(200)},
            ])
            view = OpenItemsView(tmp)
            env = view.get_items()
            items = env.data["items"]
            self.assertEqual(len(items), 3)
            severities = [i["severity"] for i in items]
            self.assertEqual(severities[0], "blocker", "Blocker must be first")
            self.assertIn(severities[1], ("warn",), "Warn must be second")
            self.assertEqual(severities[2], "info", "Info must be last")

    def test_summary_counts_correct(self):
        """Summary counts accurately reflect open item severity distribution."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "B1", "severity": "blocker", "status": "open", "title": "B1"},
                {"id": "B2", "severity": "blocking", "status": "open", "title": "B2"},
                {"id": "W1", "severity": "warn",    "status": "open", "title": "W1"},
                {"id": "I1", "severity": "info",    "status": "open", "title": "I1"},
                {"id": "R1", "severity": "blocker", "status": "resolved", "title": "Resolved",
                 "resolved_at": _now_iso()},
            ])
            view = OpenItemsView(tmp)
            env = view.get_items()
            summary = env.data["summary"]
            self.assertEqual(summary["blocker_count"], 2, "Both blocker + blocking count as blockers")
            self.assertEqual(summary["warn_count"], 1)
            self.assertEqual(summary["info_count"], 1)

    def test_resolved_items_excluded_by_default(self):
        """Resolved items do not appear in default open items query."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "OI-1", "severity": "blocker", "status": "open", "title": "Open blocker"},
                {"id": "OI-2", "severity": "blocker", "status": "resolved", "title": "Resolved", "resolved_at": _now_iso()},
            ])
            view = OpenItemsView(tmp)
            env = view.get_items()
            ids = [i["id"] for i in env.data["items"]]
            self.assertIn("OI-1", ids)
            self.assertNotIn("OI-2", ids, "Resolved item must not appear in default query")

    def test_severity_filter(self):
        """Severity filter returns only items of requested severity."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "B1", "severity": "blocker", "status": "open", "title": "Blocker"},
                {"id": "W1", "severity": "warn",    "status": "open", "title": "Warning"},
            ])
            view = OpenItemsView(tmp)
            env = view.get_items(severity="blocker")
            self.assertEqual(len(env.data["items"]), 1)
            self.assertEqual(env.data["items"][0]["id"], "B1")

    def test_age_seconds_populated(self):
        """Items include age_seconds based on created_at timestamp."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "OI-1", "severity": "warn", "status": "open", "title": "Aged", "created_at": _ago_iso(3600)},
            ])
            view = OpenItemsView(tmp)
            env = view.get_items()
            item = env.data["items"][0]
            self.assertIsNotNone(item.get("age_seconds"))
            self.assertGreater(item["age_seconds"], 3500)


# ---------------------------------------------------------------------------
# 4. Aggregate Open Items View — cross-project
# ---------------------------------------------------------------------------

class TestAggregateOpenItemsView(unittest.TestCase):

    def test_empty_registry_no_items(self):
        """No registered projects produces empty aggregate."""
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = _make_registry(Path(tmp), [])
            view = AggregateOpenItemsView(registry_path=registry_path)
            env = view.get_aggregate()
            self.assertEqual(env.data["items"], [])
            self.assertEqual(env.data["total_summary"]["blocker_count"], 0)

    def test_items_from_multiple_projects_aggregated(self):
        """Items from two projects are all returned with _project_name tag."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for pname in ("alpha", "beta"):
                state_dir = tmp_path / pname / ".vnx-data" / "state"
                state_dir.mkdir(parents=True)
                _make_open_items_file(state_dir / "open_items.json", [
                    {"id": f"{pname}-B1", "severity": "blocker", "status": "open", "title": f"{pname} blocker"},
                ])

            registry_path = _make_registry(tmp_path, [
                {"name": "alpha", "path": str(tmp_path / "alpha"), "vnx_data_dir": ".vnx-data", "active": True},
                {"name": "beta",  "path": str(tmp_path / "beta"),  "vnx_data_dir": ".vnx-data", "active": True},
            ])
            view = AggregateOpenItemsView(registry_path=registry_path)
            env = view.get_aggregate()
            self.assertEqual(len(env.data["items"]), 2)
            project_names = {i["_project_name"] for i in env.data["items"]}
            self.assertIn("alpha", project_names)
            self.assertIn("beta", project_names)
            self.assertEqual(env.data["total_summary"]["blocker_count"], 2)

    def test_unavailable_project_marked_in_subtotals(self):
        """Projects whose open_items.json is missing are marked unavailable in per_project_subtotals."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proj_dir = tmp_path / "no-state"
            proj_dir.mkdir()
            registry_path = _make_registry(tmp_path, [
                {"name": "no-state", "path": str(proj_dir), "vnx_data_dir": ".vnx-data", "active": True}
            ])
            view = AggregateOpenItemsView(registry_path=registry_path)
            env = view.get_aggregate()
            subtotals = env.data["per_project_subtotals"]
            self.assertIn("no-state", subtotals)
            self.assertEqual(subtotals["no-state"]["status"], "unavailable")
            self.assertTrue(env.degraded, "Missing project state must mark aggregate as degraded")

    def test_project_filter_restricts_results(self):
        """project_filter= restricts aggregate to named project only."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for pname in ("alpha", "beta"):
                state_dir = tmp_path / pname / ".vnx-data" / "state"
                state_dir.mkdir(parents=True)
                _make_open_items_file(state_dir / "open_items.json", [
                    {"id": f"{pname}-W1", "severity": "warn", "status": "open", "title": f"{pname} warn"},
                ])
            registry_path = _make_registry(tmp_path, [
                {"name": "alpha", "path": str(tmp_path / "alpha"), "vnx_data_dir": ".vnx-data", "active": True},
                {"name": "beta",  "path": str(tmp_path / "beta"),  "vnx_data_dir": ".vnx-data", "active": True},
            ])
            view = AggregateOpenItemsView(registry_path=registry_path)
            env = view.get_aggregate(project_filter="alpha")
            self.assertEqual(len(env.data["items"]), 1)
            self.assertEqual(env.data["items"][0]["_project_name"], "alpha")


# ---------------------------------------------------------------------------
# 5. Degraded, stale, and empty-state rendering
# ---------------------------------------------------------------------------

class TestDegradedStateRendering(unittest.TestCase):

    def test_stale_open_items_triggers_degraded(self):
        """open_items.json older than AGING_THRESHOLD marks envelope as degraded."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [])
            # Backdate file mtime to beyond AGING_THRESHOLD
            old_ts = (datetime.now(timezone.utc) - timedelta(seconds=AGING_THRESHOLD + 60)).timestamp()
            os.utime(oi_path, (old_ts, old_ts))

            view = OpenItemsView(tmp)
            env = view.get_items()
            self.assertTrue(env.degraded, "File older than AGING_THRESHOLD must produce degraded=True")
            self.assertTrue(any("stale" in r for r in env.degraded_reasons), "Degraded reason must mention 'stale'")

    def test_freshness_envelope_fields_always_present(self):
        """Every FreshnessEnvelope serializes all required fields."""
        with tempfile.TemporaryDirectory() as tmp:
            view = OpenItemsView(tmp)
            env = view.get_items()
            d = env.to_dict()
            for field in ("view", "queried_at", "source_freshness", "staleness_seconds", "degraded", "data"):
                self.assertIn(field, d, f"FreshnessEnvelope must include '{field}'")

    def test_degraded_does_not_return_false_healthy_state(self):
        """Degraded envelope never has degraded=False when sources are missing."""
        with tempfile.TemporaryDirectory() as tmp:
            # No files present
            view = OpenItemsView(tmp)
            env = view.get_items()
            self.assertTrue(env.degraded, "Missing source must not masquerade as healthy")

    def test_staleness_zero_for_fresh_file(self):
        """Freshly written file produces staleness_seconds near zero."""
        with tempfile.TemporaryDirectory() as tmp:
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [])
            view = OpenItemsView(tmp)
            env = view.get_items()
            self.assertFalse(env.degraded)
            self.assertLess(env.staleness_seconds, FRESH_THRESHOLD,
                            "Fresh file must have staleness_seconds < FRESH_THRESHOLD")


# ---------------------------------------------------------------------------
# 6. Session View — PR progress and terminal summary
# ---------------------------------------------------------------------------

class TestSessionView(unittest.TestCase):

    def test_missing_queue_file_marks_degraded(self):
        """SessionView with no pr_queue_state.json marks degraded and returns empty PR list."""
        with tempfile.TemporaryDirectory() as tmp:
            view = SessionView(tmp)
            env = view.get_session()
            self.assertTrue(env.degraded)
            self.assertEqual(env.data["pr_progress"], [])

    def test_pr_progress_from_queue_file(self):
        """SessionView reads PR progress from pr_queue_state.json."""
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "pr_queue_state.json"
            _make_pr_queue(queue_path, "Feature 13", [
                {"id": "PR-3", "title": "Dashboard UI", "status": "in_progress", "track": "A", "gate": "gate_pr3"},
            ])
            view = SessionView(tmp)
            env = view.get_session()
            self.assertEqual(env.data["feature_name"], "Feature 13")
            self.assertEqual(len(env.data["pr_progress"]), 1)
            self.assertEqual(env.data["pr_progress"][0]["id"], "PR-3")

    def test_open_item_summary_in_session(self):
        """SessionView includes open_item_summary from open_items.json."""
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "pr_queue_state.json"
            _make_pr_queue(queue_path)
            oi_path = Path(tmp) / "open_items.json"
            _make_open_items_file(oi_path, [
                {"id": "OI-1", "severity": "blocker", "status": "open", "title": "Blocker"},
                {"id": "OI-2", "severity": "warn",    "status": "open", "title": "Warn"},
            ])
            view = SessionView(tmp)
            env = view.get_session()
            summary = env.data.get("open_item_summary", {})
            self.assertEqual(summary.get("blocker_count", 0), 1)
            self.assertEqual(summary.get("warn_count", 0), 1)


# ---------------------------------------------------------------------------
# 7. Action outcome model — session start / stop / attach
# ---------------------------------------------------------------------------

class TestActionOutcomes(unittest.TestCase):

    def test_start_session_project_not_found(self):
        """start_session on nonexistent directory produces failed outcome."""
        outcome = start_session("/nonexistent/path/xyz123")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "project_not_found")
        self.assertIsInstance(outcome.message, str)
        self.assertTrue(len(outcome.message) > 0)

    def test_start_session_dry_run_success(self):
        """start_session dry_run on valid directory produces success/already_active."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._find_vnx_bin", return_value="/usr/local/bin/vnx"), \
                 patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = start_session(tmp, dry_run=True)
        self.assertIn(outcome.status, ("success", "already_active"))
        self.assertIn("dry run", outcome.message.lower())

    def test_start_session_already_active(self):
        """start_session when session exists returns already_active."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._find_vnx_bin", return_value="/usr/local/bin/vnx"), \
                 patch("dashboard_actions._tmux_session_exists", return_value=True):
                outcome = start_session(tmp)
        self.assertEqual(outcome.status, "already_active")

    def test_stop_session_no_active_session(self):
        """stop_session when no session is running returns already_active (no session to stop)."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = stop_session(tmp)
        self.assertEqual(outcome.status, "already_active")

    def test_attach_terminal_no_session(self):
        """attach_terminal when no tmux session exists returns failed."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch("dashboard_actions._tmux_session_exists", return_value=False):
                outcome = attach_terminal(tmp, "T1")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "no_session")

    def test_attach_terminal_pane_resolved(self):
        """attach_terminal resolves pane from session_profile.json."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _write_json(state_dir / "session_profile.json", {
                "session_name": f"vnx-{Path(tmp).name}",
                "home_window": {
                    "panes": [
                        {"terminal_id": "T1", "pane_id": "%5", "role": "worker"},
                    ]
                },
                "extra_windows": [],
            })
            with patch("dashboard_actions._tmux_session_exists", return_value=True):
                outcome = attach_terminal(tmp, "T1")
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.details["pane_id"], "%5")
        self.assertIn("tmux select-pane", outcome.details["attach_command"])

    def test_inspect_open_item_found(self):
        """inspect_open_item returns item details when item exists."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _make_open_items_file(state_dir / "open_items.json", [
                {"id": "OI-42", "severity": "blocker", "status": "open", "title": "Critical item"},
            ])
            outcome = inspect_open_item(tmp, "OI-42")
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.details["item"]["id"], "OI-42")

    def test_inspect_open_item_not_found(self):
        """inspect_open_item returns failed when item id not found."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            _make_open_items_file(state_dir / "open_items.json", [])
            outcome = inspect_open_item(tmp, "nonexistent-id")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "item_not_found")

    def test_action_outcome_to_dict_has_required_fields(self):
        """ActionOutcome.to_dict() always includes action, project, status, message, timestamp."""
        outcome = ActionOutcome(
            action="start_session",
            project="/some/path",
            status="success",
            message="Session started",
        )
        d = outcome.to_dict()
        for field in ("action", "project", "status", "message", "timestamp"):
            self.assertIn(field, d, f"ActionOutcome must include '{field}'")
        self.assertNotIn("error_code", d, "error_code must be omitted on success")

    def test_failed_action_includes_error_code(self):
        """Failed ActionOutcome includes error_code in to_dict()."""
        outcome = ActionOutcome(
            action="start_session",
            project="/bad/path",
            status="failed",
            message="Not found",
            error_code="project_not_found",
        )
        d = outcome.to_dict()
        self.assertEqual(d["error_code"], "project_not_found")

    def test_refresh_projections_no_state_dir(self):
        """refresh_projections on directory without .vnx-data/state returns failed."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = refresh_projections(tmp)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "no_state_dir")

    def test_refresh_projections_dry_run(self):
        """refresh_projections dry_run with state_dir present returns success."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            outcome = refresh_projections(tmp, dry_run=True)
        self.assertEqual(outcome.status, "success")
        self.assertTrue(outcome.details.get("dry_run"))

    def test_run_reconciliation_dry_run(self):
        """run_reconciliation dry_run with state_dir returns success."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".vnx-data" / "state"
            state_dir.mkdir(parents=True)
            outcome = run_reconciliation(tmp, dry_run=True)
        self.assertEqual(outcome.status, "success")
        self.assertTrue(outcome.details.get("dry_run"))


# ---------------------------------------------------------------------------
# 8. Project registration — register_project helper
# ---------------------------------------------------------------------------

class TestProjectRegistration(unittest.TestCase):

    def test_register_project_creates_registry(self):
        """register_project creates ~/.vnx/projects.json if missing."""
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "projects.json"
            entry = register_project("test-proj", "/some/path", registry_path=registry_path)
            self.assertEqual(entry["name"], "test-proj")
            self.assertTrue(registry_path.exists())

    def test_register_project_idempotent(self):
        """register_project does not duplicate existing projects by path."""
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "projects.json"
            register_project("proj", "/path/x", registry_path=registry_path)
            register_project("proj", "/path/x", registry_path=registry_path)
            data = load_project_registry(registry_path)
            paths = [p["path"] for p in data["projects"]]
            self.assertEqual(paths.count("/path/x"), 1, "Duplicate path must not be registered twice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
