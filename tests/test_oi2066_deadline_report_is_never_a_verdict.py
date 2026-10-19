"""OI-2066 round 3: a report the lane's own deadline cut off is never read for a verdict.

codex_gate on 4ac2669a (PR #2060): since round 1 the lane's own deadline
fires 60 s before the outer timeout, so the lane writes its own report, and
round 2 made that report carry ``deadline_exceeded: true``. The standalone
``scripts/kimi_gate.py`` and ``scripts/glm_gate.py`` never looked at that
field: a cut-off report holding a partial pass block booked ``status=pass``
and exited 0. On main the outer timeout killed the run first and the gate
booked ``unavailable``, so the PR had turned an unfinished review into a pass.

The fix moves the runner's reader to ``gate_lane_contract.lane_deadline_detail``
and every reader that extracts a verdict from a lane report calls it first:
both standalone gates (live and ``--reprocess``), the companion-report search,
the plan-gate seat reader, its verdict re-extraction and the tiebreaker.

Every test drives the real function with the dispatcher patched; nothing starts
a real kimi, glm, deepseek or claude. Module-level imports use only names that
exist before the fix, so on the old code these tests fail on their assertions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier
import gate_lane_contract
import gate_report_recovery
import gate_runner
import glm_gate
import kimi_gate
import plan_gate_panel as pgp
import plan_gate_tiebreaker as pgt
from review_seat_support import open_pr_in_ledger

_PR = "2060"
_HEAD_SHA = "4ac2669a145d38087058b886a2b8bafc30fc8a03"
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

# What a lane cut off mid-review leaves behind: an early pass block the model
# wrote before it went on reading, then text that stops mid-sentence.
_PARTIAL_PASS_BODY = (
    "First pass over the diff: the files I read so far look fine.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n\n"
    "Now reading scripts/lib/provider_dispatch.py for the case where the"
)

_COMPLETE_PASS_BODY = (
    "Reviewed the whole diff, no blocking issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

# The two shapes the lanes write on a deadline kill (round 2,
# test_oi2066_lane_deadline_signal.py): the kimi drainer exits 1 with its own
# failure_reason, the claude harness (glm, deepseek) is killed with SIGTERM.
_KILL_SHAPE = {
    "kimi_gate": {"exit_code": 1, "failure_reason": "total deadline 900s exceeded"},
    "glm_gate": {"exit_code": -15, "failure_reason": None},
}


def _lane_report(
    dispatch_id: str, body: str, *, exit_code: int = 0, duration: float = 412.0,
    failure_reason: "str | None" = None, deadline_exceeded: "str | None" = None,
    deadline_seconds: "float | None" = None,
) -> str:
    """A governed lane report as provider_dispatch writes it. The deadline pair
    is present only when given, as on a real report."""
    lines = [
        "---",
        f"dispatch_id: {dispatch_id}",
        "provider: kimi",
        f"exit_code: {exit_code}",
        f"duration_seconds: {duration}",
    ]
    if failure_reason:
        lines.append(f"failure_reason: '{failure_reason}'")
    if deadline_exceeded is not None:
        lines.append(f"deadline_exceeded: {deadline_exceeded}")
    if deadline_seconds is not None:
        lines.append(f"deadline_seconds: {deadline_seconds}")
    lines.append("---")
    return "\n".join(lines) + f"\n\n# Dispatch {dispatch_id}\n\n## Response\n\n" + body


def _cut_off_report(gate: str, dispatch_id: str, body: str = _PARTIAL_PASS_BODY) -> str:
    shape = _KILL_SHAPE[gate]
    return _lane_report(
        dispatch_id, body, exit_code=shape["exit_code"], duration=900.4,
        failure_reason=shape["failure_reason"], deadline_exceeded="true", deadline_seconds=900.0,
    )


@pytest.fixture
def gate_env(monkeypatch):
    """No inherited model override and no gh: the gate runs on its defaults and
    the PR identity comes from the stubs below."""
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)


def _stub_pr(module, monkeypatch) -> None:
    monkeypatch.setattr(module, "_get_diff", lambda pr_arg, diff_file: _DIFF)
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "dispatch/oi2066-r3")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: _HEAD_SHA)


def _result_path(data_dir: Path, gate: str) -> Path:
    return data_dir / "state" / "review_gates" / "results" / f"pr-{_PR}-{gate}.json"


def _run_live(module, tmp_path, monkeypatch, report_for):
    """Run ``module.main`` for a real PR. The dispatcher double writes the
    report where the governed lane writes it, then returns its text."""
    data_dir = tmp_path / "data"
    _stub_pr(module, monkeypatch)

    def factory(*_args, **_kwargs):
        def dispatch(provider, model_arg, instruction, dispatch_id):
            text = report_for(dispatch_id)
            reports = data_dir / "unified_reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / f"{dispatch_id}.md").write_text(text, encoding="utf-8")
            return text
        return dispatch

    monkeypatch.setattr(module, "_make_default_dispatcher", factory)
    # R1: the gate request that starts a governed run opens the PR in the round ledger.
    open_pr_in_ledger(data_dir / "state", _PR)
    rc = module.main(["--pr", _PR, "--data-dir", str(data_dir)])
    record = json.loads(_result_path(data_dir, module.__name__).read_text(encoding="utf-8"))
    return rc, record, data_dir


_GATES = pytest.mark.parametrize("module", [kimi_gate, glm_gate], ids=["kimi_gate", "glm_gate"])


# ---------------------------------------------------------------------------
# 1. The standalone gates: a cut-off report books timeout, never pass
# ---------------------------------------------------------------------------


@_GATES
class TestStandaloneGateRefusesACutOffReport:
    def test_a_partial_pass_in_a_cut_off_report_books_timeout(
        self, module, tmp_path, monkeypatch, gate_env,
    ):
        """codex's reproduction on 4ac2669a, both gates: pass recorded, exit 0."""
        gate = module.__name__
        rc, record, _data_dir = _run_live(
            module, tmp_path, monkeypatch, lambda did: _cut_off_report(gate, did),
        )
        assert record["status"] != "pass"
        assert (record["status"], record["reason"]) == ("unavailable", "timeout")
        assert rc != 0
        assert rc == 1, "1 is the gate's exit code for unavailable; 2 would read as a review fail"
        assert record["contract_hash"] == ""
        assert record["blocking_findings"] == []
        # The detail names the lane deadline and is built from numbers only.
        assert "deadline of 900 s" in record["residual_risk"]
        assert "provider_dispatch.py" not in record["residual_risk"]
        assert "timeout" in record["summary"]
        assert "NOT a review fail" in record["summary"]

    def test_a_cut_off_report_never_clears_the_merge_door(
        self, module, tmp_path, monkeypatch, gate_env,
    ):
        gate = module.__name__
        _rc, _record, data_dir = _run_live(
            module, tmp_path, monkeypatch, lambda did: _cut_off_report(gate, did),
        )
        door = closure_verifier.check_review_gate_for_merge(
            _PR, gate, data_dir / "state" / "review_gates" / "results",
            seats=[gate],  # one-seat project (OI-2058 R5b: the door needs every seat of the stack)
        )
        assert door["verdict"] != "GO", door["message"]

    def test_a_relabeled_partial_verdict_is_not_read_either(
        self, module, tmp_path, monkeypatch, gate_env,
    ):
        """The plan-reviewer fence is the gate's second verdict source; a cut-off
        report holding one is the same unfinished review."""
        gate = module.__name__
        body = (
            "Partial review so far.\n\n```vnx-plan-verdict\n"
            '{"verdict": "pass", "blocking_findings": [], "rationale": "so far fine"}\n'
            "```\n\nContinuing with the next file, where"
        )
        rc, record, _data_dir = _run_live(
            module, tmp_path, monkeypatch, lambda did: _cut_off_report(gate, did, body),
        )
        assert (record["status"], record["reason"]) == ("unavailable", "timeout")
        assert rc == 1

    def test_a_complete_report_without_the_field_still_passes(
        self, module, tmp_path, monkeypatch, gate_env,
    ):
        rc, record, data_dir = _run_live(
            module, tmp_path, monkeypatch, lambda did: _lane_report(did, _COMPLETE_PASS_BODY),
        )
        assert (rc, record["status"], record["reason"]) == (0, "pass", "verdict")
        door = closure_verifier.check_review_gate_for_merge(
            _PR, module.__name__, data_dir / "state" / "review_gates" / "results",
            seats=[module.__name__],  # one-seat project (OI-2058 R5b: the door needs every seat of the stack)
        )
        assert door["verdict"] == "GO", door["message"]

    @pytest.mark.parametrize("value", ["false", "'true'", "1"], ids=["false", "string", "int"])
    def test_only_a_yaml_true_is_the_signal(self, module, tmp_path, monkeypatch, gate_env, value):
        """The same rule gate_runner applies: anything but YAML ``true`` keeps
        the old reading."""
        rc, record, _data_dir = _run_live(
            module, tmp_path, monkeypatch,
            lambda did: _lane_report(
                did, _COMPLETE_PASS_BODY, deadline_exceeded=value, deadline_seconds=900.0,
            ),
        )
        assert (rc, record["status"]) == (0, "pass")

    def test_reprocess_books_a_cut_off_report_as_timeout(
        self, module, tmp_path, monkeypatch, gate_env,
    ):
        """``--reprocess`` reads the report already on disk and runs the same
        pipeline. With an original result record for this dispatch on the
        current head, the old code formalized the partial pass."""
        gate = module.__name__
        data_dir = tmp_path / "data"
        dispatch_id = f"{gate.replace('_', '-')}-pr{_PR}-1792000000"
        reports = data_dir / "unified_reports"
        reports.mkdir(parents=True)
        (reports / f"{dispatch_id}.md").write_text(
            _cut_off_report(gate, dispatch_id), encoding="utf-8",
        )
        results = data_dir / "state" / "review_gates" / "results"
        results.mkdir(parents=True)
        _result_path(data_dir, gate).write_text(
            json.dumps({"dispatch_id": dispatch_id, "commit_sha": _HEAD_SHA}), encoding="utf-8",
        )
        _stub_pr(module, monkeypatch)

        rc = module.main(["--pr", _PR, "--reprocess", dispatch_id, "--data-dir", str(data_dir)])
        record = json.loads(_result_path(data_dir, gate).read_text(encoding="utf-8"))

        assert record["status"] != "pass"
        assert (record["status"], record["reason"]) == ("unavailable", "timeout")
        assert rc == 1


