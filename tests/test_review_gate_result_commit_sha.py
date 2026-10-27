#!/usr/bin/env python3
"""GE-1 (track ``gate-engine-consolidation``): every ``review_gate_result``
receipt carries the commit it judges.

Dispatch-ID: dlv-5b99c63c7949

The idempotency key already has a commit dimension
(``append_receipt_internals/idempotency.py`` ``IDEMPOTENCY_FIELDS``), but no
``review_gate_result`` emit site stamped a ``commit_sha``: ``gate_recorder``
stamped only ``head_sha`` and ``gate_result_parser`` stamped no head at all.
The same PR passing on commit A and commit B therefore collapsed into one
receipt (same key), and the ADR-045 point 4 field name ``commit_sha`` was empty
in the ledger.

These are the red runs for the fix:

* two terminal results, same status, same gate/PR/dispatch, two heads -> two
  receipts with two distinct idempotency keys;
* the emitted receipt's ``commit_sha`` equals the result record's;
* a path with no head stamps neither ``commit_sha`` nor an empty ``head_sha``;
* the same through the ``gate_result_parser`` emit path (``record_result``),
  plus the Claude GitHub path, whose head lives on the contract request.
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

import gate_recorder  # noqa: E402
import gate_result_parser  # noqa: E402
import review_gate_manager as rgm  # noqa: E402
import review_stack_support  # noqa: E402
from append_receipt_internals.idempotency import (  # noqa: E402
    _compute_idempotency_key,
)

PR = review_stack_support.PR
BRANCH = review_stack_support.BRANCH
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway project store; the real append path writes into it."""
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _receipts(env) -> list[dict]:
    """Every ``review_gate_result`` receipt, from both store files.

    The Claude GitHub emit site carries no ``dispatch_id``, so the append path
    reroutes its receipt to ``gate_events.ndjson`` (a ghost receipt); the kimi
    path has one and lands in ``t0_receipts.ndjson``.
    """
    receipts = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if not path.exists():
            continue
        receipts.extend(
            line
            for line in (json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip())
            if line.get("event_type") == "review_gate_result"
        )
    return receipts


def _keys(receipts: list[dict]) -> list[str]:
    return [_compute_idempotency_key(receipt, receipt["event_type"]) for receipt in receipts]


def _terminal_payload(commit_sha: str = "") -> dict:
    payload = {
        "gate": "kimi_gate",
        "pr_id": str(PR),
        "pr_number": PR,
        "status": "pass",
        "summary": "kimi_gate clean",
        "contract_hash": "contract-hash",
        "report_path": "/r/report.md",
        "dispatch_id": "kimi-gate-pr1926-1789147884",
    }
    if commit_sha:
        payload["commit_sha"] = commit_sha
    return payload


# ---------------------------------------------------------------------------
# gate_recorder._emit_terminal_result_receipt
# ---------------------------------------------------------------------------


def test_two_heads_same_status_are_two_receipts_with_two_keys(env):
    gate_recorder._emit_terminal_result_receipt(
        _terminal_payload(COMMIT_A), gate="kimi_gate", pr_ref=str(PR),
    )
    gate_recorder._emit_terminal_result_receipt(
        _terminal_payload(COMMIT_B), gate="kimi_gate", pr_ref=str(PR),
    )

    receipts = _receipts(env)
    assert len(receipts) == 2, f"two heads must book two receipts, got {len(receipts)}"
    assert [receipt["commit_sha"] for receipt in receipts] == [COMMIT_A, COMMIT_B]
    keys = _keys(receipts)
    assert len(set(keys)) == 2, f"the two heads must not share an idempotency key: {keys}"


def test_receipt_commit_sha_equals_the_result_record(env):
    payload = _terminal_payload(COMMIT_A)

    gate_recorder._emit_terminal_result_receipt(payload, gate="kimi_gate", pr_ref=str(PR))

    (receipt,) = _receipts(env)
    assert receipt["commit_sha"] == payload["commit_sha"]
    assert receipt["head_sha"] == payload["commit_sha"], "head_sha stays the round ledger's alias"


def test_a_result_without_a_head_stamps_no_head_field(env):
    gate_recorder._emit_terminal_result_receipt(
        _terminal_payload(), gate="kimi_gate", pr_ref=str(PR),
    )

    (receipt,) = _receipts(env)
    assert "commit_sha" not in receipt, "an absent head must be absent, never an empty string"
    assert "head_sha" not in receipt, "an absent head must not be stamped as an empty alias"


# ---------------------------------------------------------------------------
# gate_result_parser.record_result (its receipt is commit_gate_outcome's since GE-4c-1)
# ---------------------------------------------------------------------------


def _record_result(manager, env, head_sha: str) -> None:
    report = env["reports_dir"] / "parser-gate-report.md"
    report.write_text("# report\n", encoding="utf-8")
    manager.record_result(
        gate="kimi_gate",
        pr_number=PR,
        branch=BRANCH,
        status="pass",
        summary="clean",
        contract_hash="contract-hash",
        report_path=str(report),
        pr_id=str(PR),
    )


def test_record_result_books_two_heads_as_two_receipts_with_two_keys(env, monkeypatch):
    heads = iter([COMMIT_A, COMMIT_B])
    monkeypatch.setattr(gate_result_parser, "get_pr_head_sha", lambda _pr: next(heads))
    manager = rgm.ReviewGateManager()

    _record_result(manager, env, COMMIT_A)
    _record_result(manager, env, COMMIT_B)

    receipts = _receipts(env)
    assert len(receipts) == 2, f"two heads must book two receipts, got {len(receipts)}"
    assert [receipt["commit_sha"] for receipt in receipts] == [COMMIT_A, COMMIT_B]
    keys = _keys(receipts)
    assert len(set(keys)) == 2, f"the two heads must not share an idempotency key: {keys}"

    result_record = json.loads((env["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    assert receipts[-1]["commit_sha"] == result_record["commit_sha"]


# ---------------------------------------------------------------------------
# gate_result_parser.record_claude_github_result (the head off its request, GE-4c-1)
# ---------------------------------------------------------------------------


def test_claude_github_receipt_carries_the_request_head(env):
    manager = rgm.ReviewGateManager()
    manager._contract_request_path("claude_github_optional", "PR-1").write_text(
        json.dumps({"commit_sha": COMMIT_A, "contract_hash": "contract-hash"}), encoding="utf-8",
    )
    report = env["reports_dir"] / "claude-gate-report.md"
    report.write_text("# report\n", encoding="utf-8")

    manager.record_claude_github_result(
        pr_id="PR-1",
        branch=BRANCH,
        status="pass",
        summary="clean",
        pr_number=PR,
        contract_hash="contract-hash",
        report_path=str(report),
    )

    (receipt,) = _receipts(env)
    assert receipt["commit_sha"] == COMMIT_A
    assert receipt["head_sha"] == COMMIT_A
