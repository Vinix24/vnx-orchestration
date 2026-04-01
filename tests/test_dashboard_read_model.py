#!/usr/bin/env python3
"""
Tests for dashboard_read_model.py (Feature 13, PR-1)

Quality gate coverage (gate_pr1_dashboard_read_model):
  - All dashboard read-model projection tests pass
  - Project session, terminal runtime, and open-item projections are queryable
  - Aggregate open-item projection across projects is available under test
  - Degraded or mismatched projection states produce operator-readable diagnostics
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from runtime_coordination import (
    get_connection,
    init_schema,
    register_dispatch,
    acquire_lease,
)
from worker_state_manager import WorkerStateManager

from dashboard_read_model import (
    FRESH_THRESHOLD,
    AGING_THRESHOLD,
    FreshnessEnvelope,
    TerminalView,
    OpenItemsView,
    AggregateOpenItemsView,
    SessionView,
    ProjectsView,
    load_project_registry,
    register_project,
    _compute_freshness,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_state(tmp: str) -> str:
    """Initialize runtime DB in state_dir and return state_dir path."""
    state_dir = os.path.join(tmp, "state")
    os.makedirs(state_dir, exist_ok=True)
    init_schema(state_dir)
    return state_dir


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _make_open_items(items: list) -> dict:
    return {"schema_version": "1.0", "items": items, "next_id": len(items) + 1}


def _open_item(severity="warn", status="open", title="test", **kwargs):
    item = {
        "id": f"OI-{id(title) % 1000:03d}",
        "status": status,
        "severity": severity,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "details": "",
        "origin_dispatch_id": "d-test",
    }
    item.update(kwargs)
    return item


def _setup_project_tree(base: str, name: str, *, open_items=None, queue=None):
    """Create a minimal project directory structure."""
    proj_dir = os.path.join(base, name)
    state_dir = os.path.join(proj_dir, ".vnx-data", "state")
    os.makedirs(state_dir, exist_ok=True)
    if open_items is not None:
        _write_json(Path(state_dir) / "open_items.json", open_items)
    if queue is not None:
        _write_json(Path(state_dir) / "pr_queue_state.json", queue)
    return proj_dir


# ===========================================================================
# Test: Freshness envelope (§3.4)
# ===========================================================================

class TestFreshnessEnvelope(unittest.TestCase):

    def test_fresh_sources(self):
        now = datetime.now(timezone.utc)
        sources = {"a.json": now.isoformat()}
        staleness, degraded, reasons = _compute_freshness(sources, now)
        self.assertLess(staleness, 1)
        self.assertFalse(degraded)
        self.assertEqual(reasons, [])

    def test_stale_source_degrades(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=400)).isoformat()
        sources = {"a.json": old}
        staleness, degraded, reasons = _compute_freshness(sources, now)
        self.assertTrue(degraded)
        self.assertIn("a.json: stale", reasons[0])

    def test_missing_source_degrades(self):
        now = datetime.now(timezone.utc)
        sources = {"a.json": None}
        staleness, degraded, reasons = _compute_freshness(sources, now)
        self.assertTrue(degraded)
        self.assertIn("unavailable", reasons[0])

    def test_envelope_to_dict(self):
        env = FreshnessEnvelope(
            view="TestView",
            queried_at="2026-04-01T20:00:00Z",
            source_freshness={"a": "2026-04-01T20:00:00Z"},
            staleness_seconds=1.5,
            degraded=False,
            data={"key": "value"},
        )
        d = env.to_dict()
        self.assertEqual(d["view"], "TestView")
        self.assertEqual(d["data"]["key"], "value")
        self.assertFalse(d["degraded"])


# ===========================================================================
# Test: Project Registry (§3.2)
# ===========================================================================

class TestProjectRegistry(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reg_path = Path(self._tmp.name) / "projects.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_registry(self):
        data = load_project_registry(self.reg_path)
        self.assertEqual(data["projects"], [])

    def test_register_project(self):
        entry = register_project("test-project", "/tmp/test", registry_path=self.reg_path)
        self.assertEqual(entry["name"], "test-project")
        self.assertTrue(entry["active"])

        data = load_project_registry(self.reg_path)
        self.assertEqual(len(data["projects"]), 1)

    def test_idempotent_registration(self):
        register_project("test-project", "/tmp/test", registry_path=self.reg_path)
        register_project("test-project", "/tmp/test", registry_path=self.reg_path)
        data = load_project_registry(self.reg_path)
        self.assertEqual(len(data["projects"]), 1)


# ===========================================================================
# Test: TerminalView (§2.4)
# ===========================================================================

class TestTerminalView(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_idle_terminal(self):
        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T1")
        self.assertEqual(env.view, "TerminalView")
        self.assertIn("queried_at", env.to_dict())
        data = env.data
        self.assertEqual(data["terminal_id"], "T1")
        self.assertEqual(data["lease_state"], "idle")
        self.assertEqual(data["status"], "idle")

    def test_working_terminal(self):
        with get_connection(self.state_dir) as conn:
            register_dispatch(conn, dispatch_id="d-001", terminal_id="T1", track="B")
            acquire_lease(conn, terminal_id="T1", dispatch_id="d-001")
            conn.commit()
        mgr = WorkerStateManager(self.state_dir, auto_init=False)
        mgr.initialize("T1", "d-001")
        mgr.transition("T1", "working")

        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T1")
        self.assertEqual(env.data["worker_state"], "working")
        self.assertEqual(env.data["status"], "working")
        self.assertEqual(env.data["dispatch_id"], "d-001")

    def test_all_terminals(self):
        tv = TerminalView(self.state_dir)
        env = tv.get_all_terminals()
        self.assertIsInstance(env.data, list)
        self.assertEqual(len(env.data), 3)  # T1, T2, T3
        tids = [t["terminal_id"] for t in env.data]
        self.assertIn("T1", tids)

    def test_context_pressure(self):
        # Write context window file
        ctx_path = Path(self.state_dir) / "context_window_T1.json"
        _write_json(ctx_path, {"remaining_pct": 15, "tokens_used": 85000})
        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T1")
        self.assertIn("context_pressure", env.data)
        self.assertTrue(env.data["context_pressure"]["warning"])

    def test_missing_terminal(self):
        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T99")
        self.assertTrue(env.degraded)
        self.assertEqual(env.data["status"], "unknown")

    def test_freshness_envelope_present(self):
        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T1")
        d = env.to_dict()
        self.assertIn("source_freshness", d)
        self.assertIn("staleness_seconds", d)
        self.assertIn("degraded", d)


# ===========================================================================
# Test: OpenItemsView (§2.5)
# ===========================================================================

class TestOpenItemsView(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_open_items(self):
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items([]))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertEqual(len(env.data["items"]), 0)
        self.assertEqual(env.data["summary"]["blocker_count"], 0)

    def test_severity_summary(self):
        items = [
            _open_item(severity="blocker", title="b1"),
            _open_item(severity="blocker", title="b2"),
            _open_item(severity="warn", title="w1"),
            _open_item(severity="info", title="i1"),
        ]
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items(items))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertEqual(env.data["summary"]["blocker_count"], 2)
        self.assertEqual(env.data["summary"]["warn_count"], 1)
        self.assertEqual(env.data["summary"]["info_count"], 1)

    def test_severity_sort_order(self):
        """Blockers first, then warnings, then info."""
        items = [
            _open_item(severity="info", title="i1"),
            _open_item(severity="blocker", title="b1"),
            _open_item(severity="warn", title="w1"),
        ]
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items(items))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        severities = [i["severity"] for i in env.data["items"]]
        self.assertEqual(severities, ["blocker", "warn", "info"])

    def test_resolved_items_excluded(self):
        items = [
            _open_item(severity="blocker", title="resolved", status="done",
                       closed_at="2026-04-01T12:00:00Z"),
            _open_item(severity="warn", title="still open"),
        ]
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items(items))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertEqual(len(env.data["items"]), 1)
        self.assertEqual(env.data["items"][0]["title"], "still open")

    def test_missing_file_degrades(self):
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertTrue(env.degraded)
        self.assertIn("unavailable", env.degraded_reasons[0])

    def test_age_enrichment(self):
        items = [_open_item(severity="warn", title="aged")]
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items(items))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertIn("age_seconds", env.data["items"][0])

    def test_severity_filter(self):
        items = [
            _open_item(severity="blocker", title="b1"),
            _open_item(severity="warn", title="w1"),
        ]
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items(items))
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items(severity="blocker")
        self.assertEqual(len(env.data["items"]), 1)


# ===========================================================================
# Test: AggregateOpenItemsView (§2.6, §7)
# ===========================================================================

class TestAggregateOpenItemsView(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.reg_path = Path(self.base) / "projects.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_aggregate_across_projects(self):
        """V-1: Every registered project contributes to aggregate."""
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="blocker", title="p1-blocker"),
        ]))
        p2 = _setup_project_tree(self.base, "proj-b", open_items=_make_open_items([
            _open_item(severity="warn", title="p2-warn"),
            _open_item(severity="info", title="p2-info"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)
        register_project("proj-b", p2, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate()
        self.assertEqual(env.data["total_summary"]["blocker_count"], 1)
        self.assertEqual(env.data["total_summary"]["warn_count"], 1)
        self.assertEqual(env.data["total_summary"]["info_count"], 1)
        self.assertEqual(len(env.data["items"]), 3)

    def test_per_project_subtotals(self):
        """V-5: Aggregate shows per-project subtotals."""
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="blocker", title="b1"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate()
        self.assertIn("proj-a", env.data["per_project_subtotals"])
        self.assertEqual(env.data["per_project_subtotals"]["proj-a"]["blocker_count"], 1)

    def test_unavailable_project_shows_status(self):
        """V-6: Unavailable project shows 'data unavailable', not zero."""
        p1 = os.path.join(self.base, "missing-proj")
        os.makedirs(p1, exist_ok=True)
        register_project("missing", p1, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate()
        self.assertEqual(env.data["per_project_subtotals"]["missing"]["status"], "unavailable")
        self.assertTrue(env.degraded)

    def test_project_filter(self):
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="blocker", title="b1"),
        ]))
        p2 = _setup_project_tree(self.base, "proj-b", open_items=_make_open_items([
            _open_item(severity="warn", title="w1"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)
        register_project("proj-b", p2, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate(project_filter="proj-a")
        self.assertEqual(len(env.data["items"]), 1)
        self.assertEqual(env.data["items"][0]["title"], "b1")

    def test_sort_by_severity_then_age(self):
        """V-2: Items sorted blocker > warn > info, then oldest first."""
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="info", title="new-info", created_at=new),
            _open_item(severity="blocker", title="old-blocker", created_at=old),
            _open_item(severity="blocker", title="new-blocker", created_at=new),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate()
        titles = [i["title"] for i in env.data["items"]]
        # Blockers first (old before new), then info
        self.assertEqual(titles[0], "old-blocker")
        self.assertEqual(titles[1], "new-blocker")
        self.assertEqual(titles[2], "new-info")

    def test_items_carry_project_name(self):
        """V-3: Each item shows its project name."""
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="warn", title="test"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        agg = AggregateOpenItemsView(self.reg_path)
        env = agg.get_aggregate()
        self.assertEqual(env.data["items"][0]["_project_name"], "proj-a")


# ===========================================================================
# Test: SessionView (§2.3)
# ===========================================================================

class TestSessionView(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = _setup_state(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_session_with_queue_data(self):
        queue = {"feature": "Test Feature", "prs": [
            {"id": "PR-0", "title": "Contract", "status": "completed", "track": "C", "gate": "g0"},
            {"id": "PR-1", "title": "Impl", "status": "in_progress", "track": "B", "gate": "g1"},
        ]}
        _write_json(Path(self.state_dir) / "pr_queue_state.json", queue)
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items([]))

        sv = SessionView(self.state_dir)
        env = sv.get_session()
        self.assertEqual(env.data["feature_name"], "Test Feature")
        self.assertEqual(len(env.data["pr_progress"]), 2)

    def test_session_includes_terminals(self):
        _write_json(Path(self.state_dir) / "pr_queue_state.json", {"feature": "F", "prs": []})
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items([]))

        sv = SessionView(self.state_dir)
        env = sv.get_session()
        self.assertIn("terminal_states", env.data)
        self.assertEqual(len(env.data["terminal_states"]), 3)

    def test_session_missing_queue_degrades(self):
        sv = SessionView(self.state_dir)
        env = sv.get_session()
        self.assertTrue(env.degraded)
        self.assertIsNone(env.data["feature_name"])

    def test_session_includes_open_item_summary(self):
        _write_json(Path(self.state_dir) / "pr_queue_state.json", {"feature": "F", "prs": []})
        _write_json(Path(self.state_dir) / "open_items.json", _make_open_items([
            _open_item(severity="blocker", title="b1"),
        ]))
        sv = SessionView(self.state_dir)
        env = sv.get_session()
        self.assertEqual(env.data["open_item_summary"]["blocker_count"], 1)


# ===========================================================================
# Test: ProjectsView (§2.2)
# ===========================================================================

class TestProjectsView(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.reg_path = Path(self.base) / "projects.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_registry(self):
        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        self.assertEqual(env.data, [])

    def test_project_with_attention(self):
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="blocker", title="b1"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        self.assertEqual(len(env.data), 1)
        self.assertEqual(env.data[0]["attention_level"], "critical")
        self.assertEqual(env.data[0]["open_blocker_count"], 1)

    def test_clear_attention(self):
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        self.assertEqual(env.data[0]["attention_level"], "clear")

    def test_warning_attention(self):
        p1 = _setup_project_tree(self.base, "proj-a", open_items=_make_open_items([
            _open_item(severity="warn", title="w1"),
        ]))
        register_project("proj-a", p1, registry_path=self.reg_path)

        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        self.assertEqual(env.data[0]["attention_level"], "warning")

    def test_feature_name_from_queue(self):
        p1 = _setup_project_tree(self.base, "proj-a",
                                  open_items=_make_open_items([]),
                                  queue={"feature": "Feature 13", "prs": []})
        register_project("proj-a", p1, registry_path=self.reg_path)

        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        self.assertEqual(env.data[0]["active_feature"], "Feature 13")

    def test_freshness_envelope_on_projects(self):
        pv = ProjectsView(self.reg_path)
        env = pv.list_projects()
        d = env.to_dict()
        self.assertIn("source_freshness", d)
        self.assertIn("staleness_seconds", d)


# ===========================================================================
# Test: Degraded state diagnostics (§5.1)
# ===========================================================================

class TestDegradedDiagnostics(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        os.makedirs(self.state_dir, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_db_degrades_terminal_view(self):
        """DS-1: Missing data rendered as explicitly missing, not empty."""
        tv = TerminalView(self.state_dir)
        env = tv.get_terminal("T1")
        self.assertTrue(env.degraded)
        self.assertEqual(env.data["status"], "unknown")

    def test_degraded_reasons_are_operator_readable(self):
        """DS-2: Degraded reasons are human-readable strings."""
        oi = OpenItemsView(self.state_dir)
        env = oi.get_items()
        self.assertTrue(env.degraded)
        for reason in env.degraded_reasons:
            self.assertIsInstance(reason, str)
            self.assertGreater(len(reason), 5)

    def test_stale_source_includes_age(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=400)).isoformat()
        sources = {"test.json": old}
        _, degraded, reasons = _compute_freshness(sources, now)
        self.assertTrue(degraded)
        self.assertIn("400", reasons[0])


if __name__ == "__main__":
    unittest.main()
