"""test_d1_reviewer_no_injection.py — D1 (OI-1444): review-gate / plan-reviewer
must never receive intelligence, patterns, a repo-map, or a skill/role CLAUDE.md.

Measured: kimi_gate on PR #1956 (28-09) received pattern ``intel_ap_3`` in its
review prompt. A reviewer fed patterns from past dispatches is no longer
independent, and it contaminates any later measurement of injection's effect
on quality. This dispatch closes THREE injection paths that all delegate to
the shared injector chain (skill_context._inject_skill_context +
dispatch_enricher.apply_repo_map_layer + intelligence_injection.fetch_intelligence_section):

  1. provider_dispatch.py _enrich_instruction  (kimi_gate/glm_gate/deepseek_gate,
     the plan-gate's non-claude seats via plan_gate_panel._make_default_dispatcher)
  2. envelope_prepare.py _prepare              (the headless envelope lane)
  3. skill_context.py _inject_skill_context    (the canonical injector every lane
     above — and subprocess_dispatch.py's terminal-pinned lane — delegates to)

A single decision point, ``reviewer_roles.enrichment_allowed()``, gates all three
plus the direct-CLI repo-map call in subprocess_dispatch.py's ``_enrich_cli_instruction``
(found by grepping for a second ``apply_repo_map_layer`` call site — OI-1444 point 5).

RED on the pre-fix code (see the dispatch report for the actual run): every
"reviewer role" assertion below failed — the fake pattern, the repo-map mock
call, and the agents/review-gate/CLAUDE.md marker were all present in the
enriched prompt.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import envelope_prepare
import provider_dispatch
import role_application
import skill_context
import subprocess_dispatch
from envelope_types import EnvelopeSpec
from reviewer_roles import REVIEWER_ROLES, enrichment_allowed
from role_application import RoleApplicationVerdict, verify_role_applied

REPO_ROOT = Path(__file__).resolve().parents[1]

_UNIQUE_PATTERN_TITLE = "IntelApTestPattern3"
_UNIQUE_PATTERN_CONTENT = "d1-test-marker: never reaches a reviewer prompt"

REVIEWER_ROLE_LIST = sorted(REVIEWER_ROLES)  # ["plan-reviewer", "review-gate"]
_ROLE_MARKER = {
    "review-gate": "Review Gate Agent",
    "plan-reviewer": "Plan Reviewer Agent",
}


# ---------------------------------------------------------------------------
# reviewer_roles.py — the single decision place
# ---------------------------------------------------------------------------


class TestEnrichmentAllowed:

    def test_review_gate_and_plan_reviewer_are_the_only_suppressed_roles(self):
        assert REVIEWER_ROLES == frozenset({"review-gate", "plan-reviewer"})

    def test_reviewer_roles_disallowed(self):
        assert enrichment_allowed("review-gate") is False
        assert enrichment_allowed("plan-reviewer") is False

    def test_non_reviewer_roles_allowed(self):
        assert enrichment_allowed("backend-developer") is True
        assert enrichment_allowed(None) is True
        assert enrichment_allowed("") is True
        assert enrichment_allowed("quality-engineer") is True


# ---------------------------------------------------------------------------
# Shared intelligence fake — a recognizable pattern + booking-call spies
# ---------------------------------------------------------------------------


def _make_intelligence_result():
    from intelligence_selector import IntelligenceItem, InjectionResult
    item = IntelligenceItem(
        item_id="intel_ap_3_test",
        item_class="failure_prevention",
        title=_UNIQUE_PATTERN_TITLE,
        content=_UNIQUE_PATTERN_CONTENT,
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-09-28T00:00:00.000000Z",
        scope_tags=["review-gate"],
    )
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-09-28T00:00:00.000000Z",
        items=[item],
        suppressed=[],
        task_class="research_structured",
        dispatch_id="d1-test",
    )


def _patch_intelligence_selector():
    """Patch IntelligenceSelector to return a fake pattern AND spy on the booking
    calls (record_injection / emit_event) so tests can assert they never fire for
    a reviewer role (point 3: no dispatch_pattern_offered / intelligence_usage
    write for a suppressed reviewer)."""
    import intelligence_selector as _mod
    instance = MagicMock()
    instance.select.return_value = _make_intelligence_result()
    mock_cls = MagicMock(return_value=instance)
    return patch.object(_mod, "IntelligenceSelector", mock_cls), instance


# ---------------------------------------------------------------------------
# Path 3 (canonical): skill_context._inject_skill_context
# ---------------------------------------------------------------------------


class TestSkillContextInjectSkillContext:

    def _run(self, role, tmp_path, *, dispatch_id="d1-test"):
        patcher, instance = _patch_intelligence_selector()
        with (
            patch.object(subprocess_dispatch, "_default_state_dir", return_value=tmp_path),
            patcher,
        ):
            enriched = skill_context._inject_skill_context(
                "T1",
                "review this diff\n\n```json\n{\"verdict\": \"PASS\"}\n```",
                role,
                {"dispatch_id": dispatch_id, "model": "sonnet", "provider": "kimi"},
            )
        return enriched, instance

    def test_review_gate_gets_no_pattern_no_role_source(self, tmp_path):
        enriched, instance = self._run("review-gate", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert _ROLE_MARKER["review-gate"] not in enriched
        assert "review this diff" in enriched
        instance.select.assert_not_called()
        instance.record_injection.assert_not_called()

    def test_plan_reviewer_gets_no_pattern_no_role_source(self, tmp_path):
        enriched, instance = self._run("plan-reviewer", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert _ROLE_MARKER["plan-reviewer"] not in enriched
        assert "review this diff" in enriched
        instance.select.assert_not_called()
        instance.record_injection.assert_not_called()

    def test_reviewer_prompt_still_carries_report_directive(self, tmp_path):
        """The raw instruction + report-body-contract directive survive — a
        reviewer still needs to know where to write its report."""
        enriched, _ = self._run("review-gate", tmp_path)
        assert "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->" in enriched

    def test_non_reviewer_control_still_enriched(self, tmp_path):
        """Control: backend-developer (not a reviewer role) keeps getting the
        pattern AND its role source — the fix must not suppress enrichment
        globally."""
        enriched, instance = self._run("backend-developer", tmp_path)
        assert _UNIQUE_PATTERN_TITLE in enriched
        instance.select.assert_called_once()


# ---------------------------------------------------------------------------
# Path 1: provider_dispatch._enrich_instruction (kimi_gate/glm_gate/deepseek_gate)
# ---------------------------------------------------------------------------


class TestProviderDispatchEnrichInstruction:

    def _make_args(self, role):
        from types import SimpleNamespace
        return SimpleNamespace(
            provider="kimi",
            dispatch_id="d1-provider-test",
            terminal_id="review-gate",
            instruction="review this diff against `scripts/lib/foo.py`",
            model="kimi-k2",
            pr_id=None,
            dispatch_paths="",
            role=role,
            no_auto_commit=True,
            max_retries=1,
            gate="kimi_gate",
            deadline_seconds=900,
        )

    def _run(self, role, tmp_path):
        args = self._make_args(role)
        patcher, instance = _patch_intelligence_selector()
        repo_map_mock = MagicMock(side_effect=lambda instr, meta: instr + "\n## Repo Map\nfoo")
        with (
            patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path),
            patch.object(provider_dispatch, "_resolve_data_dir", return_value=tmp_path),
            patch("dispatch_enricher.apply_repo_map_layer", repo_map_mock),
            patcher,
        ):
            enriched = provider_dispatch._enrich_instruction(args)
        return enriched, instance, repo_map_mock

    def test_review_gate_no_pattern_no_repo_map(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("review-gate", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert "## Repo Map" not in enriched
        repo_map_mock.assert_not_called()
        instance.select.assert_not_called()
        assert "review this diff" in enriched

    def test_plan_reviewer_no_pattern_no_repo_map(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("plan-reviewer", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert "## Repo Map" not in enriched
        repo_map_mock.assert_not_called()
        instance.select.assert_not_called()

    def test_non_reviewer_control_gets_repo_map_and_pattern(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("backend-developer", tmp_path)
        repo_map_mock.assert_called_once()
        assert "## Repo Map" in enriched
        instance.select.assert_called_once()
        assert _UNIQUE_PATTERN_TITLE in enriched

    def test_review_gate_role_applied_verdict_has_no_alarm(self, tmp_path):
        """OI-1444 point 2: a deliberately unenriched reviewer prompt must not
        stamp role_applied=False (which would read as a wiring bug)."""
        args = self._make_args("review-gate")
        patcher, _ = _patch_intelligence_selector()
        with (
            patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path),
            patch.object(provider_dispatch, "_resolve_data_dir", return_value=tmp_path),
            patcher,
        ):
            provider_dispatch._enrich_instruction(args)
        verdict = args._role_application
        assert verdict is not None
        assert verdict.role_applied is True
        assert verdict.tier == "suppressed_reviewer"
        assert verdict.reason is None


# ---------------------------------------------------------------------------
# Path 2: envelope_prepare._prepare (headless envelope lane)
# ---------------------------------------------------------------------------


class TestEnvelopePrepare:

    def _make_spec(self, role, tmp_path):
        return EnvelopeSpec(
            dispatch_id="d1-envelope-test",
            terminal_id="T1",
            provider="codex",
            model="gpt-test",
            instruction="review this diff against `scripts/lib/foo.py`",
            role=role,
            pr_id=None,
            state_dir=tmp_path / "state",
            data_dir=tmp_path,
        )

    def _run(self, role, tmp_path):
        spec = self._make_spec(role, tmp_path)
        patcher, instance = _patch_intelligence_selector()
        repo_map_mock = MagicMock(side_effect=lambda instr, meta: instr + "\n## Repo Map\nfoo")
        with (
            patch("dispatch_enricher.apply_repo_map_layer", repo_map_mock),
            patch.object(subprocess_dispatch, "_default_state_dir", return_value=tmp_path / "state"),
            patcher,
        ):
            enriched = envelope_prepare._prepare(spec)
        return enriched, instance, repo_map_mock

    def test_review_gate_no_pattern_no_repo_map(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("review-gate", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert "## Repo Map" not in enriched
        repo_map_mock.assert_not_called()
        instance.select.assert_not_called()
        assert "review this diff" in enriched

    def test_plan_reviewer_no_pattern_no_repo_map(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("plan-reviewer", tmp_path)
        assert _UNIQUE_PATTERN_TITLE not in enriched
        assert "## Repo Map" not in enriched
        repo_map_mock.assert_not_called()
        instance.select.assert_not_called()

    def test_non_reviewer_control_gets_repo_map_and_pattern(self, tmp_path):
        enriched, instance, repo_map_mock = self._run("backend-developer", tmp_path)
        repo_map_mock.assert_called_once()
        assert "## Repo Map" in enriched
        instance.select.assert_called_once()


# ---------------------------------------------------------------------------
# subprocess_dispatch._enrich_cli_instruction — the SECOND repo-map call site
# (found by grepping dispatch_enricher.apply_repo_map_layer call sites, point 5)
# ---------------------------------------------------------------------------


class TestSubprocessDispatchCliEnrichment:

    def test_review_gate_repo_map_suppressed(self):
        repo_map_mock = MagicMock(side_effect=lambda instr, meta: instr + "\n## Repo Map\nfoo")
        with patch("dispatch_enricher.apply_repo_map_layer", repo_map_mock):
            enriched = subprocess_dispatch._enrich_cli_instruction(
                "review this diff", "review-gate",
            )
        repo_map_mock.assert_not_called()
        assert "## Repo Map" not in enriched

    def test_plan_reviewer_repo_map_suppressed(self):
        repo_map_mock = MagicMock(side_effect=lambda instr, meta: instr + "\n## Repo Map\nfoo")
        with patch("dispatch_enricher.apply_repo_map_layer", repo_map_mock):
            enriched = subprocess_dispatch._enrich_cli_instruction(
                "review this plan", "plan-reviewer",
            )
        repo_map_mock.assert_not_called()
        assert "## Repo Map" not in enriched

    def test_non_reviewer_control_gets_repo_map(self):
        repo_map_mock = MagicMock(side_effect=lambda instr, meta: instr + "\n## Repo Map\nfoo")
        with patch("dispatch_enricher.apply_repo_map_layer", repo_map_mock):
            enriched = subprocess_dispatch._enrich_cli_instruction(
                "implement the feature", "backend-developer",
            )
        repo_map_mock.assert_called_once()
        assert "## Repo Map" in enriched


# ---------------------------------------------------------------------------
# role_application.verify_role_applied — no false role_applied=False alarm
# ---------------------------------------------------------------------------


class TestVerifyRoleAppliedSuppressedReviewer:

    def test_review_gate_no_alarm(self):
        verdict = verify_role_applied(
            "review this diff\n\n```json\n{\"verdict\": \"PASS\"}\n```",
            "review-gate",
            "review-gate",
        )
        assert verdict.role_applied is True
        assert verdict.tier == "suppressed_reviewer"
        assert verdict.reason is None
        assert verdict.source_path is None

    def test_plan_reviewer_no_alarm(self):
        verdict = verify_role_applied(
            "review this plan",
            "plan-gate",
            "plan-reviewer",
        )
        assert verdict.role_applied is True
        assert verdict.tier == "suppressed_reviewer"
        assert verdict.reason is None

    def test_non_reviewer_control_unaffected(self):
        """Control: the pre-existing containment check for a real role is
        unchanged by the D1 guard."""
        agents_body = (REPO_ROOT / "agents" / "quality-engineer" / "CLAUDE.md").read_text()
        final_prompt = f"{agents_body}\n\n---\n\nimplement the change"
        verdict = verify_role_applied(final_prompt, "T1", "quality-engineer")
        assert verdict.role_applied is True
        assert verdict.tier == "agents"
