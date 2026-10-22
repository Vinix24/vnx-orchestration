"""Tests for scripts/ci/xdist_parity_history.py (track ci-profile-a-xdist, D2b): the cutover rule.

The rule (plan "The cutover rule"): every run in the window clean (exit 0,
every step success, the comparison step included), at least 10 distinct head
SHAs over at least two UTC calendar days with the latest on main, the shadow
suite at most 15 minutes on each, every watch-list OI closed. The window:
heavy verdicts with the target fingerprint whose head descends from the merge
commit of the last D3 PR.

Every verdict is a fixture in tests/fixtures/xdist_parity_verdicts/, in the
schema the pipeline writes today; tests/test_ci_record_xdist_parity_verdict.py
regenerates each one from its source and compares it whole:

* written-heavy-parity: the verdict artifact of run 39122740681 (head
  a48d5bc5, exit 0) as the comparison wrote it, before the record step;
* heavy-parity: that verdict after the record step, comparison step success;
* heavy-differences: run 39121916675 on main (a7f9750f, exit 1, two
  outcome_class_differs), recorded with its failed comparison step;
* parity-step-failed-after-exit-0: written-heavy-parity recorded with a failed
  comparison step (OI-2145);
* light: what the parity and record steps write on a light run (OI-2147);
* emergency: the record step's exit-2 verdict when the comparison wrote none.

Each case the plan and the dispatch name keeps the rule from holding: a light
run, a re-run of one SHA, a changed fingerprint, a run older than the last D3
merge, one exit-1 run in the window, a comparison step that failed after
writing exit 0, an emergency verdict. git ancestry runs on a real throwaway
repository; the CLI runs end to end against a gh stand-in on PATH (an
external process, replaced only inside tests/), so no test calls GitHub.

Option A (operator decision, 2026-10-22): a run whose serial reference job
failed, or in which either Profile A job received a runner shutdown signal
(read from the job's check-run annotations), is not measurable. It is set
aside from every condition of the window and listed with its reason. A real
parity difference and a shadow job that failed without a shutdown still
block; an annotation read that fails is never a shutdown.

History that cannot prove ancestry is a tool error, never a placement
(codex round 2 on #2105): a shallow clone answers "not an ancestor" for a
descendant whose path to the base it cut off, so a failed run left the window
and the rule held. A shallow clone and a head that is no commit here exit 2
and name the fetch; the tool never fetches itself.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "xdist_parity_history.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "xdist_parity_verdicts"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hist = _load("xdist_parity_history", SCRIPT)
cxp = sys.modules["check_xdist_parity"]  # imported by the history module, which shares its step rule


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


FP = fixture("heavy-parity")["selection_fingerprint"]
OTHER_FP = "0" * 64
BEFORE = "b" * 40


def verdict(name: str = "heavy-parity", *, run_id: str = "1000", attempt: str = "1", **fields) -> dict:
    """Fixture *name* as the artifact of run *run_id*, attempt *attempt*; *fields* replace top-level fields."""
    document = fixture(name)
    document.update(run_id=run_id, run_attempt=attempt, **fields)
    return document


def run(n: int, *, sha=None, day=1, hour=None, branch="dispatch/x", event="pull_request", status="completed",
        attempt=1, verdict_attempt=None, name="heavy-parity", **fields) -> "hist.Run":
    """Run 1000+n carrying fixture *name*; its verdict is of its latest attempt unless *verdict_attempt* says otherwise."""
    created = f"2026-11-{day:02d}T{(hour if hour is not None else n % 24):02d}:00:00Z"
    own = verdict(name, run_id=str(1000 + n), attempt=str(attempt) if verdict_attempt is None else verdict_attempt,
                  **fields)
    return hist.Run(str(1000 + n), attempt, sha or f"{n:040x}", branch, event, created, status, "success",
                    None if status != "completed" else own)


def main_run(n: int, **kw) -> "hist.Run":
    return run(n, branch="main", event="push", **kw)


def clean_window() -> list:
    """Ten clean runs on ten SHAs over two days; the latest is on main."""
    runs = [run(n, day=1 if n < 5 else 2, hour=n) for n in range(9)]
    return runs + [main_run(9, day=2, hour=23)]


def evaluate(runs, *, watch_list=(), oi_status=None, truncated=False, ancestry=None, job_evidence=None):
    ancestry = ancestry or {}
    option = {} if job_evidence is None else {"job_evidence": job_evidence}
    return hist.evaluate(
        runs,
        target_fingerprint=FP,
        is_ancestor=lambda head: ancestry.get(head, head != BEFORE),
        watch_list=list(watch_list) if watch_list is not None else None,
        oi_status=oi_status,
        list_truncated=truncated,
        **option,
    )


def failed(evaluation) -> set:
    return {c.name for c in evaluation.conditions if not c.holds}


def test_ten_clean_runs_over_two_days_ending_on_main_hold():
    evaluation = evaluate(clean_window())
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert len(evaluation.window) == 10


# --- the cases of the plan and of the dispatch: each keeps the rule from holding ------------------------


def test_a_light_run_does_not_count():
    runs = clean_window()
    runs[0] = run(0, day=1, hour=0, name="light")
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert [p.place for p in evaluation.placements].count("light") == 1


@pytest.mark.parametrize("step_failures", [None, []], ids=["names-its-skipped-steps", "empty-list"])
def test_a_light_run_does_not_count_whatever_its_step_list_says(step_failures):
    """OI-2147: the light verdict now names its skipped steps; a light run stays outside the window either way."""
    light = run(20, day=2, hour=5, name="light")
    assert light.verdict["step_failures"], "the light fixture names its skipped steps"
    if step_failures is not None:
        light.verdict["step_failures"] = step_failures
    evaluation = evaluate(clean_window() + [light])
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert evaluation.placements[-1].place == "light" and evaluation.placements[-1].reasons == []


def test_a_rerun_of_one_sha_counts_once():
    runs = clean_window()
    runs[1] = run(1, sha=runs[0].head_sha, day=1, hour=1)
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert len(evaluation.window) == 10


def test_a_changed_fingerprint_does_not_count():
    runs = clean_window()
    runs[2].verdict["selection_fingerprint"] = OTHER_FP
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert evaluation.placements[2].place == "other_selection"


def test_a_run_older_than_the_last_d3_merge_does_not_count():
    runs = clean_window()
    runs[3] = run(3, sha=BEFORE, day=1, hour=3)
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"distinct_heads"}
    assert evaluation.placements[3].place == "before_window"


def test_one_exit_1_run_in_the_window_blocks():
    runs = clean_window() + [run(20, day=2, hour=5, name="heavy-differences")]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    [blocking] = [c for c in evaluation.conditions if c.name == "every_run_clean"]
    assert ("run 1020: exit 1 (differences) [outcome_class_differs: 2], step shadow.verdict=shadow_failed, "
            "step parity.parity=failure") in blocking.detail


def test_a_comparison_step_that_failed_after_writing_exit_0_blocks():
    """codex round 4 on #2087 (OI-2145): exit 0 and step_failures [] held the rule after the parity step failed."""
    runs = clean_window() + [run(20, day=2, hour=5, name="parity-step-failed-after-exit-0")]
    assert runs[-1].verdict["exit"] == 0
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[-1].reasons == ["step parity.parity=failure"]


def test_a_comparison_step_failure_the_list_leaves_out_still_blocks():
    runs = clean_window()
    runs[4].verdict["steps"]["parity"] = "failure"
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[4].reasons == ["step parity.parity=failure"]


def test_a_verdict_the_record_step_never_saw_is_not_clean():
    """A verdict as the comparison wrote it carries no outcome of the comparison step: it cannot prove it succeeded."""
    runs = clean_window() + [run(20, day=2, hour=5, name="written-heavy-parity")]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[-1].reasons == ["step parity.parity=unknown"]


