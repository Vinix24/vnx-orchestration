"""Regression guards for the merge-readiness report (`vnx pr-ready`).

The report exists because counting what is present is not the same as knowing
what is required, and because "I could not look" is not "I looked and it is
fine". Both distinctions are asserted here rather than assumed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import ci_contexts  # noqa: E402
import pr_ready  # noqa: E402
import pr_readiness as pr  # noqa: E402
from pr_readiness import observed_gates_for_pr  # noqa: E402
from dispatch_spec import Gate  # noqa: E402
from gate_obligations import (  # noqa: E402
    NO_GATE_KEY,
    declared_gates_for_pr,
    normalise_pr_id,
    obligations_dir,
)


# ---------------------------------------------------------------------------
# The cost half of the question
# ---------------------------------------------------------------------------


def test_every_gate_in_the_closed_enum_has_a_cost_entry():
    """A gate with no cost entry is reported as "run it" with no idea what
    running it takes — the half of the question that made the manual count
    slow. The enum is closed (OI-845), so this is enforceable.
    """
    assert set(pr.GATE_COST) == {g.value for g in Gate}


def test_a_gate_without_a_cost_entry_is_reported_as_unknown_not_free():
    report = pr.Readiness(pr_number=7, facts={"headRefOid": "a" * 40})
    report.declared_gates = ["invented_gate"]
    report.gates = [pr.GateEvidence(gate="invented_gate", verdict="NO-GO", message="absent")]
    costs = report.costs()
    assert any("UNKNOWN, not free" in c for c in costs)


def test_openrouter_cost_band_is_the_measured_range_not_the_old_estimate():
    """1.3-2.8 USD per run, measured. An earlier 0.4 estimate under-read the
    remaining balance by a factor of five.
    """
    assert "1.3" in pr.GATE_COST[Gate.GLM_GATE.value].usd
    assert "2.8" in pr.GATE_COST[Gate.GLM_GATE.value].usd


# ---------------------------------------------------------------------------
# The PR -> declared-gate join, promoted out of pr_merge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("PR-879", "879"), ("pr879", "879"), ("879", "879"), ("PR-HYG-1", "HYG-1"), ("", "")],
)
def test_normalise_pr_id(raw, expected):
    assert normalise_pr_id(raw) == expected


def _obligation(state_dir: Path, dispatch_id: str, **fields):
    obligations_dir(state_dir).mkdir(parents=True, exist_ok=True)
    record = {"schema_version": 1, "dispatch_id": dispatch_id, **fields}
    (obligations_dir(state_dir) / f"{dispatch_id}.json").write_text(json.dumps(record))


def test_declared_gates_joins_on_pr_number_and_on_pr_id(tmp_path):
    _obligation(tmp_path, "d1", gate="codex_gate", pr_number=42, pr_id=None)
    _obligation(tmp_path, "d2", gate="glm_gate", pr_number=None, pr_id="PR-42")
    _obligation(tmp_path, "d3", gate="kimi_gate", pr_number=43, pr_id=None)
    assert declared_gates_for_pr(tmp_path, 42) == ["codex_gate", "glm_gate"]


def test_declared_gates_excludes_the_explicit_no_gate_sentinel(tmp_path):
    """`__no_gate__` declares an absence, not an obligation."""
    _obligation(tmp_path, "d1", gate=NO_GATE_KEY, pr_number=42)
    _obligation(tmp_path, "d2", gate="", pr_number=42)
    assert declared_gates_for_pr(tmp_path, 42) == []


def test_declared_gates_raises_on_an_unreadable_obligation(tmp_path):
    """An obligation nobody can read must never read as "this PR owes nothing"."""
    obligations_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (obligations_dir(tmp_path) / "broken.json").write_text("{not json")
    with pytest.raises(ValueError):
        declared_gates_for_pr(tmp_path, 42)


def test_pr_merge_still_returns_the_last_declared_gate_after_delegation(tmp_path):
    """Behaviour parity: pr_merge kept "last one wins" when the join moved."""
    import pr_merge

    _obligation(tmp_path, "d1", gate="codex_gate", pr_number=42)
    _obligation(tmp_path, "d2", gate="glm_gate", pr_number=42)
    assert pr_merge._resolve_declared_gate(42, state_dir=tmp_path) == "glm_gate"


def test_pr_merge_degrades_an_unreadable_store_to_a_refusal_not_an_exception(tmp_path):
    """The merge path must not crash on a corrupt obligation; "" is a refusal."""
    import pr_merge

    obligations_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (obligations_dir(tmp_path) / "broken.json").write_text("{not json")
    assert pr_merge._resolve_declared_gate(42, state_dir=tmp_path) == ""


# ---------------------------------------------------------------------------
# Verdict precedence — the distinction the whole report exists for
# ---------------------------------------------------------------------------


def _ctx(context, state):
    return ci_contexts.ContextState(context=context, state=state, detail="d")


def _ready_report(**overrides):
    report = pr.Readiness(
        pr_number=1,
        facts={"headRefOid": "a" * 40, "state": "OPEN", "headRefName": "b", "isDraft": False},
    )
    report.contexts = [_ctx("Profile A", ci_contexts.STATE_PASSED)]
    report.declared_gates = ["glm_gate"]
    report.gates = [pr.GateEvidence(gate="glm_gate", verdict="GO", message="ok")]
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def test_a_fully_evidenced_open_pr_is_ready():
    assert _ready_report().verdict == pr.VERDICT_READY


def test_an_unmeasurable_section_beats_every_blocker_and_never_reads_as_ready():
    report = _ready_report(contexts_error="gh api failed (rc=1)")
    assert report.verdict == pr.VERDICT_UNMEASURABLE
    assert "gh api failed" in report.unmeasurable_reasons[0]


def test_an_unmeasurable_gate_is_not_a_passing_gate():
    report = _ready_report(
        gates=[pr.GateEvidence(gate="glm_gate", verdict=pr.VERDICT_UNMEASURABLE, message="boom")]
    )
    assert report.verdict == pr.VERDICT_UNMEASURABLE


def test_a_context_still_in_flight_blocks_but_is_named_as_in_flight():
    report = _ready_report(
        contexts=[
            _ctx("Profile A", ci_contexts.STATE_PASSED),
            _ctx("Profile B", ci_contexts.STATE_WAITING_UPSTREAM),
        ]
    )
    assert report.verdict == pr.VERDICT_NOT_READY
    assert any("still in flight" in b for b in report.blockers)
    assert not any("not satisfied" in b for b in report.blockers)


def test_a_never_created_context_is_reported_as_not_satisfied():
    report = _ready_report(contexts=[_ctx("Profile B", ci_contexts.STATE_NEVER_CREATED)])
    assert any("not satisfied" in b for b in report.blockers)
    assert any("gh run rerun" in c for c in report.costs())


def test_a_pr_with_no_declared_obligation_blocks_and_names_the_choice():
    """#1701 merged with no obligation record at all. That is a state, and it
    must cost something rather than reading as "nothing outstanding".
    """
    report = _ready_report(declared_gates=[], gates=[])
    assert report.verdict == pr.VERDICT_NOT_READY
    assert any("no review-gate obligation declared" in b for b in report.blockers)
    costs = report.costs()
    assert any("declare and run a review gate" in c for c in costs)
    assert any("glm_gate" in c and "codex_gate" in c for c in costs)


@pytest.mark.parametrize("state,expected", [("MERGED", True), ("CLOSED", True), ("OPEN", False)])
def test_a_non_open_pr_is_never_ready(state, expected):
    report = _ready_report()
    report.facts = dict(report.facts, state=state)
    assert any("PR state is" in b for b in report.blockers) is expected


def test_a_draft_pr_is_never_ready():
    report = _ready_report()
    report.facts = dict(report.facts, isDraft=True)
    assert any("draft" in b for b in report.blockers)


# ---------------------------------------------------------------------------
# Reading the PR itself
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_pr_facts_raises_on_a_failed_gh_call(monkeypatch, tmp_path):
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Proc(1, "", "no such PR"))
    with pytest.raises(pr.PRReadinessError) as excinfo:
        pr.fetch_pr_facts(9999, tmp_path)
    assert "no such PR" in str(excinfo.value)


def test_fetch_pr_facts_raises_when_there_is_no_head_sha(monkeypatch, tmp_path):
    """Without a head sha there is nothing to bind evidence to — refuse rather
    than measure against whatever is checked out locally.
    """
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Proc(0, json.dumps({"number": 1})))
    with pytest.raises(pr.PRReadinessError) as excinfo:
        pr.fetch_pr_facts(1, tmp_path)
    assert "no head sha" in str(excinfo.value)


def test_fetch_pr_facts_raises_on_unparseable_json(monkeypatch, tmp_path):
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Proc(0, "{not json"))
    with pytest.raises(pr.PRReadinessError):
        pr.fetch_pr_facts(1, tmp_path)


# ---------------------------------------------------------------------------
# Rendering and exit codes
# ---------------------------------------------------------------------------


def test_render_is_five_lines_for_a_ready_pr():
    """Five lines was the ask: what is complete, what is missing, what it costs."""
    assert len(pr_ready.render(_ready_report()).splitlines()) == 5


def test_render_names_a_stale_gate_record_by_its_commit():
    report = _ready_report(
        gates=[
            pr.GateEvidence(
                gate="glm_gate", verdict="NO-GO", message="geen resultaat",
                record_sha="c" * 40,
            )
        ]
    )
    line = pr_ready._gate_line(report)
    assert "NOT on head" in line and "cccccccccccc" in line


def test_render_distinguishes_an_absent_gate_from_a_stale_one():
    report = _ready_report(
        gates=[pr.GateEvidence(gate="glm_gate", verdict="NO-GO", message="geen resultaat")]
    )
    assert "absent" in pr_ready._gate_line(report)


def test_every_verdict_maps_to_a_distinct_exit_code():
    """0 ready, 1 not ready, 2 unmeasurable. Collapsing 2 into 1 would hide
    "the machine cannot reach GitHub" inside "this PR needs another run".
    """
    assert set(pr_ready._EXIT_BY_VERDICT) == {
        pr.VERDICT_READY, pr.VERDICT_NOT_READY, pr.VERDICT_UNMEASURABLE,
    }
    assert len(set(pr_ready._EXIT_BY_VERDICT.values())) == 3
    assert pr_ready._EXIT_BY_VERDICT[pr.VERDICT_READY] == 0


def test_main_refuses_a_non_numeric_pr_argument(capsys):
    assert pr_ready.main(["not-a-number"]) == pr_ready.EXIT_BAD_INPUT
    assert "must be numbers" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Gates that ran off the door
# ---------------------------------------------------------------------------


def _result(results_dir: Path, name: str, **fields):
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / name).write_text(json.dumps(fields))


def test_observed_gates_finds_a_result_the_obligation_store_never_declared(tmp_path):
    """A gate run by hand leaves a result record and no obligation. Reading
    only the obligations reports "no gate verdict" about a PR that has one —
    measured on this fleet: #1701 merged with no obligation at all.
    """
    _result(tmp_path, "pr-42-glm_gate.json", pr_id="42", gate="glm_gate")
    _result(tmp_path, "pr-42-ci_gate.json", pr_id="42", gate="ci_gate")
    assert observed_gates_for_pr(tmp_path, 42) == ["ci_gate", "glm_gate"]


def test_observed_gates_ignores_other_prs_and_offline_test_runs(tmp_path):
    _result(tmp_path, "pr-43-glm_gate.json", pr_id="43", gate="glm_gate")
    _result(tmp_path, "pr-42-kimi_gate.json", pr_id="42", gate="kimi_gate", test_run=True)
    _result(tmp_path, "pr-42-str.json", pr_id="42", gate="codex_gate", test_run="true")
    assert observed_gates_for_pr(tmp_path, 42) == []


def test_observed_gates_skips_a_corrupt_result_file(tmp_path):
    """A result file is not a claim that something is owed, so a corrupt one
    cannot hide an obligation — unlike the obligation store, which raises.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "broken.json").write_text("{not json")
    _result(tmp_path, "pr-42-glm_gate.json", pr_id="42", gate="glm_gate")
    assert observed_gates_for_pr(tmp_path, 42) == ["glm_gate"]


