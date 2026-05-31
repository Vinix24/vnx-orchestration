#!/usr/bin/env python3
"""Tests for pool_worker_runner.py — N-2 single-claim worker (ADR-007 + ADR-018).

Required coverage:
  - claims one dispatch, loads bundle, delegates to delivery, returns EXIT_OK
  - empty queue → EXIT_NO_WORK; no delivery call
  - FM-4: project_id mismatch → EXIT_PROJECT_MISMATCH; no delivery call
  - path-traversal guard: dispatch_id with '../' or absolute path → EXIT_INVALID_DISPATCH_ID
  - ledger events: claim_next_queued_dispatch (N-1) emits dispatch_claimed + dispatch_claim_provenance
  - state-dir resolution: VNX_STATE_DIR='' + VNX_DATA_DIR set → canonical dir, not empty path
"""
from __future__ import annotations
import importlib.util as _ilu, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR / "lib" / "migrations"))

from coordination_db import db_path_from_state_dir
from runtime_coordination import get_connection, init_schema

# Load migration 0026 (project-scoped claim index + claimed_by/claimed_at)
_m26 = _ilu.module_from_spec(_s := _ilu.spec_from_file_location(
    "apply_0026", SCRIPT_DIR / "lib" / "migrations" / "apply_0026.py"))
_s.loader.exec_module(_m26)
_m17 = _ilu.module_from_spec(_s2 := _ilu.spec_from_file_location(
    "apply_0017", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"))
_s2.loader.exec_module(_m17)
_MIGS_SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0026_dispatch_claim.sql"

def _apply_0026(state_dir):
    return _m26.apply_migration(db_path_from_state_dir(state_dir), _MIGS_SQL)

from pool_worker_runner import (  # noqa: E402
    EXIT_NO_WORK, EXIT_OK, EXIT_PROJECT_MISMATCH, EXIT_BUNDLE_MISSING,
    EXIT_INVALID_DISPATCH_ID, run, _validate_dispatch_id, _resolve_state_dir,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.sd = self._t.name
        init_schema(self.sd)
        with get_connection(self.sd) as c:
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (13,'stub')")
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (14,'stub')")
            c.commit()
        _apply_0026(self.sd)
        with get_connection(self.sd) as c:
            _m17._rebuild_dispatches(c)
            c.commit()
        self.dd = Path(self._t.name) / "dispatches"
        self.dd.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._t.cleanup()

    def _queue(self, did, pid="proj"):
        with get_connection(self.sd) as c:
            c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, priority) VALUES (?,?,'queued','P2')", (did, pid))
            c.commit()

    def _bundle(self, did, provider="claude", instr="# test"):
        d = self.dd / did
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.txt").write_text(instr, encoding="utf-8")
        (d / "bundle.json").write_text(
            json.dumps({"dispatch_id": did, "target_profile": {"provider": provider}, "gate": ""}),
            encoding="utf-8")

    def _run(self, tid="T1", pid="proj", **kw):
        return run(terminal_id=tid, project_id=pid,
                   state_dir=Path(self.sd), dispatch_dir=self.dd, **kw)


class TestClaimsAndDelivers(_Base):

    def test_claims_one_dispatch_delegates_to_delivery_returns_ok(self):
        """Mocked broker+delivery: runner claims one dispatch, delegates, returns EXIT_OK."""
        self._queue("d-001")
        self._bundle("d-001", instr="# Instruction body")

        with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK) as m:
            result = self._run()

        self.assertEqual(result, EXIT_OK)
        m.assert_called_once()
        args = m.call_args.args
        self.assertEqual(args[0], "T1")          # terminal_id
        self.assertEqual(args[1], "d-001")        # dispatch_id
        self.assertEqual(args[2], "# Instruction body")  # instruction


class TestEmptyQueue(_Base):

    def test_empty_queue_returns_no_work_no_delivery(self):
        """Empty queue → EXIT_NO_WORK; no side effects; delivery never called."""
        with patch("pool_worker_runner._deliver_claude") as m:
            result = self._run()
        self.assertEqual(result, EXIT_NO_WORK)
        m.assert_not_called()


class TestProjectIdMismatch(_Base):
    """ADR-018 FM-4: post-claim project_id guard refuses cross-project dispatch."""

    def test_mismatch_refuses_dispatch_no_delivery(self):
        """Simulated edge case: claim returns project-b dispatch, worker is project-a."""
        with get_connection(self.sd) as c:
            c.execute("INSERT INTO dispatches (dispatch_id, project_id, state, priority)"
                      " VALUES ('d-cross','project-b','claimed','P2')")
            c.commit()
        self._bundle("d-cross")

        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="d-cross"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run(pid="project-a")

        self.assertEqual(result, EXIT_PROJECT_MISMATCH)
        m.assert_not_called()


class TestBundleMissing(_Base):

    def test_missing_bundle_json_returns_bundle_missing(self):
        self._queue("d-nobundle")
        self.assertEqual(self._run(), EXIT_BUNDLE_MISSING)

    def test_missing_prompt_txt_returns_bundle_missing(self):
        self._queue("d-noprompt")
        d = self.dd / "d-noprompt"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bundle.json").write_text(
            json.dumps({"dispatch_id": "d-noprompt", "target_profile": {"provider": "claude"}, "gate": ""}),
            encoding="utf-8")
        self.assertEqual(self._run(), EXIT_BUNDLE_MISSING)


