#!/usr/bin/env python3
"""Tests for dispatch_prepare.py, worker_rules_footer.py, report_body_contract.py.

T1 coverage:
- prepare() output ordering: permission preamble → skill context → footer sentinel →
  directive sections → trailer sentinel.
- Sub-flag guards: VNX_WORKER_RULES_FOOTER=0 and VNX_REPORT_CONTRACT_DIRECTIVE=0
  suppress their respective blocks.
- Subprocess golden: VNX_SHARED_PREPARE=0 keeps _assemble_instruction byte-identical
  to pre-T1 behavior (no footer/directive/trailer).
- Subprocess VNX_SHARED_PREPARE=1: _assemble_instruction delegates to prepare().
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from dispatch_prepare import prepare, _WORKER_RULES_FOOTER_SENTINEL, _TRAILER_SENTINEL
from subprocess_dispatch_internals.delivery import _assemble_instruction
import worker_rules_footer
import report_body_contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patched_prepare(instruction="INSTRUCTION", role="backend-developer",
                     dispatch_id="test-dispatch-t1", **kw):
    """Run prepare() with mocked injection seams for deterministic output."""
    fake_skill_output = "## Skill Context\n\n" + instruction
    fake_perm_output = "## Permission Profile\n\n---\n\n" + fake_skill_output

    with patch(
        "subprocess_dispatch_internals.skill_injection._inject_skill_context",
        return_value=fake_skill_output,
    ):
        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
            return_value=fake_perm_output,
        ):
            return prepare(
                instruction=instruction,
                role=role,
                dispatch_id=dispatch_id,
                **kw,
            )


# ---------------------------------------------------------------------------
# worker_rules_footer module
# ---------------------------------------------------------------------------

class TestWorkerRulesFooter(unittest.TestCase):
    def test_sentinel_at_start(self):
        """build() output must start with the sentinel."""
        result = worker_rules_footer.build("backend-developer", "my-dispatch-id")
        self.assertTrue(
            result.startswith(worker_rules_footer.SENTINEL),
            f"Expected output to start with sentinel; got: {result[:80]!r}",
        )

    def test_contains_report_first_rule(self):
        """Footer must state write-report-first rule."""
        result = worker_rules_footer.build("backend-developer", "my-dispatch")
        self.assertIn("report", result.lower())
        self.assertIn("FIRST", result)

    def test_contains_receipt_last_rule(self):
        """Footer must state emit-receipt-last rule."""
        result = worker_rules_footer.build("backend-developer", "my-dispatch")
        self.assertIn("receipt", result.lower())
        self.assertIn("LAST", result)

    def test_contains_no_tests_passed_rule(self):
        """Footer must prohibit bare 'tests passed' without command name."""
        result = worker_rules_footer.build("backend-developer", "my-dispatch")
        self.assertIn("tests passed", result.lower())

    def test_dispatch_id_included(self):
        """Footer must embed the dispatch_id."""
        result = worker_rules_footer.build("some-role", "abc-123-dispatch")
        self.assertIn("abc-123-dispatch", result)

    def test_role_included(self):
        """Footer must include the role."""
        result = worker_rules_footer.build("quality-engineer", "disp-xyz")
        self.assertIn("quality-engineer", result)

    def test_role_none_does_not_crash(self):
        """build() with role=None must not raise."""
        result = worker_rules_footer.build(None, "test-dispatch")
        self.assertIsInstance(result, str)

    def test_permission_enforcement_included(self):
        """Footer must echo the permission_enforcement value."""
        result = worker_rules_footer.build("dev", "disp", permission_enforcement="strict")
        self.assertIn("strict", result)


# ---------------------------------------------------------------------------
# report_body_contract module
# ---------------------------------------------------------------------------

class TestReportBodyContract(unittest.TestCase):
    def test_sentinel_present(self):
        """build_directive() must include the directive sentinel."""
        result = report_body_contract.build_directive("my-dispatch")
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)

    def test_required_sections_present(self):
        """build_directive() must list all four required sections."""
        result = report_body_contract.build_directive("my-dispatch")
        for section in ("## Summary", "## Changes", "## Verification", "## Open Items"):
            self.assertIn(section, result, f"Missing required section: {section}")

    def test_pr_section_added_when_pr_id_set(self):
        """build_directive() adds ## PR when pr_id is provided."""
        result = report_body_contract.build_directive("my-dispatch", pr_id="PR-42")
        self.assertIn("## PR", result)

    def test_pr_section_absent_when_no_pr_id(self):
        """build_directive() omits ## PR when pr_id is None."""
        result = report_body_contract.build_directive("my-dispatch")
        self.assertNotIn("## PR", result)

    def test_dispatch_id_in_directive(self):
        """build_directive() must embed the dispatch_id."""
        result = report_body_contract.build_directive("disp-abc-789")
        self.assertIn("disp-abc-789", result)

    def test_validate_body_raises_not_implemented(self):
        """validate_body() must raise NotImplementedError (T2 deferral)."""
        with self.assertRaises(NotImplementedError):
            report_body_contract.validate_body("any text", {})


