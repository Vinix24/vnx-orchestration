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

OI-1291 fix-forward: "did a report come back" was the WRONG axis for
parse_error vs outage. A real quota/auth outage still writes a report —
the failure-path ``emit_unified_report`` call runs on the dispatcher's
failure path too — so a spooled 403 error body is "content" by the same
measure as a real review. The first cut of this fix read that as "kimi did
respond, parse miss", which books a real outage as a review event: the same
failure class OI-1291 exists to remove, just mirrored. The report's own
YAML frontmatter carries the signal content can't fake: ``exit_code`` and
``token_usage.output`` are stamped by the lane from the actual spawn
result. A non-zero exit_code (or zero output tokens) with readable
frontmatter means the underlying kimi run failed — that is
reason="dispatch_error" even though a report exists. Only exit_code == 0
with readable frontmatter and no verdict block is a genuine parse_error.

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

def _kimi_report_with_frontmatter(
    body: str, *, exit_code: int, output_tokens: int = 0, dispatch_id: str = "kimi-gate-pr0-test",
) -> str:
    """Build a report in the exact shape the governed lane actually writes:
    YAML frontmatter (``governance_emit.emit_unified_report``) followed by
    the body — the shape OI-1291's fix-forward measured on real quota-outage
    artifacts on disk (``exit_code: 1`` / ``token_usage.output: 0`` on a
    failed run, alongside a non-empty body from the failure-path report)."""
    return (
        "---\n"
        "schema_version: 1\n"
        f"dispatch_id: {dispatch_id}\n"
        "provider: kimi\n"
        "sub_provider: moonshot\n"
        "model: kimi-k3\n"
        "duration_seconds: 154.095\n"
        f"exit_code: {exit_code}\n"
        "token_usage:\n"
        "  input: 0\n"
        f"  output: {output_tokens}\n"
        "  cache_read: 0\n"
        "---\n"
        "\n"
        f"{body}"
    )


_QUOTA_403_BODY = (
    "Error: API request failed with status 403: "
    '{"error":{"type":"access_terminated_error","message":'
    '"You have reached your usage limit for this billing cycle."}}\n'
)

# OI-1291 fix-forward: a REAL quota outage — the underlying kimi run failed,
# stamped by its OWN frontmatter (exit_code: 1, output: 0), exactly like the
# real artifacts measured on disk. This must NOT land as parse_error: the
# provider never produced a review, it produced an error page.
_QUOTA_403_REPORT = _kimi_report_with_frontmatter(_QUOTA_403_BODY, exit_code=1, output_tokens=0)

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
# OI-1291 fix-forward: the frontmatter carries exit_code: 0 and a non-zero
# output-token count — the run's OWN record of itself says it completed
# cleanly, so a missing verdict block here can only be a parse miss.
_PROSE_NO_VERDICT_BODY = "## Findings\n\nNone\n"
_PROSE_NO_VERDICT_REPORT = _kimi_report_with_frontmatter(
    _PROSE_NO_VERDICT_BODY, exit_code=0, output_tokens=340,
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


def test_quota_403_frontmatter_exit_code_books_dispatch_error_not_parse_error(tmp_path, monkeypatch):
    """OI-1291 fix-forward: a REAL quota outage still writes a report — the
    failure-path ``emit_unified_report`` call runs on the dispatcher's
    failure path too — so "content exists" alone cannot separate an outage
    from a parse miss. That was the bug reintroduced by the first cut of
    this fix: it read the spooled 403 body as "kimi did respond, parse
    miss". The report's OWN frontmatter (exit_code: 1, output: 0 — exactly
    the shape measured on real quota-outage artifacts on disk) says the
    underlying kimi run failed, so this must land as dispatch_error, the
    provider-outage class, never parse_error."""
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _QUOTA_403_REPORT)
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] != "parse_error"
    assert record["reason"] == "dispatch_error"
    assert rc == 1  # infra/unavailable, not the exit-2 review fail
    assert "UNAVAILABLE" in record["summary"]
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
    """OI-1291 core case, fix-forward: kimi reviewed (real prose, e.g. a
    ``## Findings`` section) and its OWN report frontmatter stamps a clean
    run (exit_code: 0, non-zero output tokens) — but the contract's fenced
    ```json``` verdict block is missing. That is a genuine parse miss,
    correctly distinguishable from a provider outage BECAUSE the frontmatter
    says the run completed; none of the three operator surfaces (summary,
    residual_risk, stdout) may say "outage" here."""
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: _PROSE_NO_VERDICT_REPORT)
    captured = capsys.readouterr()
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] == "parse_error"
    assert rc == 1
    assert "outage" not in record["summary"].lower()
    assert "outage" not in record["residual_risk"].lower()
    assert "outage" not in captured.out.lower()
    # the operator must be able to see that content DID come back — the
    # length is measured off the FULL report text (frontmatter + body), the
    # same string the dispatcher actually returned.
    report_len = str(len(_PROSE_NO_VERDICT_REPORT))
    assert report_len in record["summary"]
    assert report_len in record["residual_risk"]
    assert record["blocking_findings"] == []