def test_an_emergency_verdict_from_the_record_step_blocks():
    """OI-2147: the record step's exit-2 verdict carries every field and no fingerprint: unplaceable, never left out."""
    emergency = run(20, day=2, hour=7, name="emergency")
    assert emergency.verdict["exit"] == 2 and emergency.verdict["selection_fingerprint"] is None
    evaluation = evaluate(clean_window() + [emergency])
    assert failed(evaluation) == {"every_run_placed"}
    assert evaluation.placements[-1].place == "unplaceable"
    assert evaluation.placements[-1].reasons == ["no selection fingerprint, exit 2 [invalid: no_verdict]"]


def test_an_emergency_verdict_with_a_fingerprint_is_still_not_clean():
    emergency = run(20, day=2, hour=7, name="emergency", selection_fingerprint=FP)
    evaluation = evaluate(clean_window() + [emergency])
    assert failed(evaluation) == {"every_run_clean", "suite_wall_time"}
    assert evaluation.placements[-1].reasons[:3] == ["exit 2 (invalid_evidence) [invalid: no_verdict]",
                                                     "step parity.verdict=missing", "step parity.parity=failure"]


def test_an_emergency_verdict_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [run(20, sha=BEFORE, day=1, hour=0, name="emergency")])
    assert evaluation.holds


# --- the other conditions -------------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [2, None], ids=["exit-2", "exit-null"])
def test_an_unclean_exit_in_the_window_blocks(exit_code):
    runs = clean_window() + [run(20, day=2, hour=6, exit=exit_code)]
    assert failed(evaluate(runs)) == {"every_run_clean"}


@pytest.mark.parametrize("failure", ["parity.prelude=failure", "shadow.verdict=shadow_failed", "parity.download_candidate=failure"],
                         ids=["parity-prelude", "shadow-phase", "download"])
def test_a_failed_step_with_exit_0_blocks(failure):
    runs = clean_window()
    runs[4].verdict["step_failures"] = [failure]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[4].reasons == [f"step {failure}"]


@pytest.mark.parametrize(("block", "change", "failure"), [
    ("reference", {"result": "missing"}, "reference.result=missing"),
    ("shadow", {"job_result": "failure"}, "shadow.job=failure"),
    ("shadow", {"verdict": "shadow_failed"}, "shadow.verdict=shadow_failed"),
    ("shadow", {"record": "stale"}, "shadow.record=stale"),
    ("steps", {"download_reference": "failure"}, "parity.download_reference=failure"),
    ("steps", {"prelude": "failure"}, "parity.prelude=failure"),
], ids=["reference-result", "shadow-job", "shadow-verdict", "shadow-record", "parity-step", "prelude"])
def test_a_failure_the_verdict_records_blocks_even_when_its_step_failures_leave_it_out(block, change, failure):
    """codex round 3: a failed Profile A sat in the verdict's reference block, absent from step_failures, and held.

    The rule applies the parity job's own step rule (check_xdist_parity.step_failures)
    to the outcomes the verdict records, so the list cannot leave one out. A
    reference *result* that is missing with the job itself green has no cause
    to set the run aside, so it still blocks.
    """
    runs = clean_window()
    runs[4].verdict[block].update(change)
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[4].reasons == [f"step {failure}"]


@pytest.mark.parametrize(("change", "reason"), [
    ({"job_result": "failure"}, "the serial reference job failed (reference.job=failure)"),
    ({"exit": 1}, "the serial reference suite exited 1"),
    ({"exit": False}, "the serial reference suite exited False"),
], ids=["job-result", "exit-1", "exit-bool"])
def test_a_reference_failure_the_verdict_records_is_not_measurable_without_the_job_list(change, reason):
    """Option A reads cause (a) from the verdict itself: the job's outcome is already in its reference block."""
    runs = clean_window()
    runs[4].verdict["reference"].update(change)
    evaluation = evaluate(runs)
    assert "every_run_clean" not in failed(evaluation)
    assert evaluation.placements[4].place == "not_measurable"
    assert evaluation.placements[4].reasons == [reason]


def test_a_failure_both_listed_and_recorded_is_named_once():
    runs = clean_window()
    runs[4].verdict["shadow"]["job_result"] = "failure"
    runs[4].verdict["step_failures"] = ["shadow.job=failure"]
    assert evaluate(runs).placements[4].reasons == ["step shadow.job=failure"]


def test_one_calendar_day_is_not_enough():
    runs = [run(n, day=1, hour=n) for n in range(9)] + [main_run(9, day=1, hour=23)]
    assert failed(evaluate(runs)) == {"calendar_days"}


def test_the_latest_run_must_be_on_main():
    runs = clean_window() + [run(20, day=3, hour=1)]
    assert failed(evaluate(runs)) == {"latest_on_main"}


@pytest.mark.parametrize("wall", [901, None], ids=["over-15-minutes", "no-wall-time"])
def test_a_slow_or_untimed_shadow_blocks(wall):
    runs = clean_window()
    runs[5].verdict["shadow"]["suite_wall_s"] = wall
    assert {"suite_wall_time", "every_run_clean"} == failed(evaluate(runs))


def test_a_shadow_of_exactly_15_minutes_is_within_the_bound():
    runs = clean_window()
    runs[5].verdict["shadow"]["suite_wall_s"] = 900
    assert evaluate(runs).holds


def test_a_run_whose_head_cannot_be_found_blocks():
    runs = clean_window()
    evaluation = evaluate(runs, ancestry={runs[0].head_sha: None})
    assert failed(evaluation) == {"every_run_placed", "distinct_heads"}
    assert evaluation.placements[0].place == "ancestry_unknown"


def test_a_heavy_verdict_without_fingerprint_blocks_inside_the_window():
    runs = clean_window() + [run(20, day=2, hour=7, exit=2, verdict="invalid_evidence", selection_fingerprint=None)]
    evaluation = evaluate(runs)
    assert failed(evaluation) == {"every_run_placed"}
    assert evaluation.placements[-1].place == "unplaceable"


@pytest.mark.parametrize("profile", ["unknown", "heavy"])
def test_a_profile_that_is_not_light_is_never_excluded_on_profile(profile):
    runs = clean_window() + [run(20, day=2, hour=7, profile=profile, exit=1)]
    assert failed(evaluate(runs)) == {"every_run_clean"}


def test_a_truncated_run_list_blocks():
    assert failed(evaluate(clean_window(), truncated=True)) == {"run_list_complete"}


def test_an_empty_window_names_every_missing_condition():
    evaluation = evaluate([])
    assert failed(evaluation) == {"distinct_heads", "calendar_days", "latest_on_main", "suite_wall_time"}


# --- missing and stale evidence: only git ancestry may exclude a run without a current verdict -------------


def _without_verdict(run_: "hist.Run") -> "hist.Run":
    run_.verdict = None
    run_.conclusion = "failure"
    return run_


def test_a_completed_run_without_a_verdict_in_the_window_blocks():
    """codex round 1: ten clean runs plus a newer failed run without an artifact held."""
    runs = clean_window() + [_without_verdict(run(30, day=3, name="heavy-differences"))]
    evaluation = evaluate(runs)
    assert not evaluation.holds
    assert failed(evaluation) == {"every_run_has_verdict"}
    assert evaluation.placements[-1].place == "no_verdict"
    [blocking] = [c for c in evaluation.conditions if c.name == "every_run_has_verdict"]
    assert "run 1030" in blocking.detail


def test_a_run_without_a_verdict_names_why_it_has_none():
    bad = _without_verdict(run(30, day=3))
    bad.verdict_problem = "gh run download exit 1: no valid artifacts found to download"
    evaluation = evaluate(clean_window() + [bad])
    assert evaluation.placements[-1].reasons == [f"{bad.verdict_problem} (run conclusion failure)"]


def test_a_run_without_a_verdict_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [_without_verdict(run(30, sha=BEFORE, day=1, hour=0))])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


