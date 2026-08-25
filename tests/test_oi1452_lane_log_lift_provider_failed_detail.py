"""tests/test_oi1452_lane_log_lift_provider_failed_detail.py — the request-time
review-gate takeover (DLv5+8, PR #1675/OI-1436) never fires on a REAL
production kimi_gate/glm_gate outage, because the reason never reaches the
result record (OI-1452).

Measured 23-08 on a COPY of the central store against the real kimi-gate
outage on PR #1677 (``pr-1677-kimi_gate.json`` /
``logs/conversations/kimi-gate-pr1677-1787477677.log``, see the dispatch
report for the full before/after transcript):

    _classify_review_seat_failure(real record)  -> 'no_response'
    takeover only fires on                       -> 'lane_exhausted'

The real record's ``residual_risk`` carries only the frontmatter-derived
detail (``kimi's own report frontmatter stamps this run as failed
(exit_code=1, token_usage.output=0) ...``) — never the raw 403/quota body,
which sits in the per-dispatch raw lane log instead. The takeover
classifier (``gate_request_handler._classify_review_seat_failure``, PR
#1675, not yet merged onto this branch) scans ``residual_risk`` /
``reason_detail`` / ``summary`` with ``governance_emit._classify_lane_log_text``
for a billing/quota exhaustion marker; with nothing but the generic
exit_code/token_usage sentence to scan, it can never see one.

kimi_gate.py / glm_gate.py now lift the real reason off the raw lane log
(``logs/conversations/<dispatch_id>.log``) into ``provider_failed_detail``
(and therefore ``residual_risk``) using the SAME classifier OI-1433 built
for exactly this purpose (``governance_emit._classify_lane_log_text``) —
never a second hand-rolled marker scan.

RED-on-main proof (recorded before the fix landed — see the dispatch report
for the exact command + failure line):

    pytest tests/test_oi1452_lane_log_lift_provider_failed_detail.py::test_kimi_real_record_format_lifts_lane_log_reason_into_residual_risk -x

    AssertionError: expected the lifted lane-log reason to land in residual_risk
    assert False

Companion fix (also in this dispatch, in ``governance_emit.py``): the
existing ``_classify_lane_log_text``/``_bounded_snippet`` always windowed the
lifted reason from the START of the raw text. That is fine for a short
single-shot error body (the original OI-1433 fixture), but the real PR #1677
lane log is a 52KB multi-turn conversation transcript with ~2KB of tool-call
chatter BEFORE the 403 body — a prefix-only snippet silently dropped the
exhaustion marker it was supposed to carry, which broke the round-trip
through the takeover classifier even with kimi_gate.py's lift wired up. The
snippet now windows around the earliest-occurring marker's actual position
in the text instead of always starting at offset 0 — see
``tests/test_oi1433_lane_log_lift.py`` for the pre-existing suite this must
not regress (verified green after this change).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
for _p in (str(SCRIPTS_DIR), str(SCRIPTS_DIR / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kimi_gate
import glm_gate
from governance_emit import _classify_lane_log_text

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lane_logs"
_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"

# Sanity check on the shared fixtures this file leans on (also used by
# tests/test_oi1433_lane_log_lift.py): one carries a real billing/quota
# exhaustion marker, the other carries content with no such marker at all.
assert _classify_lane_log_text((_FIXTURES / "kimi_403_quota.log").read_text())[0] == "lane_exhausted"
assert _classify_lane_log_text((_FIXTURES / "content_no_verdict.log").read_text())[0] == "unreadable_verdict"


def _report_with_frontmatter(
    provider: str,
    model: str,
    body: str,
    *,
    exit_code: int,
    output_tokens: int = 0,
    token_usage_measured: bool = False,
) -> str:
    """The ECHTE recordformaat (OI-1452): a governed report whose OWN
    frontmatter stamps the underlying run as failed (exit_code != 0), but
    whose body is ordinary — possibly truncated — prose with NO verdict
    block and NO 403/quota text at all. This is the exact shape measured on
    the real PR #1677 kimi_gate report on disk: 36 lines of real model
    output ("The dispatch prompt doesn't inline the exact report path...."),
    zero mention of the outage that actually killed the run. A fixture that
    embeds the 403 directly in the report body (the pre-existing takeover
    test's fixture) proves the takeover logic, not this coupling.
    """
    return (
        "---\n"
        "schema_version: 1\n"
        f"provider: {provider}\n"
        f"model: {model}\n"
        "duration_seconds: 258.33\n"
        f"exit_code: {exit_code}\n"
        "token_usage:\n"
        "  input: 0\n"
        f"  output: {output_tokens}\n"
        "  cache_read: 0\n"
        f"token_usage_measured: {'true' if token_usage_measured else 'false'}\n"
        "---\n"
        "\n"
        f"{body}"
    )


# Real body text, same shape as the actual PR #1677 kimi_gate report on
# disk: genuine (truncated) model prose, no verdict fence, no outage text.
_REAL_SHAPE_BODY = (
    "The dispatch prompt doesn't inline the exact report path, but the panel "
    "convention (`plan_gate_panel.py:976`) is `$VNX_DATA_DIR/unified_reports/"
    "{dispatch_id}.md`. Now let me verify the diff against the actual "
    "worktree files and check consumers for regressions.\n"
)

_KIMI_REAL_SHAPE_REPORT = _report_with_frontmatter(
    "kimi", "kimi-k3", _REAL_SHAPE_BODY, exit_code=1, output_tokens=0,
)
_GLM_REAL_SHAPE_REPORT = _report_with_frontmatter(
    "glm-harness", "glm-5.2", _REAL_SHAPE_BODY, exit_code=1, output_tokens=0,
)

_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)


def _fake_dispatcher_factory(data_dir: Path, report_text: str, lane_log_text: "str | None"):
    """Build a ``_make_default_dispatcher``-shaped double that writes the
    unified report AND (when given) the per-dispatch raw lane log, keyed on
    the actual ``dispatch_id`` the caller passes in — exactly like the real
    governed lane: ``provider_dispatch`` tees the raw lane log to
    ``logs/conversations/<dispatch_id>.log`` as a side effect of the run,
    and the report is already on disk by the time the dispatcher call
    returns the text kimi_gate.py/glm_gate.py go on to read.
    """
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            reports_dir = data_dir / "unified_reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / f"{dispatch_id}.md").write_text(report_text, encoding="utf-8")
            if lane_log_text is not None:
                log_dir = data_dir / "logs" / "conversations"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / f"{dispatch_id}.log").write_text(lane_log_text, encoding="utf-8")
            return report_text
        return _dispatch
    return _make


def _run_gate_for_real_pr(gate_module, tmp_path, monkeypatch, report_text, lane_log_text, *, pr="4242"):
    """Run ``gate_module.main()`` against a non-offline PR (no --diff-file,
    so ``test_run`` stays False, matching a real governed run): a stubbed
    diff source, a dispatcher double that writes the report + lane log like
    the real lane does, and stubbed gh-identity lookups (no real ``gh``
    subprocess calls). Same convention as
    tests/test_dlv2_kimi_gate_evidence.py / tests/test_dlv1_glm_gate.py.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setattr(gate_module, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        gate_module, "_make_default_dispatcher",
        _fake_dispatcher_factory(data_dir, report_text, lane_log_text),
    )
    monkeypatch.setattr(gate_module, "get_pr_head_branch", lambda pr_number: "feature/oi1452-test")
    monkeypatch.setattr(gate_module, "get_pr_head_sha", lambda pr_number: "cafedeadbeef")

    rc = gate_module.main(["--pr", pr, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-{gate_module.__name__}.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    return rc, record, data_dir


# ---------------------------------------------------------------------------
# 1. The lift: ECHTE recordformaat (frontmatter reason, no 403 in the
#    report) + a lane log carrying the real exhaustion marker -> the
#    reason must land in residual_risk. RED on pre-fix kimi_gate.py/
#    glm_gate.py, GREEN after (see dispatch report for the recorded
#    before/after pytest run).
# ---------------------------------------------------------------------------


def test_kimi_real_record_format_lifts_lane_log_reason_into_residual_risk(tmp_path, monkeypatch):
    lane_log_text = (_FIXTURES / "kimi_403_quota.log").read_text(encoding="utf-8")
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, lane_log_text,
    )
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    # the frontmatter-derived detail must stay — this fix ADDS the reason,
    # it does not replace the existing (correct) exit_code/token_usage detail
    assert "exit_code=1" in record["residual_risk"]
    assert "access_terminated_error" in record["residual_risk"], (
        "expected the lifted lane-log reason to land in residual_risk"
    )


def test_glm_real_record_format_lifts_lane_log_reason_into_residual_risk(tmp_path, monkeypatch):
    lane_log_text = (_FIXTURES / "kimi_403_quota.log").read_text(encoding="utf-8")
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _GLM_REAL_SHAPE_REPORT, lane_log_text,
    )
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    assert "exit_code=1" in record["residual_risk"]
    assert "access_terminated_error" in record["residual_risk"], (
        "expected the lifted lane-log reason to land in residual_risk"
    )


# ---------------------------------------------------------------------------
# 2. The coupling: the SAME lift, re-classified by the request-time takeover
#    (PR #1675/OI-1436, gate_request_handler._classify_review_seat_failure)
#    flips from 'no_response' to 'lane_exhausted'. Split out from test 1 —
#    asserts on the classifier's own returned value, not on residual_risk
#    text. Skips gracefully until PR #1675 merges onto this branch (its
#    classifier does not exist here yet); it activates automatically once it
#    does. See the dispatch report's "Meet het op het ECHTE record" section
#    for a real (ad hoc, uncommitted) run of this exact coupling against the
#    actual PR #1677 production record.
# ---------------------------------------------------------------------------


def test_lifted_reason_flips_takeover_classification_to_lane_exhausted(tmp_path, monkeypatch):
    gate_request_handler = pytest.importorskip(
        "gate_request_handler",
        reason="review-gate takeover classifier lands in PR #1675 (OI-1436); "
        "not yet merged onto this branch",
    )
    classify = getattr(gate_request_handler.GateRequestHandlerMixin, "_classify_review_seat_failure", None)
    if classify is None:
        pytest.skip(
            "_classify_review_seat_failure not yet on GateRequestHandlerMixin (PR #1675 pending)"
        )

    lane_log_text = (_FIXTURES / "kimi_403_quota.log").read_text(encoding="utf-8")
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, lane_log_text,
    )
    assert record["status"] == "unavailable" and record["reason"] == "dispatch_error"

    # BEFORE: the frontmatter-only detail, exactly as kimi_gate.py produced
    # it before this dispatch's fix (no lane-log lift at all).
    before_record = dict(record)
    before_record["residual_risk"] = (
        "kimi's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )
    # _classify_review_seat_failure never touches self — safe to call unbound.
    assert classify(None, before_record) == "no_response"

    # AFTER: the actual record this dispatch's fix produced.
    assert classify(None, record) == "lane_exhausted"


# ---------------------------------------------------------------------------
# 3. Control cases — must keep passing, or the fix proves nothing.
# ---------------------------------------------------------------------------


def test_kimi_no_lane_log_present_keeps_current_behavior_no_invented_reason(tmp_path, monkeypatch):
    """The frontmatter says the run failed; no lane log exists at all
    (``_read_lane_log_text`` -> None). The gate must not crash and must not
    invent a reason — residual_risk stays exactly the frontmatter-only
    detail from before this fix."""
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, lane_log_text=None,
    )
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    assert record["residual_risk"] == (
        "kimi's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )
    assert "lane log" not in record["residual_risk"]


def test_glm_no_lane_log_present_keeps_current_behavior_no_invented_reason(tmp_path, monkeypatch):
    """Mirrors the kimi case, but is also the real production shape: the
    glm lane does not always write a per-dispatch log (measured: no log
    exists for the PR #1681 glm_gate outage either)."""
    rc, record, _data_dir = _run_gate_for_real_pr(
        glm_gate, tmp_path, monkeypatch, _GLM_REAL_SHAPE_REPORT, lane_log_text=None,
    )
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    assert record["residual_risk"] == (
        "glm's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )
    assert "lane log" not in record["residual_risk"]


def test_kimi_lane_log_without_exhaustion_marker_stays_unavailable_not_lane_exhausted(tmp_path, monkeypatch):
    """A lane log exists and has content, but no billing/quota exhaustion
    marker (``_classify_lane_log_text`` -> ``unreadable_verdict``). This
    must NOT be lifted into residual_risk — toestand 2/3 behavior stays
    unchanged, never upgraded to lane_exhausted on a log that says nothing
    about the cause."""
    lane_log_text = (_FIXTURES / "content_no_verdict.log").read_text(encoding="utf-8")
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _KIMI_REAL_SHAPE_REPORT, lane_log_text,
    )
    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "dispatch_error"
    assert record["residual_risk"] == (
        "kimi's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=0) — provider-side outage, not a "
        "review outcome"
    )
    assert "lane log" not in record["residual_risk"]


def test_kimi_successful_run_never_reads_lane_log(tmp_path, monkeypatch):
    """A real parsed verdict (pass) must never trigger a lane-log read, even
    when a quota-shaped log happens to exist for the same dispatch_id — the
    frontmatter/lane-log path is only ever consulted once verdict
    extraction has already failed."""
    lane_log_text = (_FIXTURES / "kimi_403_quota.log").read_text(encoding="utf-8")
    rc, record, _data_dir = _run_gate_for_real_pr(
        kimi_gate, tmp_path, monkeypatch, _REAL_PASS_REPORT, lane_log_text,
    )
    assert rc == 0
    assert record["status"] == "pass"
    assert "access_terminated_error" not in json.dumps(record)