# ---------------------------------------------------------------------------
# 2. One reader, not a copy per consumer
# ---------------------------------------------------------------------------


def test_every_verdict_reader_uses_the_one_lane_deadline_reader():
    reader = getattr(gate_lane_contract, "lane_deadline_detail", None)
    assert callable(reader), "gate_lane_contract carries the one reader"
    for module in (gate_runner, kimi_gate, glm_gate, gate_report_recovery, pgp):
        assert getattr(module, "lane_deadline_detail", None) is reader, module.__name__


def test_the_reader_reads_the_lane_statement_and_nothing_else():
    reader = getattr(gate_lane_contract, "lane_deadline_detail", None)
    assert callable(reader)
    cut_off = _cut_off_report("glm_gate", "glm-gate-pr2060-1")
    assert reader(cut_off, 1.0) == (
        "the lane reported its own deadline of 900 s stopped the run; it ran 900 s "
        "(report exit_code=-15)"
    )
    # No duration in the report and no elapsed given: the line leaves it out.
    bare = "---\ndispatch_id: x\ndeadline_exceeded: true\n---\n\nbody\n"
    assert reader(bare) == "the lane reported its own deadline stopped the run"
    assert reader(bare, 61.0) == "the lane reported its own deadline stopped the run; it ran 61 s"
    for text in ("", "no frontmatter at all", _lane_report("x", _COMPLETE_PASS_BODY),
                 "---\n: [unclosed\n---\n"):
        assert reader(text, 900.0) is None


