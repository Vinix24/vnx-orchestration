"""Characterisation of ``_build_receipt_from_report_core`` (golf schoon R41b, class C).

Every receipt branch with full key order, the ambiguous-path stamp and log lines.
Real files under ``tmp_path``; only ``git ls-remote`` is patched. Register check
and lane identity: ``test_report_to_receipt_core_steps.py``.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import report_to_receipt_converter as rtc

_LOG = "report_to_receipt_converter"
_BODY = ("## Summary\n\nImplemented the feature per dispatch specification. All tests pass "
         "and coverage is at target.\n\n## Changes\n\n- scripts/lib/x.py: added X\n\n"
         "## Verification\n\npytest tests/ -x: 42 passed\n\n## Open Items\n\nNone\n")
_ID = "provider: claude\nmodel: opus\nrole: tester\ntimestamp: 2026-06-01T00:00:00Z\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    central = tmp_path / "data" / "unified_reports"
    central.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)
    (tmp_path / "state" / "route_decisions").mkdir(parents=True)
    (tmp_path / "reports").mkdir()
    return tmp_path, central


def _build(env, did, fm, body=_BODY, ambiguous=True, state=False, with_id=True):
    tmp, central = env
    text = f"---\n{(f'dispatch_id: {did}' + chr(10)) if with_id else ''}{_ID}{fm}---\n\n{body}"
    path = tmp / "reports" / f"{did}.md"
    path.write_text(text, encoding="utf-8")
    cands = []
    if ambiguous:
        for name in (f"{did}.md", f"dispatch-{did}.md"):
            (central / name).write_text("x" * len(name), encoding="utf-8")
            cands.append({"path": str(central / name), "size": len(name)})
    receipt = rtc._build_receipt_from_report_core(
        path, text, state_dir=tmp / "state" if state else None)
    head = {"dispatch_id": did, "receipt_kind": "dispatch", "role": "tester",
            "terminal": "unknown", "provider": "claude", "model": "opus",
            "timestamp": "2026-06-01T00:00:00Z",
            "report_path": str(central / f"{did}.md") if ambiguous else str(path)}
    amb = {"ambiguous_report_path": True, "report_path_candidates": cands} if ambiguous else {}
    return receipt, head, amb


def _same(receipt, expected):
    assert list(receipt.items()) == list(expected.items())


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == _LOG]


def test_unknown_status_refused(env, caplog):
    with caplog.at_level(logging.INFO, logger=_LOG):
        r, head, amb = _build(env, "20260601-r41b-unk", "status: bogus\n")
    _same(r, {**head, "event_type": "task_failed", "status": "failed",
              "failure_reason": "unknown_status", "unknown_status": "bogus", **amb})
    assert ("report_to_receipt_converter: unknown status dispatch=20260601-r41b-unk "
            "status='bogus': refusing") in _msgs(caplog)


@pytest.mark.parametrize("fm, derived_log", [
    ("status: success\n", None),
    ("exit_code: 0\n", "report_to_receipt_converter: dispatch=20260601-r41b-sc no status "
                       "field — derived status='success' from exit_code=0"),
])
def test_success_claim_fail_closed(env, caplog, fm, derived_log):
    with caplog.at_level(logging.INFO, logger=_LOG):
        r, head, amb = _build(env, "20260601-r41b-sc", fm)
    viol = ["frontmatter_v1: missing field: schema_version"]
    _same(r, {**head, "event_type": "task_failed", "status": "failure",
              "fail_closed_violations": viol, **amb})
    msgs = _msgs(caplog)
    assert msgs[-1] == ("report_to_receipt_converter: fail-closed REJECTION "
                        f"dispatch=20260601-r41b-sc status=success violations={viol}")
    if derived_log:
        assert msgs[-2] == derived_log


def test_declared_failure(env):
    r, head, amb = _build(env, "20260601-r41b-f", "status: failed\nfailure_reason: boom\ntask_id: T-7\n")
    _same(r, {**head, "task_id": "T-7", "event_type": "task_failed", "status": "failed",
              "failure_reason": "boom", **amb})
    r, head, _ = _build(env, "20260601-r41b-f1", "exit_code: 1\n", ambiguous=False)
    _same(r, {**head, "event_type": "task_failed", "status": "failed"})


def test_contract_invalid(env, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOG):
        r, head, amb = _build(env, "20260601-r41b-ci", "", body="## Summary\n\nshort\n", with_id=False)
    viol = ["missing_content_dispatch_id", "## Changes", "## Verification", "## Open Items",
            "## Summary (too short)"]
    _same(r, {**head, "event_type": "report_contract_invalid", "status": "contract_invalid",
              "contract_violations": viol, **amb})
    assert _msgs(caplog)[-1] == ("report_to_receipt_converter: contract violations in "
                                 f"20260601-r41b-ci.md: {viol} — emitting as report_contract_invalid")


def test_derived_task_complete_with_route_decision(env, caplog):
    route = {"decision": {"provider": "claude", "model": "sonnet"}}
    (env[0] / "state" / "route_decisions" / "20260601-r41b-d.json").write_text(json.dumps(route))
    with caplog.at_level(logging.INFO, logger=_LOG):
        r, head, amb = _build(env, "20260601-r41b-d", "exit_code: abc\ntask_id: n/a\n", state=True)
    _same(r, {**head, "model": "sonnet", "event_type": "task_complete", "status": "done",
              "status_source": "report_contract", **amb, "route_decision": route})
    assert _msgs(caplog)[-1] == ("report_to_receipt_converter: dispatch=20260601-r41b-d no status "
                                 "and no exit_code, derived status='done' from the report contract")
    r, head, _ = _build(env, "20260601-r41b-d2", "", ambiguous=False)
    _same(r, {**head, "event_type": "task_complete", "status": "done",
              "status_source": "report_contract"})


def test_no_dispatch_id_returns_none(env, caplog):
    path = env[0] / "reports" / "unknown.md"
    path.write_text("---\nprovider: claude\n---\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=_LOG):
        assert rtc._build_receipt_from_report_core(path, path.read_text()) is None
    assert _msgs(caplog) == ["report_to_receipt_converter: no dispatch_id for unknown.md — skipping"]
