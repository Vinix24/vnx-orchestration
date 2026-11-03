"""OI-2453: a re-request on a seat whose takeover verdict stands must not crash.

Measured by T0 on 2026-10-28 (PR #2328 head 1c79ba8e, PR #2342 head 0efe9e8e):
``request_reviews`` on a seat whose reviewer already decided on this head
reports that standing record as the seat's answer. A decided takeover record
carries ``takeover: True`` and ``takeover_from`` but no ``failure_reason``, and
``_emit_review_request_receipt`` read ``request["failure_reason"]``
unconditionally, so a SECOND request of the same seat on the same head died
with ``KeyError: 'failure_reason'`` before any request or result was written.
That also broke the obligation runner and ``gates.sh`` for the PR.

What is pinned here:

- a standing decided PASS from a takeover reviewer, with no ``failure_reason``
  in the record, is reported as the seat's answer, its ``review_gate_request``
  receipt is emitted, and nothing is re-dispatched (no request record is
  written, the result record is untouched);
- the same for a decided FAIL;
- a LIVE takeover request that does carry ``failure_reason``/``takeover_from``
  still travels both fields top-level on the receipt (OI-1415 unchanged).

No provider lane is reached: ``request_reviews`` requests and never runs.
Every file lives under ``tmp_path``; the only edges patched are the GitHub
head/base lookups and the receipt sink.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

CLEAN_REPORT = "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n"
_FRESH_RECORDED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_request_handler
    import review_gate_manager

    # No gh round-trip: the head and the base are fixed, and an unknown base
    # keeps the risk class derivation off local git (class high, no process).
    monkeypatch.setattr(gate_request_handler, "get_pr_base_branch", lambda _pr: "")
    # The operator's real switch file must not decide a test's seats.
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(tmp_path / "providers.json"))

    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        review_gate_manager, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    store["receipts"] = receipts
    store["ledger"] = store["state_dir"] / "review_gates" / "rounds.ndjson"
    store["headless"] = store["reports_dir"] / "headless"
    store["headless"].mkdir(parents=True, exist_ok=True)
    return store


# ---------------------------------------------------------------------------
# Plain helpers: real files, the ledger as NDJSON
# ---------------------------------------------------------------------------


def _opened(pr: int = PR) -> Dict[str, Any]:
    return {
        "kind": "opened", "project_id": "", "pr_number": pr, "basis": "new", "evidence": 0,
        "recorded_at": "2026-10-28T00:00:00Z", "source": "gate",
    }


def _line(
    seat: str, *, reviewer: str, verdict: str, blocking: int, report: str,
    takeover_from: str = "", pr: int = PR,
) -> Dict[str, Any]:
    return {
        "project_id": "", "pr_number": pr, "head_sha": HEAD_SHA, "seat": seat,
        "reviewer": reviewer, "takeover_from": takeover_from, "verdict": verdict,
        "blocking_count": blocking, "report_path": report,
        "dispatch_id": f"20261028-oi2453-{reviewer}", "recorded_at": "2026-10-28T10:00:00Z",
        "branch": BRANCH, "source": "gate",
    }


def _write_ledger(path: Path, lines: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _report(store, gate: str, *, tag: str = "01", pr: int = PR) -> Path:
    path = store["headless"] / f"20261028-1000{tag}-HEADLESS-{gate}-pr-{pr}-{tag}ab.md"
    path.write_text(
        f"# {gate} — Headless Gate Report\n\n**PR**: {pr}\n**Branch**: {BRANCH}\n**Gate**: {gate}\n"
        f"**Generated**: 2026-10-28T10:00:{tag}Z\n\n---\n\n{CLEAN_REPORT}",
        encoding="utf-8",
    )
    return path


def _decided_takeover_result(
    store, gate: str, *, seat: str, takeover_from: str, verdict: str, tag: str = "01",
) -> Dict[str, Any]:
    """A decided takeover result as measured in PR #2328: ``takeover`` and
    ``takeover_from`` are set, ``failure_reason`` is ABSENT."""
    report = _report(store, gate, tag=tag)
    payload: Dict[str, Any] = {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed",
        "summary": f"{gate} execution completed successfully",
        "contract_hash": f"{gate}-{tag}", "report_path": str(report), "findings": [],
        "blocking_findings": [{"severity": "blocking", "message": f"defect {n}"} for n in range(1 if verdict == "fail" else 0)],
        "advisory_findings": [], "commit_sha": HEAD_SHA, "branch": BRANCH,
        "dispatch_id": f"20261028-oi2453-{gate}", "recorded_at": "2026-10-28T10:00:00Z",
        "seat": seat, "takeover": True, "takeover_from": takeover_from,
        "takeover_reason": "lane_exhausted", "takeover_source_status": "unavailable",
        "takeover_path": [{
            "gate": takeover_from, "reason": "dispatch_error",
            "detail": "403 account suspended", "status": "unavailable",
        }],
    }
    path = store["results_dir"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _request(store, stack: List[str]) -> Dict[str, Any]:
    import review_gate_manager

    return review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=stack, risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"], mode="per_pr",
        dispatch_id="dlv-7c211870c9c1-test",
    )


def _request_receipts(store) -> List[Dict[str, Any]]:
    return [receipt for receipt in store["receipts"] if receipt.get("event") == "review_gate_request"]


def _assert_standing_seat_reported(store, result: Dict[str, Any], *, seat: str, reviewer: str, verdict: str):
    payload = result["requested"][0]
    assert payload["gate"] == reviewer, payload
    assert payload.get("seat") == seat, payload
    assert payload.get("round_refusal") == "verdict_on_head", payload
    assert "second run on a head with a verdict is refused" in payload["redispatch_refused"]
    if verdict == "pass":
        assert payload["status"] == "completed" and not payload.get("blocking_findings"), payload
    else:
        assert payload["status"] == "completed" and payload.get("blocking_findings"), payload

    # Nothing was re-dispatched: no request record for the reviewer exists.
    assert not (store["requests_dir"] / f"pr-{PR}-{reviewer}.json").exists(), (
        "a standing verdict must not write a new request record"
    )

    receipts = _request_receipts(store)
    assert len(receipts) == 1, receipts
    receipt = receipts[0]
    assert receipt["gate"] == reviewer, receipt
    assert receipt["request"] is not None, receipt
    assert receipt["request"].get("round_refusal") == "verdict_on_head", receipt
    # An absent field is not invented and not written as None (the OI-2453 crash).
    assert "failure_reason" not in receipt, receipt
    # The record DID carry takeover_from, so the receipt carries it too.
    assert receipt["takeover_from"] == "kimi_gate", receipt


# ---------------------------------------------------------------------------
# The crash (OI-2453)
# ---------------------------------------------------------------------------


class TestReRequestOnAStandingTakeoverVerdict:
    def test_standing_takeover_pass_is_reported_and_emits_its_request_receipt(self, env):
        record = _decided_takeover_result(
            env, "claude_gate", seat="kimi_gate", takeover_from="kimi_gate", verdict="pass",
        )
        assert "failure_reason" not in record, "the measured shape carries no failure_reason"
        report_before = (env["results_dir"] / f"pr-{PR}-claude_gate.json").read_bytes()
        _write_ledger(env["ledger"], [
            _opened(),
            _line("kimi_gate", reviewer="claude_gate", verdict="pass", blocking=0,
                  report=str(record["report_path"]), takeover_from="kimi_gate"),
        ])

        result = _request(env, ["kimi_gate"])

        _assert_standing_seat_reported(env, result, seat="kimi_gate", reviewer="claude_gate", verdict="pass")
        assert (env["results_dir"] / f"pr-{PR}-claude_gate.json").read_bytes() == report_before, (
            "a standing verdict's evidence record is never rewritten"
        )

    def test_standing_takeover_fail_is_reported_and_emits_its_request_receipt(self, env):
        record = _decided_takeover_result(
            env, "claude_gate", seat="kimi_gate", takeover_from="kimi_gate", verdict="fail",
        )
        assert "failure_reason" not in record, "the measured shape carries no failure_reason"
        _write_ledger(env["ledger"], [
            _opened(),
            _line("kimi_gate", reviewer="claude_gate", verdict="fail", blocking=1,
                  report=str(record["report_path"]), takeover_from="kimi_gate"),
        ])

        result = _request(env, ["kimi_gate"])

        _assert_standing_seat_reported(env, result, seat="kimi_gate", reviewer="claude_gate", verdict="fail")


# ---------------------------------------------------------------------------
# OI-1415 behaviour unchanged: a LIVE takeover still travels both fields
# ---------------------------------------------------------------------------


def test_live_takeover_request_still_stamps_failure_reason_and_takeover_from(env):
    _write_ledger(env["ledger"], [_opened()])
    exhausted = {
        "gate": "kimi_gate", "pr_id": str(PR), "pr_number": PR, "test_run": False,
        "status": "unavailable", "reason": "dispatch_error", "summary": "kimi gate: UNAVAILABLE",
        "contract_hash": "", "report_path": "", "provider": "kimi", "model": "kimi-k3",
        "dispatch_id": f"kimi-gate-pr{PR}-1755852660", "blocking_findings": [],
        "advisory_findings": [], "required_reruns": [],
        "residual_risk": (
            "governed kimi dispatch failed: Error code: 403 - "
            "{'error': {'message': 'Your account has been suspended, please "
            "contact us via api-feedback@moonshot.cn', "
            "'type': 'access_terminated_error'}}"
        ),
        "recorded_at": _FRESH_RECORDED_AT, "branch": BRANCH, "commit_sha": HEAD_SHA,
    }
    (env["results_dir"] / f"pr-{PR}-kimi_gate.json").write_text(
        json.dumps(exhausted, indent=2), encoding="utf-8",
    )

    result = _request(env, ["kimi_gate"])

    payload = result["requested"][0]
    assert payload["gate"] == "claude_gate", payload
    assert payload.get("takeover") is True, payload
    assert payload.get("takeover_from") == "kimi_gate", payload
    assert "kimi_gate" in payload.get("failure_reason", ""), payload

    receipts = _request_receipts(env)
    assert len(receipts) == 1, receipts
    receipt = receipts[0]
    assert receipt["takeover_from"] == "kimi_gate", receipt
    assert receipt["failure_reason"] == payload["failure_reason"], receipt
    assert receipt["failure_reason"].strip(), receipt
