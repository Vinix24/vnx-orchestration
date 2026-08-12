"""kimi_gate provider-outage status tests (OI-1142).

Eleven kimi quota-403 outages were once booked as eleven review "fail"
records (reason "no_verdict", summary 'kimi gate: fail (0 blocking
finding(s))') because ``_verdict_to_status`` defaulted the no-readable-verdict
path to "fail". A provider that cannot fire produces NO verdict — that is
absence of evidence and must surface as status ``unavailable``, never as a
rejected PR. These tests pin:

- provider-error output (403 text, empty/truncated report) -> "unavailable",
  exit 1, a summary that is unmistakably an outage
- a dispatcher exception still writes an ``unavailable`` record instead of
  vanishing without a trace
- a REAL parsed verdict (pass / fail with findings) behaves exactly as before
- gate_status treats "unavailable" as neither pass nor fail nor terminal

``_make_default_dispatcher`` is patched on the kimi_gate module namespace,
not on plan_gate_panel where it is defined — `from X import Y` binds the name
at import time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import kimi_gate
from gate_status import (
    ALL_KNOWN_STATES,
    FAIL_STATES,
    PASS_STATES,
    is_pass,
    is_terminal,
)

_QUOTA_403_REPORT = (
    "Error: API request failed with status 403: "
    '{"error":{"type":"access_terminated_error","message":'
    '"You have reached your usage limit for this billing cycle."}}\n'
)

_REAL_FAIL_REPORT = (
    "Reviewed the diff; found a real problem.\n\n"
    "```json\n"
    '{"verdict": "fail", "findings": [{"severity": "error", "message": "sql injection"}],'
    ' "residual_risk": "unsanitized input"}\n'
    "```\n"
)

_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)


def _run_gate(tmp_path, monkeypatch, dispatcher):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text("diff --git a/x b/x\n+ok\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: dispatcher)
    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, record


# ---------------------------------------------------------------------------
# Provider outage -> unavailable, never fail
# ---------------------------------------------------------------------------


def test_quota_403_text_books_unavailable_not_fail(tmp_path, monkeypatch):
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _QUOTA_403_REPORT)
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] == "no_verdict"
    assert rc == 1  # infra/unavailable, not the exit-2 review fail
    assert "UNAVAILABLE" in record["summary"]
    assert "NOT a review fail" in record["summary"]
    assert record["blocking_findings"] == []


def test_empty_report_books_unavailable(tmp_path, monkeypatch):
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: "")
    assert record is not None
    assert record["status"] == "unavailable"
    assert rc == 1


def test_dispatch_exception_writes_unavailable_record(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no report for kimi-gate-pr0 (rc=1): stderr: 403 access_terminated_error")

    rc, record = _run_gate(tmp_path, monkeypatch, _boom)
    assert rc == 1
    assert record is not None, "an outage must leave an unavailable record, not vanish"
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    assert "access_terminated_error" in record["residual_risk"]


def test_verdict_to_status_empty_verdict_is_unavailable():
    status, blocking, residual = kimi_gate._verdict_to_status({})
    assert status == "unavailable"
    assert blocking == []
    assert "no readable verdict" in residual


# ---------------------------------------------------------------------------
# Real parsed verdicts: unchanged behaviour
# ---------------------------------------------------------------------------


def test_real_fail_verdict_still_fails_with_exit_2(tmp_path, monkeypatch):
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _REAL_FAIL_REPORT)
    assert rc == 2
    assert record["status"] == "fail"
    assert record["reason"] == "verdict"
    assert record["summary"] == "kimi gate: fail (1 blocking finding(s))"
    assert len(record["blocking_findings"]) == 1


def test_real_pass_verdict_still_passes(tmp_path, monkeypatch):
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _REAL_PASS_REPORT)
    assert rc == 0
    assert record["status"] == "pass"
    assert record["summary"] == "kimi gate: pass (0 blocking finding(s))"


# ---------------------------------------------------------------------------
# gate_status: unavailable is neither pass nor fail nor terminal
# ---------------------------------------------------------------------------


def test_gate_status_unavailable_is_known_and_not_fail():
    assert "unavailable" in ALL_KNOWN_STATES
    assert "unavailable" not in FAIL_STATES
    assert "unavailable" not in PASS_STATES


def test_gate_status_unavailable_is_not_pass_with_outage_reason():
    passed, reason = is_pass({"status": "unavailable", "blocking_findings": []})
    assert passed is False
    assert "unavailable" in reason
    assert "not a review fail" in reason
    assert "unknown" not in reason


def test_gate_status_unavailable_is_not_terminal():
    # Closure must treat an outage as incomplete evidence (retryable), never as
    # a decided pass/fail outcome.
    assert is_terminal({"status": "unavailable"}) is False
