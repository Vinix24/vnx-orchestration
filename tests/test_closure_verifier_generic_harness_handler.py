#!/usr/bin/env python3
"""A registered gate without its own closure handler gets the generic handler
for its kind (review-lane P6, ADR-046 Amendment 1 A4).

Decision order in ``closure_verifier._check_single_gate``:

1. outside ``_KNOWN_GATES``                       -> UNVERIFIED (OI-1093)
2. an own handler in ``_GATE_HANDLERS``           -> that handler
3. no own handler, registry kind ``harness_lane`` -> the generic handler
4. anything else                                  -> UNVERIFIED, never a KeyError

The seven evidence invariants are split over ``gate_status.has_complete_evidence``
(record exists, terminal, ``contract_hash``, ``report_path``) and
``_merge_door_record_verdict`` / ``_record_matches_scope`` (report on disk,
verdict not contradicted by its report, ``commit_sha`` is the head). No single
function counts them, so each invariant gets its own test below, all against a
synthetic gate that has no handler of its own.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier as cv
import gate_recorder
from review_contract import ReviewContract

SYNTH = "synth_harness_gate"
HEAD = "a" * 40
PR = "PR-1"


@pytest.fixture
def synthetic_harness_gate(monkeypatch):
    """Register SYNTH as a harness_lane gate that closure knows but has no handler for."""
    monkeypatch.setitem(
        gate_recorder.GATE_PROVIDERS, SYNTH, (gate_recorder.GATE_PROVIDER_HARNESS_LANE, "synth-harness")
    )
    monkeypatch.setattr(cv, "_KNOWN_GATES", cv._KNOWN_GATES | {SYNTH})
    assert SYNTH not in cv._GATE_HANDLERS
    return SYNTH


def _contract(gate: str) -> ReviewContract:
    return ReviewContract(
        pr_id=PR, branch="feature/p6", risk_class="medium",
        review_stack=[gate], content_hash="abcdef1234567890",
    )


def _record(tmp_path: Path, **overrides) -> dict:
    report = tmp_path / "report.md"
    report.write_text("# report\nNothing to flag.\n", encoding="utf-8")
    base = {
        "gate": SYNTH, "status": "pass", "pr_id": PR, "branch": "feature/p6",
        "commit_sha": HEAD, "contract_hash": "abcdef1234567890",
        "report_path": str(report), "blocking_findings": [], "advisory_findings": [],
    }
    base.update(overrides)
    return base


def _check(gate: str, result: Optional[dict], tmp_path: Path):
    return cv._check_single_gate(gate, _contract(gate), result, tmp_path, "feature/p6")


class TestOrderOfDeciding:
    def test_synthetic_harness_gate_gets_the_generic_handler(self, synthetic_harness_gate, tmp_path):
        check = _check(SYNTH, _record(tmp_path), tmp_path)
        assert check.status == "PASS", check.detail
        assert check.name == f"gate_{SYNTH}"

    def test_generic_handler_is_the_function_the_four_wrappers_called(
        self, synthetic_harness_gate, tmp_path, monkeypatch
    ):
        """One implementation of the invariants, no second copy that can drift."""
        seen = []

        def spy(gate, result):
            seen.append(gate)
            return cv.CheckResult(f"gate_{gate}", "PASS", "spy")

        monkeypatch.setattr(cv, "_gate_full_evidence_pass", spy)
        _check(SYNTH, _record(tmp_path), tmp_path)
        assert seen == [SYNTH]

    @pytest.mark.parametrize("gate", ["kimi_gate", "glm_gate", "deepseek_gate", "claude_gate"])
    def test_the_four_real_harness_gates_pass_through_the_generic_handler(self, gate, tmp_path):
        assert gate not in cv._GATE_HANDLERS
        result = _record(tmp_path, gate=gate)
        assert _check(gate, result, tmp_path).status == "PASS"

    @pytest.mark.parametrize("gate", sorted(cv._GATE_HANDLERS))
    def test_a_gate_with_an_own_handler_keeps_it(self, gate, monkeypatch, tmp_path):
        marker = cv.CheckResult(f"gate_{gate}", "FAIL", "own handler ran")
        monkeypatch.setitem(cv._GATE_HANDLERS, gate, lambda *a, **k: marker)
        assert _check(gate, _record(tmp_path, gate=gate), tmp_path) is marker

    def test_excluded_gate_stays_unverified_even_with_a_harness_row_and_full_record(
        self, monkeypatch, tmp_path
    ):
        """The exclusion wins over the kind (step 1 before step 3)."""
        monkeypatch.setitem(
            gate_recorder.GATE_PROVIDERS, "wiring_gate", (gate_recorder.GATE_PROVIDER_HARNESS_LANE, "x")
        )
        assert "wiring_gate" not in cv._KNOWN_GATES
        check = _check("wiring_gate", _record(tmp_path, gate="wiring_gate"), tmp_path)
        assert check.status == "UNVERIFIED"

    def test_unknown_gate_with_a_record_stays_unverified(self, tmp_path):
        assert _check("never_heard_of_it", _record(tmp_path), tmp_path).status == "UNVERIFIED"

    def test_known_gate_registered_with_another_kind_is_unverified_not_keyerror(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setitem(
            gate_recorder.GATE_PROVIDERS, SYNTH, (gate_recorder.GATE_PROVIDER_PATH_BINARY, "bin")
        )
        monkeypatch.setattr(cv, "_KNOWN_GATES", cv._KNOWN_GATES | {SYNTH})
        check = _check(SYNTH, _record(tmp_path), tmp_path)
        assert check.status == "UNVERIFIED"
        assert "generic handler" in check.detail

    def test_known_gate_with_no_registry_row_is_unverified_not_keyerror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cv, "_KNOWN_GATES", cv._KNOWN_GATES | {SYNTH})
        assert SYNTH not in gate_recorder.GATE_PROVIDERS
        assert _check(SYNTH, _record(tmp_path), tmp_path).status == "UNVERIFIED"


class TestFailClosedOnEvidence:
    """Every case below leaves the gate not-PASS."""

    def test_no_result_record(self, synthetic_harness_gate, tmp_path):
        assert _check(SYNTH, None, tmp_path).status == "FAIL"

    @pytest.mark.parametrize("status", ["unavailable", "executing", "running", "pending", "weird", "", None, 7])
    def test_non_terminal_or_unknown_status(self, synthetic_harness_gate, tmp_path, status):
        assert _check(SYNTH, _record(tmp_path, status=status), tmp_path).status != "PASS"

    @pytest.mark.parametrize("field", ["contract_hash", "report_path"])
    @pytest.mark.parametrize("value", ["MISSING", None, "", "   ", 0, []])
    def test_missing_none_blank_or_malformed_evidence_field(
        self, synthetic_harness_gate, tmp_path, field, value
    ):
        record = _record(tmp_path)
        if value == "MISSING":
            del record[field]
        else:
            record[field] = value
        assert _check(SYNTH, record, tmp_path).status != "PASS"

    def test_gate_result_without_report_path_is_no_pass(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path)
        del record["report_path"]
        check = _check(SYNTH, record, tmp_path)
        assert check.status == "FAIL"
        assert "report_path" in check.detail

    def test_not_executable_record(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path, status="not_executable", failure_reason="provider_not_installed")
        assert _check(SYNTH, record, tmp_path).status != "PASS"

    def test_failing_verdict_with_blocking_findings(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path, status="fail", blocking_findings=[{"message": "x"}])
        assert _check(SYNTH, record, tmp_path).status == "FAIL"


class TestTheSevenInvariantsAreAllEnforcedForAGateWithoutItsOwnHandler:
    """Record exists, terminal, contract_hash, report_path (generic handler via
    gate_status.has_complete_evidence); report on disk, no self-contradiction,
    commit_sha is the head (merge door). Each one blocks for the synthetic gate."""

    def test_1_record_exists(self, synthetic_harness_gate, tmp_path):
        assert _check(SYNTH, None, tmp_path).status != "PASS"

    def test_2_record_is_terminal(self, synthetic_harness_gate, tmp_path):
        assert _check(SYNTH, _record(tmp_path, status="executing"), tmp_path).status != "PASS"

    def test_3_contract_hash_is_filled(self, synthetic_harness_gate, tmp_path):
        assert _check(SYNTH, _record(tmp_path, contract_hash=""), tmp_path).status != "PASS"

    def test_4_report_path_is_filled(self, synthetic_harness_gate, tmp_path):
        assert _check(SYNTH, _record(tmp_path, report_path=""), tmp_path).status != "PASS"

    def test_5_report_exists_on_disk(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path, report_path=str(tmp_path / "gone.md"))
        verdict = cv._merge_door_record_verdict(record, SYNTH, PR)
        assert verdict["verdict"] == "NO-GO"
        assert "bestaat niet" in verdict["message"]

    def test_6_verdict_is_not_contradicted_by_its_report(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path)
        Path(record["report_path"]).write_text(
            '```json\n{"verdict": "fail", "findings": [{"severity": "blocking"}]}\n```\n',
            encoding="utf-8",
        )
        verdict = cv._merge_door_record_verdict(record, SYNTH, PR)
        assert verdict["verdict"] == "NO-GO"
        assert "zichzelf tegen" in verdict["message"]

    @pytest.mark.parametrize("commit_sha", ["b" * 40, "", None, "MISSING"])
    def test_7_commit_sha_is_the_head(self, synthetic_harness_gate, tmp_path, commit_sha):
        record = _record(tmp_path)
        if commit_sha == "MISSING":
            del record["commit_sha"]
        else:
            record["commit_sha"] = commit_sha
        assert not cv._record_matches_scope(record, PR, "feature/p6", None, HEAD)

    def test_all_seven_hold_for_a_clean_record(self, synthetic_harness_gate, tmp_path):
        record = _record(tmp_path)
        assert _check(SYNTH, record, tmp_path).status == "PASS"
        assert cv._merge_door_record_verdict(record, SYNTH, PR)["verdict"] == "GO"
        assert cv._record_matches_scope(record, PR, "feature/p6", None, HEAD)
