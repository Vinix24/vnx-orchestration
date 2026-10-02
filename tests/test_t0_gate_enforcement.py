#!/usr/bin/env python3
"""scripts/t0_gate_enforcement.sh verifies the gates that ran, not the raw stack.

The wrapper used to verify request/result artifacts for every seat of the
requested ``--review-stack``. review_gate_manager can legally fill a seat with a
different gate: with codex_gate unavailable the takeover chain has kimi_gate read
in its place, and the later kimi seat is then not requested a second time. The
wrapper demanded an artifact for a gate that was intentionally never asked.

These tests cover the two halves:

  * ``gate_enforcement_verify`` (the seat resolution), called directly.
  * the wrapper itself, run through bash against a stub ``review_gate_manager.py``
    that prints a canned request-and-execute report. No real gate is spawned:
    the stub is the only "manager" in the temp project.

They also pin the commit binding: a result whose ``commit_sha`` is not the head
of this run must not verify (fix-forward after a push), a result for the head
must, and an absent sha on either side stays ``unknown`` -- never a mismatch.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import gate_enforcement_verify as verify
from gate_enforcement_verify import (
    VerificationError,
    extract_report,
    parse_request_args,
    verify_report,
)

DEFAULT_STACK = ["codex_gate", "kimi_gate"]

# Two full-length shas, so the abbreviated forms in messages are unambiguous.
NEW_HEAD = "a" * 40
OLD_HEAD = "b" * 40


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

def _entry(
    gate: str,
    *,
    request_status: str = "requested",
    takeover_path: Optional[List[str]] = None,
    head_sha: Optional[str] = None,
    result_commit_sha: Optional[str] = None,
    sha_binding: Optional[str] = None,
) -> Dict[str, Any]:
    detail: Dict[str, Any] = {"gate": gate}
    if takeover_path is not None:
        detail["takeover_path"] = [{"gate": hop, "reason": "quota", "status": "unavailable"} for hop in takeover_path]
    entry = {
        "gate": gate,
        "request_status": request_status,
        "execution_status": "completed",
        "passed": True,
        "detail": detail,
    }
    if head_sha is not None:
        entry["head_sha"] = head_sha
    if result_commit_sha is not None:
        entry["result_commit_sha"] = result_commit_sha
    if sha_binding is not None:
        entry["sha_binding"] = sha_binding
    return entry


def _report(*entries: Dict[str, Any]) -> Dict[str, Any]:
    return {"pr_number": 7, "branch": "feat/x", "gates": list(entries), "has_required_failure": False}


def _write_artifacts(
    state_dir: Path,
    gate: str,
    *,
    pr: int = 7,
    request: bool = True,
    result: bool = True,
    status: str = "completed",
    request_takeover_path: Optional[List[str]] = None,
    commit_sha: Optional[str] = None,
) -> None:
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    if request:
        payload: Dict[str, Any] = {"gate": gate, "status": "requested"}
        if request_takeover_path is not None:
            payload["takeover_path"] = [{"gate": hop} for hop in request_takeover_path]
        (requests_dir / f"pr-{pr}-{gate}.json").write_text(json.dumps(payload), encoding="utf-8")
    if result:
        result_payload: Dict[str, Any] = {"gate": gate, "status": status}
        if commit_sha is not None:
            result_payload["commit_sha"] = commit_sha
        (results_dir / f"pr-{pr}-{gate}.json").write_text(
            json.dumps(result_payload), encoding="utf-8",
        )


def _write_chain_exhausted(state_dir: Path, gate: str, *, pr: int = 7) -> None:
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"pr-{pr}-{gate}-chain-exhausted.json").write_text(
        json.dumps({"gate": gate, "status": "chain_exhausted"}), encoding="utf-8",
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Argument parsing: the seats the manager was asked for
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv",
    [
        ["--pr", "7", "--branch", "b", "--review-stack", "codex_gate,kimi_gate"],
        ["--pr", "7", "--branch", "b", "--review-stack=codex_gate,kimi_gate"],
        ["--pr=7", "--branch", "b", "--review-stack", "codex_gate, kimi_gate"],
        ["--pr", "7", "--branch", "b", "--review-stack=codex_gate , kimi_gate ,"],
        ["--review-stack", "codex_gate,kimi_gate", "--pr", "7", "--branch", "b"],
    ],
    ids=["space", "equals", "quoted-spaces", "padded-trailing-comma", "flags-reordered"],
)
def test_review_stack_forms_resolve_to_the_same_seats(argv: List[str]) -> None:
    pr, seats = parse_request_args(argv, lambda: pytest.fail("default stack must not be read"))
    assert pr == 7
    assert seats == ["codex_gate", "kimi_gate"]


def test_other_options_do_not_leak_into_the_seats() -> None:
    argv = [
        "--pr", "7", "--branch", "feat/review-stack-fix", "--risk-class", "high",
        "--changed-files", "a.py,b.py", "--review-stack", "kimi_gate",
    ]
    assert parse_request_args(argv, lambda: DEFAULT_STACK) == (7, ["kimi_gate"])


def test_no_review_stack_uses_the_managers_default_stack() -> None:
    pr, seats = parse_request_args(["--pr", "7", "--branch", "b"], lambda: DEFAULT_STACK)
    assert (pr, seats) == (7, DEFAULT_STACK)


@pytest.mark.parametrize("raw", ["", " , ,"])
def test_an_empty_review_stack_is_refused(raw: str) -> None:
    with pytest.raises(VerificationError, match="resolved empty"):
        parse_request_args(["--pr", "7", "--review-stack", raw], lambda: DEFAULT_STACK)


def test_an_empty_default_stack_is_refused() -> None:
    with pytest.raises(VerificationError, match="resolved empty"):
        parse_request_args(["--pr", "7"], lambda: [])


def test_a_seat_that_is_not_a_plain_name_is_refused() -> None:
    with pytest.raises(VerificationError, match="not a plain identifier"):
        parse_request_args(["--pr", "7", "--review-stack", "../codex_gate"], lambda: DEFAULT_STACK)


# ---------------------------------------------------------------------------
# Reading the report out of the manager's merged stdout/stderr
# ---------------------------------------------------------------------------

def test_report_is_found_between_log_lines_and_the_error_text() -> None:
    report = _report(_entry("codex_gate"))
    output = (
        "gate_request_handler: review-gate takeover chain actief -- keten={'codex_gate': 'kimi_gate'}\n"
        + json.dumps(report, indent=2)
        + "\nERROR: required gates did not PASS: codex_gate\n"
    )
    assert extract_report(output) == report


def test_a_json_object_that_is_not_the_report_is_skipped() -> None:
    report = _report(_entry("codex_gate"))
    output = json.dumps({"warning": "noise"}, indent=2) + "\n" + json.dumps(report, indent=2) + "\n"
    assert extract_report(output) == report


@pytest.mark.parametrize("output", ["", "no json here\n", "{ not json\n", '{"gates": "not a list"}\n'])
def test_no_report_is_a_failure(output: str) -> None:
    with pytest.raises(VerificationError, match="no JSON report"):
        extract_report(output)


# ---------------------------------------------------------------------------
# Seat resolution
# ---------------------------------------------------------------------------

def test_every_requested_seat_ran(state_dir: Path) -> None:
    for gate in DEFAULT_STACK:
        _write_artifacts(state_dir, gate)
    outcome = verify_report(
        _report(_entry("codex_gate"), _entry("kimi_gate")),
        pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir,
    )
    assert outcome.ok
    assert outcome.verified == ["codex_gate", "kimi_gate"]
    assert outcome.not_completed == []


def test_a_takeover_reader_answers_for_the_seat_it_replaced(state_dir: Path) -> None:
    """codex_gate unavailable -> kimi_gate read in its place; the second, kimi seat
    was not requested again. No codex_gate artifact exists and none is demanded."""
    _write_artifacts(state_dir, "kimi_gate")
    report = _report(_entry("kimi_gate", takeover_path=["codex_gate"]))

    outcome = verify_report(report, pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir)

    assert outcome.ok, outcome.missing
    assert outcome.verified == ["kimi_gate"]
    assert not (state_dir / "review_gates" / "requests" / "pr-7-codex_gate.json").exists()


def test_the_old_behaviour_would_have_demanded_the_replaced_seats_artifact(state_dir: Path) -> None:
    """The regression, pinned against the raw stack: verifying each seat's own
    artifacts fails for this run although nothing was skipped."""
    _write_artifacts(state_dir, "kimi_gate")
    missing_for_raw_stack = [
        gate for gate in DEFAULT_STACK
        if not (state_dir / "review_gates" / "requests" / f"pr-7-{gate}.json").is_file()
    ]
    assert missing_for_raw_stack == ["codex_gate"]

    outcome = verify_report(
        _report(_entry("kimi_gate", takeover_path=["codex_gate"])),
        pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir,
    )
    assert outcome.ok


def test_a_multi_hop_takeover_answers_for_every_seat_it_passed_over(state_dir: Path) -> None:
    """codex and kimi both dead: glm_gate reads for both, requested once."""
    _write_artifacts(state_dir, "glm_gate")
    report = _report(_entry("glm_gate", takeover_path=["codex_gate", "kimi_gate"]))

    outcome = verify_report(report, pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir)

    assert outcome.ok, outcome.missing
    assert outcome.verified == ["glm_gate"]


def test_takeover_path_is_read_from_the_request_record_when_the_report_lacks_it(state_dir: Path) -> None:
    """A guarded result write can leave the result without the annotation the
    request record carries."""
    _write_artifacts(state_dir, "kimi_gate", request_takeover_path=["codex_gate"])
    outcome = verify_report(
        _report(_entry("kimi_gate")), pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir,
    )
    assert outcome.ok, outcome.missing


def test_a_seat_nobody_ran_or_replaced_is_missing(state_dir: Path) -> None:
    """Fail-closed: kimi_gate was requested, no reported gate is kimi_gate, and no
    reported gate took it over."""
    _write_artifacts(state_dir, "codex_gate")
    outcome = verify_report(
        _report(_entry("codex_gate")), pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir,
    )
    assert not outcome.ok
    assert len(outcome.missing) == 1
    assert outcome.missing[0].startswith("seat kimi_gate:")


def test_a_takeover_of_a_different_seat_does_not_cover_a_missing_one(state_dir: Path) -> None:
    _write_artifacts(state_dir, "glm_gate")
    outcome = verify_report(
        _report(_entry("glm_gate", takeover_path=["codex_gate"])),
        pr_number=7, seats=["codex_gate", "kimi_gate"], state_dir=state_dir,
    )
    assert [m.split(":")[0] for m in outcome.missing] == ["seat kimi_gate"]


def test_an_empty_report_leaves_every_seat_missing(state_dir: Path) -> None:
    outcome = verify_report(_report(), pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir)
    assert [m.split(":")[0] for m in outcome.missing] == ["seat codex_gate", "seat kimi_gate"]


@pytest.mark.parametrize("missing", ["request", "result"])
def test_a_reported_gate_without_its_artifact_is_missing(state_dir: Path, missing: str) -> None:
    _write_artifacts(state_dir, "codex_gate", request=missing != "request", result=missing != "result")
    outcome = verify_report(
        _report(_entry("codex_gate")), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )
    assert not outcome.ok
    assert len(outcome.missing) == 1
    assert outcome.missing[0].endswith(f"pr-7-codex_gate.json")
    assert f"review_gates/{missing}s" in outcome.missing[0]


@pytest.mark.parametrize(
    "content",
    ['{"status": "compl', '["not", "an", "object"]', "null", ""],
    ids=["truncated", "json-list", "json-null", "empty"],
)
def test_a_reported_gate_with_an_unreadable_result_is_missing(
    state_dir: Path, content: str,
) -> None:
    """An existing result file that does not parse into a JSON object is no
    artifact at all. It used to load as nothing -> ``status=unknown`` -> only
    ``not_completed``, leaving ``ok`` true and the wrapper exiting 0 on a record
    nobody can read.
    """
    _write_artifacts(state_dir, "codex_gate", result=False)
    result_file = state_dir / "review_gates" / "results" / "pr-7-codex_gate.json"
    result_file.write_text(content, encoding="utf-8")

    outcome = verify_report(
        _report(_entry("codex_gate")), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert not outcome.ok
    assert outcome.verified == []
    assert outcome.not_completed == []
    assert len(outcome.missing) == 1
    assert str(result_file) in outcome.missing[0]
    assert "unreadable" in outcome.missing[0]


def test_an_unreadable_chain_exhausted_record_is_missing(state_dir: Path) -> None:
    """The same fail-closed rule for the chain-exhausted terminal record: a
    present-but-unreadable record is no evidence the chain was recorded."""
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    record = results_dir / "pr-7-codex_gate-chain-exhausted.json"
    record.write_text('{"status": "chain_exh', encoding="utf-8")
    entry = _entry("codex_gate", request_status="chain_exhausted", takeover_path=["codex_gate"])

    outcome = verify_report(_report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir)

    assert not outcome.ok
    assert outcome.verified == []
    assert outcome.not_completed == []
    assert outcome.missing == [f"{record} is unreadable (not a JSON object)"]


def test_main_exits_1_on_a_corrupt_result_file(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end CLI: the corrupt result makes the verifier exit 1, with the
    file named and called unreadable, instead of "all artifacts verified"."""
    _write_artifacts(state_dir, "codex_gate", result=False)
    (state_dir / "review_gates" / "results" / "pr-7-codex_gate.json").write_text(
        '{"status": "compl', encoding="utf-8",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_report(_entry("codex_gate")))))

    rc = verify.main(
        ["--state-dir", str(state_dir), "--", "--pr", "7", "--review-stack", "codex_gate"],
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "MISSING_ARTIFACT" in captured.err
    assert "pr-7-codex_gate.json" in captured.err
    assert "unreadable" in captured.err
    assert "GATE_ENFORCEMENT_COMPLETE" not in captured.out


def test_a_gate_that_ran_but_did_not_complete_is_reported_not_failed(state_dir: Path) -> None:
    _write_artifacts(state_dir, "codex_gate", status="failed")
    outcome = verify_report(
        _report(_entry("codex_gate")), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )
    assert outcome.ok
    assert outcome.not_completed == ["codex_gate status=failed"]


def test_a_recorded_exhausted_chain_accounts_for_its_seat(state_dir: Path) -> None:
    _write_chain_exhausted(state_dir, "codex_gate")
    entry = _entry("codex_gate", request_status="chain_exhausted", takeover_path=["codex_gate", "kimi_gate"])
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate", "kimi_gate"], state_dir=state_dir,
    )
    assert outcome.ok, outcome.missing
    assert outcome.not_completed == ["codex_gate status=chain_exhausted"]