def test_a_run_without_a_verdict_whose_head_cannot_be_found_blocks():
    bad = _without_verdict(run(30, day=3))
    evaluation = evaluate(clean_window() + [bad], ancestry={bad.head_sha: None})
    assert failed(evaluation) == {"every_run_placed"}
    assert evaluation.placements[-1].place == "ancestry_unknown"


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting"])
def test_a_run_still_running_in_the_window_blocks(status):
    evaluation = evaluate(clean_window() + [run(30, day=3, status=status)])
    assert failed(evaluation) == {"every_run_completed"}
    assert evaluation.placements[-1].place == "pending"


def test_a_run_still_running_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [run(30, sha=BEFORE, day=1, hour=0, status="in_progress")])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


def test_an_earlier_attempts_verdict_is_stale_evidence():
    """codex round 1: attempt 2 carrying an attempt-1 verdict held."""
    runs = clean_window()
    runs[0].attempt = 2
    runs[0].conclusion = "failure"
    evaluation = evaluate(runs)
    assert not evaluation.holds
    assert evaluation.placements[0].place == "stale_verdict"
    assert evaluation.placements[0].reasons == ["verdict of attempt 1, not of attempt 2"]
    assert "every_verdict_current" in failed(evaluation)


def test_one_stale_verdict_blocks_even_beside_ten_current_runs():
    evaluation = evaluate(clean_window() + [run(20, day=2, hour=5, attempt=2, verdict_attempt="1")])
    assert failed(evaluation) == {"every_verdict_current"}
    assert len(evaluation.window) == 10


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("run_attempt", None, "the verdict names no run_attempt"),
    ("run_attempt", "x", "the verdict names no run_attempt"),
    ("run_id", None, "verdict of run None, not of run 1020"),
    ("run_id", "999", "verdict of run 999, not of run 1020"),
], ids=["no-attempt", "unparseable-attempt", "no-run-id", "other-run"])
def test_a_verdict_that_does_not_prove_its_run_and_attempt_is_stale(field, value, reason):
    bad = run(20, day=2, hour=5)
    bad.verdict[field] = value
    evaluation = evaluate(clean_window() + [bad])
    assert failed(evaluation) == {"every_verdict_current"}
    assert evaluation.placements[-1].reasons == [reason]


def test_an_integer_run_attempt_proves_its_attempt():
    current = run(20, day=2, hour=5, attempt=3)
    current.verdict["run_attempt"] = 3
    assert evaluate(clean_window() + [current]).holds


def test_a_stale_light_verdict_is_not_read_as_light():
    """Profile and fingerprint of a stale verdict are not evidence: only ancestry may exclude it."""
    stale = run(20, day=2, hour=5, attempt=2, verdict_attempt="1", profile="light")
    evaluation = evaluate(clean_window() + [stale])
    assert failed(evaluation) == {"every_verdict_current"}
    assert evaluation.placements[-1].place == "stale_verdict"


def test_a_stale_verdict_before_the_window_is_ignored():
    evaluation = evaluate(clean_window() + [run(20, sha=BEFORE, day=1, hour=0, attempt=2, verdict_attempt="1")])
    assert evaluation.holds
    assert evaluation.placements[-1].place == "before_window"


def test_the_history_and_the_parity_job_share_one_rule_for_current_evidence():
    import check_xdist_parity as cxp

    current = run(20, day=2, hour=5, attempt=2)
    assert hist.stale_reason(current.verdict, current) is None
    current.verdict["run_attempt"] = "1"
    assert hist.stale_reason(current.verdict, current) == cxp.stale_evidence_reason("verdict", current.verdict, "1020", 2)


# --- option A: a run without a measurable comparison does not count (operator 2026-10-22) ----------------------


REFERENCE_OK = {"duration_s": 900, "exit": 0, "job_result": "success", "result": "present"}
SHADOW_OK = {"durations_s": {"parallel": 600, "serial": None}, "exit_codes": {"parallel": 0, "serial": None},
             "job_result": "success", "record": "present", "suite_wall_s": 600, "verdict": "shadow_passed"}


def job_evidence(reference=None, shadow=None, shutdowns=(), problem="") -> "hist.JobEvidence":
    """*JobEvidence* as read from a run's two Profile A jobs; None is a conclusion that was not read."""
    return hist.JobEvidence({"reference": reference, "shadow": shadow}, tuple(shutdowns), problem)


def shadow_shutdown_fields() -> dict:
    """A verdict whose shadow job was shut down: no record, no candidate, exit 2 (runs 39794190857/39730303804)."""
    return {"exit": 2, "verdict": "invalid_evidence", "differences": None, "reference": dict(REFERENCE_OK),
            "step_failures": ["parity.download_candidate=failure", "shadow.record=missing", "shadow.job=failure",
                              "parity.parity=failure"],
            "shadow": {"job_result": "failure", "record": "missing"}}


def reference_shutdown_fields() -> dict:
    """A verdict whose serial reference job was shut down: no reference evidence, exit 2 (runs 39742671151/39828822170)."""
    return {"exit": 2, "verdict": "invalid_evidence", "differences": None,
            "reference": {"duration_s": None, "exit": None, "job_result": "failure", "result": "missing"},
            "step_failures": ["parity.download_reference=failure", "reference.job=failure", "reference.result=missing",
                              "parity.parity=failure"],
            "shadow": dict(SHADOW_OK)}


def serial_failed_parity_clean_fields() -> dict:
    """The shape of run 39803599874: the serial job failed one test, the shadow failed it too, parity exit 0."""
    return {"exit": 0, "verdict": "parity",
            "reference": {"duration_s": 2265, "exit": 1, "job_result": "failure", "result": "present"},
            "step_failures": ["reference.job=failure", "reference.exit=1", "shadow.verdict=shadow_failed"],
            "shadow": {**SHADOW_OK, "exit_codes": {"parallel": 1, "serial": None}, "verdict": "shadow_failed"}}


def test_a_shadow_job_shutdown_is_not_measurable():
    shutdown = run(20, day=2, hour=5, **shadow_shutdown_fields())
    evaluation = evaluate(clean_window() + [shutdown],
                          job_evidence=lambda _run: job_evidence("success", "failure", ("shadow",)))
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert evaluation.placements[-1].place == "not_measurable"
    assert evaluation.placements[-1].reasons == ["a runner shutdown signal in the xdist shadow job"]
    assert all(r is not shutdown for r in evaluation.window)


def test_a_reference_job_shutdown_is_not_measurable():
    shutdown = run(20, day=2, hour=5, **reference_shutdown_fields())
    evaluation = evaluate(clean_window() + [shutdown],
                          job_evidence=lambda _run: job_evidence("failure", "success", ("reference",)))
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert evaluation.placements[-1].place == "not_measurable"
    assert "runner shutdown signal in the serial Profile A job" in evaluation.placements[-1].reasons[0]


def test_a_run_without_a_verdict_whose_job_was_shut_down_is_not_measurable():
    """The job evidence is what makes a run without a verdict measurable, not the verdict it lacks."""
    missing = _without_verdict(run(30, day=3))
    evaluation = evaluate(clean_window() + [missing],
                          job_evidence=lambda _run: job_evidence("success", "failure", ("shadow",)))
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert evaluation.placements[-1].place == "not_measurable"


def test_a_serial_job_failure_with_parity_exit_0_is_not_measurable():
    """Run 39803599874: exit 0, no difference, but the serial suite failed one test (and the shadow the same one)."""
    failed = run(20, day=2, hour=5, **serial_failed_parity_clean_fields())
    assert failed.verdict["exit"] == 0 and not any(failed.verdict["differences"].values())
    evaluation = evaluate(clean_window() + [failed],
                          job_evidence=lambda _run: job_evidence("failure", "success"))
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert evaluation.placements[-1].place == "not_measurable"
    assert evaluation.placements[-1].reasons == ["the serial reference job concluded failure"]


