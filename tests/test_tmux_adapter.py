#!/usr/bin/env python3
"""
Tests for tmux_adapter.py (PR-3)

Quality gate coverage (gate_pr3_tmux_delivery_adapter):
  - tmux delivery can activate a dispatch using load-dispatch ID (primary path)
  - Delivery target resolution uses canonical lease/adapter state, not pane ID as truth
  - Pane remap does not corrupt dispatch state or attempt history
  - Adapter failures are recorded as runtime events with dispatch and terminal linkage
  - Current legacy prompt-paste delivery remains available as fallback
  - All tests pass for adapter routing and fallback activation behavior
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import (
    acquire_lease,
    get_connection,
    get_events,
    init_schema,
    register_dispatch,
)
from adapter_types import DeliveryResult
from tmux_adapter import (
    AdapterDisabledError,
    LeaseNotActiveError,
    PaneNotFoundError,
    PaneTarget,
    TmuxAdapter,
    adapter_config_from_env,
    adapter_enabled,
    load_adapter,
    primary_path_active,
    resolve_pane,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _write_panes_json(state_dir: str, panes: dict) -> None:
    """Write a panes.json into state_dir."""
    path = Path(state_dir) / "panes.json"
    path.write_text(json.dumps(panes), encoding="utf-8")


def _make_adapter(state_dir: str, *, primary_path: bool = True) -> TmuxAdapter:
    return TmuxAdapter(state_dir, primary_path=primary_path)


def _register_and_lease(state_dir: str, dispatch_id: str, terminal_id: str) -> None:
    """Register a dispatch and acquire a lease for terminal in the DB."""
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
        acquire_lease(conn, terminal_id=terminal_id, dispatch_id=dispatch_id)
        conn.commit()


SAMPLE_PANES = {
    "T1": {"pane_id": "vnx:0.1", "provider": "claude_code"},
    "T2": {"pane_id": "vnx:0.2", "provider": "claude_code"},
    "T3": {"pane_id": "vnx:0.3", "provider": "claude_code"},
    "t0": {"pane_id": "vnx:0.0", "provider": "claude_code"},
}


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class _AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        _write_panes_json(self.state_dir, SAMPLE_PANES)

    def tearDown(self):
        self._tmpdir.cleanup()

    def get_events_for(self, dispatch_id: str) -> list:
        with get_connection(self.state_dir) as conn:
            return get_events(conn, entity_id=dispatch_id)


# ---------------------------------------------------------------------------
# TestPaneResolution
# ---------------------------------------------------------------------------

class TestPaneResolution(_AdapterTestCase):
    """Pane resolution reads from panes.json adapter projection only."""

    def test_resolve_known_terminal(self):
        adapter = _make_adapter(self.state_dir)
        target = adapter.resolve_target("T2")
        self.assertEqual(target.terminal_id, "T2")
        self.assertEqual(target.pane_id, "vnx:0.2")
        self.assertEqual(target.provider, "claude_code")

    def test_resolve_lowercase_key(self):
        # panes.json may use lowercase "t0"
        adapter = _make_adapter(self.state_dir)
        target = adapter.resolve_target("t0")
        self.assertEqual(target.pane_id, "vnx:0.0")

    def test_resolve_unknown_terminal_raises(self):
        adapter = _make_adapter(self.state_dir)
        with self.assertRaises(PaneNotFoundError):
            adapter.resolve_target("T9")

    def test_pane_remap_updates_resolution(self):
        """Pane remap (panes.json update) changes pane_id but not dispatch state."""
        _register_and_lease(self.state_dir, "d-remap-001", "T1")

        # Simulate pane remap: T1 gets a new pane_id after tmux restart
        remapped = dict(SAMPLE_PANES)
        remapped["T1"] = {"pane_id": "vnx:1.1", "provider": "claude_code"}
        _write_panes_json(self.state_dir, remapped)

        adapter = _make_adapter(self.state_dir)
        target = adapter.resolve_target("T1")

        # Pane ID updated by remap
        self.assertEqual(target.pane_id, "vnx:1.1")

        # Dispatch state in DB is unaffected by pane remap
        with get_connection(self.state_dir) as conn:
            row = conn.execute(
                "SELECT state FROM dispatches WHERE dispatch_id = 'd-remap-001'"
            ).fetchone()
        self.assertEqual(row["state"], "queued")

    def test_pane_remap_does_not_affect_lease(self):
        """Pane remap must not corrupt lease state."""
        _register_and_lease(self.state_dir, "d-lease-remap", "T3")

        remapped = dict(SAMPLE_PANES)
        remapped["T3"] = {"pane_id": "vnx:9.3", "provider": "claude_code"}
        _write_panes_json(self.state_dir, remapped)

        with get_connection(self.state_dir) as conn:
            lease = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id = 'T3'"
            ).fetchone()

        self.assertEqual(dict(lease)["state"], "leased")
        self.assertEqual(dict(lease)["dispatch_id"], "d-lease-remap")


# ---------------------------------------------------------------------------
# TestLeaseValidation
# ---------------------------------------------------------------------------

class TestLeaseValidation(_AdapterTestCase):
    def test_validate_lease_passes_for_active_lease(self):
        _register_and_lease(self.state_dir, "d-val-001", "T2")
        adapter = _make_adapter(self.state_dir)
        # No exception expected
        adapter.validate_lease("T2", "d-val-001")

    def test_validate_lease_raises_on_wrong_dispatch(self):
        _register_and_lease(self.state_dir, "d-val-002", "T1")
        adapter = _make_adapter(self.state_dir)
        with self.assertRaises(LeaseNotActiveError):
            adapter.validate_lease("T1", "d-different-dispatch")

    def test_validate_lease_raises_on_idle_terminal(self):
        """Terminal with no active lease raises LeaseNotActiveError."""
        # Init leases but T2 stays idle (never acquired)
        adapter = _make_adapter(self.state_dir)
        with self.assertRaises(LeaseNotActiveError):
            adapter.validate_lease("T2", "d-any")

    def test_validate_lease_graceful_on_db_unavailable(self):
        """Validate silently skips when DB path does not exist (shadow mode)."""
        adapter = TmuxAdapter("/nonexistent/state/dir")
        # Should not raise — DB unavailable is tolerated in shadow mode
        adapter.validate_lease("T1", "d-shadow-001")


# ---------------------------------------------------------------------------
# TestPrimaryDelivery
# ---------------------------------------------------------------------------

class TestPrimaryDelivery(_AdapterTestCase):
    """Primary path: send `load-dispatch <id>` via send-keys."""

    def _mock_tmux_ok(self):
        """Patch subprocess.run to simulate successful tmux commands."""
        mock = MagicMock()
        mock.returncode = 0
        return mock

    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_primary_deliver_success(self, mock_avail, mock_send):
        adapter = _make_adapter(self.state_dir, primary_path=True)
        result = adapter.deliver("T2", "d-primary-001")

        self.assertTrue(result.success)
        self.assertEqual(result.path_used, "primary")
        self.assertEqual(result.pane_id, "vnx:0.2")

    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_primary_sends_load_dispatch_command(self, mock_avail, mock_send):
        """Primary delivery must send `load-dispatch <dispatch_id>`."""
        adapter = _make_adapter(self.state_dir, primary_path=True)
        adapter.deliver("T2", "d-cmd-check-001")

        # Verify that the load-dispatch command was sent as a literal key
        sent_calls = mock_send.call_args_list
        load_dispatch_sent = any(
            "load-dispatch d-cmd-check-001" in str(c) for c in sent_calls
        )
        self.assertTrue(
            load_dispatch_sent,
            f"Expected 'load-dispatch d-cmd-check-001' in send_keys calls: {sent_calls}"
        )

    @patch("tmux_adapter._tmux_send_keys", return_value=1)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_primary_failure_recorded_as_event(self, mock_avail, mock_send):
        """Adapter failures must be recorded in coordination_events."""
        _register_and_lease(self.state_dir, "d-fail-001", "T2")
        adapter = _make_adapter(self.state_dir, primary_path=True)
        result = adapter.deliver("T2", "d-fail-001", attempt_id="attempt-x")

        self.assertFalse(result.success)

        events = self.get_events_for("d-fail-001")
        failure_events = [e for e in events if "failure" in e["event_type"]]
        self.assertGreater(len(failure_events), 0, "Expected failure event in DB")

    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_primary_success_recorded_as_event(self, mock_avail, mock_send):
        """Successful delivery emits adapter_deliver_success event."""
        _register_and_lease(self.state_dir, "d-ok-001", "T1")
        adapter = _make_adapter(self.state_dir, primary_path=True)
        adapter.deliver("T1", "d-ok-001", attempt_id="attempt-y")

        events = self.get_events_for("d-ok-001")
        success_events = [e for e in events if e["event_type"] == "adapter_deliver_success"]
        self.assertGreater(len(success_events), 0)

    @patch("tmux_adapter._tmux_available", return_value=False)
    def test_tmux_unavailable_returns_failure(self, mock_avail):
        adapter = _make_adapter(self.state_dir, primary_path=True)
        result = adapter.deliver("T2", "d-notmux-001")
        self.assertFalse(result.success)
        self.assertIn("tmux", result.failure_reason.lower())


# ---------------------------------------------------------------------------
# TestLegacyFallback
# ---------------------------------------------------------------------------

class TestLegacyFallback(_AdapterTestCase):
    """Legacy path: skill send-keys + paste-buffer prompt."""

    @patch("tmux_adapter._tmux_load_and_paste", return_value=0)
    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_legacy_deliver_success(self, mock_avail, mock_send, mock_paste):
        adapter = _make_adapter(self.state_dir, primary_path=False)
        result = adapter.deliver(
            "T2", "d-legacy-001",
            skill_command="/backend-developer",
            prompt="Do some work.",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.path_used, "legacy")

    @patch("tmux_adapter._tmux_load_and_paste", return_value=0)
    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_legacy_sends_skill_then_prompt(self, mock_avail, mock_send, mock_paste):
        """Legacy path must invoke send_keys for skill and load_and_paste for prompt."""
        adapter = _make_adapter(self.state_dir, primary_path=False)
        adapter.deliver(
            "T1", "d-legacy-skill-001",
            skill_command="/backend-developer",
            prompt="Do the thing.",
        )
        # send_keys was called (skill command + Enter + C-u clear)
        self.assertTrue(mock_send.called)
        # paste was called for prompt
        self.assertTrue(mock_paste.called)

    @patch("tmux_adapter._tmux_load_and_paste", return_value=1)
    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_legacy_paste_failure_recorded(self, mock_avail, mock_send, mock_paste):
        """Paste-buffer failure is recorded as a coordination event."""
        _register_and_lease(self.state_dir, "d-paste-fail-001", "T2")
        adapter = _make_adapter(self.state_dir, primary_path=False)
        result = adapter.deliver(
            "T2", "d-paste-fail-001",
            skill_command="/backend-developer",
            prompt="Work.",
        )
        self.assertFalse(result.success)

        events = self.get_events_for("d-paste-fail-001")
        failure_events = [e for e in events if "failure" in e["event_type"]]
        self.assertGreater(len(failure_events), 0)

    @patch("tmux_adapter._tmux_load_and_paste", return_value=0)
    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_legacy_codex_provider_uses_single_paste(self, mock_avail, mock_send, mock_paste):
        """Codex CLI provider combines skill + prompt into one paste-buffer call."""
        panes = dict(SAMPLE_PANES)
        panes["T1"] = {"pane_id": "vnx:0.1", "provider": "codex_cli"}
        _write_panes_json(self.state_dir, panes)

        adapter = _make_adapter(self.state_dir, primary_path=False)
        result = adapter.deliver(
            "T1", "d-codex-001",
            skill_command="/backend-developer",
            prompt="Build it.",
        )
        self.assertTrue(result.success)
        # Paste called once for combined payload
        self.assertEqual(mock_paste.call_count, 1)


# ---------------------------------------------------------------------------
# TestPaneNotFound
# ---------------------------------------------------------------------------

class TestPaneNotFound(_AdapterTestCase):
    def test_delivery_to_unknown_terminal_fails_gracefully(self):
        """Delivery to an unknown terminal returns failure, not exception."""
        adapter = _make_adapter(self.state_dir)
        result = adapter.deliver("T9", "d-nopane-001")
        self.assertFalse(result.success)
        self.assertIsNone(result.pane_id)
        self.assertIn("T9", result.failure_reason)

    def test_pane_not_found_event_recorded(self):
        """PaneNotFound must emit adapter_pane_not_found event."""
        _register_and_lease(self.state_dir, "d-nopane-event-001", "T2")
        adapter = _make_adapter(self.state_dir)
        adapter.deliver("T9", "d-nopane-event-001")

        events = self.get_events_for("d-nopane-event-001")
        pane_events = [e for e in events if e["event_type"] == "adapter_pane_not_found"]
        self.assertGreater(len(pane_events), 0)


# ---------------------------------------------------------------------------
# TestAdapterFeatureFlags
# ---------------------------------------------------------------------------

class TestAdapterFeatureFlags(unittest.TestCase):
    def test_adapter_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_TMUX_ADAPTER_ENABLED", None)
            self.assertTrue(adapter_enabled())

    def test_adapter_disabled_when_flag_zero(self):
        with patch.dict(os.environ, {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            self.assertFalse(adapter_enabled())

    def test_primary_path_active_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_ADAPTER_PRIMARY", None)
            self.assertTrue(primary_path_active())

    def test_primary_path_disabled_when_flag_zero(self):
        with patch.dict(os.environ, {"VNX_ADAPTER_PRIMARY": "0"}):
            self.assertFalse(primary_path_active())

    def test_load_adapter_returns_none_when_disabled(self):
        with patch.dict(os.environ, {"VNX_TMUX_ADAPTER_ENABLED": "0"}):
            adapter = load_adapter("/tmp")
            self.assertIsNone(adapter)

    def test_load_adapter_returns_instance_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"VNX_TMUX_ADAPTER_ENABLED": "1"}):
                adapter = load_adapter(tmp)
                self.assertIsInstance(adapter, TmuxAdapter)

    def test_config_from_env_structure(self):
        with patch.dict(os.environ, {"VNX_TMUX_ADAPTER_ENABLED": "1", "VNX_ADAPTER_PRIMARY": "0"}):
            cfg = adapter_config_from_env()
            self.assertTrue(cfg["enabled"])
            self.assertFalse(cfg["primary_path"])


# ---------------------------------------------------------------------------
# TestDeliveryEventLinkage
# ---------------------------------------------------------------------------

class TestDeliveryEventLinkage(_AdapterTestCase):
    """Events must include dispatch_id and terminal_id linkage."""

    @patch("tmux_adapter._tmux_send_keys", return_value=0)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_event_metadata_includes_terminal_id(self, mock_avail, mock_send):
        _register_and_lease(self.state_dir, "d-meta-001", "T1")
        adapter = _make_adapter(self.state_dir, primary_path=True)
        adapter.deliver("T1", "d-meta-001", attempt_id="atm-001")

        events = self.get_events_for("d-meta-001")
        # Filter to adapter-emitted events only (they all carry terminal_id in metadata)
        adapter_events = [e for e in events if e["event_type"].startswith("adapter_")]
        self.assertGreater(len(adapter_events), 0)

        for evt in adapter_events:
            meta = json.loads(evt["metadata_json"] or "{}")
            self.assertEqual(meta.get("terminal_id"), "T1")

    @patch("tmux_adapter._tmux_send_keys", return_value=1)
    @patch("tmux_adapter._tmux_available", return_value=True)
    def test_event_includes_attempt_id_when_provided(self, mock_avail, mock_send):
        _register_and_lease(self.state_dir, "d-atm-001", "T2")
        adapter = _make_adapter(self.state_dir, primary_path=True)
        adapter.deliver("T2", "d-atm-001", attempt_id="atm-xyz")

        events = self.get_events_for("d-atm-001")
        attempt_ids = []
        for evt in events:
            meta = json.loads(evt["metadata_json"] or "{}")
            if "attempt_id" in meta:
                attempt_ids.append(meta["attempt_id"])

        self.assertIn("atm-xyz", attempt_ids)


# ---------------------------------------------------------------------------
# TestLoadDispatchScript
# ---------------------------------------------------------------------------

class TestLoadDispatchScript(unittest.TestCase):
    """Tests for load_dispatch.py worker-side bundle loader."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dispatch_dir = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_bundle(self, dispatch_id: str, prompt: str, metadata: dict = None) -> None:
        bundle_dir = Path(self.dispatch_dir) / dispatch_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = {
            "dispatch_id": dispatch_id,
            "bundle_version": 1,
            "target_profile": metadata or {},
        }
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        (bundle_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    def test_load_bundle_returns_dict(self):
        from load_dispatch import load_bundle
        self._write_bundle("d-loader-001", "Do the work.")
        bundle = load_bundle("d-loader-001", self.dispatch_dir)
        self.assertEqual(bundle["dispatch_id"], "d-loader-001")
        self.assertEqual(bundle["_prompt"], "Do the work.")

    def test_load_bundle_raises_on_missing_dispatch(self):
        from load_dispatch import load_bundle
        with self.assertRaises(FileNotFoundError):
            load_bundle("d-nonexistent-999", self.dispatch_dir)

    def test_format_output_with_skill(self):
        from load_dispatch import format_worker_output
        bundle = {
            "dispatch_id": "d-fmt-001",
            "target_profile": {"skill": "backend-developer"},
            "_prompt": "Build the feature.",
        }
        output = format_worker_output(bundle)
        self.assertIn("/backend-developer", output)
        self.assertIn("Build the feature.", output)

    def test_format_output_no_skill(self):
        from load_dispatch import format_worker_output
        bundle = {
            "dispatch_id": "d-fmt-002",
            "target_profile": {},
            "_prompt": "Just do it.",
        }
        output = format_worker_output(bundle)
        self.assertNotIn("/", output)
        self.assertIn("Just do it.", output)

    def test_main_exits_zero_on_valid_dispatch(self):
        from load_dispatch import main
        self._write_bundle("d-main-001", "Work prompt.")
        rc = main(["--dispatch-id", "d-main-001", "--dispatch-dir", self.dispatch_dir])
        self.assertEqual(rc, 0)

    def test_main_exits_one_on_missing_dispatch(self):
        from load_dispatch import main
        rc = main(["--dispatch-id", "d-missing-999", "--dispatch-dir", self.dispatch_dir])
        self.assertEqual(rc, 1)

    def test_show_bundle_flag(self):
        from load_dispatch import main
        import io
        from contextlib import redirect_stdout
        self._write_bundle("d-show-001", "prompt text")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--dispatch-id", "d-show-001", "--dispatch-dir", self.dispatch_dir, "--show-bundle"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["dispatch_id"], "d-show-001")
        self.assertNotIn("_prompt", data)  # _prompt excluded from --show-bundle

    def test_json_flag_includes_prompt(self):
        from load_dispatch import main
        import io
        from contextlib import redirect_stdout
        self._write_bundle("d-json-001", "full prompt here")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--dispatch-id", "d-json-001", "--dispatch-dir", self.dispatch_dir, "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("_prompt", data)
        self.assertEqual(data["_prompt"], "full prompt here")


# ---------------------------------------------------------------------------
# TestAdapterCliModule
# ---------------------------------------------------------------------------

class TestAdapterCliResolveCommand(unittest.TestCase):
    """Tests for tmux_adapter_cli.py resolve sub-command."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        _write_panes_json(self.state_dir, SAMPLE_PANES)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_resolve_known_terminal(self):
        from tmux_adapter_cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["resolve", "--terminal", "T2", "--state-dir", self.state_dir])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["terminal_id"], "T2")
        self.assertEqual(data["pane_id"], "vnx:0.2")

    def test_resolve_unknown_terminal_returns_one(self):
        from tmux_adapter_cli import main
        rc = main(["resolve", "--terminal", "T9", "--state-dir", self.state_dir])
        self.assertEqual(rc, 1)

    def test_config_command_returns_zero(self):
        from tmux_adapter_cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["config"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("enabled", data)
        self.assertIn("primary_path", data)


if __name__ == "__main__":
    unittest.main()
