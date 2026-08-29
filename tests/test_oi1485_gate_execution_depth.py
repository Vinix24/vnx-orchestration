"""A gate run that investigated nothing is not a PASS (OI-1485).

Measured on PR #1707: two codex_gate runs on the same head sha under the same
contract_hash — same instruction, same diff — where one took 16 shell calls,
read 11 files, spent 239992 input tokens and found a real defect, and the
other took none, spent 18219, and returned clean in 14 seconds. Its own
residual_risk said so: "The review is limited to the provided diff."

Every one of the seven headless-review invariants held for both. Nothing in
that list asks whether the gate did any work, so the shallow PASS was
indistinguishable from the real one on the record a merge decision reads.

Two things are asserted here. The record must carry what the run DID, not only
what it concluded; and a run that reached a verdict without a single
investigative action must not be recorded as a completed review.
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

from gate_artifacts import materialize_artifacts

# The two shapes, as codex actually emits them. The degenerate run's single
# item.completed is an agent_message: the verdict itself. Emitting a verdict is
# not evidence of having looked at anything.
DEGENERATE_STREAM = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "01a0-dead-beef"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message",
        "text": "No blocking issues found. The review is limited to the provided diff.",
    }}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 18219, "output_tokens": 602}}),
]) + "\n"

REAL_STREAM = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "01a0-live-0001"}),
        json.dumps({"type": "turn.started"}),
    ]
    + [
        json.dumps({"type": "item.completed", "item": {
            "id": f"item_{i}", "type": "command_execution",
            "command": f"/bin/zsh -lc \"sed -n '1,200p' scripts/lib/mod_{i}.py\"",
            "exit_code": 0, "status": "completed",
        }})
        for i in range(16)
    ]
    + [
        json.dumps({"type": "item.completed", "item": {
            "id": "item_99", "type": "agent_message",
            "text": "One advisory: heredoc termination is looser than Bash.",
        }}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 239992, "output_tokens": 11728}}),
    ]
) + "\n"

# A lane that emits no event stream at all. kimi_gate and glm_gate reports
# carry prose only — measured, both had zero JSON event lines.
NO_STREAM = (
    "## Review\n\n"
    "No blocking findings. Two advisories below.\n\n"
    "1. scripts/lib/mod.py:41 — the retry loop re-reads the config each pass;\n"
    "   hoisting it out is cheaper and removes a window where a mid-run edit\n"
    "   changes behaviour between attempts.\n"
    "2. tests/test_mod.py:12 — the fixture builds its own tmp dir instead of\n"
    "   taking tmp_path, so a failed run leaves it behind.\n\n"
    "Residual risk: the diff was reviewed against main at 17de88de.\n"
)


@pytest.fixture
def gate_dirs(tmp_path):
    requests = tmp_path / "state" / "review_gates" / "requests"
    results = tmp_path / "state" / "review_gates" / "results"
    reports = tmp_path / "unified_reports"
    for d in (requests, results, reports):
        d.mkdir(parents=True, exist_ok=True)
    return requests, results, reports


def _request(reports_dir: Path, gate: str = "codex_gate") -> dict:
    return {
        "gate": gate,
        "pr_id": "1707",
        "pr_number": 1707,
        "branch": "fix/oi1482-bin-vnx-command-parser",
        "report_path": str(reports_dir / f"{gate}-report.md"),
        "contract_hash": "088a30754169bb91",
        "dispatch_id": "20260828-t0-alpha-oi1482-parser",
    }


def _run(gate_dirs, stdout: str, gate: str = "codex_gate", duration: float = 14.0):
    requests, results, reports = gate_dirs
    return materialize_artifacts(
        gate=gate, pr_number=1707, pr_id="1707", stdout=stdout,
        request_payload=_request(reports, gate), duration_seconds=duration,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )


def test_a_run_that_investigated_nothing_is_not_recorded_as_completed(gate_dirs):
    """The core of OI-1485: 14 seconds and no actions must not read as a review."""
    payload = _run(gate_dirs, DEGENERATE_STREAM)

    assert payload["status"] != "completed", (
        "a gate run that took zero investigative actions was recorded as a "
        "completed review — every headless invariant passes on this record, so "
        "nothing downstream can tell it from a real PASS"
    )
    assert payload["status"] == "unavailable", (
        "a degenerate run is an absence of evidence, like a timeout — not a "
        f"rejected PR; got status={payload['status']!r}"
    )
    assert payload.get("required_reruns") == ["codex_gate"], (
        "the run must be bought again, not silently accepted"
    )


def test_the_refusal_carries_the_counts_that_justify_it(gate_dirs):
    """A record that says "degenerate" without the numbers cannot be checked."""
    payload = _run(gate_dirs, DEGENERATE_STREAM)

    depth = payload.get("execution_depth")
    assert isinstance(depth, dict), (
        f"the refusal record carries no execution_depth: {payload.get('reason')!r}"
    )
    assert depth["parsed"] is True
    assert depth["investigative_actions"] == 0
    assert depth["shell_calls"] == 0
    assert depth["agent_messages"] == 1
    assert depth["input_tokens"] == 18219
    assert "18219" in payload["reason_detail"], (
        "reason_detail must name the measurement, not just the verdict"
    )


def test_the_refused_runs_report_is_kept_as_evidence(gate_dirs):
    """Deleting it would repeat the evidence loss the overwrite guard prevents."""
    _requests, _results, reports = gate_dirs
    payload = _run(gate_dirs, DEGENERATE_STREAM)

    report = reports / "codex_gate-report.md"
    assert report.exists(), "the refused run's report was deleted"
    assert payload.get("degenerate_report_path") == str(report), (
        "the record must point at the report of the run it refused, or the "
        "evidence is on disk with nothing referring to it"
    )
    assert payload["report_path"] == "", (
        "an unavailable record must not claim a report as gate evidence"
    )


def test_a_real_run_is_recorded_with_what_it_did(gate_dirs):
    payload = _run(gate_dirs, REAL_STREAM, duration=227.0)

    assert payload["status"] == "completed"
    depth = payload.get("execution_depth")
    assert isinstance(depth, dict), (
        "the result record carries no execution_depth — the record still says "
        "the gate ran without saying whether it looked"
    )
    assert depth["investigative_actions"] == 16
    assert depth["shell_calls"] == 16
    assert depth["files_read"] == 16
    assert depth["input_tokens"] == 239992


def test_a_lane_that_emits_no_event_stream_is_unaffected(gate_dirs):
    """Fail open on measurement, closed only on measured emptiness.

    kimi_gate and glm_gate emit no events. Unmeasured must never be read as
    "did nothing" — that would take every unmeasurable lane offline.
    """
    payload = _run(gate_dirs, NO_STREAM, gate="kimi_gate")

    assert payload["status"] == "completed", (
        "a lane without an event stream was refused as degenerate — "
        "unparsed depth is not measured emptiness"
    )
    depth = payload.get("execution_depth")
    assert isinstance(depth, dict)
    assert depth["parsed"] is False


def test_depth_measurement_matches_the_two_measured_shapes():
    """Unit-level, against the shapes taken from the real PR #1707 reports."""
    from gate_depth import is_degenerate, measure_execution_depth

    shallow = measure_execution_depth(DEGENERATE_STREAM)
    assert shallow.parsed is True
    assert shallow.investigative_actions == 0
    assert shallow.agent_messages == 1
    assert is_degenerate(shallow) is True

    real = measure_execution_depth(REAL_STREAM)
    assert real.parsed is True
    assert real.investigative_actions == 16
    assert real.files_read == 16
    assert is_degenerate(real) is False

    unmeasured = measure_execution_depth(NO_STREAM)
    assert unmeasured.parsed is False
    assert is_degenerate(unmeasured) is False, (
        "an unmeasured run must never be judged degenerate"
    )