class TestPathTraversalGuard(_Base):
    """Security: dispatch_id containing path separators or traversal must be refused."""

    def test_dotdot_slash_refused(self):
        """dispatch_id '../secret' triggers EXIT_INVALID_DISPATCH_ID, no file read."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="../secret"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_absolute_path_refused(self):
        """/etc/passwd as dispatch_id triggers EXIT_INVALID_DISPATCH_ID."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="/etc/passwd"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_embedded_slash_refused(self):
        """dispatch_id 'a/b' triggers EXIT_INVALID_DISPATCH_ID."""
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="a/b"):
            with patch("pool_worker_runner._deliver_claude") as m:
                result = self._run()
        self.assertEqual(result, EXIT_INVALID_DISPATCH_ID)
        m.assert_not_called()

    def test_normal_slug_resolves(self):
        """A well-formed dispatch_id resolves correctly and does not raise."""
        with tempfile.TemporaryDirectory() as td:
            dd = Path(td) / "dispatches"
            dd.mkdir()
            resolved = _validate_dispatch_id("20260529-abc123", dd)
            self.assertTrue(str(resolved).startswith(os.path.realpath(dd)))
            self.assertTrue(str(resolved).endswith("20260529-abc123"))


class TestLedgerEventsOnClaim(_Base):
    """ADR-005: claim_next_queued_dispatch (N-1) emits dispatch_claimed + dispatch_claim_provenance.

    The runner must NOT emit a third duplicate event. We verify the N-1 events exist and that
    pool_worker_runner itself does not emit an additional event of its own.
    """

    def test_n1_emits_two_events_runner_does_not_duplicate(self):
        """After a real claim, exactly dispatch_claimed + dispatch_claim_provenance exist; no third."""
        from coordination_db import get_events
        self._queue("d-ledger")
        self._bundle("d-ledger", instr="# ledger test")

        with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK):
            result = self._run(tid="T1", pid="proj")

        self.assertEqual(result, EXIT_OK)

        with get_connection(self.sd) as c:
            dispatch_events = get_events(c, entity_id="d-ledger", limit=50)

        event_types = [e["event_type"] for e in dispatch_events]
        self.assertIn("dispatch_claimed", event_types)
        self.assertIn("dispatch_claim_provenance", event_types)
        # Runner must not add a third distinct event for the same dispatch
        self.assertEqual(len(dispatch_events), 2,
                         f"Expected exactly 2 events for dispatch, got {len(dispatch_events)}: {event_types}")


class TestStateDirResolution(unittest.TestCase):
    """VNX_STATE_DIR='' must fall through to VNX_DATA_DIR, not produce Path('') / 'state'."""

    def test_empty_vnx_state_dir_falls_through_to_vnx_data_dir(self):
        env = {"VNX_STATE_DIR": "", "VNX_DATA_DIR": "/tmp/mydata"}
        with patch.dict(os.environ, env, clear=False):
            result = _resolve_state_dir()
        self.assertEqual(result, Path("/tmp/mydata") / "state")

    def test_non_empty_vnx_state_dir_wins(self):
        env = {"VNX_STATE_DIR": "/tmp/mystate", "VNX_DATA_DIR": "/tmp/mydata"}
        with patch.dict(os.environ, env, clear=False):
            result = _resolve_state_dir()
        self.assertEqual(result, Path("/tmp/mystate"))

    def test_both_absent_returns_default(self):
        stripped = {k: v for k, v in os.environ.items()
                    if k not in ("VNX_STATE_DIR", "VNX_DATA_DIR")}
        with patch.dict(os.environ, stripped, clear=True):
            result = _resolve_state_dir()
        self.assertTrue(str(result).endswith(os.path.join(".vnx-data", "state")))


if __name__ == "__main__":
    unittest.main()