def test_a_serial_job_failure_and_a_parity_difference_still_block():
    """Never more: a real difference keeps a run dirty even when the serial reference job also failed."""
    dirty = run(20, day=2, hour=5, name="heavy-differences", exit=1, verdict="differences",
                reference={"duration_s": 900, "exit": 0, "job_result": "failure", "result": "present"})
    evaluation = evaluate(clean_window() + [dirty], job_evidence=lambda _run: job_evidence("failure", "success"))
    assert failed(evaluation) == {"every_run_clean"}
    assert evaluation.placements[-1].place == "in_window"
    assert evaluation.placements[-1].reasons[0].startswith("exit 1 (differences) [outcome_class_differs: 2]")


def test_a_shadow_job_that_failed_without_a_shutdown_still_blocks():
    shadow_failed = run(20, day=2, hour=5, **shadow_shutdown_fields())
    evaluation = evaluate(clean_window() + [shadow_failed],
                          job_evidence=lambda _run: job_evidence("success", "failure"))
    assert failed(evaluation) == {"every_run_clean", "suite_wall_time"}
    assert evaluation.placements[-1].place == "in_window"
    assert "step shadow.job=failure" in evaluation.placements[-1].reasons


def test_an_annotation_read_that_fails_is_not_a_shutdown():
    """Fail-closed: a read that could not answer never becomes the cause that sets a run aside."""
    shadow_failed = run(20, day=2, hour=5, **shadow_shutdown_fields())
    problem = "gh api repos/o/r/check-runs/12/annotations: HTTP 502"
    evaluation = evaluate(clean_window() + [shadow_failed],
                          job_evidence=lambda _run: job_evidence("success", "failure", (), problem))
    assert failed(evaluation) == {"every_run_clean", "suite_wall_time"}
    assert evaluation.placements[-1].place == "in_window"
    assert f"evidence: {problem}" in evaluation.placements[-1].reasons


@pytest.mark.parametrize(("message", "shutdown"), [
    ("The runner has received a shutdown signal. This can happen when the runner service is stopped", True),
    ("The operation was canceled.", True),
    ("Process completed with exit code 1.", False),
], ids=["runner-shutdown", "canceled-annotation", "step-failed"])
def test_only_a_shutdown_annotation_counts(message, shutdown):
    assert hist.is_shutdown_annotation(message) is shutdown


def test_the_profile_a_jobs_are_taken_from_the_runs_job_list():
    payload = {"total_count": 3, "jobs": [
        {"id": 1, "name": "Lint Patterns (silent-except + atomic-write)", "conclusion": "success"},
        {"id": 2, "name": "Profile A (doctor + core tests)", "conclusion": "failure"},
        {"id": 3, "name": "Profile A (xdist shadow)", "conclusion": "success"},
    ]}
    assert hist.profile_a_jobs(payload) == {"reference": {"id": 2, "conclusion": "failure"},
                                            "shadow": {"id": 3, "conclusion": "success"}}
    assert hist.profile_a_jobs({"total_count": 0, "jobs": []}) == {}
    assert hist.profile_a_jobs({"jobs": "not a list"}) is None


def test_a_window_of_clean_and_not_measurable_runs_holds_once_the_clean_runs_meet_the_counts():
    """The set-aside runs are not evidence either way: the ten clean runs alone satisfy the rule."""
    shut_down = [run(20, day=3, hour=1, **shadow_shutdown_fields()),
                 run(21, day=3, hour=2, **reference_shutdown_fields())]
    evidence = {"1020": job_evidence("success", "failure", ("shadow",)),
                "1021": job_evidence("failure", "success", ("reference",))}
    evaluation = evaluate(clean_window() + shut_down, job_evidence=lambda run_: evidence[run_.run_id])
    assert evaluation.holds, [vars(c) for c in evaluation.conditions]
    assert len(evaluation.window) == 10
    assert [p.place for p in evaluation.placements[-2:]] == ["not_measurable", "not_measurable"]
    assert evaluation.placements[-1].run.head_branch == "dispatch/x", "the newest run is set aside, not the latest"


def test_the_table_lists_a_not_measurable_run_and_its_reason():
    shutdown = run(20, day=2, hour=5, **shadow_shutdown_fields())
    evaluation = evaluate(clean_window() + [shutdown],
                          job_evidence=lambda _run: job_evidence("success", "failure", ("shadow",)))
    text = hist.render(evaluation, HEADER)
    assert "set aside (not measurable, option A):" in text
    assert "run 1020: a runner shutdown signal in the xdist shadow job" in text
    document = json.loads(hist.as_json(evaluation, HEADER))
    assert document["runs"][-1]["place"] == "not_measurable"


# --- a verdict that does not read as one is no verdict --------------------------------------------------------


def test_a_verdict_without_a_step_summary_does_not_count_as_clean():
    """A verdict that leaves step_failures out proves nothing about its steps; it held with a failed shadow phase."""
    runs = clean_window()
    del runs[4].verdict["step_failures"]
    runs[4].verdict["shadow"]["steps"] = {"parallel": "failure"}
    evaluation = evaluate(runs)
    assert not evaluation.holds
    assert evaluation.placements[4].place == "no_verdict"
    assert evaluation.placements[4].reasons == ["malformed verdict: step_failures is missing"]


@pytest.mark.parametrize(("field", "value"), [
    ("shadow", "fast"),
    ("shadow", {"suite_wall_s": "600"}),
    ("step_failures", "parity.prelude=failure"),
    ("differences", ["outcome_class_differs"]),
    ("invalid_evidence", ["stale_evidence"]),
    ("exit", "0"),
    ("exit", True),
    ("profile", ["heavy"]),
    ("selection_fingerprint", 7),
    ("invalid_evidence", [{"reason": ["stale_evidence"]}]),
    ("invalid_evidence", [{"detail": "no reason"}]),
    ("steps", ["checkout"]),
    ("steps", {"checkout": 1}),
    ("reference", "ok"),
], ids=["shadow-text", "wall-time-text", "step-failures-text", "differences-list", "problems-text", "exit-text",
        "exit-bool", "profile-list", "fingerprint-number", "problem-reason-list", "problem-without-reason",
        "steps-list", "step-outcome-number", "reference-text"])
def test_a_verdict_field_of_the_wrong_type_blocks_without_a_crash(field, value):
    runs = clean_window()
    runs[4].verdict[field] = value
    evaluation = evaluate(runs)
    assert evaluation.placements[4].place == "no_verdict"
    assert evaluation.placements[4].reasons[0].startswith(f"malformed verdict: {field} ")
    assert failed(evaluation) == {"every_run_has_verdict", "distinct_heads"}
    assert "cutover rule: DOES NOT HOLD" in hist.render(evaluation, HEADER)
    assert json.loads(hist.as_json(evaluation, HEADER))["holds"] is False


@pytest.mark.parametrize("field", [name for name, _ in hist.VERDICT_FIELDS])
def test_a_verdict_without_a_field_every_writer_writes_is_no_verdict(field):
    """OI-2147: every writer now writes every field, so a reader can index them; one that lacks one blocks."""
    runs = clean_window()
    del runs[4].verdict[field]
    evaluation = evaluate(runs)
    assert evaluation.placements[4].place in ("no_verdict", "stale_verdict")
    assert not evaluation.holds


@pytest.mark.parametrize("name", ["heavy-parity", "heavy-differences", "light", "parity-step-failed-after-exit-0",
                                  "emergency", "written-heavy-parity"])
def test_every_verdict_the_pipeline_writes_reads_as_one(name):
    assert hist.verdict_shape_problem(fixture(name)) is None


def test_a_verdict_of_another_schema_is_no_verdict():
    runs = clean_window()
    runs[4].verdict["schema"] = "vnx-xdist-parity/2"
    evaluation = evaluate(runs)
    assert evaluation.placements[4].reasons == ["malformed verdict: schema is 'vnx-xdist-parity/2'"]


