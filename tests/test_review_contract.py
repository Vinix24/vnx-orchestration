#!/usr/bin/env python3
"""Tests for the review contract schema and its deterministic materializer."""

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from review_contract import (
    SCHEMA_VERSION,
    Deliverable,
    DeterministicFinding,
    QualityGate,
    ReviewContract,
    TestEvidence,
    derive_closure_stage,
    materialize_review_contract,
    materialize_from_files,
    _normalize_line,
    _parse_pr_section,
    _parse_feature_title,
)
import claude_github_receipt
import gemini_prompt_renderer


SAMPLE_FEATURE_PLAN = """\
# Feature: Review Contracts And Gates

**Status**: Draft
**Priority**: P0
**Branch**: `feature/review-contract-gates`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

## Dependency Flow
```text
PR-1 (no dependencies)
PR-1 -> PR-2
```

## PR-1: Review Contract Schema And Materializer
**Track**: C
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-4 hours
**Dependencies**: []

### Description
Define the canonical review contract schema and build the materializer.

### Scope
- review contract schema in `scripts/lib/review_contract.py`
- materializer and serializer
- stable fields for deliverables, non-goals, changed files, test evidence

### Success Criteria
- each PR can produce one structured review contract without handwritten prompt assembly
- contract fields cover deliverables, non-goals, tests, risk class, merge policy, and review stack
- contract generation is deterministic for the same inputs

### Quality Gate
`gate_pr1_review_contract_schema`:
- [ ] Review contract schema covers deliverables, non-goals, tests, risk class, merge policy, and review stack
- [ ] Contract generation is deterministic for identical inputs
- [ ] Schema serialization and parsing tests pass

---

## PR-2: Gemini Review Prompt Renderer
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-4 hours
**Dependencies**: [PR-1]

### Description
Render deliverable-aware Gemini review prompts from the review contract.

### Scope
- Gemini prompt templates
- receipt payloads

### Success Criteria
- Gemini review prompts include deliverables and changed files
- emitted receipts clearly distinguish advisory and blocking findings

### Quality Gate
`gate_pr2_gemini_contract_review`:
- [ ] Gemini prompts include deliverables
- [ ] Advisory vs blocking findings are emitted distinctly
"""

DISPATCH_ID = "dlv-0123456789ab"

# The register line scripts/pr_merge.py appends at the merge (``_emit_register_event``).
MERGED_EVENT = {"event": "pr_merged", "pr_number": 2176, "dispatch_id": DISPATCH_ID,
                "terminal": "T0", "extra": {"merge_method": "squash", "conclusion": "merged"}}

# What the retired PR_QUEUE.md said for PR-1 when it was "merged".
STALE_PR_QUEUE_COMPLETED = "### ✅ Completed PRs\n- PR-1: Review Contract Schema And Materializer\n"


class TestParseFeaturePlan:
    def test_parse_feature_title(self):
        assert _parse_feature_title(SAMPLE_FEATURE_PLAN) == "Review Contracts And Gates"

    def test_parse_pr_section_extracts_all_fields(self):
        section = _parse_pr_section(SAMPLE_FEATURE_PLAN, "PR-1")
        assert section["title"] == "Review Contract Schema And Materializer"
        assert section["track"] == "C"
        assert section["risk_class"] == "medium"
        assert section["merge_policy"] == "human"
        assert section["estimated_time"] == "2-4 hours"
        assert section["dependencies"] == []
        assert len(section["success_criteria"]) == 3
        assert section["quality_gate"]["gate_id"] == "gate_pr1_review_contract_schema"
        assert len(section["quality_gate"]["checks"]) == 3

    def test_parse_pr_section_missing_pr(self):
        assert _parse_pr_section(SAMPLE_FEATURE_PLAN, "PR-99") == {}

    def test_parse_pr_section_with_dependencies(self):
        section = _parse_pr_section(SAMPLE_FEATURE_PLAN, "PR-2")
        assert section["dependencies"] == ["PR-1"]


