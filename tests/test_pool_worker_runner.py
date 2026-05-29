#!/usr/bin/env python3
"""Tests for pool_worker_runner.py — N-2 single-claim worker (ADR-007 + ADR-018).

Required coverage:
  - claims one dispatch, loads bundle, delegates to delivery, returns EXIT_OK
  - empty queue → EXIT_NO_WORK; no delivery call
  - FM-4: project_id mismatch → EXIT_PROJECT_MISMATCH; no delivery call
"""
from __future__ import annotations
import importlib.util as _ilu, json, sys, tempfile, unittest
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

from pool_worker_runner import EXIT_NO_WORK, EXIT_OK, EXIT_PROJECT_MISMATCH, EXIT_BUNDLE_MISSING, run  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