def test_a_problem_reason_that_is_not_text_never_crashes_the_rule():
    """The note on an unclean verdict joins its problem reasons: a list-valued reason raised TypeError."""
    runs = clean_window() + [run(20, day=2, hour=6, exit=2, verdict="invalid_evidence")]
    runs[-1].verdict["invalid_evidence"] = [{"reason": ["stale_evidence"], "detail": "x"}]
    evaluation = evaluate(runs)
    assert evaluation.placements[-1].place == "no_verdict"
    assert "cutover rule: DOES NOT HOLD" in hist.render(evaluation, HEADER)


# --- the watch list -----------------------------------------------------------------------------------


def test_without_a_watch_list_the_rule_does_not_hold():
    assert failed(evaluate(clean_window(), watch_list=None)) == {"watch_list_closed"}


def test_an_open_watch_list_oi_blocks():
    evaluation = evaluate(clean_window(), watch_list=["OI-2128"], oi_status={"OI-2128": "open"})
    assert failed(evaluation) == {"watch_list_closed"}
    assert "OI-2128=open" in [c for c in evaluation.conditions if c.name == "watch_list_closed"][0].detail


@pytest.mark.parametrize(("status", "holds"), [("done", True), ("deferred", False), ("wontfix", False), (None, False)],
                         ids=["done", "deferred", "wontfix", "absent"])
def test_only_a_done_oi_counts_as_closed(status, holds):
    oi_status = {} if status is None else {"OI-9001": status}
    assert evaluate(clean_window(), watch_list=["OI-9001"], oi_status=oi_status).holds is holds


def test_an_unreadable_ledger_blocks():
    assert failed(evaluate(clean_window(), watch_list=["OI-9001"], oi_status=None)) == {"watch_list_closed"}


def test_read_oi_status_reads_the_ledger_shape(tmp_path):
    ledger = tmp_path / "open_items.json"
    ledger.write_text(json.dumps({"schema_version": "1.0", "items": [{"id": "OI-1", "status": "done"},
                                                                     {"id": "OI-2", "status": "open"}]}), encoding="utf-8")
    assert hist.read_oi_status(ledger) == {"OI-1": "done", "OI-2": "open"}
    assert hist.read_oi_status(tmp_path / "missing.json") is None
    ledger.write_text(json.dumps({"items": 7}), encoding="utf-8")
    assert hist.read_oi_status(ledger) is None, "a ledger whose items are not a list cannot be read"


# --- output -------------------------------------------------------------------------------------------


HEADER = {"repo": "o/r", "workflow": "VNX CI", "window_base": "a" * 40, "window_base_source": "the last D3 merge, given",
          "fingerprint": FP, "fingerprint_source": "given", "watch_list": "none (--no-watch-list)"}


def test_the_table_names_the_attempt_of_each_verdict_and_the_outcome():
    runs = clean_window() + [run(20, day=2, hour=5, attempt=3, verdict_attempt="1")]
    runs[0].attempt = 2
    runs[0].verdict["run_attempt"] = "2"
    text = hist.render(evaluate(runs), HEADER)
    assert "2 (of 2)" in text and "1 (of 3)" in text
    assert "[x] every_verdict_current: run 1020: verdict of attempt 1, not of attempt 3" in text
    assert text.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_table_notes_an_earlier_attempt_it_cannot_read():
    """A re-run replaces the earlier attempt's verdict (overwrite: true): the reader sees which runs carry one."""
    runs = clean_window()
    runs[0].attempt = 2
    runs[0].verdict["run_attempt"] = "2"
    stale = run(20, day=2, hour=5, attempt=3, verdict_attempt="1")
    lines = hist.render(evaluate(runs + [stale]), HEADER).splitlines()
    [current] = [line for line in lines if line.startswith("1000 ")]
    [stale_row] = [line for line in lines if line.startswith("1020 ")]
    assert current.endswith("attempt(s) before 2 not read: the artifact is overwritten")
    assert "not read" not in stale_row, "a stale verdict is named by its own reason"
    assert "watch list: none (--no-watch-list)" in lines


def test_the_table_ends_with_holds_when_the_rule_holds():
    assert hist.render(evaluate(clean_window()), HEADER).rstrip().endswith("cutover rule: HOLDS")


def test_the_table_says_why_the_rule_does_not_hold():
    runs = clean_window() + [run(20, day=2, hour=5, name="heavy-differences")]
    text = hist.render(evaluate(runs, watch_list=None), HEADER)
    assert "[x] every_run_clean: run 1020: exit 1 (differences)" in text
    assert "[x] watch_list_closed: no watch list given" in text
    assert text.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_json_output_carries_every_run_and_condition():
    document = json.loads(hist.as_json(evaluate(clean_window()), HEADER))
    assert document["holds"] is True and len(document["runs"]) == 10
    assert {c["name"] for c in document["conditions"]} >= {"every_run_clean", "distinct_heads", "watch_list_closed"}
    assert document["runs"][0]["verdict_run_attempt"] == "1"


# --- git ancestry on a real repository ---------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history_repo(tmp_path) -> tuple:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas = []
    for n in range(3):
        (repo / "f.txt").write_text(str(n), encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"c{n}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas


def test_git_ancestry_answers_true_false_and_a_missing_commit_is_a_tool_error(history_repo):
    repo, (first, base, head) = history_repo
    assert hist.git_is_ancestor(repo, base, head) is True
    assert hist.git_is_ancestor(repo, base, first) is False
    with pytest.raises(hist.ToolError, match=f"head {'e' * 40}.*git fetch origin {'e' * 40}"):
        hist.git_is_ancestor(repo, base, "e" * 40)


def test_commit_time_is_utc(history_repo):
    repo, shas = history_repo
    assert hist.commit_time(repo, shas[1]).tzinfo is not None
    assert hist.commit_time(repo, "e" * 40) is None


# --- the CLI end to end, with a gh stand-in on PATH ---------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, shutil, sys
spec = json.load(open(os.environ["FAKE_GH_SPEC"]))
args = sys.argv[1:]
if " ".join(args[:2]) in spec.get("raw", {}):
    print(spec["raw"][" ".join(args[:2])])
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "o/r"}))
elif args[:2] == ["run", "list"]:
    print(json.dumps(spec["runs"]))