# ---------------------------------------------------------------------------
# dispatch_prepare.prepare() — ordering and content
# ---------------------------------------------------------------------------

class TestPrepareOrdering(unittest.TestCase):
    """prepare() must produce blocks in the specified order."""

    def test_prepare_contains_all_required_blocks(self):
        """prepare() output contains: preamble text, skill context, footer sentinel,
        directive sections — trailer is NOT in prepare() (each lane appends it)."""
        result = _patched_prepare()

        self.assertIn("## Permission Profile", result, "permission preamble missing")
        self.assertIn("## Skill Context", result, "skill context missing")
        self.assertIn(_WORKER_RULES_FOOTER_SENTINEL, result, "worker-rules footer sentinel missing")
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result, "report contract directive missing")
        self.assertNotIn(_TRAILER_SENTINEL, result, "trailer sentinel must NOT be in prepare() output")

    def test_prepare_ordering_preamble_before_skill(self):
        """Permission preamble appears before skill context in output."""
        result = _patched_prepare()
        preamble_pos = result.index("## Permission Profile")
        skill_pos = result.index("## Skill Context")
        self.assertLess(preamble_pos, skill_pos, "preamble must precede skill context")

    def test_prepare_ordering_skill_before_footer(self):
        """Skill context appears before worker-rules footer."""
        result = _patched_prepare()
        skill_pos = result.index("## Skill Context")
        footer_pos = result.index(_WORKER_RULES_FOOTER_SENTINEL)
        self.assertLess(skill_pos, footer_pos, "skill context must precede footer")

    def test_prepare_ordering_footer_before_directive(self):
        """Worker-rules footer appears before report-contract directive."""
        result = _patched_prepare()
        footer_pos = result.index(_WORKER_RULES_FOOTER_SENTINEL)
        directive_pos = result.index("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->")
        self.assertLess(footer_pos, directive_pos, "footer must precede directive")

    def test_prepare_directive_is_last_block(self):
        """Report-contract directive is the last meaningful block in prepare() output
        (trailer sentinel is appended by each lane after prepare(), not by prepare() itself)."""
        result = _patched_prepare()
        directive_pos = result.index("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->")
        self.assertGreaterEqual(directive_pos, 0, "directive must be present")
        # Nothing meaningful after the directive (only whitespace)
        after_directive = result[directive_pos + len("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->"):]
        self.assertNotIn(_TRAILER_SENTINEL, after_directive,
                         "trailer must NOT appear in prepare() output")

    def test_prepare_no_trailer(self):
        """prepare() must NOT append the trailer sentinel — lanes own that step."""
        result = _patched_prepare()
        self.assertNotIn(_TRAILER_SENTINEL, result,
                         "prepare() must not include the trailer sentinel")


