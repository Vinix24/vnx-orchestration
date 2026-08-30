"""Every gate result that is not a pass says why, in the field named for it (OI-1453).

``failure_reason`` is the field a generic reader queries. OI-1415 established it
for phantom_guard and pr_enforcement; gate result records never adopted it.

Re-measured across the whole central store on 2026-08-30, three project stores,
739 non-pass gate-result records:

    failure_reason filled:  4 of 739

    summary          790 filled (prose, often frontmatter-derived)
    residual_risk    699 filled (prose, often frontmatter-derived)
    reason           397 filled (a CLASSIFICATION: provider_not_installed 145,
                                 claude_github_not_configured 63, exit_nonzero
                                 60, provider_disabled 57, dispatch_error 6 ...)
    reason_detail    270 filled (a CAUSE: "codex binary not found in PATH" 106,
                                  "Subprocess exited with code 1" 59,
                                  "VNX_CI_GATE_REQUIRED is set to 0" 57 ...)
    blocking_findings  64 filled (a cause)
    failure_reason     4 filled (the gap OI-1453 opens)

``failure_reason`` carries a CAUSE, never a category, a summary, or a
placeholder. The cause reaches it by one of three routes, and only those:

    1. the lane-log lift (OI-1452) stamps it directly in the report
       frontmatter when it found a real exhaustion marker;
    2. a gate that ran to completion and failed on its findings carries it as
       ``blocking_findings``;
    3. a lane that filed the cause under ``reason_detail`` (the PATH-missing,
       exit-nonzero, disabled-flag records above).

What is deliberately NOT a route: ``reason`` (a classification), and
``residual_risk``/``summary`` (prose that resembles a cause but is not one).
The original OI-1453 derivation walked all of them, and on the real PR #1677
outage record ``reason="dispatch_error"`` won -- a category landed where a
cause belongs, and four OI-1452 tests went red asserting the field should be
empty when the cause was never established.

Three states, each with its own meaning:

    pass                              -> failure_reason absent
    non-pass WITH an established cause -> failure_reason carries the cause
    non-pass, cause NOT established    -> failure_reason empty (a valid third
                                          state, NOT a defect)

A guard that treats the third state as a violation is the trap this PR
repairs: it forces placeholders back into the field, every gate fills it with
something to pass the check, and the field stops meaning anything.
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

import gate_artifacts
import gate_recorder
from gate_recorder import record_failure, record_not_executable


@pytest.fixture
def dirs(tmp_path):
    requests = tmp_path / "state" / "review_gates" / "requests"
    results = tmp_path / "state" / "review_gates" / "results"
    state = tmp_path / "state"
    reports = tmp_path / "unified_reports"
    for d in (requests, results, state, reports):
        d.mkdir(parents=True, exist_ok=True)
    return requests, results, state, reports


def _request(**over):
    payload = {
        "gate": "codex_gate", "pr_id": "", "pr_number": 1453,
        "branch": "fix/x", "commit_sha": "a" * 40,
        "contract_hash": "088a30754169bb91",
    }
    payload.update(over)
    return payload


def _record(results_dir, gate="codex_gate", pr=1453):
    return json.loads(
        (results_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------
# Every writer, through the one point they share.
# --------------------------------------------------------------------------

def test_a_not_executable_record_says_why(dirs):
    requests, results, state, _reports = dirs

    record_not_executable(
        gate="codex_gate", pr_number=1453, pr_id="",
        reason="provider_not_installed",
        reason_detail="codex binary not found in PATH",
        request_payload=_request(),
        requests_dir=requests, results_dir=results, state_dir=state,
    )

    assert _record(results)["failure_reason"] == "codex binary not found in PATH"


def test_a_failure_record_says_why(dirs):
    requests, results, _state, _reports = dirs

    record_failure(
        gate="codex_gate", pr_number=1453, pr_id="",
        result={
            "reason": "timeout", "reason_detail": "codex headless stalled at 900s",
            "duration_seconds": 900.0, "partial_output_lines": 0, "runner_pid": 1,
        },
        request_payload=_request(),
        requests_dir=requests, results_dir=results,
    )

    assert _record(results)["failure_reason"] == "codex headless stalled at 900s"


def test_a_completed_record_that_failed_on_findings_says_why(dirs):
    """No reason field exists on such a record. The findings are the reason.

    A gate that ran to completion and failed on what it found carries no
    ``reason``/``reason_detail`` at all, so a derivation that only walked the
    text fields would fall through to prose about something else — or to
    nothing.
    """
    requests, results, _state, reports = dirs
    stream = "\n".join([
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i0", "type": "command_execution", "command": "sed -n 1,40p x.py"}}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message",
            "text": json.dumps({
                "findings": [{
                    "severity": "blocking",
                    "title": "unguarded index access on an empty list",
                    "description": "scripts/x.py:12",
                }],
                "residual_risk": "reviewed the diff only",
            })}}),
    ]) + "\n"

    gate_artifacts.materialize_artifacts(
        gate="codex_gate", pr_number=1453, pr_id="",
        stdout=stream,
        request_payload=_request(report_path=str(reports / "codex-1453.md")),
        duration_seconds=120.0,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )

    record = _record(results)
    assert record.get("blocking_findings"), (
        "the fixture produced no blocking finding, so this test would have "
        "asserted nothing — the shape the parser accepts is a JSON verdict in "
        "the agent_message text, not a [BLOCKING] prose marker"
    )
    assert record.get("failure_reason"), (
        "a record that failed on its findings carries no canonical reason"
    )
    assert "blocking finding" in record["failure_reason"]


def test_residual_risk_alone_is_not_lifted_into_the_canonical_field():
    """The OI-1452 case, corrected: the provider cause lives ONLY in
    ``residual_risk``, and that is exactly why it must NOT be lifted.

    A field named "remaining risk" is where a 403 ends up, and it is the last
    place a generic reader looks. But a derivation that lifted it would also
    lift every frontmatter-derived sentence that merely resembles a cause
    (measured 2026-08-30: 648 of 739 non-pass records carry ``residual_risk``
    prose). The real cause reaches the canonical field by a different route:
    the lane-log lift (OI-1452) stamps ``failure_reason`` directly in the
    report frontmatter when it found a real marker, so the ``existing``
    shortcut wins before the source chain is ever walked.

    A record that reaches the chain with only ``residual_risk`` populated is a
    record whose cause was NOT established. The honest value is "", the third
    state -- not a best-effort summary borrowed from a field that sounds like
    a cause.
    """
    payload = {
        "gate": "glm_gate", "pr_id": "1694", "status": "unavailable",
        "residual_risk": "OpenRouter returned 403: key expired",
        "blocking_findings": [], "advisory_findings": [],
    }

    assert gate_recorder.derive_failure_reason(payload) == ""


def test_a_pre_stamped_cause_wins_over_the_source_chain():
    """The route the real 403 cause actually takes: stamped upstream, not
    derived. A lane that established the cause stamps ``failure_reason``
    directly; the derivation honors it via the ``existing`` shortcut and never
    re-derives from the surrounding fields."""
    payload = {
        "gate": "glm_gate", "pr_id": "1694", "status": "unavailable",
        "failure_reason": "access_terminated_error: kimi quota exhausted",
        "residual_risk": "OpenRouter returned 403: key expired",
        "reason": "dispatch_error",
        "blocking_findings": [], "advisory_findings": [],
    }

    assert gate_recorder.derive_failure_reason(payload) == (
        "access_terminated_error: kimi quota exhausted"
    )


# --------------------------------------------------------------------------
# The guard, in THREE buckets -- the property, not the six call sites.
# A two-bucket guard ("pass empty, non-pass filled") is the trap this PR
# repairs: it books the third state as a violation and forces placeholders
# back into the field. Three buckets:
#   1. pass                          -> failure_reason absent (no failure)
#   2. non-pass WITH an established cause -> failure_reason carries the cause
#   3. non-pass, cause NOT established    -> failure_reason empty/absent,
#      and that is an allowed state, not a defect
# --------------------------------------------------------------------------

def test_bucket_pass_leaves_the_field_absent(tmp_path):
    """Bucket 1: a pass has no failure to explain. The field stays absent so
    its presence on a record remains a real signal."""
    target = tmp_path / "pr-1-some_gate.json"
    gate_recorder._write_result_atomic(target, {
        "status": "pass", "contract_hash": "abc", "report_path": "/tmp/r.md",
        "blocking_findings": [], "advisory_findings": [],
    })
    assert "failure_reason" not in json.loads(target.read_text(encoding="utf-8"))


@pytest.mark.parametrize("payload,expected", [
    ({"status": "unavailable", "reason_detail": "quota exhausted"}, "quota exhausted"),
    ({"status": "not_executable", "reason_detail": "codex binary not found in PATH"},
     "codex binary not found in PATH"),
])
def test_bucket_non_pass_with_established_cause_carries_it(tmp_path, payload, expected):
    """Bucket 2: a non-pass whose cause was filed under ``reason_detail``
    carries that cause in the canonical field.

    Driven through the write primitive itself rather than through one gate,
    because the claim is about every record that reaches disk. ``reason`` and
    ``summary`` are deliberately absent here -- a record that carries its
    cause only as a classification or a summary is NOT in this bucket.
    """
    target = tmp_path / "pr-2-some_gate.json"
    gate_recorder._write_result_atomic(target, dict(payload))
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written.get("failure_reason") == expected, (
        f"a {payload['status']!r} record with an established cause must carry "
        f"it: {written}"
    )


@pytest.mark.parametrize("payload", [
    {"status": "not_executable", "reason": "provider_disabled"},
    {"status": "fail", "summary": "gate found problems"},
    {"status": "unavailable", "residual_risk": "provider-side outage, not a review outcome"},
    {"status": "failed"},
])
def test_bucket_non_pass_with_no_established_cause_is_empty_not_invented(tmp_path, payload):
    """Bucket 3: a non-pass whose cause was NOT established. ``reason`` is a
    classification, ``summary``/``residual_risk`` are prose, and a bare
    ``status`` is neither. None of these is a cause.

    The old derivation lifted all of them (``reason`` won, then
    ``residual_risk``/``summary``, then an invented ``f"status: {status}"``).
    That made the field look full while explaining nothing. The honest value
    is empty -- an allowed third state, not a violation. A guard that treated
    this as a violation would force every gate to fill the field with
    something to pass the check, and the field would stop meaning anything.
    """
    target = tmp_path / "pr-3-some_gate.json"
    gate_recorder._write_result_atomic(target, dict(payload))
    written = json.loads(target.read_text(encoding="utf-8"))
    fr = written.get("failure_reason", "")
    assert fr == "", (
        f"a {payload['status']!r} record with no established cause must NOT "
        f"have a cause invented into the canonical field, got: {fr!r}"
    )


def test_a_lane_that_already_filled_it_is_left_alone(tmp_path):
    """kimi_gate and glm_gate compute this themselves (OI-1415). Their text wins."""
    target = tmp_path / "pr-3-kimi_gate.json"
    gate_recorder._write_result_atomic(target, {
        "status": "unavailable",
        "failure_reason": "access_terminated_error: kimi quota exhausted",
        "reason_detail": "dispatch_error",
    })

    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["failure_reason"] == "access_terminated_error: kimi quota exhausted"


def test_the_caller_holds_the_record_that_is_on_disk(dirs):
    """A caller comparing its own dict to the file must not find a field it
    never set and cannot account for."""
    requests, results, state, _reports = dirs

    returned = record_not_executable(
        gate="codex_gate", pr_number=1453, pr_id="",
        reason="provider_disabled", reason_detail="VNX_CODEX_HEADLESS_ENABLED is 0",
        request_payload=_request(),
        requests_dir=requests, results_dir=results, state_dir=state,
    )

    assert returned["failure_reason"] == _record(results)["failure_reason"]
