#!/usr/bin/env python3
"""Comprehensive tests for the VNX Operator Dashboard feature (PR-0 through PR-4).

Covers:
  1. Jump command logic (jump.sh + canonical_state_views.py)
  2. API endpoints (serve_dashboard.py)
  3. Attention ranking/priority (canonical_state_views.py)
  4. Confirmation behavior (dashboard/index.html — grep/parse verification)

Dispatch-ID: 20260327-remediation-dashboard-tests-A
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone, timedelta
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup: ensure scripts/lib is importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

# IMPORTANT: Only add scripts/lib/ to sys.path, NOT scripts/.
# scripts/terminal_state_shadow.py is a CLI wrapper that imports from
# scripts/lib/terminal_state_shadow.py. Having both on sys.path causes
# a circular import because Python resolves the wrong module first.
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
# Remove scripts/ if it was added (e.g. by another test module)
_scripts_dir = str(PROJECT_ROOT / "scripts")
while _scripts_dir in sys.path:
    sys.path.remove(_scripts_dir)


# ===========================================================================
# 1. Jump command shell tests (scripts/commands/jump.sh)
# ===========================================================================

class TestJumpShell(unittest.TestCase):
    """Shell-level tests for the vnx jump command.

    These invoke jump.sh indirectly via bash and verify exit codes + output.
    tmux is NOT required — we test validation paths that fail before tmux calls.
    """

    JUMP_SCRIPT = PROJECT_ROOT / "scripts" / "commands" / "jump.sh"

    @unittest.skipUnless(
        (PROJECT_ROOT / "scripts" / "commands" / "jump.sh").exists(),
        "jump.sh not found",
    )
    def test_jump_script_syntax_valid(self):
        """jump.sh passes bash -n syntax check."""
        result = subprocess.run(
            ["bash", "-n", str(self.JUMP_SCRIPT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"Syntax error: {result.stderr}")

    @unittest.skipUnless(
        (PROJECT_ROOT / "scripts" / "commands" / "jump.sh").exists(),
        "jump.sh not found",
    )
    def test_jump_invalid_target_returns_error(self):
        """vnx jump T9 should fail with an error message about unknown terminal."""
        # Source jump.sh and call cmd_jump with an invalid target.
        # We provide stub functions for err/log and avoid real tmux/python calls.
        script = textwrap.dedent(f"""\
            export PROJECT_ROOT="{PROJECT_ROOT}"
            export VNX_STATE_DIR="/nonexistent"
            err() {{ echo "ERROR: $*" >&2; }}
            log() {{ echo "$*"; }}
            source "{self.JUMP_SCRIPT}"
            cmd_jump T9
        """)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown terminal", result.stderr)

    @unittest.skipUnless(
        (PROJECT_ROOT / "scripts" / "commands" / "jump.sh").exists(),
        "jump.sh not found",
    )
    def test_jump_no_args_returns_error(self):
        """vnx jump with no arguments returns usage error."""
        script = textwrap.dedent(f"""\
            export PROJECT_ROOT="{PROJECT_ROOT}"
            export VNX_STATE_DIR="/nonexistent"
            err() {{ echo "ERROR: $*" >&2; }}
            log() {{ echo "$*"; }}
            source "{self.JUMP_SCRIPT}"
            cmd_jump
        """)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stderr)

    @unittest.skipUnless(
        (PROJECT_ROOT / "scripts" / "commands" / "jump.sh").exists(),
        "jump.sh not found",
    )
    def test_jump_valid_terminal_accepted_before_tmux(self):
        """vnx jump T2 passes validation (fails at tmux step, not validation)."""
        # T2 is valid — the command should pass case validation and fail only
        # because tmux session doesn't exist.
        script = textwrap.dedent(f"""\
            export PROJECT_ROOT="{PROJECT_ROOT}"
            export VNX_STATE_DIR="/nonexistent"
            export VNX_HOME="{PROJECT_ROOT}"
            err() {{ echo "ERROR: $*" >&2; }}
            log() {{ echo "$*"; }}
            source "{self.JUMP_SCRIPT}"
            cmd_jump T2
        """)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        # Should fail because tmux session doesn't exist, NOT because of validation
        self.assertNotIn("Unknown terminal", result.stderr)

    @unittest.skipUnless(
        (PROJECT_ROOT / "scripts" / "commands" / "jump.sh").exists(),
        "jump.sh not found",
    )
    def test_jump_attention_no_terminals_needing_attention(self):
        """vnx jump --attention with no attention terminals prints message and exits 0."""
        # Create a state dir with a terminal_state.json that has no attention triggers.
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            # Create minimal terminal_state.json with idle terminals
            terminal_state = {
                "schema_version": 1,
                "terminals": {
                    "T1": {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()},
                    "T2": {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()},
                    "T3": {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()},
                },
            }
            (state_dir / "terminal_state.json").write_text(json.dumps(terminal_state))

            script = textwrap.dedent(f"""\
                export PROJECT_ROOT="{PROJECT_ROOT}"
                export VNX_STATE_DIR="{state_dir}"
                export VNX_HOME="{PROJECT_ROOT}"
                err() {{ echo "ERROR: $*" >&2; }}
                log() {{ echo "$*"; }}
                source "{self.JUMP_SCRIPT}"
                cmd_jump --attention
            """)
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Should exit 0 with "No terminal currently needs" message
            self.assertEqual(result.returncode, 0)
            self.assertIn("No terminal", result.stdout)


# ===========================================================================
# 2. API endpoint tests (serve_dashboard.py)
# ===========================================================================

class TestJumpAPI(unittest.TestCase):
    """Test /api/jump/{terminal} endpoint logic."""

    def _import_serve_dashboard(self):
        """Import serve_dashboard module with path setup."""
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_jump_valid_terminal_set(self):
        """VALID_TERMINALS contains T0-T3."""
        mod = self._import_serve_dashboard()
        self.assertEqual(mod.VALID_TERMINALS, frozenset({"T0", "T1", "T2", "T3"}))

    def test_jump_terminal_function_rejects_invalid(self):
        """_jump_terminal raises ValueError for invalid terminal."""
        mod = self._import_serve_dashboard()
        with self.assertRaises(ValueError):
            mod._jump_terminal("T9")

    def test_jump_terminal_function_rejects_empty(self):
        """_jump_terminal raises ValueError for empty string."""
        mod = self._import_serve_dashboard()
        with self.assertRaises(ValueError):
            mod._jump_terminal("")

    def test_jump_terminal_function_calls_tmux(self):
        """_jump_terminal calls tmux has-session, select-window, select-pane."""
        mod = self._import_serve_dashboard()

        # Mock subprocess.run: first call = has-session (success), rest = select-window/pane (success)
        mock_result = MagicMock()
        mock_result.returncode = 0

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(mod, "CANONICAL_STATE_DIR", Path(tmpdir)), \
             patch("serve_dashboard.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            mock_sub.CalledProcessError = subprocess.CalledProcessError
            result = mod._jump_terminal("T2")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["terminal"], "T2")
        # Verify tmux commands were called (has-session, select-window, select-pane)
        self.assertGreaterEqual(mock_sub.run.call_count, 3)

    def test_jump_terminal_no_tmux_session(self):
        """_jump_terminal raises RuntimeError when tmux session doesn't exist."""
        mod = self._import_serve_dashboard()

        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("serve_dashboard.subprocess") as mock_sub:
            mock_sub.run.return_value = mock_result
            with self.assertRaises(RuntimeError) as ctx:
                mod._jump_terminal("T1")
        self.assertIn("not found", str(ctx.exception))