class TestClosureStageFromDispatchRegister:
    """ADR-047 §6: the closure stage comes from the dispatch register, not PR_QUEUE.md."""

    def test_merged_pr_by_github_number(self):
        assert derive_closure_stage("2176", "", [MERGED_EVENT]) == "merged"

    @pytest.mark.parametrize("pr_id, pr_number", [("#2176", 2176), (" 2176 ", "2176"), ("2176", "#2176")])
    def test_github_number_forms_are_one_pr(self, pr_id, pr_number):
        assert derive_closure_stage(pr_id, "", [dict(MERGED_EVENT, pr_number=pr_number)]) == "merged"

    def test_open_pr_when_only_another_pr_merged(self):
        events = [dict(MERGED_EVENT, pr_number=2175), {"event": "gate_passed", "pr_number": 2175}]
        assert derive_closure_stage("2176", DISPATCH_ID, events) == "open"

    @pytest.mark.parametrize("event", ["gate_passed", "gate_failed"])
    def test_review_result_is_in_review(self, event):
        assert derive_closure_stage("2176", "", [{"event": event, "pr_number": 2176}]) == "in_review"

    def test_merge_wins_over_a_later_review_result(self):
        events = [MERGED_EVENT, {"event": "gate_failed", "pr_number": 2176}]
        assert derive_closure_stage("2176", "", events) == "merged"

    def test_label_joins_through_the_dispatch_id(self):
        assert derive_closure_stage("PR-1", DISPATCH_ID, [MERGED_EVENT]) == "merged"
        assert derive_closure_stage("PR-1", "dlv-other", [MERGED_EVENT]) == "open"

    def test_label_is_not_the_github_number(self):
        event = dict(MERGED_EVENT, pr_number=3, dispatch_id="dlv-other")
        assert derive_closure_stage("PR-3", DISPATCH_ID, [event]) == "open"

    def test_label_without_dispatch_id_never_matches(self):
        assert derive_closure_stage("PR-1", "", [dict(MERGED_EVENT, dispatch_id="")]) == "open"

    @pytest.mark.parametrize("event", [
        None,
        "pr_merged",
        {"pr_number": 2176},
        {"event": None, "pr_number": 2176},
        {"event": "pr_merge_refused", "pr_number": 2176},
        {"event": "gate_requested", "pr_number": 2176},
        {"event": "pr_merged"},
        {"event": "pr_merged", "pr_number": None},
        {"event": "pr_merged", "pr_number": True},
        {"event": "pr_merged", "pr_number": "21x76"},
        {"event": "pr_merged", "pr_number": 0},
    ])
    def test_missing_malformed_or_unknown_evidence_stays_open(self, event):
        assert derive_closure_stage("2176", "", [event]) == "open"

    def test_empty_register_is_open(self):
        assert derive_closure_stage("2176", DISPATCH_ID, []) == "open"


class TestReviewContractSchema:
    def test_to_dict_and_from_dict_roundtrip(self):
        contract = ReviewContract(
            pr_id="PR-1",
            pr_title="Test Contract",
            feature_title="Test Feature",
            risk_class="high",
            merge_policy="human",
            review_stack=["gemini_review", "codex_gate"],
            deliverables=[Deliverable(description="thing works", category="implementation")],
            non_goals=["PR-2 is out of scope"],
            quality_gate=QualityGate(gate_id="gate_test", checks=["check_a", "check_b"]),
            test_evidence=TestEvidence(test_files=["tests/test_foo.py"], test_command="pytest tests/"),
            deterministic_findings=[
                DeterministicFinding(source="mypy", severity="warning", message="unused var")
            ],
        )
        d = contract.to_dict()
        restored = ReviewContract.from_dict(d)
        assert restored == contract

    def test_json_roundtrip(self):
        contract = ReviewContract(
            pr_id="PR-3",
            pr_title="JSON Test",
            review_stack=["gemini_review"],
            deliverables=[Deliverable(description="serialization works")],
        )
        json_str = contract.to_json()
        restored = ReviewContract.from_json(json_str)
        assert restored == contract

    def test_content_hash_is_deterministic(self):
        d = {"pr_id": "PR-1", "pr_title": "Test", "risk_class": "medium"}
        hash1 = ReviewContract.compute_content_hash(d)
        hash2 = ReviewContract.compute_content_hash(d)
        assert hash1 == hash2
        assert len(hash1) == 16

    def test_content_hash_changes_with_input(self):
        d1 = {"pr_id": "PR-1", "pr_title": "Test"}
        d2 = {"pr_id": "PR-1", "pr_title": "Different"}
        assert ReviewContract.compute_content_hash(d1) != ReviewContract.compute_content_hash(d2)

    def test_content_hash_backward_compat_empty_deleted_files(self):
        """Empty deleted_files must hash identically to a contract without the field at all."""
        legacy_dict = {
            'dispatch_id': 'x', 'pr_id': 'PR-1', 'branch': 'main',
            'commit_hash_before': 'abc', 'changed_files': ['a.py'],
            'deliverables': [], 'quality_gate': None, 'test_evidence': None,
            'review_stack': [], 'role': 'backend-developer', 'created_at': '2026-01-01',
            'content_hash': '',
        }
        legacy_hash = ReviewContract.compute_content_hash(legacy_dict)

        new_dict = dict(legacy_dict)
        new_dict['deleted_files'] = []
        new_hash = ReviewContract.compute_content_hash(new_dict)

        assert legacy_hash == new_hash, f'backward-compat broken: {legacy_hash} != {new_hash}'

    def test_content_hash_changes_when_deleted_files_populated(self):
        """Non-empty deleted_files DOES change the hash (intended behavior)."""
        base = {
            'dispatch_id': 'x', 'pr_id': 'PR-1', 'branch': 'main',
            'commit_hash_before': 'abc', 'changed_files': ['a.py'],
            'deleted_files': [],
            'deliverables': [], 'quality_gate': None, 'test_evidence': None,
            'review_stack': [], 'role': 'backend-developer', 'created_at': '2026-01-01',
            'content_hash': '',
        }
        empty_hash = ReviewContract.compute_content_hash(base)

        populated = dict(base)
        populated['deleted_files'] = ['old.py']
        populated_hash = ReviewContract.compute_content_hash(populated)

        assert empty_hash != populated_hash, 'deleted_files must affect hash when non-empty'

    def test_from_dict_with_none_optional_fields(self):
        d = {"pr_id": "PR-5", "quality_gate": None, "test_evidence": None}
        contract = ReviewContract.from_dict(d)
        assert contract.pr_id == "PR-5"
        assert contract.quality_gate is None
        assert contract.test_evidence is None

    def test_schema_version_default(self):
        contract = ReviewContract()
        assert contract.schema_version == SCHEMA_VERSION


