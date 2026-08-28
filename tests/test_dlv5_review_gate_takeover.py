"""Review-gate takeover on toestand 1 (dispatch 20260823-beta2-c-overname-en-rode-test).

Plan: claudedocs/plans/review-gate-provider-agnostic.md, deliverable 5 (overname
met verplichte, niet-lege reden in het canonieke veld) and deliverable 8 (rode
test: a failed kimi seat falls over to glm and does not stay undecided).

RED-on-main proof (recorded before the fix landed, dispatch report has the
full command + failure line):

    pytest tests/test_dlv5_review_gate_takeover.py::TestKimiTakeoverToGlm::test_kimi_lane_exhausted_takes_over_to_glm -x

    AssertionError: expected a glm_gate request record for the takeover seat
    assert False
     +  where False = exists()

This asserts on a GEDRAG (a written record's shape), not on an
ImportError/missing symbol -- ``_dispatch_review_seat``/``_REVIEW_GATE_
TAKEOVER_ORDER`` did not exist on main, so the seat was dispatched straight
to ``_dispatch_one_review("kimi_gate", ...)`` and no ``glm_gate`` request
record was ever written for PR 501.

Three states from the plan (governance_emit._classify_lane_log_text is the
canonical classifier, deliverable 0):
  1. lane_exhausted  -- exhaustion with body (payment/quota code). TAKE OVER.
  2. unreadable_verdict -- a response existed, verdict block corrupted. Abstain.
  3. no_response     -- bare non-zero exit, no body. Abstain.

Control cases (must keep passing, or this test cannot prove anything):
  1. A kimi seat that just succeeds is NOT taken over.
  2. A kimi seat with an unreadable verdict (state 2) is NOT taken over.
  3. Without an available fallback, the seat stays unavailable/not_executable
     and no pass is ever booked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _write_result(results_dir: Path, pr_number: int, gate: str, payload: dict) -> Path:
    path = results_dir / f"pr-{pr_number}-{gate}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# A real kimi_gate lane-exhaustion body (toestand 1), shaped exactly like
# kimi_gate.py's own "dispatch_error" record -- residual_risk carries the raw
# 403 body, never a parser-side "msg" field.
_KIMI_LANE_EXHAUSTED_RESULT = {
    "gate": "kimi_gate",
    "pr_id": "501",
    "pr_number": 501,
    "test_run": False,
    "status": "unavailable",
    "reason": "dispatch_error",
    "duration_seconds": 4.2,
    "summary": "kimi gate: UNAVAILABLE (provider outage/no verdict — NOT a review fail)",
    "contract_hash": "",
    "report_path": "",
    "provider": "kimi",
    "model": "kimi-k3",
    "dispatch_id": "kimi-gate-pr501-1755852660",
    "blocking_findings": [],
    "advisory_findings": [],
    "required_reruns": [],
    "residual_risk": (
        "governed kimi dispatch failed: Error code: 403 - "
        "{'error': {'message': 'Your account has been suspended, please "
        "contact us via api-feedback@moonshot.cn', "
        "'type': 'access_terminated_error'}}"
    ),
    "recorded_at": "2026-08-22T09:51:00Z",
    "branch": "fix/takeover-test",
    "commit_sha": "a" * 40,
}

# toestand 2 -- a real response came back, the verdict block just didn't parse.
_KIMI_UNREADABLE_VERDICT_RESULT = {
    "gate": "kimi_gate",
    "pr_id": "502",
    "pr_number": 502,
    "test_run": False,
    "status": "unavailable",
    "reason": "parse_error",
    "duration_seconds": 61.0,
    "summary": (
        "kimi gate: UNAVAILABLE (parse_error — kimi returned a 812-char "
        "report, but it contained no readable verdict block — NOT a review fail)"
    ),
    "contract_hash": "",
    "report_path": "",
    "provider": "kimi",
    "model": "kimi-k3",
    "dispatch_id": "kimi-gate-pr502-1755852700",
    "blocking_findings": [],
    "advisory_findings": [],
    "required_reruns": [],
    "residual_risk": (
        "kimi returned a 812-char report, but it contained no readable "
        "```json``` verdict block (parse miss — kimi did respond)"
    ),
    "recorded_at": "2026-08-22T09:52:00Z",
    "branch": "fix/takeover-test",
    "commit_sha": "b" * 40,
}


class TestKimiTakeoverToGlm:
    """Deliverable 8: a failed kimi seat (toestand 1) falls over to glm and
    is not left undecided."""

    def test_kimi_lane_exhausted_takes_over_to_glm(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 501

        _write_result(manager_env["results_dir"], pr_number, "kimi_gate", _KIMI_LANE_EXHAUSTED_RESULT)

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=pr_number,
                branch="fix/takeover-test",
                review_stack=["kimi_gate"],
                risk_class="medium",
                changed_files=["scripts/lib/gate_request_handler.py"],
                mode="per_pr",
                dispatch_id="dlv5-takeover-test",
            )

        # The seat is filled by glm_gate, not left as an undecided kimi_gate.
        assert len(result["requested"]) == 1
        assert result["requested"][0]["gate"] == "glm_gate"

        glm_request_file = manager_env["requests_dir"] / f"pr-{pr_number}-glm_gate.json"
        assert glm_request_file.exists(), "expected a glm_gate request record for the takeover seat"
        record = json.loads(glm_request_file.read_text(encoding="utf-8"))

        # Split asserts (no compound judgement): "glm heeft overgenomen" and
        # "de reden is niet leeg" are two independent claims and must fail
        # independently if either half regresses.
        assert record.get("takeover_from") == "kimi_gate", (
            f"expected glm_gate's record to name kimi_gate as the takeover source, got: {record.get('takeover_from')!r}"
        )
        assert record.get("failure_reason", "").strip() != "", (
            "failure_reason must be a non-empty, mandatory field on a takeover record — "
            "an empty reason here is a silent refusal, not a documented overname"
        )
        assert "kimi_gate" in record["failure_reason"], (
            f"failure_reason must name the failed seat, got: {record['failure_reason']!r}"
        )
        assert "access_terminated_error" in record["failure_reason"] or "403" in record["failure_reason"], (
            "failure_reason must carry the kimi outage's own detail, not a generic placeholder: "
            f"{record['failure_reason']!r}"
        )

    def test_kimi_never_taken_over_when_it_just_succeeds(self, manager_env, monkeypatch):
        """Control case 1: a kimi seat with no recorded failure is dispatched
        normally — never routed to glm."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 601
        # No pre-existing result file — this is the seat's first attempt.

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=pr_number,
                branch="fix/control-success",
                review_stack=["kimi_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="dlv5-control-success",
            )

        assert result["requested"][0]["gate"] == "kimi_gate"
        assert "takeover" not in result["requested"][0]

        glm_request_file = manager_env["requests_dir"] / f"pr-{pr_number}-glm_gate.json"
        assert not glm_request_file.exists(), "a healthy kimi seat must never spawn a glm_gate takeover request"

    def test_kimi_unreadable_verdict_never_taken_over(self, manager_env, monkeypatch):
        """Control case 2: toestand 2 (a response existed, verdict block
        corrupted) abstains — it must never be treated as toestand 1."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 502

        _write_result(manager_env["results_dir"], pr_number, "kimi_gate", _KIMI_UNREADABLE_VERDICT_RESULT)

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=pr_number,
                branch="fix/control-unreadable",
                review_stack=["kimi_gate"],
                risk_class="medium",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="dlv5-control-unreadable",
            )

        assert result["requested"][0]["gate"] == "kimi_gate", (
            "an unreadable-verdict (toestand 2) seat must abstain, never take over to glm_gate"
        )
        assert "takeover" not in result["requested"][0]

        glm_request_file = manager_env["requests_dir"] / f"pr-{pr_number}-glm_gate.json"
        assert not glm_request_file.exists(), "toestand 2 must never spawn a glm_gate takeover request"

    def test_no_available_fallback_stays_unavailable_never_a_pass(self, manager_env, monkeypatch):
        """Control case 3: when the configured fallback is itself unavailable,
        the seat stays undecided — never silently booked as a pass."""
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 503

        _write_result(manager_env["results_dir"], pr_number, "kimi_gate", {
            **_KIMI_LANE_EXHAUSTED_RESULT, "pr_id": "503", "pr_number": 503,
        })
        # Force the configured fallback (glm_gate) unavailable too, so the
        # takeover attempt itself fails.
        monkeypatch.setattr(manager, "_glm_gate_available", lambda: False)

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=pr_number,
                branch="fix/control-no-fallback",
                review_stack=["kimi_gate"],
                risk_class="medium",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="dlv5-control-no-fallback",
            )

        seat = result["requested"][0]
        assert seat["gate"] == "glm_gate", "the takeover must still be attempted"
        assert seat["status"] != "pass", (
            "an unavailable fallback must never resolve the seat to a pass"
        )
        assert seat["status"] == "not_executable"

        # And the ON-DISK result record (not just the in-memory payload) must
        # show the same — read back from disk, never asserted from code alone.
        glm_result_file = manager_env["results_dir"] / f"pr-{pr_number}-glm_gate.json"
        assert glm_result_file.exists()
        recorded = json.loads(glm_result_file.read_text(encoding="utf-8"))
        assert recorded["status"] != "pass"
        assert recorded.get("failure_reason", "").strip() != ""


class TestMarkGateUnavailableStampsCanonicalFailureReason:
    """Bevinding 1 (fix-forward on #1675): a seat that REFUSES on its very
    first round (no prior recorded result yet, so ``_dispatch_review_seat``
    never reaches the takeover branch at all) writes its reason into the
    lane-own ``reason``/``reason_detail`` fields via ``_mark_gate_unavailable``
    but never into the canonical ``failure_reason`` field -- precisely the
    OI-1415 defect #1666 fixed for phantom_guard/pr_enforcement, left
    unaddressed here.

    Measured probe (23-08, kimi CLI absent from PATH, first request for a
    fresh PR/state-dir pair -- no pre-existing result file, so no takeover
    branch is even entered):

        status          'not_executable'
        reason          'provider_not_installed'
        reason_detail   'kimi_gate.py binary not found in PATH'
        failure_reason  ABSENT

    OI-1490 (28-08) changed the reason VALUE without touching this test's
    subject. That probe's label was itself the bug: 'kimi_gate.py' is not a
    binary and never was, so the PATH lookup could only fail and the seat
    could only be booked 'provider_not_installed' -- an environment complaint
    for a routing fact. ``_classify_unavailable`` now resolves the provider
    from ``gate_recorder.GATE_PROVIDERS``; a script-runner gate reaching it
    means its own availability check (the runner FILE, which this test forces
    to False) already said no, so the honest label is 'gate_runner_missing'.
    The same probe today reads:

        status          'not_executable'
        reason          'gate_runner_missing'
        reason_detail   'scripts/kimi_gate.py does not exist -- ...'
        failure_reason  == reason_detail   (what this test is actually about)

    What is asserted below is unchanged in substance: a refused seat stamps
    the canonical ``failure_reason``, with the SAME text as the lane-own
    ``reason_detail``, in the request record AND the result record.

    RED-on-branch proof (recorded before the fix landed):

        pytest tests/test_dlv5_review_gate_takeover.py::TestMarkGateUnavailableStampsCanonicalFailureReason::test_kimi_not_executable_first_round_stamps_failure_reason -x

        AssertionError: failure_reason must be present on a refused seat's request record, exactly like reason_detail (OI-1415) -- got None
        assert None is not None
    """

    def test_kimi_not_executable_first_round_stamps_failure_reason(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 701
        # No pre-existing result file -- this is the seat's first attempt, so
        # `_dispatch_review_seat` never reaches the takeover branch; the
        # refusal comes straight from `_mark_gate_unavailable` inside
        # `_request_kimi`.
        monkeypatch.setattr(manager, "_kimi_gate_available", lambda: False)

        with patch("governance_receipts.emit_governance_receipt"):
            result = manager.request_reviews(
                pr_number=pr_number,
                branch="fix/mark-unavailable-failure-reason",
                review_stack=["kimi_gate"],
                risk_class="medium",
                changed_files=["scripts/lib/gate_request_handler.py"],
                mode="per_pr",
                dispatch_id="dlv5-mark-unavailable-test",
            )

        seat = result["requested"][0]
        assert seat["gate"] == "kimi_gate"
        assert seat["status"] == "not_executable"
        assert seat["reason"] == "gate_runner_missing", (
            "OI-1490: a script-runner gate whose runner file is absent is not a "
            "missing PATH binary; see test_oi1490_gate_provider_registry"
        )

        assert seat.get("failure_reason") is not None, (
            "failure_reason must be present on a refused seat's request record, "
            f"exactly like reason_detail (OI-1415) -- got {seat.get('failure_reason')!r}"
        )
        assert seat["failure_reason"] == seat["reason_detail"], (
            "failure_reason must carry the SAME text as the lane-own reason_detail, "
            f"not a second formulation: {seat['failure_reason']!r} != {seat['reason_detail']!r}"
        )

        request_file = manager_env["requests_dir"] / f"pr-{pr_number}-kimi_gate.json"
        assert request_file.exists()
        on_disk_request = json.loads(request_file.read_text(encoding="utf-8"))
        assert on_disk_request.get("failure_reason") == seat["reason_detail"], (
            "failure_reason must be persisted on disk, not only in the in-memory payload"
        )

        result_file = manager_env["results_dir"] / f"pr-{pr_number}-kimi_gate.json"
        assert result_file.exists()
        on_disk_result = json.loads(result_file.read_text(encoding="utf-8"))
        assert on_disk_result.get("failure_reason") == seat["reason_detail"], (
            "failure_reason must land in the result record too, not only the request record"
        )


class TestStampTakeoverAnnotationsAtomicWrites:
    """Bevinding 2 (fix-forward on #1675): ``_stamp_takeover_annotations``
    must never leave a torn (partially-written) evidence file on disk.

    These are the request/result records that PROVE a takeover happened. A
    half-written record passes a bare existence check and only fails on the
    content a reader trusts -- worse than no record at all, because it
    surfaces exactly when the record is needed. Both writes must go through
    the tempfile-in-same-dir + ``os.replace`` pattern (``atomic_write_json``,
    already used elsewhere in the repo -- not a second hand-rolled variant),
    never a bare ``write_text``.
    """

    def test_crash_mid_write_leaves_original_request_record_intact(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 801
        gate = "glm_gate"

        original_payload = {"gate": gate, "status": "requested", "pr_number": pr_number}
        request_file = manager_env["requests_dir"] / f"pr-{pr_number}-{gate}.json"
        request_file.write_text(json.dumps(original_payload, indent=2), encoding="utf-8")

        import atomic_io

        def _boom(*args, **kwargs):
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(atomic_io.os, "replace", _boom)

        with pytest.raises(OSError):
            manager._stamp_takeover_annotations(
                dict(original_payload),
                pr_number=pr_number,
                takeover_from="kimi_gate",
                failure_reason="kimi_gate unavailable (dispatch_error): 403 -- glm_gate substituted as reader",
                takeover_reason="dispatch_error",
                takeover_source_status="unavailable",
            )

        # The ORIGINAL record must survive untouched -- not a half-written
        # file, not an empty file, not the tmp file left in its place.
        assert request_file.exists()
        recovered = json.loads(request_file.read_text(encoding="utf-8"))
        assert recovered == original_payload, (
            "a crash mid-write must never leave a torn or partially-updated evidence file"
        )
        # No leaked temp file next to it -- atomic_write_json cleans up on failure.
        leaked_tmp = list(manager_env["requests_dir"].glob("*.tmp"))
        assert leaked_tmp == [], f"atomic write must clean up its temp file on failure, found: {leaked_tmp}"

    def test_crash_mid_write_leaves_original_result_record_intact(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        manager = _make_manager()
        pr_number = 802
        gate = "glm_gate"

        # No request file for this PR -- only the result-record write path
        # is exercised.
        original_result = {"gate": gate, "status": "pass", "pr_number": pr_number, "summary": "ok"}
        result_file = manager_env["results_dir"] / f"pr-{pr_number}-{gate}.json"
        result_file.write_text(json.dumps(original_result, indent=2), encoding="utf-8")

        import atomic_io

        def _boom(*args, **kwargs):
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(atomic_io.os, "replace", _boom)

        with pytest.raises(OSError):
            manager._stamp_takeover_annotations(
                {"gate": gate, "status": "pass", "pr_number": pr_number},
                pr_number=pr_number,
                takeover_from="kimi_gate",
                failure_reason="kimi_gate unavailable (dispatch_error): 403 -- glm_gate substituted as reader",
                takeover_reason="dispatch_error",
                takeover_source_status="unavailable",
            )

        assert result_file.exists()
        recovered = json.loads(result_file.read_text(encoding="utf-8"))
        assert recovered == original_result, (
            "a crash mid-write must never leave a torn or partially-updated result record"
        )
        leaked_tmp = list(manager_env["results_dir"].glob("*.tmp"))
        assert leaked_tmp == [], f"atomic write must clean up its temp file on failure, found: {leaked_tmp}"
