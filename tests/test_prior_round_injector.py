#!/usr/bin/env python3
"""Unit tests for prior_round_injector.py (Wave 5 P0).

Coverage:
  - Graceful empty when no gate results exist
  - Fetches codex_gate blocking findings
  - Fetches gemini_review blocking findings
  - Blocking before advisory in priority order
  - Scope filter prioritizes dispatch_paths overlap
  - Most recent round first when multiple gate result timestamps differ
  - Budget truncates at max_chars
  - format_findings_section produces valid markdown
  - Graceful empty for unknown pr_id
  - Anti-anchoring instruction present in formatted section
  - LRU cache invalidates after 60s (via time-bucket separation)
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from prior_round_injector import (
    MAX_INJECTION_CHARS,
    PriorFinding,
    _extract_file_paths,
    _fetch_cached,
    fetch_prior_findings,
    format_findings_section,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_results_dir(tmp: Path) -> Path:
    d = tmp / "review_gates" / "results"
    d.mkdir(parents=True)
    return d


def _write_gate_file(
    results_dir: Path,
    pr_id: str,
    gate: str,
    blocking: list | None = None,
    advisory: list | None = None,
    recorded_at: str = "2026-04-01T10:00:00Z",
    contract_hash: str = "abc123",
) -> Path:
    data = {
        "gate": gate,
        "pr_id": pr_id,
        "blocking_findings": [{"message": m} for m in (blocking or [])],
        "advisory_findings": [{"message": m} for m in (advisory or [])],
        "recorded_at": recorded_at,
        "contract_hash": contract_hash,
        "status": "completed",
    }
    path = results_dir / f"pr-{pr_id}-{gate}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPriorRoundInjector(unittest.TestCase):

    def setUp(self):
        # Flush LRU cache before each test to guarantee isolation.
        _fetch_cached.cache_clear()

    def test_no_findings_when_pr_has_no_gate_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            _make_results_dir(state_dir)
            findings = fetch_prior_findings("999", state_dir=state_dir)
            self.assertEqual(findings, [])

    def test_no_findings_for_unknown_pr_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(results_dir, "42", "codex_gate", blocking=["Some issue."])
            findings = fetch_prior_findings("9999", state_dir=state_dir)
            self.assertEqual(findings, [])

    def test_fetches_codex_blocking_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "42", "codex_gate",
                blocking=["Missing migration in scripts/lib/foo.py:10."],
            )
            findings = fetch_prior_findings("42", state_dir=state_dir)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].gate, "codex_gate")
            self.assertEqual(findings[0].severity, "blocking")
            self.assertIn("Missing migration", findings[0].message)

    def test_fetches_gemini_blocking_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "43", "gemini_review",
                blocking=["SSE reuse-after-close in dashboard/foo.ts:55."],
            )
            findings = fetch_prior_findings("43", state_dir=state_dir)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].gate, "gemini_review")
            self.assertEqual(findings[0].severity, "blocking")

    def test_blocking_before_advisory_in_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "44", "codex_gate",
                blocking=["Blocker: table missing."],
                advisory=["Advisory: consider refactoring."],
            )
            findings = fetch_prior_findings("44", state_dir=state_dir)
            self.assertGreaterEqual(len(findings), 2)
            severity_order = [f.severity for f in findings]
            blocking_idx = severity_order.index("blocking")
            advisory_idx = severity_order.index("advisory")
            self.assertLess(blocking_idx, advisory_idx)

    def test_scope_filter_prioritizes_dispatch_paths_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "45", "codex_gate",
                advisory=[
                    "Unrelated issue in other/module.py:5.",
                    "Matched issue in scripts/lib/target.py:10.",
                ],
            )
            findings = fetch_prior_findings(
                "45",
                dispatch_paths=["scripts/lib/target.py"],
                state_dir=state_dir,
            )
            self.assertGreaterEqual(len(findings), 1)
            # Matched finding should come first
            first = findings[0]
            self.assertIn("target.py", first.message)

    def test_most_recent_round_first_when_multiple_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            # codex_gate has newer recorded_at than gemini_review
            _write_gate_file(
                results_dir, "46", "codex_gate",
                blocking=["Codex recent finding."],
                recorded_at="2026-04-10T12:00:00Z",
            )
            _write_gate_file(
                results_dir, "46", "gemini_review",
                blocking=["Gemini older finding."],
                recorded_at="2026-04-09T08:00:00Z",
            )
            findings = fetch_prior_findings("46", state_dir=state_dir)
            # Codex (newer) findings should appear before Gemini (older)
            gates = [f.gate for f in findings]
            self.assertIn("codex_gate", gates)
            self.assertIn("gemini_review", gates)
            self.assertEqual(gates.index("codex_gate"), 0)

    def test_budget_truncates_at_max_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            # Write many large advisory findings that exceed MAX_INJECTION_CHARS
            large_messages = [f"Advisory finding #{i}: " + ("x" * 200) for i in range(20)]
            _write_gate_file(
                results_dir, "47", "codex_gate",
                advisory=large_messages,
            )
            findings = fetch_prior_findings("47", max_chars=MAX_INJECTION_CHARS, state_dir=state_dir)
            section = format_findings_section(findings)
            self.assertLessEqual(len(section), MAX_INJECTION_CHARS)

    def test_format_section_is_valid_markdown(self):
        findings = [
            PriorFinding(
                pr_id="10",
                gate="codex_gate",
                severity="blocking",
                message="Atomic write missing in scripts/lib/foo.py:42.",
                file_paths=("scripts/lib/foo.py",),
                contract_hash="abc",
                recorded_at="2026-04-01T10:00:00Z",
            ),
            PriorFinding(
                pr_id="10",
                gate="gemini_review",
                severity="advisory",
                message="Consider refactoring this section.",
                file_paths=(),
                contract_hash="def",
                recorded_at="2026-04-01T10:00:00Z",
            ),
        ]
        section = format_findings_section(findings)
        self.assertTrue(section.startswith("## PRIOR ROUND REVIEW FINDINGS"))
        self.assertIn("### Blocking", section)
        self.assertIn("### Advisory", section)
        self.assertIn("codex_gate", section)
        self.assertIn("gemini_review", section)
        self.assertIn("Atomic write missing", section)
        self.assertIn("Consider refactoring", section)

    def test_anti_anchoring_instruction_present_in_formatted_section(self):
        findings = [
            PriorFinding(
                pr_id="11",
                gate="codex_gate",
                severity="blocking",
                message="Some blocker.",
                file_paths=(),
                contract_hash="abc",
                recorded_at="2026-04-01T10:00:00Z",
            ),
        ]
        section = format_findings_section(findings)
        self.assertIn("Anti-anchoring notice", section)
        self.assertIn("Re-read current code at touched lines", section)
        self.assertIn("subsequent rounds", section)

    def test_format_section_empty_when_no_findings(self):
        section = format_findings_section([])
        self.assertEqual(section, "")

    def test_lru_cache_invalidates_after_60s(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "48", "codex_gate",
                blocking=["Initial finding."],
            )

            bucket_a = int(time.time() // 60)
            result_a = _fetch_cached("48", (), MAX_INJECTION_CHARS, state_dir, bucket_a)
            self.assertEqual(len(result_a), 1)
            self.assertIn("Initial finding", result_a[0].message)

            # Update file to simulate new findings in the next 60s window
            _write_gate_file(
                results_dir, "48", "codex_gate",
                blocking=["Updated finding."],
            )

            # Same bucket: should still return cached (old) result
            result_same = _fetch_cached("48", (), MAX_INJECTION_CHARS, state_dir, bucket_a)
            self.assertEqual(len(result_same), 1)
            self.assertIn("Initial finding", result_same[0].message)

            # Different bucket: should return fresh (new) result
            bucket_b = bucket_a + 1
            result_b = _fetch_cached("48", (), MAX_INJECTION_CHARS, state_dir, bucket_b)
            self.assertEqual(len(result_b), 1)
            self.assertIn("Updated finding", result_b[0].message)

    def test_extract_file_paths_from_message(self):
        msg = "Issue in scripts/lib/foo.py:10. Also see dashboard/bar.ts:5-20."
        paths = _extract_file_paths(msg)
        self.assertIn("scripts/lib/foo.py", paths)
        self.assertIn("dashboard/bar.ts", paths)

    def test_contract_hash_recorded_in_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            results_dir = _make_results_dir(state_dir)
            _write_gate_file(
                results_dir, "49", "codex_gate",
                blocking=["Some blocker."],
                contract_hash="deadbeef1234",
            )
            findings = fetch_prior_findings("49", state_dir=state_dir)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].contract_hash, "deadbeef1234")


if __name__ == "__main__":
    unittest.main()
