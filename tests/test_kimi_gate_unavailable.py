"""kimi_gate provider-outage status tests (OI-1142, OI-1291).

Eleven kimi quota-403 outages were once booked as eleven review "fail"
records (reason "no_verdict", summary 'kimi gate: fail (0 blocking
finding(s))') because ``_verdict_to_status`` defaulted the no-readable-verdict
path to "fail". A provider that cannot fire produces NO verdict — that is
absence of evidence and must surface as status ``unavailable``, never as a
rejected PR. These tests pin:

- provider-error output (403 text, empty/truncated report) -> "unavailable",
  exit 1, a summary that is unmistakably not a review fail
- a dispatcher exception still writes an ``unavailable`` record instead of
  vanishing without a trace
- a REAL parsed verdict (pass / fail with findings) behaves exactly as before
- gate_status treats "unavailable" as neither pass nor fail nor terminal

OI-1291: ``unavailable`` conflated two very different situations under one
"provider outage" label — a dispatcher exception (no report at all) and a
report that came back with real content but no readable ```json``` verdict
block (kimi reviewed in prose; the fence just didn't parse). The latter is a
parse miss, not an outage, and must say so: reason="parse_error", and none
of the three operator-facing strings (summary, residual_risk, stdout) may
claim "outage" when a report actually existed. A genuinely empty report
(no exception, but nothing came back) is not a parse miss and keeps the
old reason="no_verdict" behavior.

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

# OI-1291: kimi reviewed and produced real prose, but no fenced ```json```
# verdict block — this must land as a parse miss, not a provider outage.
_PROSE_NO_VERDICT_REPORT = "## Findings\n\nNone\n"


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


def test_quota_403_text_books_parse_error_not_outage(tmp_path, monkeypatch):
    """A report with real (error) content but no fenced verdict is a parse
    miss (OI-1291), not an outage: kimi plainly responded with something."""
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _QUOTA_403_REPORT)
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] == "parse_error"
    assert rc == 1  # infra/unavailable, not the exit-2 review fail
    assert "UNAVAILABLE" in record["summary"]
    assert "outage" not in record["summary"].lower()
    assert str(len(_QUOTA_403_REPORT)) in record["summary"]
    assert record["blocking_findings"] == []


def test_empty_report_books_unavailable(tmp_path, monkeypatch):
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: "")
    assert record is not None
    assert record["status"] == "unavailable"
    # OI-1291: a genuinely empty report (no exception, nothing came back) is
    # NOT a parse miss — it keeps the pre-existing no_verdict reason.
    assert record["reason"] == "no_verdict"
    assert rc == 1


def test_prose_report_without_fence_books_parse_error_not_outage(tmp_path, monkeypatch, capsys):
    """OI-1291 core case: kimi reviewed (real prose, e.g. a ``## Findings``
    section) but the contract's fenced ```json``` verdict block is missing.
    That is a parse miss, distinguishable from a dispatch/provider outage —
    none of the three operator surfaces (summary, residual_risk, stdout) may
    say "outage" here."""
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _PROSE_NO_VERDICT_REPORT)
    captured = capsys.readouterr()
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] == "parse_error"
    assert rc == 1
    assert "outage" not in record["summary"].lower()
    assert "outage" not in record["residual_risk"].lower()
    assert "outage" not in captured.out.lower()
    # the operator must be able to see that content DID come back
    report_len = str(len(_PROSE_NO_VERDICT_REPORT))
    assert report_len in record["summary"]
    assert report_len in record["residual_risk"]
    assert record["blocking_findings"] == []


def test_parse_miss_and_dispatch_error_are_distinguishable(tmp_path, monkeypatch):
    """The two 'unavailable' causes OI-1291 conflated must fall apart on
    ``reason``: a parse miss (report with content, no fence) is not the same
    as a dispatch exception (no report produced at all)."""
    rc_parse, record_parse = _run_gate(
        tmp_path, monkeypatch, lambda *a, **k: _PROSE_NO_VERDICT_REPORT
    )

    def _boom(*a, **k):
        raise RuntimeError("kimi dispatch exploded")

    rc_dispatch, record_dispatch = _run_gate(tmp_path, monkeypatch, _boom)

    assert record_parse["status"] == record_dispatch["status"] == "unavailable"
    assert record_parse["reason"] == "parse_error"
    assert record_dispatch["reason"] == "dispatch_error"
    assert record_parse["reason"] != record_dispatch["reason"]
    assert rc_parse == rc_dispatch == 1
    # only the parse-miss summary is forbidden from claiming "outage"
    assert "outage" not in record_parse["summary"].lower()


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