class TestPrepareSubFlags(unittest.TestCase):
    """Sub-flag guards: VNX_WORKER_RULES_FOOTER and VNX_REPORT_CONTRACT_DIRECTIVE."""

    def test_footer_omitted_when_flag_off(self):
        """VNX_WORKER_RULES_FOOTER=0 suppresses the worker-rules footer."""
        with patch.dict(os.environ, {"VNX_WORKER_RULES_FOOTER": "0",
                                      "VNX_REPORT_CONTRACT_DIRECTIVE": "1"}):
            result = _patched_prepare()
        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result, "footer must be absent when flag=0")
        # Directive must still be present; trailer is not part of prepare()
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)
        self.assertNotIn(_TRAILER_SENTINEL, result)

    def test_directive_omitted_when_flag_off(self):
        """VNX_REPORT_CONTRACT_DIRECTIVE=0 suppresses the report-contract directive."""
        with patch.dict(os.environ, {"VNX_WORKER_RULES_FOOTER": "1",
                                      "VNX_REPORT_CONTRACT_DIRECTIVE": "0"}):
            result = _patched_prepare()
        self.assertNotIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result, "directive must be absent when flag=0")
        # Footer must still be present; trailer is not part of prepare()
        self.assertIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertNotIn(_TRAILER_SENTINEL, result)

    def test_both_off_omits_both(self):
        """Both sub-flags off: neither footer nor directive nor trailer appears."""
        with patch.dict(os.environ, {"VNX_WORKER_RULES_FOOTER": "0",
                                      "VNX_REPORT_CONTRACT_DIRECTIVE": "0"}):
            result = _patched_prepare()
        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertNotIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)
        self.assertNotIn(_TRAILER_SENTINEL, result)

    def test_both_on_by_default(self):
        """With no env overrides, both footer and directive are present."""
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ("VNX_WORKER_RULES_FOOTER", "VNX_REPORT_CONTRACT_DIRECTIVE")}
        with patch.dict(os.environ, env_clean, clear=True):
            result = _patched_prepare()
        self.assertIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)

    def test_footer_idempotent_guard(self):
        """Footer sentinel in existing body prevents duplicate footer via prepare()."""
        # Simulated scenario: perm+skill already contains the sentinel
        fake_perm_with_sentinel = (
            "## Permission Profile\n\n---\n\n"
            "## Skill Context\n\nINSTRUCTION\n\n"
            f"{_WORKER_RULES_FOOTER_SENTINEL}\n\nSome prior footer content."
        )
        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            return_value="## Skill Context\n\nINSTRUCTION",
        ):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                return_value=fake_perm_with_sentinel,
            ):
                result = prepare(
                    instruction="INSTRUCTION",
                    role="dev",
                    dispatch_id="idem-test",
                )
        # Sentinel must appear exactly once
        self.assertEqual(
            result.count(_WORKER_RULES_FOOTER_SENTINEL), 1,
            "footer sentinel must appear exactly once (idempotent guard)",
        )


class TestPrepareRepoMap(unittest.TestCase):
    def test_repo_map_forwarded_to_skill_injection(self):
        """repo_map is appended to instruction before _inject_skill_context."""
        captured = []

        def capture_skill(terminal_id, instruction, role, dispatch_metadata):
            captured.append(instruction)
            return instruction

        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            side_effect=capture_skill,
        ):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                return_value="PREAMBLE\n\nSKILL",
            ):
                prepare(
                    instruction="DO WORK",
                    role="dev",
                    dispatch_id="rm-test",
                    repo_map="### Repo Map\n\n1. foo/bar.py",
                )

        self.assertTrue(captured, "_inject_skill_context must have been called")
        raw_instruction = captured[0]
        self.assertIn("DO WORK", raw_instruction)
        self.assertIn("### Repo Map", raw_instruction)

    def test_no_repo_map_no_append(self):
        """Without repo_map, instruction is not modified before skill injection."""
        captured = []

        def capture_skill(terminal_id, instruction, role, dispatch_metadata):
            captured.append(instruction)
            return instruction

        with patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
            side_effect=capture_skill,
        ):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                return_value="PREAMBLE",
            ):
                prepare(
                    instruction="PLAIN",
                    role="dev",
                    dispatch_id="no-rm-test",
                )

        self.assertEqual(captured[0], "PLAIN")


class TestPreparePrId(unittest.TestCase):
    def test_pr_section_in_directive_when_pr_id_set(self):
        """prepare() with pr_id includes ## PR in the report-contract directive."""
        result = _patched_prepare(pr_id="PR-77")
        self.assertIn("## PR", result)

    def test_no_pr_section_when_pr_id_none(self):
        """prepare() without pr_id omits ## PR from the directive."""
        result = _patched_prepare()
        # Check directive specifically (after the directive sentinel)
        directive_start = result.find("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->")
        self.assertGreaterEqual(directive_start, 0)
        directive_tail = result[directive_start:]
        self.assertNotIn("## PR", directive_tail)


