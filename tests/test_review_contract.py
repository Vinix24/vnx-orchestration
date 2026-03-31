#!/usr/bin/env python3
"""Tests for review contract schema, materializer, and CLI."""

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
    materialize_review_contract,
    materialize_from_files,
    _parse_pr_section,
    _parse_feature_title,
    _parse_pr_queue_status,
)
import review_contract_materializer as rcm


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

SAMPLE_PR_QUEUE = """\
# PR Queue - Feature: Review Contracts And Gates

## Progress Overview
Total: 2 PRs | Complete: 0 | Active: 0 | Queued: 2 | Blocked: 0
Progress: ░░░░░░░░░░ 0%

## Status

### ⏳ Queued PRs
- PR-1: Review Contract Schema And Materializer (dependencies: none) [risk=medium]
- PR-2: Gemini Review Prompt Renderer (dependencies: PR-1) [risk=medium]

## Dependency Flow
```
PR-1 (no dependencies)
PR-1 -> PR-2
```
"""

SAMPLE_PR_QUEUE_COMPLETED = """\
# PR Queue - Feature: Review Contracts And Gates

## Progress Overview
Total: 2 PRs | Complete: 1 | Active: 0 | Queued: 1 | Blocked: 0

## Status

### ✅ Completed PRs
- PR-1: Review Contract Schema And Materializer

### ⏳ Queued PRs
- PR-2: Gemini Review Prompt Renderer (dependencies: PR-1)
"""


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

    def test_parse_pr_queue_status_open(self):
        assert _parse_pr_queue_status(SAMPLE_PR_QUEUE, "PR-1") == "open"

    def test_parse_pr_queue_status_completed(self):
        assert _parse_pr_queue_status(SAMPLE_PR_QUEUE_COMPLETED, "PR-1") == "merged"


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
            pr_queue_content=SAMPLE_PR_QUEUE,
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
            pr_queue_content=SAMPLE_PR_QUEUE,
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
            pr_queue_content=SAMPLE_PR_QUEUE,
        )
        assert any("PR-2" in ng for ng in contract.non_goals)

    def test_closure_stage_merged(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            pr_queue_content=SAMPLE_PR_QUEUE_COMPLETED,
        )
        assert contract.closure_stage == "merged"

    def test_missing_pr_raises(self):
        with pytest.raises(ValueError, match="PR-99"):
            materialize_review_contract(
                pr_id="PR-99",
                feature_plan_content=SAMPLE_FEATURE_PLAN,
                pr_queue_content=SAMPLE_PR_QUEUE,
            )

    def test_changed_files_sorted(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            pr_queue_content=SAMPLE_PR_QUEUE,
            changed_files=["z.py", "a.py", "m.py"],
        )
        assert contract.changed_files == ["a.py", "m.py", "z.py"]

    def test_with_test_evidence(self):
        evidence = TestEvidence(test_files=["tests/test_foo.py"], test_command="pytest tests/")
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            pr_queue_content=SAMPLE_PR_QUEUE,
            test_evidence=evidence,
        )
        assert contract.test_evidence is not None
        assert contract.test_evidence.test_files == ["tests/test_foo.py"]

    def test_with_deterministic_findings(self):
        findings = [DeterministicFinding(source="ruff", severity="warning", message="unused import")]
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            pr_queue_content=SAMPLE_PR_QUEUE,
            deterministic_findings=findings,
        )
        assert len(contract.deterministic_findings) == 1
        assert contract.deterministic_findings[0].source == "ruff"

    def test_scope_file_extraction(self):
        contract = materialize_review_contract(
            pr_id="PR-1",
            feature_plan_content=SAMPLE_FEATURE_PLAN,
            pr_queue_content=SAMPLE_PR_QUEUE,
        )
        assert "scripts/lib/review_contract.py" in contract.scope_files

    def test_materialize_from_files(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        contract = materialize_from_files(
            pr_id="PR-1",
            feature_plan_path=fp,
            pr_queue_path=pq,
            branch="feature/test",
        )
        assert contract.pr_id == "PR-1"
        assert contract.branch == "feature/test"


class TestCLI:
    def test_materialize_stdout(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        exit_code = rcm.main([
            "materialize",
            "--pr", "PR-1",
            "--feature-plan", str(fp),
            "--pr-queue", str(pq),
            "--branch", "feature/test",
        ])
        assert exit_code == 0

    def test_materialize_to_file(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        out = tmp_path / "contract.json"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        exit_code = rcm.main([
            "materialize",
            "--pr", "PR-1",
            "--feature-plan", str(fp),
            "--pr-queue", str(pq),
            "--output", str(out),
        ])
        assert exit_code == 0
        assert out.exists()

        contract = ReviewContract.from_json(out.read_text(encoding="utf-8"))
        assert contract.pr_id == "PR-1"
        assert contract.content_hash != ""

    def test_materialize_missing_pr(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        exit_code = rcm.main([
            "materialize",
            "--pr", "PR-99",
            "--feature-plan", str(fp),
            "--pr-queue", str(pq),
        ])
        assert exit_code == 10  # EXIT_VALIDATION

    def test_materialize_missing_file(self, tmp_path):
        exit_code = rcm.main([
            "materialize",
            "--pr", "PR-1",
            "--feature-plan", str(tmp_path / "missing.md"),
            "--pr-queue", str(tmp_path / "also_missing.md"),
        ])
        assert exit_code == 20  # EXIT_IO

    def test_validate_valid_contract(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        out = tmp_path / "contract.json"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        rcm.main([
            "materialize",
            "--pr", "PR-1",
            "--feature-plan", str(fp),
            "--pr-queue", str(pq),
            "--output", str(out),
        ])

        exit_code = rcm.main(["validate", "--contract", str(out)])
        assert exit_code == 0

    def test_validate_tampered_hash(self, tmp_path):
        fp = tmp_path / "FEATURE_PLAN.md"
        pq = tmp_path / "PR_QUEUE.md"
        out = tmp_path / "contract.json"
        fp.write_text(SAMPLE_FEATURE_PLAN, encoding="utf-8")
        pq.write_text(SAMPLE_PR_QUEUE, encoding="utf-8")

        rcm.main([
            "materialize",
            "--pr", "PR-1",
            "--feature-plan", str(fp),
            "--pr-queue", str(pq),
            "--output", str(out),
        ])

        data = json.loads(out.read_text(encoding="utf-8"))
        data["pr_title"] = "TAMPERED"
        out.write_text(json.dumps(data), encoding="utf-8")

        exit_code = rcm.main(["validate", "--contract", str(out)])
        assert exit_code == 10  # EXIT_VALIDATION

    def test_validate_missing_fields(self, tmp_path):
        out = tmp_path / "contract.json"
        out.write_text(json.dumps({"schema_version": "1.0.0"}), encoding="utf-8")

        exit_code = rcm.main(["validate", "--contract", str(out)])
        assert exit_code == 10  # EXIT_VALIDATION