def test_a_truncated_stream_still_yields_what_arrived():
    """A malformed line must not abort the count of the lines that parsed."""
    from gate_depth import measure_execution_depth

    truncated = (
        json.dumps({"type": "thread.started"}) + "\n"
        + json.dumps({"type": "item.completed", "item": {
            "id": "a", "type": "command_execution", "command": "cat x.py"}}) + "\n"
        + '{"type": "item.completed", "item": {"id": "b", "type": "comm\n'
    )
    depth = measure_execution_depth(truncated)
    assert depth.parsed is True
    assert depth.investigative_actions == 1
    assert depth.files_read == 1


# --------------------------------------------------------------------------
# Item-type classification. The permissive default has to sit where being
# wrong is safe, and for a degeneracy check that is nowhere.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("alias", ["assistant_message", "output_text", "reasoning"])
def test_a_message_alias_is_not_an_investigative_action(alias):
    """kimi_gate blocking finding on baabcec4.

    Classifying by "everything that is not a known message is work" reads an
    unknown MESSAGE alias as work. A run that took zero tools while emitting
    one would then clear the floor — the exact shape the floor exists to catch,
    let through by the alias it happened not to know.
    """
    from gate_depth import is_degenerate, measure_execution_depth

    stream = "\n".join([
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "item_0", "type": alias, "text": "No blocking issues found."}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 18219, "output_tokens": 602}}),
    ]) + "\n"

    depth = measure_execution_depth(stream)
    assert depth.investigative_actions == 0, (
        f"{alias!r} counted as an investigative action"
    )
    assert depth.agent_messages == 1
    assert depth.unrecognised_item_types == ()
    assert is_degenerate(depth) is True, (
        f"a zero-tool run emitting {alias!r} cleared the degeneracy floor"
    )


@pytest.mark.parametrize("work_type", ["command_execution", "file_change", "web_search"])
def test_the_measured_work_types_all_count(work_type):
    """The three that actually occur in this store, plus command_execution."""
    from gate_depth import is_degenerate, measure_execution_depth

    stream = "\n".join([
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "w", "type": work_type, "command": "sed -n '1,40p' x.py"}}),
    ]) + "\n"

    depth = measure_execution_depth(stream)
    assert depth.investigative_actions == 1
    assert depth.unrecognised_item_types == ()
    assert is_degenerate(depth) is False


def test_an_unknown_item_type_suspends_the_judgement_and_is_named():
    """Neither bucket: the count becomes a floor, not a measurement.

    Refusing on it would be refusing on something not measured — the one thing
    this check must never do (the same rule that keeps stream-less lanes
    working). Counting it as work would restore the hole. So it is recorded,
    named, and the judgement is suspended.
    """
    from gate_depth import is_degenerate, measure_execution_depth

    stream = "\n".join([
        json.dumps({"type": "thread.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "u", "type": "some_future_type", "detail": "?"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900}}),
    ]) + "\n"

    depth = measure_execution_depth(stream)
    assert depth.investigative_actions == 0
    assert depth.unrecognised_item_types == ("some_future_type",), (
        "an unclassifiable item type vanished from the record — a reader "
        "cannot tell a clean zero from an unmeasured one"
    )
    assert is_degenerate(depth) is False