# ---------------------------------------------------------------------------
# Subprocess golden: VNX_SHARED_PREPARE=0 is byte-identical to pre-T1 behavior
# ---------------------------------------------------------------------------

class TestSubprocessGolden(unittest.TestCase):
    """_assemble_instruction with VNX_SHARED_PREPARE=0 must be byte-identical to pre-T1."""

    def _run_assemble(self, **env_overrides):
        fake_skill = "ENRICHED_BODY_FROM_SKILL"
        fake_perm = "PREAMBLE_TEXT\n---\n\n" + fake_skill

        with patch.dict(os.environ, env_overrides):
            with patch(
                "subprocess_dispatch._inject_skill_context", return_value=fake_skill
            ) as mock_skill:
                with patch(
                    "subprocess_dispatch._inject_permission_profile", return_value=fake_perm
                ) as mock_perm:
                    result = _assemble_instruction(
                        "T1", "INSTRUCTION", "backend-developer",
                        "golden-dispatch-id", "sonnet", None,
                    )
        return result, mock_skill, mock_perm

    def test_flag_off_no_footer_no_directive_no_trailer(self):
        """VNX_SHARED_PREPARE=0: output has no footer, directive, or trailer sentinel."""
        result, _, _ = self._run_assemble(VNX_SHARED_PREPARE="0")
        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertNotIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)
        self.assertNotIn(_TRAILER_SENTINEL, result)

    def test_flag_off_output_is_permission_profile_return(self):
        """VNX_SHARED_PREPARE=0: _assemble_instruction returns exactly what _inject_permission_profile returns."""
        result, _, _ = self._run_assemble(VNX_SHARED_PREPARE="0")
        self.assertEqual(result, "PREAMBLE_TEXT\n---\n\nENRICHED_BODY_FROM_SKILL")

    def test_flag_off_both_seams_called(self):
        """VNX_SHARED_PREPARE=0: both _inject_skill_context and _inject_permission_profile are called."""
        _, mock_skill, mock_perm = self._run_assemble(VNX_SHARED_PREPARE="0")
        mock_skill.assert_called_once()
        mock_perm.assert_called_once()

    def test_flag_on_adds_footer_and_directive(self):
        """VNX_SHARED_PREPARE=1: _assemble_instruction output includes footer sentinel + directive."""
        fake_skill = "ENRICHED_BODY"
        fake_perm = "PREAMBLE\n---\n\n" + fake_skill

        with patch.dict(os.environ, {"VNX_SHARED_PREPARE": "1"}):
            with patch(
                "subprocess_dispatch_internals.skill_injection._inject_skill_context",
                return_value=fake_skill,
            ):
                with patch(
                    "subprocess_dispatch_internals.skill_injection._inject_permission_profile",
                    return_value=fake_perm,
                ):
                    result = _assemble_instruction(
                        "T1", "INSTRUCTION", "backend-developer",
                        "shared-dispatch-id", "sonnet", None,
                    )

        self.assertIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertIn("<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->", result)
        self.assertIn(_TRAILER_SENTINEL, result)

    def test_default_env_is_flag_off(self):
        """Without VNX_SHARED_PREPARE set, behavior is identical to flag=0 (safe ship)."""
        env_without_flag = {k: v for k, v in os.environ.items()
                            if k != "VNX_SHARED_PREPARE"}
        fake_perm = "PREAMBLE\n---\n\nSKILL"
        with patch.dict(os.environ, env_without_flag, clear=True):
            with patch("subprocess_dispatch._inject_skill_context", return_value="SKILL"):
                with patch(
                    "subprocess_dispatch._inject_permission_profile", return_value=fake_perm
                ):
                    result = _assemble_instruction(
                        "T1", "INSTRUCTION", "dev", "safe-ship-id", "sonnet", None,
                    )
        self.assertNotIn(_WORKER_RULES_FOOTER_SENTINEL, result)
        self.assertNotIn(_TRAILER_SENTINEL, result)


if __name__ == "__main__":
    unittest.main()
