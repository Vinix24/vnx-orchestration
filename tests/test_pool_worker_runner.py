#!/usr/bin/env python3
"""Tests for pool_worker_runner.py — N-2 single-claim worker entrypoint.

Covers:
  - claims one queued dispatch, loads bundle, delegates to delivery, returns EXIT_OK
  - empty queue → EXIT_NO_WORK; no delivery call; no side effects
  - FM-4: project_id mismatch → EXIT_PROJECT_MISMATCH; delivery not called
  - missing bundle → EXIT_BUNDLE_MISSING

ADR-007 (project_id scoping) + ADR-018 (single-claim, FM-4) cited.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR / "lib" / "migrations"))

from coordination_db import db_path_from_state_dir
from runtime_coordination import get_connection, init_schema

# Load migration 0026 (adds claimed_by / claimed_at / project-scoped claim index)
_spec_0026 = _ilu.spec_from_file_location("apply_0026", SCRIPT_DIR / "lib" / "migrations" / "apply_0026.py")
_mod_0026 = _ilu.module_from_spec(_spec_0026)
_spec_0026.loader.exec_module(_mod_0026)
apply_migration_0026 = _mod_0026.apply_migration

_spec_0017 = _ilu.spec_from_file_location("apply_0017", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)
_rebuild_dispatches_composite = _mod_0017._rebuild_dispatches

_MIGRATION_SQL = Path(__file__).resolve().parent.parent / "schemas" / "migrations" / "0026_dispatch_claim.sql"


def _apply_0026(state_dir: str) -> bool:
    return apply_migration_0026(db_path_from_state_dir(state_dir), _MIGRATION_SQL)


from pool_worker_runner import (  # noqa: E402
    EXIT_BUNDLE_MISSING,
    EXIT_DELIVERY_FAILED,
    EXIT_NO_WORK,
    EXIT_OK,
    EXIT_PROJECT_MISMATCH,
    run,
)


# ---------------------------------------------------------------------------
# Base test case — temp DB with full migration stack
# ---------------------------------------------------------------------------

class _DbTestCase(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self._tmpdir.name
        init_schema(self.state_dir)
        with get_connection(self.state_dir) as c:
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (13, 'test-stub')")
            c.execute("INSERT OR IGNORE INTO runtime_schema_version (version, description) VALUES (14, 'test-stub')")
            c.commit()
        _apply_0026(self.state_dir)
        with get_connection(self.state_dir) as c:
            _rebuild_dispatches_composite(c)
            c.commit()
        self.dispatch_dir = Path(self._tmpdir.name) / "dispatches"
        self.dispatch_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _insert_queued(self, dispatch_id: str, project_id: str = "test-proj") -> None:
        with get_connection(self.state_dir) as c:
            c.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state, priority) VALUES (?, ?, 'queued', 'P2')",
                (dispatch_id, project_id),
            )
            c.commit()

    def _write_bundle(self, dispatch_id: str, provider: str = "claude", instruction: str = "# Test") -> None:
        bundle_dir = self.dispatch_dir / dispatch_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "prompt.txt").write_text(instruction, encoding="utf-8")
        (bundle_dir / "bundle.json").write_text(
            json.dumps({"dispatch_id": dispatch_id, "target_profile": {"provider": provider}, "gate": ""}),
            encoding="utf-8",
        )

    def _run(self, terminal_id: str = "T1", project_id: str = "test-proj", **kwargs) -> int:
        return run(
            terminal_id=terminal_id,
            project_id=project_id,
            state_dir=Path(self.state_dir),
            dispatch_dir=self.dispatch_dir,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Test: claim + delivery delegation
# ---------------------------------------------------------------------------

class TestClaimsAndDelivers(_DbTestCase):

    def test_claims_one_dispatch_delegates_to_delivery_returns_ok(self):
        """Runner claims one queued dispatch, loads bundle, delegates to _deliver_claude, returns EXIT_OK."""
        self._insert_queued("d-001", project_id="test-proj")
        self._write_bundle("d-001", provider="claude", instruction="# Dispatch body")

        with patch("pool_worker_runner._deliver_claude", return_value=EXIT_OK) as mock_deliver:
            result = self._run(project_id="test-proj")

        self.assertEqual(result, EXIT_OK)
        mock_deliver.assert_called_once()
        args = mock_deliver.call_args.args
        self.assertEqual(args[0], "T1")       # terminal_id
        self.assertEqual(args[1], "d-001")    # dispatch_id
        self.assertEqual(args[2], "# Dispatch body")  # instruction

    def test_non_claude_provider_routes_to_deliver_provider(self):
        self._insert_queued("d-codex", project_id="test-proj")
        self._write_bundle("d-codex", provider="codex")

        with patch("pool_worker_runner._deliver_provider", return_value=EXIT_OK) as mock_deliver:
            result = self._run(project_id="test-proj")

        self.assertEqual(result, EXIT_OK)
        mock_deliver.assert_called_once()
        self.assertEqual(mock_deliver.call_args.args[0], "codex")  # provider


# ---------------------------------------------------------------------------
# Test: empty queue
# ---------------------------------------------------------------------------

class TestEmptyQueue(_DbTestCase):

    def test_empty_queue_returns_no_work_no_delivery(self):
        """Empty queue → EXIT_NO_WORK; delivery never called."""
        with patch("pool_worker_runner._deliver_claude") as mock_deliver:
            result = self._run(project_id="test-proj")

        self.assertEqual(result, EXIT_NO_WORK)
        mock_deliver.assert_not_called()

    def test_cross_project_dispatch_invisible_returns_no_work(self):
        """ADR-007: dispatch queued for project-b is invisible to project-a worker."""
        self._insert_queued("d-other", project_id="project-b")
        self._write_bundle("d-other")

        with patch("pool_worker_runner._deliver_claude") as mock_deliver:
            result = self._run(project_id="project-a")

        self.assertEqual(result, EXIT_NO_WORK)
        mock_deliver.assert_not_called()


# ---------------------------------------------------------------------------
# Test: FM-4 project_id mismatch guard
# ---------------------------------------------------------------------------

class TestProjectIdMismatchGuard(_DbTestCase):
    """ADR-018 FM-4: post-claim project_id verification refuses cross-project dispatch."""

    def test_mismatch_refuses_dispatch_no_delivery(self):
        """Simulated edge case: claim returns dispatch from project-b, worker is project-a."""
        # Insert a dispatch belonging to project-b (already claimed to avoid state-machine guard)
        with get_connection(self.state_dir) as c:
            c.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state, priority)"
                " VALUES ('d-cross', 'project-b', 'claimed', 'P2')",
            )
            c.commit()
        self._write_bundle("d-cross")

        # Patch claim to return the cross-project dispatch_id (defense-in-depth scenario)
        with patch("pool_worker_runner.claim_next_queued_dispatch", return_value="d-cross"):
            with patch("pool_worker_runner._deliver_claude") as mock_deliver:
                result = self._run(project_id="project-a")

        self.assertEqual(result, EXIT_PROJECT_MISMATCH)
        mock_deliver.assert_not_called()


# ---------------------------------------------------------------------------
# Test: missing bundle
# ---------------------------------------------------------------------------

class TestBundleMissing(_DbTestCase):

    def test_missing_bundle_json_returns_bundle_missing(self):
        self._insert_queued("d-nobundle", project_id="test-proj")
        # No bundle written

        result = self._run(project_id="test-proj")
        self.assertEqual(result, EXIT_BUNDLE_MISSING)

    def test_missing_prompt_txt_returns_bundle_missing(self):
        self._insert_queued("d-noprompt", project_id="test-proj")
        bundle_dir = self.dispatch_dir / "d-noprompt"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "bundle.json").write_text(
            json.dumps({"dispatch_id": "d-noprompt", "target_profile": {"provider": "claude"}, "gate": ""}),
            encoding="utf-8",
        )
        # No prompt.txt

        result = self._run(project_id="test-proj")
        self.assertEqual(result, EXIT_BUNDLE_MISSING)


if __name__ == "__main__":
    unittest.main()
