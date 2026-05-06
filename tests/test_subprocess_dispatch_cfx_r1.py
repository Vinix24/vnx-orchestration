#!/usr/bin/env python3
"""Codex round-1 regression tests for PR #320 (CFX-1).

Finding 1 — `deliver_via_subprocess` was promoting the dispatch manifest
into ``dispatches/completed/`` *before* the fail-closed checks (non-zero
returncode, timeout-kill).  Failed dispatches were therefore recorded and
later drained as completed work instead of going to ``dead_letter/``.

OI-1319 update: dead_letter promotion is now deferred from
``_classify_completion()`` to ``_handle_final_failure()`` so that transient
failures do not pre-bucket the manifest before retries are exhausted.

Corrected ordering:
    success path        → manifest promoted to completed/ (in _classify_completion)
    non-zero returncode → manifest NOT promoted in _classify_completion;
                         deferred to _handle_final_failure after retries done
    timeout kill        → same deferral
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch  # noqa: E402


def _make_adapter(returncode, was_timed_out=False, session_id="sess"):
    deliver_result = MagicMock()
    deliver_result.success = True

    obs_result = MagicMock()
    obs_result.transport_state = {"returncode": returncode}

    adapter = MagicMock()
    adapter.deliver.return_value = deliver_result
    adapter.read_events_with_timeout.return_value = iter([])
    adapter.get_session_id.return_value = session_id
    adapter.observe.return_value = obs_result
    adapter.was_timed_out.return_value = was_timed_out
    adapter._get_event_store.return_value = None
    adapter.trigger_report_pipeline.return_value = None
    return adapter


def _common_patches(promote_mock):
    return [
        patch("subprocess_dispatch._inject_skill_context", return_value="instr"),
        patch("subprocess_dispatch._inject_permission_profile", return_value="instr"),
        patch("subprocess_dispatch._resolve_agent_cwd", return_value=None),
        patch("subprocess_dispatch._write_manifest", return_value="/tmp/m.json"),
        patch("subprocess_dispatch._promote_manifest", promote_mock),
        patch("subprocess_dispatch._capture_dispatch_parameters"),
        patch("subprocess_dispatch._capture_dispatch_outcome"),
    ]


class TestManifestStageRoutedByOutcome(unittest.TestCase):
    """Failed dispatches must NOT be promoted to completed/."""

    def _run(self, returncode, was_timed_out=False):
        promote = MagicMock(return_value="/tmp/destination.json")
        adapter = _make_adapter(returncode=returncode, was_timed_out=was_timed_out)
        cms = _common_patches(promote) + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-cfx-r1",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()
        return result, promote

    def test_nonzero_exit_does_not_promote_in_classify_completion(self):
        """OI-1319: _classify_completion must NOT call _promote_manifest(dead_letter).

        dead_letter promotion is deferred to _handle_final_failure() so that a
        transient failure followed by a successful retry cannot leave the manifest
        in both dead_letter/ and completed/ (dual-bucket regression).
        """
        result, promote = self._run(returncode=1)
        self.assertFalse(result.success)
        dead_calls = [
            c for c in promote.call_args_list
            if c.kwargs.get("stage") == "dead_letter"
        ]
        self.assertEqual(
            len(dead_calls), 0,
            "deliver_via_subprocess/_classify_completion must NOT promote to dead_letter "
            "on a single failure — deferral to _handle_final_failure prevents dual-bucket "
            f"(OI-1319). Found calls: {dead_calls}",
        )

    def test_timeout_does_not_promote_in_classify_completion(self):
        """OI-1319: same dead_letter deferral applies to timeout-terminated dispatches."""
        result, promote = self._run(returncode=None, was_timed_out=True)
        self.assertFalse(result.success)
        dead_calls = [
            c for c in promote.call_args_list
            if c.kwargs.get("stage") == "dead_letter"
        ]
        self.assertEqual(
            len(dead_calls), 0,
            "deliver_via_subprocess/_classify_completion must NOT promote to dead_letter "
            f"on timeout — deferred to _handle_final_failure (OI-1319). Found: {dead_calls}",
        )

    def test_clean_success_routes_manifest_to_completed(self):
        result, promote = self._run(returncode=0)
        self.assertTrue(result.success)
        promote.assert_called_once_with("d-cfx-r1", stage="completed")

    def test_completed_path_not_called_on_failure(self):
        """Belt-and-braces: collect every promote call and assert no call
        slipped through with stage=completed when the dispatch failed."""
        result, promote = self._run(returncode=137)  # SIGKILL-ish exit
        self.assertFalse(result.success)
        for call in promote.call_args_list:
            self.assertNotEqual(
                call.kwargs.get("stage"),
                "completed",
                "failed dispatch must never promote manifest to completed/",
            )


class TestPromoteManifestStageSemantics(unittest.TestCase):
    """_promote_manifest itself must reject unexpected stages and move
    (not copy) so a failed dispatch never has a parallel record."""

    def test_rejects_invalid_stage(self):
        self.assertIsNone(
            subprocess_dispatch._promote_manifest("any-id", stage="bogus")
        )

    def test_moves_manifest_to_completed(self, *_):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("subprocess_dispatch._dispatch_manifest_dir") as mock_dir:
                mock_dir.side_effect = lambda stage, did: tmp_path / stage / did
                src_dir = tmp_path / "active" / "d1"
                src_dir.mkdir(parents=True)
                (src_dir / "manifest.json").write_text(json.dumps({"ok": True}))
                dst = subprocess_dispatch._promote_manifest("d1", stage="completed")
                self.assertIsNotNone(dst)
                self.assertFalse((src_dir / "manifest.json").exists())
                self.assertTrue((tmp_path / "completed" / "d1" / "manifest.json").exists())

    def test_moves_manifest_to_dead_letter(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch("subprocess_dispatch._dispatch_manifest_dir") as mock_dir:
                mock_dir.side_effect = lambda stage, did: tmp_path / stage / did
                src_dir = tmp_path / "active" / "d2"
                src_dir.mkdir(parents=True)
                (src_dir / "manifest.json").write_text(json.dumps({"ok": False}))
                dst = subprocess_dispatch._promote_manifest("d2", stage="dead_letter")
                self.assertIsNotNone(dst)
                self.assertFalse((src_dir / "manifest.json").exists())
                self.assertTrue((tmp_path / "dead_letter" / "d2" / "manifest.json").exists())
                # No parallel record in completed/.
                self.assertFalse((tmp_path / "completed" / "d2").exists())


if __name__ == "__main__":
    unittest.main()