def test_a_chain_exhausted_entry_with_non_dict_detail_does_not_crash(state_dir: Path) -> None:
    """``entry['detail']`` is not guaranteed to be a dict; a bare
    ``(entry.get("detail") or {}).get(...)`` raises AttributeError on a
    truthy non-dict value (e.g. a string) instead of treating the
    takeover_path as absent, same as the dict-guarded path above."""
    _write_chain_exhausted(state_dir, "codex_gate")
    entry = {
        "gate": "codex_gate",
        "request_status": "chain_exhausted",
        "execution_status": "completed",
        "passed": True,
        "detail": "not-a-dict",
    }
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )
    assert outcome.ok, outcome.missing
    assert outcome.not_completed == ["codex_gate status=chain_exhausted"]


def test_an_exhausted_chain_without_its_record_is_missing(state_dir: Path) -> None:
    entry = _entry("codex_gate", request_status="chain_exhausted", takeover_path=["codex_gate"])
    outcome = verify_report(_report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir)
    assert not outcome.ok
    assert outcome.missing[0].endswith("pr-7-codex_gate-chain-exhausted.json")


def test_a_reported_gate_name_that_is_not_a_plain_name_is_refused(state_dir: Path) -> None:
    with pytest.raises(VerificationError, match="not a plain identifier"):
        verify_report(
            _report(_entry("../../etc/passwd")), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
        )