elif args[:2] == ["run", "download"]:
    source = spec["verdicts"].get(args[2])
    if source is None:
        print("no valid artifacts found to download", file=sys.stderr)
        sys.exit(1)
    if source == "error":
        print("downloading artifact\nHTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    target = args[args.index("--dir") + 1]
    os.makedirs(target, exist_ok=True)
    if source != "empty":
        shutil.copy(source, os.path.join(target, "xdist-parity-verdict.json"))
elif args[:1] == ["api"]:
    path = args[1].split("?")[0]
    if "/actions/runs/" in path and path.endswith("/jobs"):
        key = path.split("/actions/runs/")[1].split("/")[0]
        payload = spec.get("jobs", {}).get(key)
    elif "/check-runs/" in path and path.endswith("/annotations"):
        key = path.split("/check-runs/")[1].split("/")[0]
        payload = spec.get("annotations", {}).get(key)
    else:
        payload = None
    if payload is None:
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    if payload == "error":
        print("gh: HTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    print(payload if isinstance(payload, str) else json.dumps(payload))
else:
    print("unexpected gh call: %r" % (args,), file=sys.stderr)
    sys.exit(3)
'''


def _real_commits() -> tuple:
    """Two real commits of this checkout: the parent (window base) and HEAD (in the window)."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "log", "-2", "--format=%H"], capture_output=True, text=True, check=True)
    head, base = out.stdout.split()
    return base, head


def _cli(tmp_path: Path, runs: list, verdicts: dict, *args: str, raw: dict | None = None,
         jobs: dict | None = None, annotations: dict | None = None) -> subprocess.CompletedProcess:
    """The CLI against the gh stand-in; *raw* maps a gh command ("run list") to the exact text it prints."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    paths = {}
    for run_id, document in verdicts.items():
        paths[run_id] = str(tmp_path / f"verdict-{run_id}.json")
        Path(paths[run_id]).write_text(json.dumps(document), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"runs": runs, "verdicts": paths, "raw": raw or {},
                                "jobs": jobs or {}, "annotations": annotations or {}}), encoding="utf-8")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")},
           "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_SPEC": str(spec)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """Put the gh stand-in on PATH for direct calls; *serve(run_id, source)* sets what a download yields."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    spec_path = tmp_path / "spec.json"
    spec = {"runs": [], "verdicts": {}}

    def serve(run_id: str, source) -> None:
        if isinstance(source, dict):
            path = tmp_path / f"verdict-{run_id}.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            source = str(path)
        elif isinstance(source, bytes):
            path = tmp_path / f"verdict-{run_id}.json"
            path.write_bytes(source)
            source = str(path)
        spec["verdicts"][run_id] = source
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_SPEC", str(spec_path))
    return serve


def _completed(run_id: str, attempt: int = 1) -> "hist.Run":
    return hist.Run(run_id, attempt, "a" * 40, "main", "push", "2099-01-01T00:00:00Z", "completed", "success")


@pytest.mark.parametrize(("source", "problem"), [
    (None, "gh run download exit 1: no valid artifacts found to download"),
    ("error", "gh run download exit 1: HTTP 502: Bad Gateway"),
    ("empty", "the xdist-parity-verdict artifact holds no xdist-parity-verdict.json"),
    (b"{not json", "xdist-parity-verdict.json is not a readable JSON object"),
    (b"[1, 2]", "xdist-parity-verdict.json is not a readable JSON object"),
], ids=["no-artifact", "gh-error", "empty-artifact", "unparsable", "not-an-object"])
def test_a_download_without_a_usable_verdict_names_why_and_caches_nothing(fake_gh, tmp_path, source, problem):
    if source is not None:
        fake_gh("7", source)
    cache = tmp_path / "cache"
    assert hist.download_verdict("o/r", _completed("7"), cache) == (None, problem)
    assert not (cache / "7").exists()
    assert [p.name for p in cache.iterdir()] == []


def test_a_downloaded_verdict_of_another_run_is_returned_for_placement_but_not_cached(fake_gh, tmp_path):
    fake_gh("7", verdict(run_id="8", attempt="1"))
    cache = tmp_path / "cache"
    fetched, problem = hist.download_verdict("o/r", _completed("7"), cache)
    assert fetched["run_id"] == "8" and problem == ""
    assert not (cache / "7").exists()
    assert hist.stale_reason(fetched, _completed("7")) == "verdict of run 8, not of run 7"


def test_a_downloaded_verdict_of_the_latest_attempt_is_cached_with_its_stamp(fake_gh, tmp_path):
    fake_gh("7", verdict(run_id="7", attempt="3"))
    cache = tmp_path / "cache"
    fetched, problem = hist.download_verdict("o/r", _completed("7", attempt=3), cache)
    assert fetched["run_attempt"] == "3" and problem == ""
    assert (cache / "7" / ".attempt").read_text(encoding="utf-8") == "3\n"
    assert json.loads((cache / "7" / "xdist-parity-verdict.json").read_text(encoding="utf-8"))["run_attempt"] == "3"
    assert sorted(p.name for p in (cache / "7").iterdir()) == [".attempt", "xdist-parity-verdict.json"]


def _listed(run_id: int, head: str, created: str, **kw) -> dict:
    return {"databaseId": run_id, "headSha": head, "headBranch": kw.get("branch", "main"), "event": kw.get("event", "push"),
            "status": kw.get("status", "completed"), "conclusion": "success", "createdAt": created, "attempt": kw.get("attempt", 1)}


def test_the_cli_reads_the_verdicts_and_prints_why_the_rule_does_not_hold(tmp_path):
    base, head = _real_commits()
    runs = [_listed(2, head, "2099-01-02T00:00:00Z"), _listed(1, head, "2099-01-01T00:00:00Z", attempt=2),
            _listed(3, head, "2099-01-03T00:00:00Z", status="in_progress")]
    verdicts = {"1": verdict(run_id="1", attempt="2"), "2": verdict("heavy-differences", run_id="2")}
    proc = _cli(tmp_path, runs, verdicts, "--last-d3-merge", base, "--fingerprint", FP, "--no-watch-list")
    assert proc.returncode == 1, proc.stderr
    assert f"window base: {base} (the last D3 merge, given)" in proc.stdout
    assert "2 (of 2)" in proc.stdout and "pending" in proc.stdout
    assert "[x] every_run_clean: run 2: exit 1" in proc.stdout
    assert "[x] distinct_heads: 1 distinct head SHAs" in proc.stdout
    assert proc.stdout.rstrip().endswith("cutover rule: DOES NOT HOLD")


def test_the_cli_computes_the_fingerprint_of_this_checkout_by_default(tmp_path):
    proc = _cli(tmp_path, [], {}, "--last-d3-merge", _real_commits()[0], "--no-watch-list", "--json")
    assert proc.returncode == 1, proc.stderr
    document = json.loads(proc.stdout)
    assert document["fingerprint_source"].startswith("emitted by this checkout: ") and len(document["fingerprint"]) == 64
    assert document["holds"] is False and document["runs"] == []


def test_the_cli_flags_a_list_cut_off_by_the_limit(tmp_path):
    base, head = _real_commits()
    runs = [_listed(n, head, f"2099-01-0{n}T00:00:00Z") for n in (1, 2)]
    verdicts = {"1": verdict(run_id="1"), "2": verdict(run_id="2")}
    proc = _cli(tmp_path, runs, verdicts, "--last-d3-merge", base, "--fingerprint", FP, "--no-watch-list", "--limit", "2")
    assert "[x] run_list_complete" in proc.stdout


@pytest.mark.parametrize(("args", "needle"), [
    (["--no-watch-list"], "the following arguments are required: --last-d3-merge"),
    (["--last-d3-merge", "HEAD"], "one of the arguments --watch-list --no-watch-list is required"),
    (["--last-d3-merge", "HEAD", "--watch-list", "2128"], "'2128' is not an OI id of the form OI-<n>"),
    (["--last-d3-merge", "HEAD", "--watch-list", "OI-1", "--no-watch-list"], "not allowed with argument"),
    (["--last-d3-merge", "HEAD", "--no-watch-list", "--limit", "0"], "argument --limit: 0 is not a whole number of at least 1"),
], ids=["no-d3-merge", "no-watch-list", "oi-without-prefix", "both-watch-list-flags", "limit-0"])
def test_the_inputs_that_are_a_judgment_are_required_flags_exit_2(tmp_path, args, needle):
    """The last D3 merge and the watch list are the operator's to give; the tool never guesses either."""
    proc = _cli(tmp_path, [], {}, *args, "--repo", "o/r", "--fingerprint", FP)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert needle in proc.stderr, proc.stderr


def test_the_help_says_where_the_operator_gets_the_d3_merge_and_the_watch_list(tmp_path):
    proc = _cli(tmp_path, [], {}, "--help")
    assert proc.returncode == 0
    help_text = " ".join(proc.stdout.split())
    assert "vnx deliverable list --objective ci-profile-a-xdist" in help_text
    assert "gh pr view <n> --json mergeCommit --jq .mergeCommit.oid" in help_text
    assert "never a default" in help_text and "ff48a463" in help_text
    assert "open_items_manager.py list" in help_text


def test_the_cli_reads_the_watch_list_status_from_the_ledger_it_is_given(tmp_path):
    base, _ = _real_commits()
    ledger = tmp_path / "open_items.json"
    ledger.write_text(json.dumps({"items": [{"id": "OI-9001", "status": "open"}]}), encoding="utf-8")
    proc = _cli(tmp_path, [], {}, "--last-d3-merge", base, "--fingerprint", FP, "--repo", "o/r",
                "--watch-list", "OI-9001", "--open-items", str(ledger))
    assert proc.returncode == 1, proc.stderr
    assert "watch list: OI-9001" in proc.stdout
    assert "[x] watch_list_closed: not closed: OI-9001=open" in proc.stdout


def test_a_window_base_that_is_no_commit_here_is_a_tool_error_exit_2(tmp_path):
    proc = _cli(tmp_path, [], {}, "--last-d3-merge", "e" * 40, "--repo", "o/r", "--fingerprint", FP, "--no-watch-list")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stderr.startswith(f"xdist_parity_history: window base {'e' * 40} is not a commit in this checkout")


def test_git_failing_inside_the_rule_is_a_tool_error_exit_2_through_main(tmp_path, monkeypatch, capsys):
    """deepseek round 3: evaluate() calls git_is_ancestor, whose ToolError escaped main() as a traceback.

    git answers for the window base, then is gone when the rule asks for
    ancestry: the real _run turns the FileNotFoundError into a ToolError.
    """
    base, head = _real_commits()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    verdict_path = tmp_path / "verdict-1.json"
    verdict_path.write_text(json.dumps(verdict(run_id="1")), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"runs": [_listed(1, head, "2099-01-01T00:00:00Z")],
                                "verdicts": {"1": str(verdict_path)}, "pr": {}}), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_SPEC", str(spec))
    real_run = subprocess.run

    def git_gone_for_ancestry(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["git", "merge-base"]:
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", git_gone_for_ancestry)
    code = hist.main(["--last-d3-merge", base, "--repo", "o/r", "--fingerprint", FP, "--no-watch-list",
                      "--cache-dir", str(tmp_path / "cache")])
    assert code == 2
    assert capsys.readouterr().err.startswith("xdist_parity_history: git is not installed")


_ROW = {"databaseId": 1001, "headSha": "a" * 40, "headBranch": "main", "event": "push", "status": "completed",
        "conclusion": "success", "createdAt": "2099-01-01T00:00:00Z", "attempt": 1}


def _row(**change) -> dict:
    row = {**_ROW, **change}
    return {k: v for k, v in row.items() if v is not None}


@pytest.mark.parametrize(("command", "payload", "needle"), [
    ("run list", "not json at all", "gh run list"),
    ("run list", json.dumps({"runs": []}), "not a list"),
    ("run list", json.dumps(["1001"]), "not an object"),
    ("run list", json.dumps([_row(createdAt=None)]), "createdAt"),
    ("run list", json.dumps([_row(createdAt="yesterday")]), "createdAt"),
    ("run list", json.dumps([_row(attempt="x")]), "attempt"),
    ("run list", json.dumps([_row(attempt=None)]), "attempt"),
    ("run list", json.dumps([_row(databaseId="1001x")]), "databaseId"),
    ("run list", json.dumps([_row(headSha=None)]), "headSha"),
    ("repo view", json.dumps({"name": "r"}), "nameWithOwner"),
], ids=["runs-not-json", "runs-not-a-list", "run-not-an-object", "no-created-at", "created-at-text", "attempt-text",
        "no-attempt", "run-id-text", "no-head", "repo-without-name"])
def test_a_malformed_gh_payload_is_a_tool_error_exit_2(tmp_path, command, payload, needle):
    """deepseek round 2: json.loads, int() and the createdAt parse raised out of main: exit 1 and a traceback."""
    repo = [] if command == "repo view" else ["--repo", "o/r"]
    proc = _cli(tmp_path, [], {}, "--last-d3-merge", _real_commits()[0], *repo, "--fingerprint", FP, "--no-watch-list",
                raw={command: payload})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stderr.startswith("xdist_parity_history: ") and needle in proc.stderr, proc.stderr


# --- the CLI on a window that holds, then the two codex round-1 cases end to end ------------------------------


def _first_parent_window() -> tuple:
    """(base, ten heads newest first): eleven first-parent commits of this checkout, each head a descendant of base."""
    out = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-list", "--first-parent", "--max-count=11", "HEAD"],
                         capture_output=True, text=True, check=True)
    chain = out.stdout.split()
    assert len(chain) == 11, "the CLI window tests need eleven first-parent commits (fetch-depth 0)"
    return chain[10], chain[:10]


def _holding_runs(heads: list) -> tuple:
    """Ten listed runs on ten heads over two days, the latest a push on main, each with its own current verdict."""
    runs = [_listed(k, heads[10 - k], f"2099-01-0{1 if k <= 5 else 2}T{k:02d}:00:00Z",
                    branch="main" if k == 10 else "dispatch/x", event="push" if k == 10 else "pull_request")
            for k in range(1, 11)]
    return runs, {str(k): verdict(run_id=str(k)) for k in range(1, 11)}


def _cli_window(tmp_path: Path, runs: list, verdicts: dict, base: str, cache: Path) -> tuple:
    proc = _cli(tmp_path, runs, verdicts, "--last-d3-merge", base, "--fingerprint", FP, "--no-watch-list",
                "--cache-dir", str(cache), "--json")
    assert proc.returncode in (0, 1), proc.stderr
    document = json.loads(proc.stdout)
    places = {r["run_id"]: r for r in document["runs"]}
    return proc.returncode, document, places


def _failed_names(document: dict) -> set:
    return {c["name"] for c in document["conditions"] if not c["holds"]}


def test_the_cli_holds_on_ten_clean_runs_and_caches_each_verdict_with_its_attempt(tmp_path):
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    cache = tmp_path / "cache"
    code, document, _ = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 0 and document["holds"] is True, document["conditions"]
    assert (cache / "3" / ".attempt").read_text(encoding="utf-8").strip() == "1"


def test_the_cli_blocks_on_a_completed_run_whose_artifact_is_missing(tmp_path):
    """codex round 1, end to end: a newer failed run without an artifact must not leave the window."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append({**_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request"),
                 "conclusion": "failure"})
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, tmp_path / "cache")
    assert code == 1
    assert _failed_names(document) == {"every_run_has_verdict"}
    assert places["11"]["place"] == "no_verdict"
    assert "no valid artifacts found to download" in places["11"]["reasons"][0]


def test_the_cli_does_not_count_or_cache_an_earlier_attempts_verdict(tmp_path):
    """codex round 1, end to end: a re-run whose upload never happened serves the attempt-1 verdict."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    verdicts["11"] = verdict(run_id="11", attempt="1")
    cache = tmp_path / "cache"
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 1
    assert _failed_names(document) == {"every_verdict_current"}
    assert places["11"]["place"] == "stale_verdict" and places["11"]["verdict_run_attempt"] == "1"
    assert not (cache / "11").exists()


def test_a_cache_entry_whose_stamp_and_verdict_attempt_disagree_is_downloaded_again(tmp_path):
    """The entry round 1 could write (stamp 2 over an attempt-1 verdict) is not trusted: attempt 2 is fetched."""
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    verdicts["11"] = verdict("heavy-differences", run_id="11", attempt="2")
    cache = tmp_path / "cache"
    (cache / "11").mkdir(parents=True)
    (cache / "11" / "xdist-parity-verdict.json").write_text(json.dumps(verdict(run_id="11", attempt="1")),
                                                           encoding="utf-8")
    (cache / "11" / ".attempt").write_text("2\n", encoding="utf-8")
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 1
    assert _failed_names(document) == {"every_run_clean"}
    assert places["11"]["place"] == "in_window" and places["11"]["verdict_run_attempt"] == "2"
    cached = json.loads((cache / "11" / "xdist-parity-verdict.json").read_text(encoding="utf-8"))
    assert cached["run_attempt"] == "2"


def test_a_cache_entry_that_proves_the_latest_attempt_is_used_without_a_download(tmp_path):
    base, heads = _first_parent_window()
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, heads[0], "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request", attempt=2))
    cache = tmp_path / "cache"
    (cache / "11").mkdir(parents=True)
    (cache / "11" / "xdist-parity-verdict.json").write_text(json.dumps(verdict(run_id="11", attempt="2")),
                                                           encoding="utf-8")
    (cache / "11" / ".attempt").write_text("2\n", encoding="utf-8")
    code, document, places = _cli_window(tmp_path, runs, verdicts, base, cache)
    assert code == 0, document["conditions"]
    assert places["11"]["place"] == "in_window"


# --- option A end to end: the job list and its check-run annotations are read from gh ---------------------------


def _profile_a_jobs(reference: str, shadow: str) -> dict:
    return {"total_count": 2, "jobs": [
        {"id": 11, "name": "Profile A (doctor + core tests)", "status": "completed", "conclusion": reference},
        {"id": 12, "name": "Profile A (xdist shadow)", "status": "completed", "conclusion": shadow},
    ]}


def test_the_cli_sets_aside_a_run_whose_shadow_job_was_shut_down(tmp_path):
    base, head = _real_commits()
    runs = [_listed(1, head, "2099-01-01T00:00:00Z")]
    verdicts = {"1": verdict("heavy-parity", run_id="1", **shadow_shutdown_fields())}
    proc = _cli(tmp_path, runs, verdicts, "--last-d3-merge", base, "--fingerprint", FP, "--no-watch-list", "--json",
                jobs={"1": _profile_a_jobs("success", "failure")},
                annotations={"12": [{"annotation_level": "failure", "message": "The operation was canceled."}]})
    assert proc.returncode == 1, proc.stderr
    [row] = json.loads(proc.stdout)["runs"]
    assert row["place"] == "not_measurable"
    assert row["reasons"] == ["a runner shutdown signal in the xdist shadow job"]


def test_the_cli_keeps_a_run_with_a_failed_annotation_read_dirty(tmp_path):
    """Fail-closed end to end: the annotation request fails, so the shadow job failure is not a shutdown."""
    base, head = _real_commits()
    runs = [_listed(1, head, "2099-01-01T00:00:00Z")]
    verdicts = {"1": verdict("heavy-parity", run_id="1", **shadow_shutdown_fields())}
    proc = _cli(tmp_path, runs, verdicts, "--last-d3-merge", base, "--fingerprint", FP, "--no-watch-list", "--json",
                jobs={"1": _profile_a_jobs("success", "failure")}, annotations={"12": "error"})
    assert proc.returncode == 1, proc.stderr
    [row] = json.loads(proc.stdout)["runs"]
    assert row["place"] == "in_window"
    assert any("evidence:" in reason and "HTTP 502" in reason for reason in row["reasons"]), row["reasons"]


# --- codex round 2 on #2105: ancestry only from complete history -----------------------------------------------


def _commit(repo: Path, text: str) -> str:
    (repo / "f.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", text)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def split_history(tmp_path) -> tuple:
    """(complete, shallow, base, ten heads newest first, failed): one history, once whole and once shallow.

    main is base -> h1..h10; branch failed continues h10 -> s1 -> s2 -> s3.
    The shallow clone holds main eleven deep (base included) and failed one
    deep: every commit is present, but the path from s3 to base is cut.
    """
    complete = tmp_path / "complete"
    complete.mkdir()
    _git(complete, "init", "-q", "-b", "main")
    _commit(complete, "before")
    base = _commit(complete, "base")
    heads = [_commit(complete, f"h{n}") for n in range(1, 11)][::-1]
    _git(complete, "checkout", "-q", "-b", "failed")
    failed = [_commit(complete, f"s{n}") for n in range(1, 4)][-1]
    _git(complete, "checkout", "-q", "main")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "11", "--branch", "main", f"file://{complete}", str(shallow))
    _git(shallow, "fetch", "-q", "--depth", "1", "origin", "failed")
    return complete, shallow, base, heads, failed


def _with_failed_descendant(heads: list, failed: str) -> tuple:
    """Ten clean runs that hold, plus run 11: an exit-1 verdict on *failed*, a descendant of the window base."""
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, failed, "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request"))
    verdicts["11"] = verdict("heavy-differences", run_id="11")
    return runs, verdicts


def _main_on(repo: Path, base: str, runs: list, verdicts: dict, tmp_path: Path, monkeypatch, capsys) -> tuple:
    """(exit, stdout, stderr, every command run) of main() in process with *repo* as the checkout.

    The script resolves its checkout from its own path; here that resolution
    is pointed at *repo*, and gh is the stand-in on PATH.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    paths = {}
    for run_id, document in verdicts.items():
        paths[run_id] = str(tmp_path / f"verdict-{run_id}.json")
        Path(paths[run_id]).write_text(json.dumps(document), encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"runs": runs, "verdicts": paths}), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_SPEC", str(spec))
    monkeypatch.setattr(importlib.import_module("project_root"), "resolve_project_root", lambda _caller: repo)
    commands = []
    real_run = subprocess.run

    def recorded(cmd, *args, **kwargs):
        commands.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    code = hist.main(["--last-d3-merge", base, "--repo", "o/r", "--fingerprint", FP, "--no-watch-list",
                      "--cache-dir", str(tmp_path / "cache"), "--json"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err, commands


def _fetches(commands: list) -> list:
    return [c for c in commands if c[:2] == ["git", "fetch"]]


def test_a_shallow_clone_is_refused_not_read_as_a_window_that_holds(split_history, tmp_path, monkeypatch, capsys):
    """codex round 2, its reproduction: ten clean runs and one failed descendant, in a shallow clone.

    git answers exit 1 for the failed run's head although it descends from
    the base, so the old reading placed it before_window and printed HOLDS.
    """
    _, shallow, base, heads, failed = split_history
    assert subprocess.run(["git", "-C", str(shallow), "merge-base", "--is-ancestor", base, failed]).returncode == 1
    runs, verdicts = _with_failed_descendant(heads, failed)
    code, out, err, commands = _main_on(shallow, base, runs, verdicts, tmp_path, monkeypatch, capsys)
    assert code == 2, out
    assert out == ""
    assert err.startswith("xdist_parity_history: this checkout is a shallow clone"), err
    assert "git fetch --unshallow" in err and "Traceback" not in err
    assert _fetches(commands) == []


def test_the_same_runs_on_complete_history_block_on_the_failed_descendant(split_history, tmp_path, monkeypatch,
                                                                          capsys):
    complete, _, base, heads, failed = split_history
    runs, verdicts = _with_failed_descendant(heads, failed)
    code, out, err, _ = _main_on(complete, base, runs, verdicts, tmp_path, monkeypatch, capsys)
    assert code == 1, err
    document = json.loads(out)
    assert _failed_names(document) == {"every_run_clean"}
    places = {r["run_id"]: r for r in document["runs"]}
    assert places["11"]["place"] == "in_window" and places["11"]["reasons"][0].startswith("exit 1")


def test_a_run_head_that_is_no_commit_here_is_a_tool_error_exit_2(split_history, tmp_path, monkeypatch, capsys):
    """A head the checkout lacks is named with the fetch that brings it; the tool does not fetch it."""
    complete, _, base, heads, _ = split_history
    runs, verdicts = _holding_runs(heads)
    runs.append(_listed(11, "e" * 40, "2099-01-02T08:30:00Z", branch="dispatch/y", event="pull_request"))
    verdicts["11"] = verdict(run_id="11")
    code, out, err, commands = _main_on(complete, base, runs, verdicts, tmp_path, monkeypatch, capsys)
    assert code == 2, out
    assert out == ""
    assert err.startswith("xdist_parity_history: 1 commit(s) the rule needs are not in this checkout: "
                          f"head of run 11 {'e' * 40}"), err
    assert f"git fetch origin {'e' * 40}" in err and "Traceback" not in err
    assert _fetches(commands) == []


def test_git_ancestry_does_not_read_exit_1_of_a_shallow_clone_as_not_an_ancestor(split_history):
    """Exit 0 proves descent in any history; exit 1 proves nothing when the path can be cut."""
    _, shallow, base, heads, failed = split_history
    assert hist.git_is_ancestor(shallow, base, heads[0]) is True
    with pytest.raises(hist.ToolError, match="shallow clone.*git fetch --unshallow"):
        hist.git_is_ancestor(shallow, base, failed)