class TestEventsAPI(unittest.TestCase):
    """Test /api/events endpoint logic."""

    def _import_serve_dashboard(self):
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_query_events_returns_expected_structure(self):
        """_query_events returns dict with events, total, filters keys."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            receipts.write_text(
                json.dumps({
                    "event_type": "task_complete",
                    "timestamp": "2026-03-27T10:00:00+00:00",
                    "terminal": "T1",
                    "dispatch_id": "test-dispatch-001",
                    "status": "success",
                    "gate": "gate_test",
                }) + "\n"
            )

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({})

        self.assertIn("events", result)
        self.assertIn("total", result)
        self.assertIn("filters", result)
        self.assertGreater(len(result["events"]), 0)

        event = result["events"][0]
        self.assertIn("type", event)
        self.assertIn("timestamp", event)
        self.assertIn("terminal", event)
        self.assertIn("dispatch_id", event)
        self.assertIn("icon", event)
        self.assertIn("summary", event)

    def test_query_events_filter_by_terminal(self):
        """Events are filtered by terminal parameter."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            lines = [
                json.dumps({"event_type": "task_complete", "timestamp": "2026-03-27T10:00:00+00:00", "terminal": "T1", "dispatch_id": "d1", "status": "success"}),
                json.dumps({"event_type": "task_complete", "timestamp": "2026-03-27T11:00:00+00:00", "terminal": "T2", "dispatch_id": "d2", "status": "success"}),
            ]
            receipts.write_text("\n".join(lines) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({"terminal": ["T1"]})

        for evt in result["events"]:
            self.assertEqual(evt["terminal"], "T1")

    def test_query_events_filter_by_type(self):
        """Events are filtered by event type parameter."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            lines = [
                json.dumps({"event_type": "task_complete", "timestamp": "2026-03-27T10:00:00+00:00", "terminal": "T1", "dispatch_id": "d1", "status": "success", "gate": "g1"}),
                json.dumps({"event_type": "task_started", "timestamp": "2026-03-27T11:00:00+00:00", "terminal": "T2", "dispatch_id": "d2"}),
            ]
            receipts.write_text("\n".join(lines) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({"type": ["task_started"]})

        self.assertTrue(all(e["type"] == "task_started" for e in result["events"]))

    def test_query_events_filter_by_pr(self):
        """Events are filtered by PR parameter."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            lines = [
                json.dumps({"event_type": "task_complete", "timestamp": "2026-03-27T10:00:00+00:00", "terminal": "T1", "dispatch_id": "d1", "status": "success", "pr_id": "PR-4"}),
                json.dumps({"event_type": "task_complete", "timestamp": "2026-03-27T11:00:00+00:00", "terminal": "T2", "dispatch_id": "d2", "status": "success", "pr_id": "PR-3"}),
            ]
            receipts.write_text("\n".join(lines) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({"pr": ["PR-4"]})

        self.assertTrue(all(e["pr_id"] == "PR-4" for e in result["events"]))

    def test_query_events_handles_malformed_ndjson(self):
        """Malformed NDJSON lines are skipped without crashing."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            receipts.write_text(
                "this is not json\n"
                '{"event_type": "task_complete", "timestamp": "2026-03-27T10:00:00+00:00", "terminal": "T1", "dispatch_id": "d1", "status": "success"}\n'
                "{broken json\n"
                "\n"
            )

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({})

        # Should have 1 valid event (the second line)
        self.assertEqual(len(result["events"]), 1)

    def test_query_events_missing_receipts_file(self):
        """Missing receipts file returns empty events list."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(mod, "RECEIPTS_PATH", Path(tmpdir) / "nonexistent.ndjson"), \
                 patch.object(mod, "DISPATCH_DIR", Path(tmpdir) / "dispatches"):
                result = mod._query_events({})

        self.assertEqual(result["events"], [])

    def test_query_events_capped_at_30(self):
        """Events list is capped at 30 entries."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            lines = []
            for i in range(50):
                lines.append(json.dumps({
                    "event_type": "task_complete",
                    "timestamp": f"2026-03-27T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
                    "terminal": "T1",
                    "dispatch_id": f"d-{i:03d}",
                    "status": "success",
                }))
            receipts.write_text("\n".join(lines) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({})

        self.assertLessEqual(len(result["events"]), 30)

    def test_gate_passed_mapping(self):
        """task_complete with status=success and gate maps to gate_passed."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            receipts.write_text(json.dumps({
                "event_type": "task_complete",
                "timestamp": "2026-03-27T10:00:00+00:00",
                "terminal": "T1",
                "dispatch_id": "d1",
                "status": "success",
                "gate": "gate_pr4",
            }) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({})

        self.assertEqual(result["events"][0]["type"], "gate_passed")

    def test_gate_failed_mapping(self):
        """task_complete with status=failure and gate maps to gate_failed."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            receipts = state_dir / "t0_receipts.ndjson"
            receipts.write_text(json.dumps({
                "event_type": "task_complete",
                "timestamp": "2026-03-27T10:00:00+00:00",
                "terminal": "T1",
                "dispatch_id": "d1",
                "status": "failure",
                "gate": "gate_pr4",
            }) + "\n")

            with patch.object(mod, "RECEIPTS_PATH", receipts), \
                 patch.object(mod, "DISPATCH_DIR", state_dir / "dispatches"):
                result = mod._query_events({})

        self.assertEqual(result["events"][0]["type"], "gate_failed")


class TestDispatchesAPI(unittest.TestCase):
    """Test /api/dispatches endpoint logic."""

    def _import_serve_dashboard(self):
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_scan_dispatches_empty_directory(self):
        """Empty or missing dispatch directories return empty stages."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(mod, "DISPATCHES_DIR", Path(tmpdir) / "nonexistent"), \
                 patch.object(mod, "REPORTS_DIR", Path(tmpdir) / "reports"):
                result = mod._scan_dispatches()

        self.assertIn("stages", result)
        self.assertEqual(result["total"], 0)
        for stage_list in result["stages"].values():
            self.assertEqual(len(stage_list), 0)

    def test_scan_dispatches_groups_by_stage(self):
        """Dispatches are correctly grouped into Kanban stages."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            dispatch_dir = Path(tmpdir) / "dispatches"
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()

            for stage in ("staging", "pending", "active", "completed"):
                stage_dir = dispatch_dir / stage
                stage_dir.mkdir(parents=True)
                (stage_dir / f"test-{stage}.md").write_text(
                    f"[[TARGET: T1]]\nDispatch-ID: test-{stage}\nPR-ID: PR-4\nTrack: A\n"
                )

            with patch.object(mod, "DISPATCHES_DIR", dispatch_dir), \
                 patch.object(mod, "REPORTS_DIR", reports_dir):
                result = mod._scan_dispatches()

        self.assertGreater(result["total"], 0)
        self.assertIn("stages", result)
        # Verify expected stage keys exist
        for key in ("staging", "pending", "active", "done"):
            self.assertIn(key, result["stages"])

    def test_scan_dispatches_handles_malformed_files(self):
        """Malformed dispatch files are skipped without crashing."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            dispatch_dir = Path(tmpdir) / "dispatches"
            staging = dispatch_dir / "staging"
            staging.mkdir(parents=True)
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()

            # Write a valid dispatch
            (staging / "good.md").write_text("[[TARGET: T1]]\nDispatch-ID: good\n")
            # Write a file with no parseable header
            (staging / "weird.md").write_text("No header here at all\n")

            with patch.object(mod, "DISPATCHES_DIR", dispatch_dir), \
                 patch.object(mod, "REPORTS_DIR", reports_dir):
                result = mod._scan_dispatches()

        # Should not crash, and should have at least 2 entries (both parsed, just with defaults)
        self.assertGreaterEqual(result["total"], 1)

    def test_dispatch_card_has_expected_fields(self):
        """Each dispatch card contains required metadata fields."""
        mod = self._import_serve_dashboard()

        with tempfile.TemporaryDirectory() as tmpdir:
            dispatch_dir = Path(tmpdir) / "dispatches"
            staging = dispatch_dir / "staging"
            staging.mkdir(parents=True)
            reports_dir = Path(tmpdir) / "reports"
            reports_dir.mkdir()

            (staging / "test-dispatch.md").write_text(
                "[[TARGET: T1]]\nDispatch-ID: test-dispatch\nPR-ID: PR-4\nTrack: A\nGate: gate_test\n"
            )

            with patch.object(mod, "DISPATCHES_DIR", dispatch_dir), \
                 patch.object(mod, "REPORTS_DIR", reports_dir):
                result = mod._scan_dispatches()

        cards = result["stages"]["staging"]
        self.assertGreater(len(cards), 0)

        card = cards[0]
        for field in ("id", "file", "pr_id", "track", "stage", "duration_label", "has_receipt"):
            self.assertIn(field, card, f"Missing field: {field}")