# ---------------------------------------------------------------------------
# Commit binding: a result about another commit is not evidence about this head
# ---------------------------------------------------------------------------

def test_a_result_for_another_commit_does_not_verify(state_dir: Path) -> None:
    """The reproduced defect: a completed result from the previous head, with a
    mismatched binding in the report entry, used to verify as clean."""
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    entry = _entry(
        "codex_gate", head_sha=NEW_HEAD, result_commit_sha=OLD_HEAD, sha_binding="mismatch",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert not outcome.ok
    assert outcome.missing == []
    assert outcome.verified == []
    assert len(outcome.sha_mismatch) == 1
    message = outcome.sha_mismatch[0]
    assert message.startswith("codex_gate:")
    assert OLD_HEAD[:8] in message
    assert NEW_HEAD[:8] in message


def test_a_result_for_this_commit_still_verifies(state_dir: Path) -> None:
    _write_artifacts(state_dir, "codex_gate", commit_sha=NEW_HEAD)
    entry = _entry(
        "codex_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert outcome.ok, outcome.missing
    assert outcome.verified == ["codex_gate"]
    assert outcome.sha_mismatch == []


def test_the_results_own_commit_sha_is_read_when_the_entry_omits_it(state_dir: Path) -> None:
    """An older report may carry ``head_sha`` but not ``result_commit_sha``; the
    result record's own ``commit_sha`` is the same fact."""
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    entry = _entry("codex_gate", head_sha=NEW_HEAD)
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert not outcome.ok
    assert len(outcome.sha_mismatch) == 1
    assert OLD_HEAD[:8] in outcome.sha_mismatch[0]
    assert NEW_HEAD[:8] in outcome.sha_mismatch[0]


def test_the_result_record_is_the_evidence_over_the_entry_summary(state_dir: Path) -> None:
    """The artifact is what gets merged. An entry claiming the head while the
    record on disk names another commit is still a mismatch."""
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    entry = _entry(
        "codex_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert not outcome.ok
    assert len(outcome.sha_mismatch) == 1


def test_a_lying_sha_binding_field_does_not_override_the_shas(state_dir: Path) -> None:
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    entry = _entry(
        "codex_gate", head_sha=NEW_HEAD, result_commit_sha=OLD_HEAD, sha_binding="match",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert not outcome.ok
    assert len(outcome.sha_mismatch) == 1


@pytest.mark.parametrize(
    "head_sha,result_sha",
    [("", NEW_HEAD), (NEW_HEAD, ""), ("", "")],
    ids=["no-head", "no-result", "neither"],
)
def test_an_unknown_binding_behaves_as_today(
    state_dir: Path, head_sha: str, result_sha: str,
) -> None:
    """Unknown is not a polite mismatch: either sha absent stays verified."""
    _write_artifacts(state_dir, "codex_gate", commit_sha=result_sha)
    entry = _entry(
        "codex_gate", head_sha=head_sha, result_commit_sha=result_sha, sha_binding="unknown",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert outcome.ok, outcome.missing
    assert outcome.verified == ["codex_gate"]
    assert outcome.sha_mismatch == []


def test_a_result_without_any_commit_fields_behaves_as_today(state_dir: Path) -> None:
    _write_artifacts(state_dir, "codex_gate")
    outcome = verify_report(
        _report(_entry("codex_gate")), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert outcome.ok, outcome.missing
    assert outcome.verified == ["codex_gate"]
    assert outcome.sha_mismatch == []


def test_a_mismatched_gate_still_accounts_for_its_seat(state_dir: Path) -> None:
    """The gate ran and answered the seat; the failure is the binding, not a
    missing artifact, so only one failure is reported."""
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    entry = _entry(
        "codex_gate", head_sha=NEW_HEAD, result_commit_sha=OLD_HEAD, sha_binding="mismatch",
    )
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate"], state_dir=state_dir,
    )

    assert outcome.missing == []
    assert not outcome.ok


def test_a_takeover_reader_about_another_commit_does_not_verify(state_dir: Path) -> None:
    _write_artifacts(state_dir, "kimi_gate", commit_sha=OLD_HEAD)
    report = _report(
        _entry("kimi_gate", takeover_path=["codex_gate"], head_sha=NEW_HEAD,
               result_commit_sha=OLD_HEAD, sha_binding="mismatch"),
    )
    outcome = verify_report(
        report, pr_number=7, seats=DEFAULT_STACK, state_dir=state_dir,
    )

    assert not outcome.ok
    assert outcome.missing == []
    assert outcome.sha_mismatch[0].startswith("kimi_gate:")


def test_chain_exhausted_reporting_is_unchanged_by_the_binding_check(state_dir: Path) -> None:
    _write_chain_exhausted(state_dir, "codex_gate")
    entry = _entry("codex_gate", request_status="chain_exhausted", takeover_path=["codex_gate", "kimi_gate"])
    outcome = verify_report(
        _report(entry), pr_number=7, seats=["codex_gate", "kimi_gate"], state_dir=state_dir,
    )
    assert outcome.ok, outcome.missing
    assert outcome.not_completed == ["codex_gate status=chain_exhausted"]
    assert outcome.sha_mismatch == []


# ---------------------------------------------------------------------------
# main(): the verifier's own exit code, over stdin
# ---------------------------------------------------------------------------

def _run_main(
    state_dir: Path,
    report: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report)))
    return verify.main(
        ["--state-dir", str(state_dir), "--", "--pr", "7", "--review-stack", "codex_gate"],
    )


def test_main_fails_when_a_result_is_bound_to_another_commit(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _write_artifacts(state_dir, "codex_gate", commit_sha=OLD_HEAD)
    report = _report(
        _entry("codex_gate", head_sha=NEW_HEAD, result_commit_sha=OLD_HEAD, sha_binding="mismatch"),
    )
    rc = _run_main(state_dir, report, monkeypatch)
    captured = capsys.readouterr()

    assert rc == 1
    assert "SHA_MISMATCH" in captured.err
    assert "codex_gate" in captured.err
    assert OLD_HEAD[:8] in captured.err
    assert NEW_HEAD[:8] in captured.err
    assert "GATE_ENFORCEMENT_COMPLETE" not in captured.out


def test_main_passes_when_a_result_is_bound_to_this_head(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _write_artifacts(state_dir, "codex_gate", commit_sha=NEW_HEAD)
    report = _report(
        _entry("codex_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match"),
    )
    rc = _run_main(state_dir, report, monkeypatch)
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "GATE_ENFORCEMENT_COMPLETE" in captured.out
    assert "SHA_MISMATCH" not in captured.err


def test_main_passes_when_the_binding_is_unknown(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _write_artifacts(state_dir, "codex_gate")
    report = _report(_entry("codex_gate"))
    rc = _run_main(state_dir, report, monkeypatch)
    captured = capsys.readouterr()

    assert rc == 0, captured.err
    assert "GATE_ENFORCEMENT_COMPLETE" in captured.out
    assert "SHA_MISMATCH" not in captured.err


# ---------------------------------------------------------------------------
# The wrapper itself, through bash, against a stub manager
# ---------------------------------------------------------------------------

STUB_MANAGER = '''\
"""Stub review_gate_manager: prints a canned request-and-execute report.

STUB_REPORT names the JSON file to print, STUB_RC the exit code. The log line on
stderr and the ERROR line on a failure mimic the merged stream the wrapper reads.
"""
import os
import sys
from pathlib import Path

DEFAULT_REVIEW_STACK = ["codex_gate", "kimi_gate"]

if __name__ == "__main__":
    sys.stderr.write("gate_request_handler: review-gate takeover chain actief -- keten={'codex_gate': 'kimi_gate'}\\n")
    sys.stdout.write(Path(os.environ["STUB_REPORT"]).read_text(encoding="utf-8") + "\\n")
    rc = int(os.environ.get("STUB_RC", "0"))
    if rc:
        sys.stderr.write("ERROR: required gates did not PASS: codex_gate\\n")
    sys.exit(rc)
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway git project holding the real wrapper, its resolver and the
    verifier, plus a stub review_gate_manager. Nothing else can run."""
    root = tmp_path / "project"
    (root / "scripts" / "lib").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    shutil.copy2(REPO_ROOT / "scripts" / "t0_gate_enforcement.sh", root / "scripts")
    shutil.copy2(REPO_ROOT / "scripts" / "lib" / "vnx_resolve_root.sh", root / "scripts" / "lib")
    shutil.copy2(REPO_ROOT / "scripts" / "lib" / "gate_enforcement_verify.py", root / "scripts" / "lib")
    (root / "scripts" / "review_gate_manager.py").write_text(STUB_MANAGER, encoding="utf-8")
    return root


def _run_wrapper(
    project: Path, report: Dict[str, Any], *args: str, rc: int = 0,
) -> subprocess.CompletedProcess:
    report_file = project.parent / "stub_report.json"
    report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(STUB_REPORT=str(report_file), STUB_RC=str(rc))
    return subprocess.run(
        ["bash", str(project / "scripts" / "t0_gate_enforcement.sh"), *args],
        cwd=project, env=env, capture_output=True, text=True, timeout=60,
    )


def _project_state_dir(project: Path) -> Path:
    return project / ".vnx-data" / "state"


def test_wrapper_passes_when_a_takeover_reader_ran_in_place_of_a_seat(project: Path) -> None:
    _write_artifacts(_project_state_dir(project), "kimi_gate")
    proc = _run_wrapper(
        project, _report(_entry("kimi_gate", takeover_path=["codex_gate"])),
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )
    assert proc.returncode == 0, proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE: all artifacts verified" in proc.stdout
    assert "MISSING_ARTIFACT" not in proc.stderr


@pytest.mark.parametrize(
    "stack_args",
    [
        ["--review-stack", "codex_gate,kimi_gate"],
        ["--review-stack=codex_gate,kimi_gate"],
        ["--review-stack", "codex_gate, kimi_gate"],
        ["--review-stack=codex_gate, kimi_gate"],
        [],
    ],
    ids=["space", "equals", "quoted-spaces", "equals-quoted-spaces", "default-stack"],
)
def test_wrapper_resolves_every_review_stack_form_to_both_seats(
    project: Path, stack_args: List[str],
) -> None:
    """Only codex_gate ran. Whatever the form, kimi_gate is a requested seat that
    no reported gate answers for, so the wrapper must say so, not verify one
    seat or parse none."""
    _write_artifacts(_project_state_dir(project), "codex_gate")
    proc = _run_wrapper(
        project, _report(_entry("codex_gate")),
        "--pr", "7", "--branch", "feat/x", *stack_args,
    )
    assert proc.returncode == 1
    assert "MISSING_ARTIFACT: seat kimi_gate" in proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" not in proc.stdout


@pytest.mark.parametrize(
    "stack_args",
    [["--review-stack=codex_gate, kimi_gate"], []],
    ids=["equals-quoted-spaces", "default-stack"],
)
def test_wrapper_passes_both_seats_ran_for_every_stack_form(project: Path, stack_args: List[str]) -> None:
    for gate in DEFAULT_STACK:
        _write_artifacts(_project_state_dir(project), gate)
    proc = _run_wrapper(
        project, _report(_entry("codex_gate"), _entry("kimi_gate")),
        "--pr", "7", "--branch", "feat/x", *stack_args,
    )
    assert proc.returncode == 0, proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" in proc.stdout


def test_wrapper_fails_a_reported_gate_whose_result_is_missing(project: Path) -> None:
    _write_artifacts(_project_state_dir(project), "kimi_gate", result=False)
    proc = _run_wrapper(
        project, _report(_entry("kimi_gate", takeover_path=["codex_gate"])),
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )
    assert proc.returncode == 1
    assert "MISSING_ARTIFACT" in proc.stderr
    assert "pr-7-kimi_gate.json" in proc.stderr


def test_wrapper_fails_a_result_bound_to_another_commit(project: Path) -> None:
    """Fix-forward: codex passed on the new head, kimi's completed result is
    about the old one. The wrapper must not call this head reviewed."""
    state = _project_state_dir(project)
    _write_artifacts(state, "codex_gate", commit_sha=NEW_HEAD)
    _write_artifacts(state, "kimi_gate", commit_sha=OLD_HEAD)
    report = _report(
        _entry("codex_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match"),
        _entry("kimi_gate", head_sha=NEW_HEAD, result_commit_sha=OLD_HEAD, sha_binding="mismatch"),
    )
    proc = _run_wrapper(
        project, report,
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )

    assert proc.returncode == 1
    assert "SHA_MISMATCH" in proc.stderr
    assert "kimi_gate" in proc.stderr
    assert OLD_HEAD[:8] in proc.stderr
    assert NEW_HEAD[:8] in proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" not in proc.stdout


def test_wrapper_passes_when_every_result_matches_the_head(project: Path) -> None:
    state = _project_state_dir(project)
    for gate in DEFAULT_STACK:
        _write_artifacts(state, gate, commit_sha=NEW_HEAD)
    report = _report(
        _entry("codex_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match"),
        _entry("kimi_gate", head_sha=NEW_HEAD, result_commit_sha=NEW_HEAD, sha_binding="match"),
    )
    proc = _run_wrapper(
        project, report,
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )

    assert proc.returncode == 0, proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" in proc.stdout
    assert "SHA_MISMATCH" not in proc.stderr


def test_wrapper_passes_when_the_binding_is_unknown(project: Path) -> None:
    """No sha on either side: the pre-fix behaviour is preserved."""
    state = _project_state_dir(project)
    for gate in DEFAULT_STACK:
        _write_artifacts(state, gate)
    proc = _run_wrapper(
        project, _report(_entry("codex_gate"), _entry("kimi_gate")),
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )

    assert proc.returncode == 0, proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" in proc.stdout
    assert "SHA_MISMATCH" not in proc.stderr


def test_wrapper_fails_a_reported_gate_whose_result_is_unreadable(project: Path) -> None:
    """The wrapper must not exit 0 on a result file nobody can read, even though
    the file is present and the gate only lands in not_completed today."""
    state_dir = _project_state_dir(project)
    _write_artifacts(state_dir, "kimi_gate", result=False)
    (state_dir / "review_gates" / "results" / "pr-7-kimi_gate.json").write_text(
        '{"status": "compl', encoding="utf-8",
    )
    proc = _run_wrapper(
        project, _report(_entry("kimi_gate", takeover_path=["codex_gate"])),
        "--pr", "7", "--branch", "feat/x", "--review-stack", "codex_gate,kimi_gate",
    )
    assert proc.returncode == 1
    assert "MISSING_ARTIFACT" in proc.stderr
    assert "pr-7-kimi_gate.json" in proc.stderr
    assert "unreadable" in proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" not in proc.stdout


def test_wrapper_reports_but_does_not_fail_a_gate_that_ran_without_completing(project: Path) -> None:
    for gate in DEFAULT_STACK:
        _write_artifacts(_project_state_dir(project), gate, status="failed" if gate == "kimi_gate" else "completed")
    proc = _run_wrapper(
        project, _report(_entry("codex_gate"), _entry("kimi_gate")),
        "--pr", "7", "--branch", "feat/x",
    )
    assert proc.returncode == 0, proc.stderr
    assert "GATE_NOT_COMPLETED: kimi_gate status=failed" in proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" in proc.stdout


def test_wrapper_fails_when_the_manager_reports_a_required_failure(project: Path) -> None:
    proc = _run_wrapper(
        project, {**_report(_entry("codex_gate")), "has_required_failure": True},
        "--pr", "7", "--branch", "feat/x", rc=1,
    )
    assert proc.returncode == 1
    assert "GATE_ENFORCEMENT_FAILED: one or more required gates did not complete successfully" in proc.stderr
    assert "GATE_ENFORCEMENT_COMPLETE" not in proc.stdout


def test_wrapper_fails_when_the_manager_prints_no_report(project: Path) -> None:
    report_file = project.parent / "stub_report.json"
    report_file.write_text("not a report", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(STUB_REPORT=str(report_file), STUB_RC="0")
    proc = subprocess.run(
        ["bash", str(project / "scripts" / "t0_gate_enforcement.sh"), "--pr", "7", "--branch", "feat/x"],
        cwd=project, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "GATE_ENFORCEMENT_FAILED: request-and-execute printed no JSON report" in proc.stderr


def test_wrapper_passes_bash_syntax_check() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "scripts" / "t0_gate_enforcement.sh")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_main_requires_the_argument_separator(capsys: pytest.CaptureFixture) -> None:
    assert verify.main(["--state-dir", "/nonexistent"]) == 2
    assert "usage" in capsys.readouterr().err
