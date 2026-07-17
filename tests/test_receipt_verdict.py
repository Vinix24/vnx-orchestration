#!/usr/bin/env python3
"""Tests for scripts/lib/receipt_verdict.py::compute_verdict (ADR-035 §3.1/§3.1.1/§10).

Covers the mandatory dispatch DoD subset: T3, T4, T5, T23, T35.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from receipt_verdict import compute_verdict  # noqa: E402


# ── T3 — reject on every hard-failure status ──────────────────────────────


@pytest.mark.parametrize(
    "status",
    ["failed", "failure", "error", "blocked", "timeout", "contract_invalid"],
)
def test_t3_reject_on_hard_failure_status(status):
    receipt = {"status": status}
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "reject"
    assert status in verdict["reason"]


def test_t3_reject_on_status_failure_explicit():
    """r4: 'failure' is the literal Path 1 stamps on a real provider failure —
    distinct from 'failed', both must reject."""
    receipt = {
        "status": "failure",
        "verification": {"method": "pytest", "tests_run": 5, "tests_failed": 0},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "reject"


def test_t3_reject_on_blocker_warning_regardless_of_status():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 5, "tests_failed": 0},
        "warnings": [{"code": "x", "severity": "blocker", "destination": "oi", "oi_id": "OI-1"}],
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "reject"


# ── T4 — investigate when success claimed but no test evidence ───────────


def test_t4_investigate_tests_run_absent():
    receipt = {"status": "done", "verification": {"method": "pytest"}}
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t4_investigate_tests_run_null():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": None, "tests_failed": None},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t4_investigate_no_verification_key_at_all():
    receipt = {"status": "done"}
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t4_investigate_tests_failed_nonzero():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 10, "tests_failed": 2},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t4_investigate_pending_report_method():
    receipt = {"status": "success", "verification": {"method": "pending-report"}}
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"
    assert verdict["evidence_complete"] is False


def test_t4_investigate_oi_pending_warning():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 5, "tests_failed": 0},
        "warnings": [
            {
                "code": "worker_permission_violation",
                "severity": "blocker",
                "destination": "oi_pending",
                "oi_id": None,
                "reason": "store lock held",
            }
        ],
    }
    verdict = compute_verdict(receipt)
    # NB: this entry is also severity=blocker, so reject fires first
    # (reject has higher precedence than oi_pending-investigate). Use a
    # warn-severity oi_pending entry to isolate the investigate path.
    assert verdict["decision"] == "reject"

    receipt["warnings"][0]["severity"] = "warn"
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


# ── T5 — accept on success + clean verification ───────────────────────────


def test_t5_accept_success_clean_verification():
    receipt = {
        "status": "done",
        "verification": {
            "method": "pytest",
            "tests_run": 12,
            "tests_passed": 12,
            "tests_failed": 0,
        },
        "warnings": [],
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "accept"
    assert verdict["evidence_complete"] is True


def test_t5_accept_no_warnings_key_at_all():
    receipt = {
        "status": "success",
        "verification": {"method": "pytest", "tests_run": 3, "tests_failed": 0},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "accept"


def test_t5_not_accept_when_blocker_warning_present():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 12, "tests_failed": 0},
        "warnings": [{"code": "x", "severity": "blocker", "destination": "oi", "oi_id": "OI-1"}],
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "reject"


# ── T23 — doc-only invariant: non-docs path present forces investigate ───


def test_t23_na_method_nondocs_path_forces_investigate():
    receipt = {
        "status": "done",
        "verification": {"method": "n/a", "spec_deviation": "doc-only dispatch"},
        "provenance": {
            "diff_summary": {
                "paths": [
                    "docs/governance/decisions/ADR-035-receipt-v2.md",
                    "scripts/lib/receipt_verdict.py",
                ]
            }
        },
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


@pytest.mark.parametrize("status", ["done", "success"])
def test_t23_na_method_nondocs_path_forces_investigate_regardless_of_status(status):
    receipt = {
        "status": status,
        "verification": {"method": "n/a"},
        "provenance": {"diff_summary": {"paths": ["scripts/lib/foo.py"]}},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t23_na_method_all_docs_paths_accepts():
    receipt = {
        "status": "done",
        "verification": {"method": "n/a", "spec_deviation": "doc-only dispatch"},
        "provenance": {
            "diff_summary": {
                "paths": [
                    "docs/governance/decisions/ADR-035-receipt-v2.md",
                    "README.md",
                ]
            }
        },
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "accept"


# ── T35 — doc-only invariant: missing/null/empty paths is fail-safe ──────


def test_t35_na_method_paths_absent_forces_investigate():
    receipt = {
        "status": "done",
        "verification": {"method": "n/a"},
        "provenance": {"diff_summary": {}},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t35_na_method_paths_null_forces_investigate():
    receipt = {
        "status": "done",
        "verification": {"method": "n/a"},
        "provenance": {"diff_summary": {"paths": None}},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t35_na_method_paths_empty_forces_investigate():
    receipt = {
        "status": "done",
        "verification": {"method": "n/a"},
        "provenance": {"diff_summary": {"paths": []}},
    }
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


def test_t35_na_method_no_provenance_key_at_all():
    receipt = {"status": "done", "verification": {"method": "n/a"}}
    verdict = compute_verdict(receipt)
    assert verdict["decision"] == "investigate"


# ── evidence_complete field ────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["unknown", "none_claimed", "pending-report"])
def test_evidence_complete_false_for_incomplete_methods(method):
    receipt = {"status": "done", "verification": {"method": method}}
    verdict = compute_verdict(receipt)
    assert verdict["evidence_complete"] is False


def test_evidence_complete_true_for_pytest_method():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 1, "tests_failed": 0},
    }
    verdict = compute_verdict(receipt)
    assert verdict["evidence_complete"] is True


# ── pure-function guarantees ────────────────────────────────────────────────


def test_compute_verdict_is_pure_no_mutation():
    receipt = {
        "status": "done",
        "verification": {"method": "pytest", "tests_run": 1, "tests_failed": 0},
        "warnings": [{"code": "x", "severity": "warn", "destination": "counted"}],
    }
    import copy

    before = copy.deepcopy(receipt)
    compute_verdict(receipt)
    assert receipt == before


def test_compute_verdict_returns_exactly_three_keys():
    receipt = {"status": "done", "verification": {"method": "pytest", "tests_run": 1, "tests_failed": 0}}
    verdict = compute_verdict(receipt)
    assert set(verdict.keys()) == {"decision", "reason", "evidence_complete"}