def test_frontmatter_exit_code_alone_flips_reason_dispatch_error_vs_parse_error(tmp_path, monkeypatch):
    """OI-1291 fix-forward, item 3: the report's own frontmatter exit_code is
    the ONLY thing allowed to separate an outage from a parse miss — not
    body content, not body length. Two reports share the exact same
    non-empty body (and therefore the same body length); only the exit_code
    (and its paired output-tokens signal) fed into the frontmatter differs,
    and that alone must flip the reason."""
    shared_body = "## Findings\n\nNone\n"
    failed_exit_code = 1
    clean_exit_code = 0
    failed_report = _kimi_report_with_frontmatter(
        shared_body, exit_code=failed_exit_code, output_tokens=0,
    )
    clean_report = _kimi_report_with_frontmatter(
        shared_body, exit_code=clean_exit_code, output_tokens=340,
    )

    rc_failed, record_failed = _run_gate(tmp_path, monkeypatch, lambda *a, **k: failed_report)
    rc_clean, record_clean = _run_gate(tmp_path, monkeypatch, lambda *a, **k: clean_report)

    assert record_failed["reason"] == "dispatch_error"
    assert record_clean["reason"] == "parse_error"
    assert record_failed["reason"] != record_clean["reason"]
    assert rc_failed == rc_clean == 1
    assert "outage" not in record_clean["summary"].lower()
    # the exit_code value itself, not just the derived reason, must be
    # traceable in the failed record's residual_risk
    assert f"exit_code={failed_exit_code!r}" in record_failed["residual_risk"]


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


def test_content_without_frontmatter_falls_back_to_parse_error(tmp_path, monkeypatch):
    """OI-1291 fix-forward: an older/foreign report shape with no readable
    frontmatter at all carries no exit_code to read, so there is no signal
    left to tell a masked provider failure apart from a genuine parse miss.
    This falls back to the pre-fix-forward default (parse_error) — the
    fallback is intentionally too broad, not silently wrong."""
    no_frontmatter_report = "Reviewed in prose, no fenced block, no frontmatter at all.\n"
    rc, record = _run_gate(tmp_path, monkeypatch, lambda *a, **k: no_frontmatter_report)
    assert record is not None
    assert record["status"] == "unavailable"
    assert record["reason"] == "parse_error"
    assert rc == 1


def test_frontmatter_run_outcome_unit_cases():
    """Direct coverage of the three ``_frontmatter_run_outcome`` branches:
    failed run, clean run, and unreadable/missing frontmatter."""
    failed = _kimi_report_with_frontmatter("body text here", exit_code=1, output_tokens=0)
    outcome, exit_code, output_tokens = kimi_gate._frontmatter_run_outcome(failed)
    assert outcome is True
    assert exit_code == 1
    assert output_tokens == 0

    clean = _kimi_report_with_frontmatter("body text here", exit_code=0, output_tokens=50)
    outcome, exit_code, output_tokens = kimi_gate._frontmatter_run_outcome(clean)
    assert outcome is False
    assert exit_code == 0
    assert output_tokens == 50

    # exit_code 0 but zero output tokens is still a failure signal per the
    # OI-1291 fix-forward directive ("exit_code niet 0 (of output-tokens 0)").
    zero_output = _kimi_report_with_frontmatter("body text here", exit_code=0, output_tokens=0)
    outcome, _, _ = kimi_gate._frontmatter_run_outcome(zero_output)
    assert outcome is True

    outcome, exit_code, output_tokens = kimi_gate._frontmatter_run_outcome("no frontmatter here")
    assert outcome is None
    assert exit_code is None
    assert output_tokens is None


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
