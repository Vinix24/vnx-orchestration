#!/usr/bin/env python3
"""Tests for PR-197: F37 Hardening (OI-1062, OI-1063, OI-1064, OI-1066).

Gate: gate_f37_hardening

Covers:
OI-1062 — Event archive in subprocess_dispatch finally block
OI-1063 — FALLBACK_POLICY constant in auto_report_contract
OI-1064 — No auto- prefix in assembler filename
OI-1066 — Gate sidecar JSON written by materialize_artifacts
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

SCRIPTS_ROOT = str(Path(__file__).resolve().parent.parent / "scripts")
SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
for _p in (SCRIPTS_LIB, SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ─── OI-1062: Event archive in deliver_via_subprocess ────────────────────────

class TestEventArchiveOnCompletion(unittest.TestCase):
    """archive() must be called in the finally block after subprocess completes."""

    def _run_deliver(self, mock_adapter_cls, archive_raises=False):
        """Helper: run deliver_via_subprocess with a mocked adapter."""
        mock_event_store = MagicMock()
        if archive_raises:
            mock_event_store.archive.side_effect = RuntimeError("archive error")

        mock_adapter = MagicMock()
        mock_adapter._get_event_store.return_value = mock_event_store
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.trigger_report_pipeline.return_value = True
        mock_adapter_cls.return_value = mock_adapter

        from subprocess_dispatch import deliver_via_subprocess
        deliver_via_subprocess("T1", "instruction", "sonnet", "dispatch-123")
        return mock_event_store

    @patch("subprocess_dispatch.SubprocessAdapter")
    def test_archive_called_in_finally_on_success(self, mock_cls):
        es = self._run_deliver(mock_cls)
        es.archive.assert_called_once_with("T1", "dispatch-123")

    @patch("subprocess_dispatch.SubprocessAdapter")
    def test_archive_called_before_trigger(self, mock_cls):
        """archive() must be called before trigger_report_pipeline()."""
        call_order = []

        mock_event_store = MagicMock()
        mock_event_store.archive.side_effect = lambda *a: call_order.append("archive")

        mock_adapter = MagicMock()
        mock_adapter._get_event_store.return_value = mock_event_store
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.trigger_report_pipeline.side_effect = lambda *a, **kw: call_order.append("trigger")
        mock_cls.return_value = mock_adapter

        from subprocess_dispatch import deliver_via_subprocess
        deliver_via_subprocess("T1", "instruction", "sonnet", "dispatch-123")

        self.assertEqual(call_order, ["archive", "trigger"])

    @patch("subprocess_dispatch.SubprocessAdapter")
    def test_archive_called_even_when_subprocess_raises(self, mock_cls):
        """archive() runs in finally even when read_events raises."""
        mock_event_store = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter._get_event_store.return_value = mock_event_store
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.side_effect = RuntimeError("stream error")
        mock_adapter.trigger_report_pipeline.return_value = True
        mock_cls.return_value = mock_adapter

        from subprocess_dispatch import deliver_via_subprocess
        result = deliver_via_subprocess("T1", "instruction", "sonnet", "dispatch-abc")
        self.assertFalse(result)
        mock_event_store.archive.assert_called_once_with("T1", "dispatch-abc")

    @patch("subprocess_dispatch.SubprocessAdapter")
    def test_archive_failure_does_not_crash(self, mock_cls):
        """archive() exception must not propagate — pipeline must remain non-crashing."""
        es = self._run_deliver(mock_cls, archive_raises=True)
        es.archive.assert_called_once()  # was called; exception was swallowed

    @patch("subprocess_dispatch.SubprocessAdapter")
    def test_no_archive_when_event_store_unavailable(self, mock_cls):
        """When _get_event_store() returns None, no crash."""
        mock_adapter = MagicMock()
        mock_adapter._get_event_store.return_value = None
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])
        mock_adapter.trigger_report_pipeline.return_value = True
        mock_cls.return_value = mock_adapter

        from subprocess_dispatch import deliver_via_subprocess
        # Should not raise
        deliver_via_subprocess("T1", "instruction", "sonnet", "dispatch-xyz")


# ─── OI-1063: FALLBACK_POLICY in auto_report_contract ────────────────────────

class TestFallbackPolicy(unittest.TestCase):

    def test_fallback_policy_exists(self):
        from auto_report_contract import FALLBACK_POLICY
        self.assertIsInstance(FALLBACK_POLICY, str)

    def test_fallback_policy_documents_env_var(self):
        from auto_report_contract import FALLBACK_POLICY
        self.assertIn("VNX_AUTO_REPORT", FALLBACK_POLICY)

    def test_fallback_policy_documents_pipeline_failure(self):
        from auto_report_contract import FALLBACK_POLICY
        # Must mention what happens when the pipeline fails
        lower = FALLBACK_POLICY.lower()
        self.assertTrue(
            "failure" in lower or "fail" in lower or "manual" in lower,
            "FALLBACK_POLICY must describe pipeline failure behavior",
        )

    def test_fallback_policy_documents_gate_path(self):
        from auto_report_contract import FALLBACK_POLICY
        # Codex/Gemini gate separation must be mentioned
        lower = FALLBACK_POLICY.lower()
        self.assertTrue(
            "gate" in lower or "codex" in lower or "gemini" in lower,
            "FALLBACK_POLICY must mention gate report path",
        )

    def test_fallback_policy_nonempty(self):
        from auto_report_contract import FALLBACK_POLICY
        self.assertGreater(len(FALLBACK_POLICY.strip()), 50)


# ─── OI-1064: No auto- prefix in filename ────────────────────────────────────

class TestFilenameNoAutoPrefix(unittest.TestCase):

    def test_assembled_report_filename_has_no_auto_prefix(self):
        from report_assembler import assemble, write_report
        with tempfile.TemporaryDirectory() as tmpdir:
            result = assemble(
                dispatch_id="20260408-999-hardening-test-A",
                terminal="T1",
                track="A",
                gate="gate_f37_hardening",
                pr_id="PR-197",
            )
            _, md_path = write_report(result, vnx_data_dir=Path(tmpdir))
        self.assertIsNotNone(md_path)
        self.assertNotIn("-auto-", md_path.name)

    def test_assembled_report_filename_matches_convention(self):
        """Filename: {YYYYMMDD}-{HHMMSS}-{track}-{short_title}.md"""
        from report_assembler import assemble, write_report
        import re
        with tempfile.TemporaryDirectory() as tmpdir:
            result = assemble(
                dispatch_id="20260408-999-hardening-test-A",
                terminal="T1",
                track="A",
                gate="gate_f37_hardening",
                pr_id="PR-197",
            )
            _, md_path = write_report(result, vnx_data_dir=Path(tmpdir))
        # Pattern: YYYYMMDD-HHMMSS-A-<slug>.md
        self.assertRegex(md_path.name, r"^\d{8}-\d{6}-A-.+\.md$")

    def test_auto_generated_flag_still_true(self):
        """Removing the filename prefix must not break auto_generated metadata."""
        from report_assembler import assemble
        result = assemble(
            dispatch_id="20260408-999-hardening-test-A",
            terminal="T1",
            track="A",
            gate="gate_f37_hardening",
            pr_id="PR-197",
        )
        self.assertTrue(result.report.metadata.auto_generated)


# ─── OI-1066: Gate sidecar JSON in materialize_artifacts ─────────────────────

class TestGateSidecar(unittest.TestCase):

    def _make_dirs(self, base: Path):
        requests = base / "state" / "review_gates" / "requests"
        results = base / "state" / "review_gates" / "results"
        # reports_dir must be a direct child of base (.vnx-data) so that
        # reports_dir.parent resolves to base for the sidecar path calculation
        reports = base / "unified_reports"
        for d in (requests, results, reports):
            d.mkdir(parents=True, exist_ok=True)
        return requests, results, reports

    def _base_payload(self, reports_dir: Path) -> dict:
        return {
            "gate": "gemini_gate",
            "pr_id": "PR-197",
            "pr_number": 197,
            "branch": "fix/f37-hardening",
            "report_path": str(reports_dir / "test-report.md"),
        }

    def test_sidecar_written_after_materialize(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts
            materialize_artifacts(
                gate="gemini_gate",
                pr_number=197,
                pr_id="PR-197",
                stdout="Finding 1\nFinding 2\nFinding 3\n",
                request_payload=payload,
                duration_seconds=5.0,
                requests_dir=requests,
                results_dir=results,
                reports_dir=reports,
            )

            sidecar_dir = base / "state" / "report_pipeline"
            sidecars = list(sidecar_dir.glob("gate-gemini_gate-pr-*.json"))
            self.assertEqual(len(sidecars), 1, "Expected exactly 1 sidecar file")

    def test_sidecar_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts
            materialize_artifacts(
                gate="gemini_gate",
                pr_number=197,
                pr_id="PR-197",
                stdout="Line one\nLine two\nLine three\n",
                request_payload=payload,
                duration_seconds=3.0,
                requests_dir=requests,
                results_dir=results,
                reports_dir=reports,
            )

            sidecar_path = base / "state" / "report_pipeline" / "gate-gemini_gate-pr-197.json"
            self.assertTrue(sidecar_path.exists())
            data = json.loads(sidecar_path.read_text())

            for field in ("gate", "pr_id", "pr_number", "status", "source", "recorded_at"):
                self.assertIn(field, data, f"Missing field: {field}")

    def test_sidecar_status_is_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts
            materialize_artifacts(
                gate="gemini_gate",
                pr_number=197,
                pr_id="PR-197",
                stdout="A\nB\nC\n",
                request_payload=payload,
                duration_seconds=2.0,
                requests_dir=requests,
                results_dir=results,
                reports_dir=reports,
            )

            sidecar_path = base / "state" / "report_pipeline" / "gate-gemini_gate-pr-197.json"
            data = json.loads(sidecar_path.read_text())
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["source"], "gate_runner")

    def test_sidecar_source_is_gate_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts
            materialize_artifacts(
                gate="codex_gate",
                pr_number=99,
                pr_id="PR-99",
                stdout="Finding\nAnother\nThird\n",
                request_payload={**payload, "gate": "codex_gate", "pr_number": 99,
                                  "pr_id": "PR-99",
                                  "report_path": str(reports / "codex-report.md")},
                duration_seconds=1.0,
                requests_dir=requests,
                results_dir=results,
                reports_dir=reports,
            )

            sidecar_dir = base / "state" / "report_pipeline"
            sidecars = list(sidecar_dir.glob("*.json"))
            self.assertGreater(len(sidecars), 0)
            data = json.loads(sidecars[0].read_text())
            self.assertEqual(data["source"], "gate_runner")

    def test_sidecar_failure_does_not_propagate(self):
        """Sidecar write failure must not crash materialize_artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts

            # Patch Path.mkdir to fail only for sidecar dir
            original_mkdir = Path.mkdir

            def patched_mkdir(self, *args, **kwargs):
                if "report_pipeline" in str(self):
                    raise OSError("simulated disk full")
                return original_mkdir(self, *args, **kwargs)

            with patch.object(Path, "mkdir", patched_mkdir):
                # Should not raise
                result = materialize_artifacts(
                    gate="gemini_gate",
                    pr_number=197,
                    pr_id="PR-197",
                    stdout="X\nY\nZ\n",
                    request_payload=payload,
                    duration_seconds=1.0,
                    requests_dir=requests,
                    results_dir=results,
                    reports_dir=reports,
                )
            self.assertEqual(result.get("status"), "completed")

    def test_sidecar_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            requests, results, reports = self._make_dirs(base)
            payload = self._base_payload(reports)

            from gate_artifacts import materialize_artifacts
            materialize_artifacts(
                gate="gemini_gate",
                pr_number=197,
                pr_id="PR-197",
                stdout="A\nB\nC\n",
                request_payload=payload,
                duration_seconds=2.0,
                requests_dir=requests,
                results_dir=results,
                reports_dir=reports,
            )

            sidecar_path = base / "state" / "report_pipeline" / "gate-gemini_gate-pr-197.json"
            # Must parse as valid JSON
            data = json.loads(sidecar_path.read_text())
            self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
