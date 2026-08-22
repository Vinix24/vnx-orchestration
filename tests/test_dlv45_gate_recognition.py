"""dlv45 — kimi_gate and glm_gate as recognised Gate enum members with handlers.

Before this dispatch, ``Gate`` (scripts/lib/dispatch_spec.py) was a closed set
of five names. kimi_gate had a real runner (scripts/kimi_gate.py, nine
historical result records) but was not in the enum, so
``gate_request_handler._dispatch_one_review`` fell through its branch chain to
the generic ``unknown_review_gate`` rejection for ANY requested kimi_gate — the
runner existed, recognition did not. glm_gate had neither a runner nor
recognition.

This module proves, on behavior (not on a missing symbol):

1. A requested kimi_gate no longer resolves to ``unknown_review_gate``.
2. A requested glm_gate resolves to a distinct, speaking "runner missing"
   rejection — never the generic ``unknown_review_gate`` — because glm_gate
   IS a recognised gate (Gate.GLM_GATE), it just has no runner yet (a separate
   deliverable ships scripts/glm_gate.py).
3. ``closure_verifier._KNOWN_GATES`` and ``_GATE_HANDLERS`` stay synchronised
   for both new names.
4. A kimi_gate result with full evidence (contract_hash + report_path + an
   existing report file) passes the closure handler; the same result missing
   those fields is refused, exactly like codex_gate.
5. An ``unavailable`` kimi_gate result (OI-1142 provider outage) is never
   booked as a PASS — it is not even terminal, so it can never reach the
   pass/fail decision at all.

``test_kimi_gate_request_no_longer_resolves_to_unknown_review_gate`` and
``test_glm_gate_request_gets_a_speaking_runner_missing_rejection`` are the two
assertions that are RED on unmodified main *on behavior*: both calls succeed
and return a plain dict there too (no ImportError, no AttributeError) — main
simply answers ``{"status": "blocked", "reason": "unknown_review_gate"}`` for
both, so the assertions on the returned value fail with a real
AssertionError, not a collection error. That is the strong form of red this
dispatch calls for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
import gate_request_handler
from dispatch_spec import Gate
from review_contract import ReviewContract


# ---------------------------------------------------------------------------
# gate_request_handler: kimi_gate / glm_gate routing
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


class TestKimiGateNoLongerUnknown:
    def test_kimi_gate_request_no_longer_resolves_to_unknown_review_gate(self, manager_env, monkeypatch):
        """Behavioral red/green pin (dlv45 evidence #1).

        On unmodified main, ``_dispatch_one_review`` has no kimi_gate branch,
        so this call reaches the fallback and returns
        ``{"gate": "kimi_gate", "status": "blocked", "reason": "unknown_review_gate"}``
        — a real dict, not an exception. The assertion below fails there with
        a plain AssertionError on the VALUE, which is the point: this is red
        on behavior, not on a missing symbol.
        """
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "f" * 40)
        manager = _make_manager()

        result = manager._dispatch_one_review(
            "kimi_gate", pr_number=7, branch="feature/dlv45",
            risk_class="low", changed_files=["scripts/foo.py"],
            mode="per_pr", dispatch_id="dlv45-kimi-pin",
        )

        assert result.get("reason") != "unknown_review_gate", (
            f"kimi_gate must no longer fall through to unknown_review_gate, got: {result}"
        )
        assert result["status"] != "blocked"
        assert result["gate"] == "kimi_gate"

    def test_kimi_gate_request_is_requested_because_runner_exists(self, manager_env, monkeypatch):
        """scripts/kimi_gate.py exists in this repo, so the request must be
        marked 'requested', not 'not_executable' — the runner-presence check
        actually inspects the filesystem, it is not hardcoded true."""
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "a" * 40)
        manager = _make_manager()

        result = manager._dispatch_one_review(
            "kimi_gate", pr_number=8, branch="feature/dlv45",
            risk_class="low", changed_files=[],
            mode="per_pr", dispatch_id="dlv45-kimi-available",
        )

        assert result["status"] == "requested"
        req_file = manager_env["requests_dir"] / "pr-8-kimi_gate.json"
        assert req_file.exists()
        payload = json.loads(req_file.read_text())
        assert payload["gate"] == "kimi_gate"
        assert payload["commit_sha"] == "a" * 40


class TestGlmGateSpeakingRejection:
    def test_glm_gate_request_gets_a_speaking_runner_missing_rejection(self, manager_env, monkeypatch):
        """Behavioral red/green pin (dlv45 evidence #2).

        scripts/glm_gate.py now ships for real (a later dlv1 deliverable), so
        the "runner missing" state can no longer be produced by the file's
        actual absence on disk. Force it explicitly via the availability
        helper instead — the rejection this test guards (speaking,
        'gate_runner_missing', never the generic 'unknown_review_gate') must
        still fire whenever the runner is unavailable, regardless of why.
        """
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "b" * 40)
        manager = _make_manager()
        monkeypatch.setattr(manager, "_glm_gate_available", lambda: False)

        result = manager._dispatch_one_review(
            "glm_gate", pr_number=9, branch="feature/dlv45",
            risk_class="low", changed_files=[],
            mode="per_pr", dispatch_id="dlv45-glm-pin",
        )

        assert result["status"] == "not_executable"
        assert result.get("reason") == "gate_runner_missing"
        assert result.get("reason") != "unknown_review_gate"

    def test_glm_gate_request_is_accepted_once_the_runner_exists(self, manager_env, monkeypatch):
        """The other side of evidence #2: with the runner present (its real,
        shipped state in this repo since dlv1), the gate must resolve to
        'requested', not the speaking rejection above. Without this test the
        suite would stay green even if recognition silently broke again and
        glm_gate started refusing a runner that is actually there."""
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "d" * 40)
        manager = _make_manager()
        monkeypatch.setattr(manager, "_glm_gate_available", lambda: True)

        result = manager._dispatch_one_review(
            "glm_gate", pr_number=12, branch="feature/dlv45",
            risk_class="low", changed_files=[],
            mode="per_pr", dispatch_id="dlv45-glm-available",
        )

        assert result["status"] == "requested"
        assert result.get("reason") is None
        assert result.get("reason") != "gate_runner_missing"
        assert result.get("reason") != "unknown_review_gate"

    def test_kimi_and_glm_rejections_are_distinguishable_side_by_side(self, manager_env, monkeypatch):
        """Evidence #2: the kimi (available) and glm (runner missing) outcomes
        must differ from each other AND from the old unknown_review_gate
        rejection — never collapse to the same string."""
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: "c" * 40)
        manager = _make_manager()
        monkeypatch.setattr(manager, "_glm_gate_available", lambda: False)

        kimi_result = manager._dispatch_one_review(
            "kimi_gate", pr_number=10, branch="feature/dlv45",
            risk_class="low", changed_files=[], mode="per_pr", dispatch_id="dlv45-side-kimi",
        )
        glm_result = manager._dispatch_one_review(
            "glm_gate", pr_number=11, branch="feature/dlv45",
            risk_class="low", changed_files=[], mode="per_pr", dispatch_id="dlv45-side-glm",
        )

        assert kimi_result["status"] == "requested"
        assert glm_result["status"] == "not_executable"
        assert glm_result["reason"] == "gate_runner_missing"
        assert {kimi_result["status"], glm_result.get("reason")} & {"unknown_review_gate"} == set()


# ---------------------------------------------------------------------------
# dispatch_spec.Gate: enum membership
# ---------------------------------------------------------------------------


class TestGateEnumMembership:
    def test_kimi_gate_and_glm_gate_are_gate_enum_members(self):
        assert Gate("kimi_gate") is Gate.KIMI_GATE
        assert Gate("glm_gate") is Gate.GLM_GATE

    def test_gate_enum_stays_a_closed_set(self):
        """OI-845 discipline: the enum grew by exactly two, everything else
        that was legal stays legal, nothing else was silently added."""
        values = {g.value for g in Gate}
        assert values == {
            "gemini_review", "codex_gate", "claude_github_optional",
            "ci_gate", "wiring_gate", "kimi_gate", "glm_gate",
        }


# ---------------------------------------------------------------------------
# closure_verifier: _KNOWN_GATES / _GATE_HANDLERS synchronisation (evidence #3)
# ---------------------------------------------------------------------------


class TestKnownGatesHandlersSynchronised:
    def test_kimi_and_glm_are_known_and_have_handlers(self):
        assert {"kimi_gate", "glm_gate"} <= cv._KNOWN_GATES
        assert {"kimi_gate", "glm_gate"} <= set(cv._GATE_HANDLERS.keys())

    def test_known_gates_and_handlers_are_symmetric(self):
        """Both directions of the diff must be empty — a known gate with no
        handler would KeyError at runtime; a handler for a gate not in
        _KNOWN_GATES is dead/stray code the enum does not authorise."""
        known_without_handler = cv._KNOWN_GATES - set(cv._GATE_HANDLERS.keys())
        handler_without_known = set(cv._GATE_HANDLERS.keys()) - cv._KNOWN_GATES
        assert known_without_handler == set(), f"known gates missing handlers: {known_without_handler}"
        assert handler_without_known == set(), f"handlers for gates outside _KNOWN_GATES: {handler_without_known}"

    def test_kimi_and_glm_not_parked_on_not_implemented(self):
        """Neither name may sit on _GATES_NOT_IMPLEMENTED_BY_CLOSURE — that
        route satisfies the drift test while making closure structurally
        unreachable (routes to UNVERIFIED forever). Explicitly ruled out by
        the dispatch."""
        assert "kimi_gate" not in cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE
        assert "glm_gate" not in cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE


# ---------------------------------------------------------------------------
# closure_verifier: kimi_gate handler evidence discipline (evidence #5, #6)
# ---------------------------------------------------------------------------


def _contract(review_stack):
    return ReviewContract(
        pr_id="PR-0",
        branch="feature/dlv45",
        risk_class="medium",
        review_stack=list(review_stack),
        content_hash="abcdef1234567890",
    )


class TestKimiGateClosureHandlerEvidenceDiscipline:
    def test_full_evidence_kimi_result_passes(self, tmp_path):
        """contract_hash + report_path + an existing report file + status
        pass -> PASS, exactly the same bar codex_gate is held to."""
        report_file = tmp_path / "kimi_report.md"
        report_file.write_text("# kimi_gate report\nAll clear.\n", encoding="utf-8")
        result = {
            "gate": "kimi_gate",
            "status": "pass",
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
            "blocking_findings": [],
            "advisory_findings": [],
        }

        check = cv._check_single_gate(
            "kimi_gate", _contract(["kimi_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status == "PASS", check.detail

    def test_result_missing_contract_hash_and_report_path_is_refused(self, tmp_path):
        """The same status=pass result, but with neither contract_hash nor
        report_path, must be refused — mirrors codex_gate's report_path
        requirement, extended to contract_hash (gate_has_complete_evidence)."""
        result = {
            "gate": "kimi_gate",
            "status": "pass",
            "pr_id": "0",
            "provider": "kimi",
            "dispatch_id": "kimi-gate-pr0-1782546770",
            "blocking_findings": [],
            "advisory_findings": [],
        }

        check = cv._check_single_gate(
            "kimi_gate", _contract(["kimi_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status == "FAIL"
        assert check.status != "PASS"

    def test_result_missing_only_report_path_is_refused(self, tmp_path):
        result = {
            "gate": "kimi_gate",
            "status": "pass",
            "contract_hash": "abcdef1234567890",
            "blocking_findings": [],
        }

        check = cv._check_single_gate(
            "kimi_gate", _contract(["kimi_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status == "FAIL"

    def test_unavailable_kimi_result_is_never_a_pass(self, tmp_path):
        """OI-1142: a provider outage (status='unavailable'), even with full
        evidence fields present, must never be booked as PASS. unavailable is
        deliberately not terminal, so it is refused before gate_is_pass is
        ever consulted."""
        report_file = tmp_path / "kimi_report_unavailable.md"
        report_file.write_text("kimi dispatch failed\n", encoding="utf-8")
        result = {
            "gate": "kimi_gate",
            "status": "unavailable",
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
            "reason": "dispatch_error",
            "blocking_findings": [],
        }

        check = cv._check_single_gate(
            "kimi_gate", _contract(["kimi_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status != "PASS"
        assert check.status == "FAIL"

    def test_no_result_is_refused_not_silently_passed(self, tmp_path):
        check = cv._check_single_gate(
            "kimi_gate", _contract(["kimi_gate"]), None, tmp_path, "feature/dlv45",
        )
        assert check.status == "FAIL"


class TestGlmGateClosureHandlerSameDiscipline:
    """glm_gate has no runner yet, but its closure handler must hold results
    (once any exist) to the same bar as kimi_gate/codex_gate — the handler is
    generic evidence discipline, independent of whether a runner ships it."""

    def test_full_evidence_glm_result_passes(self, tmp_path):
        report_file = tmp_path / "glm_report.md"
        report_file.write_text("# glm_gate report\nAll clear.\n", encoding="utf-8")
        result = {
            "gate": "glm_gate",
            "status": "pass",
            "contract_hash": "abcdef1234567890",
            "report_path": str(report_file),
            "blocking_findings": [],
        }

        check = cv._check_single_gate(
            "glm_gate", _contract(["glm_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status == "PASS", check.detail

    def test_not_executable_glm_result_is_refused(self, tmp_path):
        result = {
            "gate": "glm_gate",
            "status": "not_executable",
            "reason": "gate_runner_missing",
            "reason_detail": "scripts/glm_gate.py does not exist yet",
        }

        check = cv._check_single_gate(
            "glm_gate", _contract(["glm_gate"]), result, tmp_path, "feature/dlv45",
        )

        assert check.status == "FAIL"
