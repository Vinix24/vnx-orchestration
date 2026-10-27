#!/usr/bin/env python3
"""GE-3: every module that writes a result record is inside the guard, and the
three raw writers now go through ``gate_recorder.write_result_guarded``.

Dispatch-ID: dlv-4e1db03b74f8

The inventory ``20261024-inv-poortmotor`` (section 3.1, on ``1ca67db2``)
counted eleven writers of a ``review_gates/results/`` record. Eight went
through ``gate_recorder.write_result_guarded``; three still wrote the record
with a bare ``write_text``:

  * ``gate_result_parser.record_claude_github_result``
    (``<slug>-claude_github_optional-contract.json``)
  * ``gate_result_parser._validate_and_persist_result`` (the ``record-result``
    CLI, ``pr-<N>-<gate>.json``)
  * ``gate_request_handler._persist_claude_github_files`` (a result mirror for
    the optional gate, written from the request path)

``tests/test_oi1472_residual_guard_writers.py`` now lists both modules, so a
later writer that bypasses the guard fails CI instead of passing it. The tests
here drive the three REAL writers through a throwaway store and pin what GE-3
adds: the overwrite guard applies (point 3 of the dispatch), and each record
still lands at the same path with the same keys (point 2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_recorder
import gate_result_parser
import review_stack_support
from review_contract import Deliverable, QualityGate, ReviewContract

PR = review_stack_support.PR
BRANCH = review_stack_support.BRANCH
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway project store; the real writers and the real append path
    write into it."""
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _receipts(env) -> list[dict]:
    """Every ``review_gate_result`` receipt in the store, from both files."""
    receipts = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if not path.exists():
            continue
        receipts.extend(
            line
            for line in (
                json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()
            )
            if line.get("event_type") == "review_gate_result"
        )
    return receipts