class TestMaterializeReviewContract:
    def test_basic_materialization(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
            branch="feature/review-contract",
            changed_files=["scripts/lib/review_contract.py"],
        )
        assert contract.pr_id == "PR-1"
        assert contract.pr_title == "Review Contract Schema And Materializer"
        assert contract.feature_title == "Review Contracts And Gates"
        assert contract.track == "C"
        assert contract.risk_class == "medium"
        assert contract.merge_policy == "human"
        assert contract.review_stack == ["gemini_review", "codex_gate", "claude_github_optional"]
        assert contract.closure_stage == "open"
        assert len(contract.deliverables) == 3
        assert contract.quality_gate is not None
        assert contract.quality_gate.gate_id == "gate_pr1_review_contract_schema"
        assert contract.changed_files == ["scripts/lib/review_contract.py"]
        assert contract.content_hash != ""

    def test_determinism(self):
        kwargs = dict(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
            branch="feature/review-contract",
            changed_files=["a.py", "b.py"],
        )
        c1 = materialize_review_contract(**kwargs)
        c2 = materialize_review_contract(**kwargs)
        assert c1 == c2
        assert c1.content_hash == c2.content_hash
        assert c1.to_json() == c2.to_json()

    def test_non_goals_derived(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
        )
        assert any("PR-2" in ng for ng in contract.non_goals)

    def test_closure_stage_merged(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[MERGED_EVENT],
            dispatch_id=DISPATCH_ID,
        )
        assert contract.closure_stage == "merged"

    def test_missing_pr_raises(self):
        with pytest.raises(ValueError, match="PR-99"):
            materialize_review_contract(
                pr_id="PR-99",
                feature_plan_content=SAMPLE_FEATURE_PLAN,
                register_events=[],
            )

    def test_changed_files_sorted(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
            changed_files=["z.py", "a.py", "m.py"],
        )
        assert contract.changed_files == ["a.py", "m.py", "z.py"]

    def test_with_test_evidence(self):
        evidence = TestEvidence(test_files=["tests/test_foo.py"], test_command="pytest tests/")
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
            test_evidence=evidence,
        )
        assert contract.test_evidence is not None
        assert contract.test_evidence.test_files == ["tests/test_foo.py"]

    def test_with_deterministic_findings(self):
        findings = [DeterministicFinding(source="ruff", severity="warning", message="unused import")]
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
            deterministic_findings=findings,
        )
        assert len(contract.deterministic_findings) == 1
        assert contract.deterministic_findings[0].source == "ruff"

    def test_scope_file_extraction(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            register_events=[],
        )
        assert "scripts/lib/review_contract.py" in contract.scope_files

    def test_materialize_from_files(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")

        contract = materialize_from_files(
            pr_id="PR-1",
            feature_plan_path=fp,
            state_dir=tmp_path / "state",
            branch="feature/test",
        )
        assert contract.pr_id == "PR-1"
        assert contract.branch == "feature/test"
        assert contract.closure_stage == "open"


def _project(tmp_path, events, *, pr_queue: str = ""):
    """A project root with a plan and a dispatch register; PR_QUEUE.md only when given."""
    (tmp_path / "FEATURE_PLAN.md").write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
    if pr_queue:
        (tmp_path / "PR_QUEUE.md").write_text(pr_queue, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lines = "".join(json.dumps(event) + "\n" for event in events)
    (state_dir / "dispatch_register.ndjson").write_text(lines, encoding="utf-8")
    return tmp_path / "FEATURE_PLAN.md", state_dir


class TestClosureStageWithoutPrQueue:
    """The closure stage is read from the register on disk; PR_QUEUE.md is never read."""

    def test_merged_pr_from_the_register(self, tmp_path):
        fp, state_dir = _project(tmp_path, [{"event": "dispatch_completed", "dispatch_id": DISPATCH_ID},
                                            MERGED_EVENT])
        contract = materialize_from_files(
            pr_id="PR-1", feature_plan_path=fp, state_dir=state_dir, dispatch_id=DISPATCH_ID,
        )
        assert not (tmp_path / "PR_QUEUE.md").exists()
        assert contract.closure_stage == "merged"

    def test_open_pr_from_the_register(self, tmp_path):
        fp, state_dir = _project(tmp_path, [{"event": "dispatch_started", "dispatch_id": DISPATCH_ID}])
        contract = materialize_from_files(
            pr_id="PR-1", feature_plan_path=fp, state_dir=state_dir, dispatch_id=DISPATCH_ID,
        )
        assert contract.closure_stage == "open"

    def test_a_stale_pr_queue_md_changes_nothing(self, tmp_path):
        with_file, without_file = tmp_path / "with", tmp_path / "without"
        with_file.mkdir()
        without_file.mkdir()
        events = [{"event": "dispatch_started", "dispatch_id": DISPATCH_ID}]
        contracts = [
            materialize_from_files(pr_id="PR-1", feature_plan_path=fp, state_dir=sd, dispatch_id=DISPATCH_ID)
            for fp, sd in (_project(with_file, events, pr_queue=STALE_PR_QUEUE_COMPLETED),
                           _project(without_file, events))
        ]
        assert contracts[0].closure_stage == contracts[1].closure_stage == "open"
        assert contracts[0].content_hash == contracts[1].content_hash

    def test_malformed_register_lines_are_no_evidence(self, tmp_path):
        fp, state_dir = _project(tmp_path, [])
        (state_dir / "dispatch_register.ndjson").write_text(
            '{"event": "pr_merged", "dispatch_id": "' + DISPATCH_ID + '"\nnot json\n', encoding="utf-8",
        )
        contract = materialize_from_files(
            pr_id="PR-1", feature_plan_path=fp, state_dir=state_dir, dispatch_id=DISPATCH_ID,
        )
        assert contract.closure_stage == "open"


class TestNormalizeLine:
    """_normalize_line is the shared coercion behind every gate that turns an
    untrusted model's raw ``"line"`` value into a finding anchor.

    Measured on the pre-fix code (duplicated in gemini_prompt_renderer.py and
    claude_github_receipt.py): ``_normalize_line("137")`` returned ``0``, not
    ``137`` — a reviewing LLM emitting a numeric string for "line" silently
    lost its anchor.
    """

    @pytest.mark.parametrize(
        "raw_line, expected",
        [
            (137, 137),
            ("137", 137),
            (" 137 ", 137),
            (0, 0),
            ("ergens", 0),
            (-3, 0),
            (True, 0),
            (12.5, 0),
            ("-3", 0),
            ("12.5", 0),
            (None, 0),
        ],
    )
    def test_coerces_to_expected_value(self, raw_line, expected):
        assert _normalize_line(raw_line) == expected

    def test_gemini_prompt_renderer_imports_shared_function(self):
        assert gemini_prompt_renderer._normalize_line is _normalize_line

    def test_claude_github_receipt_imports_shared_function(self):
        assert claude_github_receipt._normalize_line is _normalize_line