# ---------------------------------------------------------------------------
# 3. The companion-report search skips a cut-off sibling report
# ---------------------------------------------------------------------------


def test_the_companion_search_skips_a_cut_off_report(tmp_path):
    """A sibling gate's cut-off report on the same PR, in the run's window,
    carries a partial pass. It is no evidence for this gate's run."""
    reports = tmp_path / "unified_reports"
    reports.mkdir()
    sibling = reports / f"glm-gate-pr{_PR}-1792000000.md"
    sibling.write_text(_cut_off_report("glm_gate", sibling.stem), encoding="utf-8")
    mtime = sibling.stat().st_mtime

    found = gate_report_recovery.find_recovery_candidate(
        reports, pr_id=_PR, exclude_name=f"kimi-gate-pr{_PR}-1792000000.md",
        window_start=mtime - 5, window_end=mtime + 5,
    )
    assert found is None

    # The same report without the pair is still a candidate, as before.
    sibling.write_text(_lane_report(sibling.stem, _PARTIAL_PASS_BODY), encoding="utf-8")
    found = gate_report_recovery.find_recovery_candidate(
        reports, pr_id=_PR, exclude_name=f"kimi-gate-pr{_PR}-1792000000.md",
        window_start=mtime - 5, window_end=sibling.stat().st_mtime + 5,
    )
    assert found is not None and found.path == sibling