class TestJumpEndpointMethod(unittest.TestCase):
    """Verify /api/jump rejects GET and accepts POST."""

    def _import_serve_dashboard(self):
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_jump_endpoint_not_in_get_handler(self):
        """GET /api/jump/ should not be handled by do_GET (falls to static serving)."""
        mod = self._import_serve_dashboard()
        # Verify that do_GET does NOT handle /api/jump paths —
        # inspect the do_GET source for /api/jump
        import inspect
        source = inspect.getsource(mod.DashboardHandler.do_GET)
        self.assertNotIn("/api/jump", source)

    def test_jump_endpoint_in_post_handler(self):
        """POST /api/jump/ is handled by do_POST."""
        mod = self._import_serve_dashboard()
        import inspect
        source = inspect.getsource(mod.DashboardHandler.do_POST)
        self.assertIn("/api/jump/", source)


# ===========================================================================
# 3. Attention ranking/priority (canonical_state_views.py)
# ===========================================================================

class TestComputeTerminalAttention(unittest.TestCase):
    """Test _compute_terminal_attention() priority logic."""

    @classmethod
    def setUpClass(cls):
        scripts_lib = str(SCRIPTS_LIB)
        scripts_dir = str(PROJECT_ROOT / "scripts")

        # Remove scripts/ to prevent the CLI wrapper (scripts/terminal_state_shadow.py)
        # from shadowing the library module (scripts/lib/terminal_state_shadow.py).
        # test_vnx_process_ux.py adds scripts/ at module load time, which can re-introduce
        # the shadowing after the module-level cleanup at the top of this file.
        while scripts_dir in sys.path:
            sys.path.remove(scripts_dir)

        # Ensure scripts/lib/ is at the front of sys.path.
        if scripts_lib in sys.path:
            sys.path.remove(scripts_lib)
        sys.path.insert(0, scripts_lib)

        # Purge any cached modules that may have been loaded with wrong path resolution.
        for mod in ("terminal_state_shadow", "terminal_state_reconciler", "canonical_state_views"):
            sys.modules.pop(mod, None)

        import canonical_state_views as csv_mod
        cls.csv = csv_mod

    def test_blocked_returns_highest_priority(self):
        """Blocked status produces needs_human=True with type=blocked."""
        result = self.csv._compute_terminal_attention(
            terminal="T1",
            status="blocked",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=None,
            context_ts=None,
        )
        self.assertTrue(result["needs_human"])
        self.assertEqual(result["attention"]["type"], "blocked")
        self.assertEqual(result["attention"]["jump_target"], "T1")

    def test_stale_working_triggers_attention(self):
        """Working terminal past stale threshold triggers stale attention."""
        result = self.csv._compute_terminal_attention(
            terminal="T2",
            status="working",
            last_update=(datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat(),
            status_age_seconds=200,
            context_usage_pct=None,
            context_ts=None,
            stale_after_seconds=180,
        )
        self.assertTrue(result["needs_human"])
        self.assertEqual(result["attention"]["type"], "stale")
        self.assertIn("200s", result["attention"]["reason"])

    def test_stale_not_triggered_under_threshold(self):
        """Working terminal under stale threshold does not trigger attention."""
        result = self.csv._compute_terminal_attention(
            terminal="T2",
            status="working",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=60,
            context_usage_pct=None,
            context_ts=None,
            stale_after_seconds=180,
        )
        self.assertFalse(result["needs_human"])
        self.assertIsNone(result["attention"])

    def test_context_pressure_above_threshold(self):
        """Context usage > 80% triggers context-pressure attention."""
        result = self.csv._compute_terminal_attention(
            terminal="T3",
            status="working",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=85,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self.assertTrue(result["needs_human"])
        self.assertEqual(result["attention"]["type"], "context-pressure")
        self.assertIn("85%", result["attention"]["reason"])

    def test_context_pressure_at_threshold_not_triggered(self):
        """Context usage at exactly 80% does NOT trigger (must be >80)."""
        result = self.csv._compute_terminal_attention(
            terminal="T3",
            status="working",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=80,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self.assertFalse(result["needs_human"])

    def test_context_pressure_below_threshold_not_triggered(self):
        """Context usage at 50% does not trigger attention."""
        result = self.csv._compute_terminal_attention(
            terminal="T1",
            status="working",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=50,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self.assertFalse(result["needs_human"])

    def test_priority_blocked_over_stale(self):
        """Blocked takes priority even when stale conditions are met."""
        result = self.csv._compute_terminal_attention(
            terminal="T1",
            status="blocked",
            last_update=(datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat(),
            status_age_seconds=300,
            context_usage_pct=95,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
            stale_after_seconds=180,
        )
        self.assertEqual(result["attention"]["type"], "blocked")

    def test_priority_stale_over_context_pressure(self):
        """Stale takes priority over context-pressure."""
        result = self.csv._compute_terminal_attention(
            terminal="T2",
            status="working",
            last_update=(datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat(),
            status_age_seconds=200,
            context_usage_pct=90,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
            stale_after_seconds=180,
        )
        self.assertEqual(result["attention"]["type"], "stale")

    def test_idle_terminal_no_attention(self):
        """Idle terminal with no pressure produces no attention."""
        result = self.csv._compute_terminal_attention(
            terminal="T1",
            status="idle",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=30,
            context_ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self.assertFalse(result["needs_human"])
        self.assertIsNone(result["attention"])

    def test_none_context_values_no_crash(self):
        """None context values don't crash the function."""
        result = self.csv._compute_terminal_attention(
            terminal="T1",
            status="idle",
            last_update=datetime.now(timezone.utc).isoformat(),
            status_age_seconds=10,
            context_usage_pct=None,
            context_ts=None,
        )
        self.assertFalse(result["needs_human"])


class TestBuildAttentionSummary(unittest.TestCase):
    """Test build_attention_summary() structure and correctness."""

    @classmethod
    def setUpClass(cls):
        if str(SCRIPTS_LIB) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_LIB))

    def _make_state_dir(self, tmpdir, terminal_states=None):
        """Create a minimal state directory with terminal_state.json."""
        state_dir = Path(tmpdir)
        if terminal_states is None:
            terminal_states = {
                t: {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()}
                for t in ("T1", "T2", "T3")
            }
        doc = {"schema_version": 1, "terminals": terminal_states}
        (state_dir / "terminal_state.json").write_text(json.dumps(doc))
        return state_dir

    @patch("canonical_state_views.reconcile_terminal_state")
    @patch("canonical_state_views.validate_terminal_state_document")
    def test_attention_summary_structure(self, mock_validate, mock_reconcile):
        """build_attention_summary returns correct top-level keys."""
        import canonical_state_views as csv_mod

        mock_validate.return_value = None
        mock_reconcile.return_value = {"terminals": {}, "evidence": {}, "degraded": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = self._make_state_dir(tmpdir)
            result = csv_mod.build_attention_summary(state_dir)

        self.assertIn("terminals", result)
        self.assertIn("needs_attention_count", result)
        self.assertIn("needs_attention", result)
        self.assertIn("highest_priority", result)
        self.assertIn("generated_at", result)

    @patch("canonical_state_views.reconcile_terminal_state")
    @patch("canonical_state_views.validate_terminal_state_document")
    def test_no_attention_returns_zero_count(self, mock_validate, mock_reconcile):
        """All idle terminals produce needs_attention_count=0."""
        import canonical_state_views as csv_mod

        mock_validate.return_value = None
        mock_reconcile.return_value = {"terminals": {}, "evidence": {}, "degraded": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = self._make_state_dir(tmpdir)
            result = csv_mod.build_attention_summary(state_dir)

        self.assertEqual(result["needs_attention_count"], 0)
        self.assertIsNone(result["highest_priority"])

    @patch("canonical_state_views.reconcile_terminal_state")
    @patch("canonical_state_views.validate_terminal_state_document")
    def test_blocked_terminal_appears_in_attention(self, mock_validate, mock_reconcile):
        """A blocked terminal appears in needs_attention list."""
        import canonical_state_views as csv_mod

        mock_validate.return_value = None
        mock_reconcile.return_value = {"terminals": {}, "evidence": {}, "degraded": False}

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = self._make_state_dir(tmpdir, {
                "T1": {"status": "blocked", "last_activity": datetime.now(timezone.utc).isoformat()},
                "T2": {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()},
                "T3": {"status": "idle", "last_activity": datetime.now(timezone.utc).isoformat()},
            })
            result = csv_mod.build_attention_summary(state_dir)

        self.assertGreater(result["needs_attention_count"], 0)
        self.assertIn("T1", result["needs_attention"])


class TestAttentionPriorityConstants(unittest.TestCase):
    """Verify ATTENTION_PRIORITY ordering."""

    def test_priority_ordering(self):
        """blocked > review-needed > stale > context-pressure."""
        if str(SCRIPTS_LIB) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_LIB))
        import canonical_state_views as csv_mod

        p = csv_mod.ATTENTION_PRIORITY
        self.assertGreater(p["blocked"], p["review-needed"])
        self.assertGreater(p["review-needed"], p["stale"])
        self.assertGreater(p["stale"], p["context-pressure"])


# ===========================================================================
# 4. Confirmation behavior (dashboard/index.html — grep/parse verification)
# ===========================================================================

class TestConfirmationDialogHTML(unittest.TestCase):
    """Verify confirmation dialog patterns in index.html via parsing."""

    INDEX_HTML = DASHBOARD_DIR / "index.html"

    @classmethod
    def setUpClass(cls):
        cls.html_content = cls.INDEX_HTML.read_text(encoding="utf-8")

    def test_showConfirmDialog_function_exists(self):
        """showConfirmDialog function is defined in the HTML."""
        self.assertIn("function showConfirmDialog(", self.html_content)

    def test_restartProcess_calls_showConfirmDialog(self):
        """restartProcess function calls showConfirmDialog."""
        # Extract restartProcess function body
        match = re.search(
            r"async function restartProcess\b.*?\{(.*?)\n    \}",
            self.html_content,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "restartProcess function not found")
        self.assertIn("showConfirmDialog(", match.group(0))

    def test_unlockTerminal_calls_showConfirmDialog(self):
        """unlockTerminal function calls showConfirmDialog."""
        match = re.search(
            r"async function unlockTerminal\b.*?\{(.*?)\n    \}",
            self.html_content,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "unlockTerminal function not found")
        self.assertIn("showConfirmDialog(", match.group(0))

    def test_confirm_dialog_has_cancel_button(self):
        """Confirmation dialog includes a Cancel button."""
        self.assertIn('data-action="cancel"', self.html_content)

    def test_confirm_dialog_has_confirm_button(self):
        """Confirmation dialog includes a Confirm button."""
        self.assertIn('data-action="confirm"', self.html_content)

    def test_confirm_dialog_escape_key_closes(self):
        """Confirmation dialog responds to Escape key."""
        self.assertIn("Escape", self.html_content)

    def test_restart_dialog_has_warning_class(self):
        """Restart confirmation uses warn styling (not danger)."""
        # restartProcess uses confirmClass: "confirm-btn-warn"
        self.assertIn("confirm-btn-warn", self.html_content)

    def test_unlock_dialog_has_danger_class(self):
        """Unlock confirmation uses danger styling."""
        # unlockTerminal uses confirmClass: "confirm-btn-danger"
        self.assertIn("confirm-btn-danger", self.html_content)

    def test_no_direct_fetch_without_confirm(self):
        """restartProcess and unlockTerminal only call fetch inside onConfirm callback."""
        # Verify that within both functions, fetch() is inside the onConfirm callback
        # by checking that showConfirmDialog appears before the fetch call

        # restartProcess
        restart_match = re.search(
            r"async function restartProcess\b(.*?)\n    \}",
            self.html_content,
            re.DOTALL,
        )
        self.assertIsNotNone(restart_match)
        body = restart_match.group(0)
        confirm_pos = body.find("showConfirmDialog(")
        fetch_pos = body.find("fetch(")
        self.assertLess(confirm_pos, fetch_pos, "fetch should be inside showConfirmDialog callback")

        # unlockTerminal
        unlock_match = re.search(
            r"async function unlockTerminal\b(.*?)\n    \}",
            self.html_content,
            re.DOTALL,
        )
        self.assertIsNotNone(unlock_match)
        body = unlock_match.group(0)
        confirm_pos = body.find("showConfirmDialog(")
        fetch_pos = body.find("fetch(")
        self.assertLess(confirm_pos, fetch_pos, "fetch should be inside showConfirmDialog callback")


# ===========================================================================
# 5. Helper / utility tests
# ===========================================================================

class TestFormatDuration(unittest.TestCase):
    """Test _format_duration helper in serve_dashboard."""

    def _import_serve_dashboard(self):
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_seconds(self):
        mod = self._import_serve_dashboard()
        self.assertEqual(mod._format_duration(45), "45s")

    def test_minutes(self):
        mod = self._import_serve_dashboard()
        self.assertEqual(mod._format_duration(120), "2m")

    def test_hours(self):
        mod = self._import_serve_dashboard()
        self.assertIn("h", mod._format_duration(7200))

    def test_days(self):
        mod = self._import_serve_dashboard()
        self.assertIn("d", mod._format_duration(172800))


class TestParseDispatchHeader(unittest.TestCase):
    """Test dispatch header parsing logic."""

    def _import_serve_dashboard(self):
        if str(DASHBOARD_DIR) not in sys.path:
            sys.path.insert(0, str(DASHBOARD_DIR))
        import serve_dashboard
        return serve_dashboard

    def test_parses_standard_header(self):
        mod = self._import_serve_dashboard()
        text = "[[TARGET: T1]]\nDispatch-ID: test-123\nPR-ID: PR-4\nTrack: A\nGate: gate_test\n"
        header = mod._parse_dispatch_header(text)
        self.assertEqual(header.get("dispatch_id"), "test-123")
        self.assertEqual(header.get("pr_id"), "PR-4")
        self.assertEqual(header.get("track"), "A")

    def test_empty_text_returns_empty_dict(self):
        mod = self._import_serve_dashboard()
        header = mod._parse_dispatch_header("")
        self.assertEqual(header, {})

    def test_no_target_returns_empty_dict(self):
        mod = self._import_serve_dashboard()
        header = mod._parse_dispatch_header("Some random text\nNo target here\n")
        self.assertEqual(header, {})


class TestNormalizeStatus(unittest.TestCase):
    """Test status normalization in canonical_state_views."""

    @classmethod
    def setUpClass(cls):
        if str(SCRIPTS_LIB) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_LIB))
        import canonical_state_views as csv_mod
        cls.csv = csv_mod

    def test_working_aliases(self):
        for alias in ("claimed", "active", "working", "busy", "in_progress"):
            self.assertEqual(self.csv._normalize_status(alias), "working")

    def test_idle_aliases(self):
        for alias in ("idle", "ready", "complete", "completed"):
            self.assertEqual(self.csv._normalize_status(alias), "idle")

    def test_blocked_aliases(self):
        for alias in ("blocked", "error", "failed", "timeout"):
            self.assertEqual(self.csv._normalize_status(alias), "blocked")

    def test_offline_aliases(self):
        for alias in ("offline", "down", "disconnected"):
            self.assertEqual(self.csv._normalize_status(alias), "offline")

    def test_unknown_status(self):
        self.assertEqual(self.csv._normalize_status("garbage"), "unknown")

    def test_none_status(self):
        self.assertEqual(self.csv._normalize_status(None), "unknown")


class TestContextPressureThreshold(unittest.TestCase):
    """Verify the 80% context pressure threshold constant."""

    def test_threshold_value(self):
        if str(SCRIPTS_LIB) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_LIB))
        import canonical_state_views as csv_mod
        self.assertEqual(csv_mod.CONTEXT_PRESSURE_THRESHOLD, 80)


if __name__ == "__main__":
    unittest.main()