def test_observed_gates_on_a_missing_results_dir_is_empty_not_an_error(tmp_path):
    assert observed_gates_for_pr(tmp_path / "nope", 42) == []


def test_an_off_the_door_gate_is_reported_as_such():
    report = _ready_report(declared_gates=[], observed_gates=["glm_gate"])
    report.gates = [
        pr.GateEvidence(gate="glm_gate", verdict="GO", message="ok", declared=False)
    ]
    line = pr_ready._gate_line(report)
    assert "off the door" in line
    assert any("gate result(s) exist off the door" in b for b in report.blockers)


def test_an_off_the_door_gate_removes_the_declare_and_run_cost():
    """The gate already ran; telling the reader to run one would be wrong."""
    report = _ready_report(declared_gates=[], observed_gates=["glm_gate"])
    report.gates = [
        pr.GateEvidence(gate="glm_gate", verdict="GO", message="ok", declared=False)
    ]
    assert not any("declare and run a review gate" in c for c in report.costs())


def test_an_empty_required_context_set_is_unmeasurable_not_ready():
    """The renderer already called this UNMEASURABLE. The verdict said READY,
    so the command exited 0 on a set nobody measured — the report
    contradicting itself, which is the defect class this command exists to
    surface on other people's evidence. Found by codex_gate on this PR.
    """
    report = _ready_report(contexts=[])
    assert report.verdict == pr.VERDICT_UNMEASURABLE
    assert any("lists none" in r for r in report.unmeasurable_reasons)
    assert pr_ready._EXIT_BY_VERDICT[report.verdict] == pr_ready.EXIT_UNMEASURABLE


def test_the_verdict_and_the_rendered_ci_line_never_disagree():
    """Both halves must reach the same conclusion about an empty set."""
    report = _ready_report(contexts=[])
    assert "UNMEASURABLE" in pr_ready._context_line(report)
    assert report.verdict == pr.VERDICT_UNMEASURABLE