def _report(env, name: str) -> str:
    path = env["reports_dir"] / name
    path.write_text("# gate report\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Writer 8 — gate_result_parser.record_claude_github_result
# ---------------------------------------------------------------------------


def test_record_claude_github_result_lands_at_the_same_path_with_the_same_keys(env):
    """The record path and content shape are unchanged; only the write path is.

    ``_contract_result_path``: ``results/<slug>-claude_github_optional-contract.json``
    with the receipt's own ``to_dict`` keys plus the three evidence fields the
    method adds (``findings``, ``report_path``, ``required_reruns``) and
    ``residual_risk``, and the ``attempt_seq`` the commit stamps (GE-4c-1).
    """
    manager = _manager()
    report_path = _report(env, "claude-report.md")

    receipt = manager.record_claude_github_result(
        pr_id="PR-4",
        branch=BRANCH,
        status="pass",
        summary="clean",
        findings=[{"severity": "advisory", "category": "style", "message": "nit"}],
        contract_hash="contract-hash",
        report_path=report_path,
        pr_number=PR,
        required_reruns=["codex_gate"],
    )

    result_path = env["results_dir"] / "pr4-claude_github_optional-contract.json"
    assert result_path.exists(), "writer 8 must keep writing the contract result path"
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    expected_keys = set(receipt.to_dict()) | {
        "findings", "report_path", "required_reruns", "residual_risk", "attempt_seq",
    }
    assert set(saved) == expected_keys, (
        "the shared write path changed the record's keys\n"
        f"missing: {sorted(expected_keys - set(saved))}\n"
        f"extra: {sorted(set(saved) - expected_keys)}"
    )
    assert saved["gate"] == "claude_github_optional"
    assert saved["pr_id"] == "PR-4"
    assert saved["state"] == "completed"
    assert saved["result_status"] == "pass"
    assert saved["contract_hash"] == "contract-hash"
    assert saved["report_path"] == report_path
    assert saved["required_reruns"] == ["codex_gate"]
    assert saved["blocking_count"] == 0
    assert list(env["results_dir"].glob("*.tmp")) == [], "the write must be atomic"


def test_record_claude_github_result_books_exactly_one_result_receipt(env):
    """GE-3: routing must not book the claude_github outcome twice.

    The gate-specific emitter stays (it carries the head read off the contract
    request, which is not on the record); the generic outcome emitter is
    opted out.
    """
    manager = _manager()
    (env["requests_dir"] / "pr4-claude_github_optional-contract.json").write_text(
        json.dumps({"commit_sha": COMMIT_A, "contract_hash": "contract-hash"}),
        encoding="utf-8",
    )

    manager.record_claude_github_result(
        pr_id="PR-4", branch=BRANCH, status="pass", summary="clean",
        pr_number=PR, report_path=_report(env, "claude-report.md"),
    )

    (receipt,) = _receipts(env)
    assert receipt["commit_sha"] == COMMIT_A


# ---------------------------------------------------------------------------
# Writer 9 — gate_result_parser._validate_and_persist_result (record-result)
# ---------------------------------------------------------------------------


def test_record_result_lands_at_the_same_path_with_the_same_keys(env):
    manager = _manager()
    report_path = _report(env, "kimi-report.md")

    payload = manager.record_result(
        gate="kimi_gate",
        pr_number=PR,
        branch=BRANCH,
        status="pass",
        summary="clean",
        findings=[{"severity": "advisory", "title": "nit"}],
        residual_risk="low",
        contract_hash="contract-hash",
        report_path=report_path,
        pr_id=str(PR),
    )

    result_path = env["results_dir"] / f"pr-{PR}-kimi_gate.json"
    assert result_path.exists(), "writer 9 must keep writing pr-<N>-<gate>.json"
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert set(saved) == set(payload), (
        "the record on disk must be the payload the caller built\n"
        f"missing: {sorted(set(payload) - set(saved))}\n"
        f"extra: {sorted(set(saved) - set(payload))}"
    )
    assert saved["status"] == "pass"
    assert saved["residual_risk"] == "low"
    assert saved["contract_hash"] == "contract-hash"
    assert saved["report_path"] == report_path
    assert saved["commit_sha"] == review_stack_support.HEAD_SHA
    assert list(env["results_dir"].glob("*.tmp")) == [], "the write must be atomic"


def _terminal_record(
    env, *, gate: str = "kimi_gate", commit_sha: str = COMMIT_A, filename: str = "",
) -> Path:
    """A decided, evidenced record already on disk for ``pr-<N>-<gate>``."""
    slot = env["results_dir"] / (filename or f"pr-{PR}-{gate}.json")
    slot.write_text(
        json.dumps({
            "gate": gate,
            "pr_number": PR,
            "pr_id": str(PR),
            "branch": BRANCH,
            "status": "pass",
            "contract_hash": "existing-contract",
            "report_path": _report(env, "existing-report.md"),
            "commit_sha": commit_sha,
        }),
        encoding="utf-8",
    )
    return slot


@pytest.mark.parametrize("status", ["not_executable", "unavailable"])
def test_record_result_is_refused_over_a_terminal_record_on_the_same_head(env, monkeypatch, status):
    """Point 3: a terminal record on head A is not downgraded by ``record-result``
    for the SAME PR, gate and head; on a new head B the write lands."""
    slot = _terminal_record(env, commit_sha=COMMIT_A)
    before = slot.read_text(encoding="utf-8")
    assert _receipts(env) == []

    monkeypatch.setattr(gate_result_parser, "get_pr_head_sha", lambda _pr: COMMIT_A)
    with pytest.raises(gate_recorder.ResultOverwriteRefused):
        _manager().record_result(
            gate="kimi_gate", pr_number=PR, branch=BRANCH, status=status,
            summary="no verdict", pr_id=str(PR),
        )

    assert slot.read_text(encoding="utf-8") == before, "the refused write must leave the record unchanged"
    assert _receipts(env) == [], "no receipt may be booked for a write that never landed"

    monkeypatch.setattr(gate_result_parser, "get_pr_head_sha", lambda _pr: COMMIT_B)
    _manager().record_result(
        gate="kimi_gate", pr_number=PR, branch=BRANCH, status=status,
        summary="no verdict", pr_id=str(PR),
    )

    saved = json.loads(slot.read_text(encoding="utf-8"))
    assert saved["status"] == status, "a verdict about another head must not hold this slot (OI-1668)"
    assert saved["commit_sha"] == COMMIT_B
    # GE-4c-1: the receipt is commit_gate_outcome's, and an ``unavailable`` or
    # ``not_executable`` is no decided verdict, so it owes none (ADR-046 §2);
    # ``_emit_result_receipt`` booked one for any status before.
    assert _receipts(env) == []


# ---------------------------------------------------------------------------
# Writer 10 — gate_request_handler._persist_claude_github_files
# ---------------------------------------------------------------------------


def _contract() -> ReviewContract:
    return ReviewContract(
        pr_id="PR-7",
        pr_title="GE-3",
        feature_title="gate engine consolidation",
        branch=BRANCH,
        track="gate-engine-consolidation",
        risk_class="medium",
        merge_policy="human",
        review_stack=["claude_github_optional"],
        deliverables=[Deliverable(description="the guard sees every writer", category="implementation")],
        quality_gate=QualityGate(gate_id="gate_ge3", checks=["guard applies"]),
        changed_files=["scripts/lib/gate_result_parser.py"],
        content_hash="contract-hash",
    )


def test_persist_claude_github_files_lands_at_the_same_path_with_the_same_keys(env):
    manager = _manager()
    contract = _contract()

    manager.request_claude_github_with_contract(
        contract=contract, mode="per_pr", dispatch_id="dlv-4e1db03b74f8",
    )

    result_path = env["results_dir"] / "pr7-claude_github_optional-contract.json"
    assert result_path.exists(), "writer 10 must keep mirroring the state at the contract result path"
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    expected_keys = {
        "gate", "pr_id", "state", "contributed_evidence", "was_intentionally_absent",
        "contract_hash", "branch", "pr_number", "gh_comment_body", "reason",
        "result_status", "result_summary", "advisory_findings", "blocking_findings",
        "advisory_count", "blocking_count", "requested_at", "completed_at",
        "review_mode", "risk_class", "changed_files", "commit_sha", "dispatch_id",
        "report_path", "recorded_at",
    }
    assert set(saved) == expected_keys, (
        "the shared write path changed the mirror's keys\n"
        f"missing: {sorted(expected_keys - set(saved))}\n"
        f"extra: {sorted(set(saved) - expected_keys)}"
    )
    assert saved["state"] == "not_configured"
    assert saved["gate"] == "claude_github_optional"
    assert saved["dispatch_id"] == "dlv-4e1db03b74f8"
    assert saved["recorded_at"], "the mirror's own recorded_at stays"
    assert list(env["results_dir"].glob("*.tmp")) == [], "the write must be atomic"


def test_persist_claude_github_files_does_not_erase_a_decided_result(env):
    """The mirror shares its slot with writer 8. A fresh request must not erase
    the decided result recorded there — and the request itself still stands."""
    manager = _manager()
    contract = _contract()
    # The same head the store resolves for this PR: the mirror names that head,
    # so OI-1668's head scoping cannot wave the write through and the overwrite
    # guard is the only thing left to judge it.
    _terminal_record(
        env,
        gate="claude_github_optional",
        commit_sha=review_stack_support.HEAD_SHA,
        filename="pr7-claude_github_optional-contract.json",
    )

    receipt = manager.request_claude_github_with_contract(
        contract=contract, mode="per_pr", dispatch_id="dlv-4e1db03b74f8",
    )

    saved = json.loads((env["results_dir"] / "pr7-claude_github_optional-contract.json").read_text())
    assert saved["status"] == "pass" and saved["commit_sha"] == review_stack_support.HEAD_SHA, (
        "the decided record must survive a request-time mirror (OI-1469/OI-1470)"
    )
    assert receipt.state == "not_configured", "the request itself is not refused by the mirror"
    assert (env["requests_dir"] / "pr7-claude_github_optional-contract.json").exists()