# ---------------------------------------------------------------------------
# 4. The plan gate: seat reader, re-extraction and tiebreaker
# ---------------------------------------------------------------------------

_SEAT = {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k3"}
_PLAN_PASS_FENCE = (
    f"```{pgp.VERDICT_FENCE}\n"
    '{"verdict": "pass", "blocking_findings": [], "rationale": "looks sound so far"}\n'
    "```\n"
)


def test_a_cut_off_seat_report_is_a_no_verdict_seat():
    """A kimi/glm/deepseek seat runs on the same provider lane. Its report,
    cut off by the lane deadline, holds a parseable pass fence."""
    report = _cut_off_report("kimi_gate", "plan-gate-t-kimi-1", "Partial.\n\n" + _PLAN_PASS_FENCE)
    result = pgp._dispatch_one(lambda *_a: report, _SEAT, "review this plan", "plan-gate-t-kimi-1")

    assert pgp.is_scoring(result) is False
    assert result.no_verdict is True
    assert result.seat_status == pgp.SEAT_LANE_NO_ANSWER
    assert "deadline of 900 s" in result.rationale


def test_a_complete_seat_report_still_scores():
    report = _lane_report("plan-gate-t-kimi-2", "Reviewed.\n\n" + _PLAN_PASS_FENCE)
    result = pgp._dispatch_one(lambda *_a: report, _SEAT, "review this plan", "plan-gate-t-kimi-2")
    assert pgp.is_scoring(result) is True
    assert (result.verdict, result.seat_status) == ("pass", pgp.SEAT_SCORED)


def test_the_synthesized_seat_keeps_its_rationale():
    """The synthesized-report branch shares the new no-answer return; its
    rationale is unchanged."""
    report = f"# Dispatch x\n\n- Status: timeout\n- {pgp.SYNTHESIZED_REPORT_MARKER}\n"
    result = pgp._dispatch_one(lambda *_a: report, _SEAT, "review this plan", "plan-gate-t-kimi-3")
    assert result.rationale == "lane produced no verdict (synthesized report)"
    assert result.seat_status == pgp.SEAT_LANE_NO_ANSWER


def test_a_cut_off_reextraction_answer_leaves_the_seat_unchanged():
    seat = pgp.PanelistResult(
        label="kimi", provider="kimi", model="kimi-k3", dispatched=True, parse_error=True,
        raw_text="The plan is sound overall; I would ship it.", report_path="plan-gate-t-kimi-4",
        seat_status=pgp.SEAT_PROSE_NO_FENCE,
    )
    answer = _cut_off_report("kimi_gate", "plan-gate-t-kimi-4-reextract", _PLAN_PASS_FENCE)
    result = pgp._reextract_verdict(
        lambda *_a: answer, _SEAT, seat, "plan-gate-t-kimi-4-reextract",
    )
    assert pgp.is_scoring(result) is False
    assert result.seat_status == pgp.SEAT_PROSE_NO_FENCE
    assert result.parse_error is True
    assert "cut off" in result.error


def test_a_cut_off_tiebreaker_report_is_a_timeout_not_a_decision():
    fence = (
        f"```{pgt.TIEBREAKER_FENCE}\n"
        '{"outcome": "START", "required_change": "", "rationale": "so far fine"}\n```\n'
    )
    report = _cut_off_report("glm_gate", "plan-tiebreak-t-1", "Deciding.\n\n" + fence)
    with pytest.raises(pgt.TiebreakerNoAnswerError) as excinfo:
        pgt.run_tiebreaker(
            doc_text="## Problem\n## Approach\n", track_id="t", project_id="p1",
            round_number=3, model_arg="glm-5.2", dispatcher=lambda *_a: report,
        )
    assert excinfo.value.status == "timeout"
