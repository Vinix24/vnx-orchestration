"""R2c (RG-3, dispatch dlv-1144c10d92f2): ``mergen-met-OI`` is carried out at the B3.2 cap.

ADR-041 amendment R2c. Pinned here: a B3.2 cap whose stubbed model picks
``mergen-met-OI`` records that outcome, writes one open item per remaining codex
blocker with the dedup key, leaves the deliverable ``ready`` and the PR open,
and books ``open_items_written``; a second call writes nothing; a missing record
of the counted head refuses loudly; and the approval counts only for its own
verdict id, PR and head. No provider lane and no network.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from review_stack_support import BRANCH, PR, isolate_config_registry
from test_r2a_cap_tiebreaker import (
    CAP, HEAD, PID, X, _answer, _line, _opened, _vid, _write_ledger, env, model,
)
from test_r2b_terug_naar_plan import (
    _cli, _deliverable_plane, _deliverable_row, _gh_calls, _open_items, _pr_state, _project_a,
    _step_receipts, gh,
)

MERGEN = "mergen-met-OI"
MERGE = "merge_ready"
_REPORT = f"/r/20261020-000003-HEADLESS-codex_gate-pr-{PR}-x.md"
# The two remaining blockers of the counted head; a round of three would be B3.1.
_BLOCKERS = [
    {"severity": "error", "message": "The cap check reads the wrong ledger.",
     "file_path": "scripts/lib/review_rounds.py", "line": 812},
    {"severity": "warning", "message": "The refusal text names no way out."},
]


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _world(store) -> None:
    """B3.2: 2 + 2 + 2 blockers over three heads, and a result record on the counted head."""
    _project_a(store)
    _write_ledger(store["ledger"], [{**line, "project_id": PID} for line in (
        _opened(), _line("codex_gate", "1" * 40, "fail", 2, at=1),
        _line("codex_gate", "2" * 40, "fail", 2, at=2), _line("codex_gate", store["cap_head"], "fail", 2, at=3))])
    (store["results_dir"] / f"pr-{PR}-codex_gate.json").write_text(json.dumps({
        "pr_number": PR, "commit_sha": store["cap_head"], "branch": BRANCH, "report_path": _REPORT,
        "blocking_findings": _BLOCKERS}), encoding="utf-8")
    _deliverable_plane(store["state_dir"])


def _records(state_dir) -> list:
    return [receipt for receipt in _step_receipts(state_dir) if receipt.get("step") == MERGE]


def _verdict(**fields):
    import review_cap_verdicts as rcv

    return rcv.CapVerdict(**{
        "project_id": PID, "pr_number": PR, "verdict_id": _vid(CAP), "head_sha": CAP, "cap_rule": "B3.2",
        "outcome": rcv.MERGEN_MET_OI, "chosen_outcome": rcv.MERGEN_MET_OI,
        "created_at": "2026-10-20T09:00:00Z", **fields,
    })


class TestMergenMetOiIsCarriedOut:
    def test_one_oi_per_blocker_everything_open_and_a_repeat_writes_nothing(self, env, model, gh, capsys):
        import review_cap_verdicts as rcv

        _world(env)
        model["answers"].append(_answer(MERGEN))

        assert _cli("--for-dispatch", X) == 0
        out = capsys.readouterr().out
        (verdict,) = rcv.read_verdicts(env["state_dir"], PR)
        assert (verdict.cap_rule, verdict.outcome, verdict.chosen_outcome) == ("B3.2", MERGEN, MERGEN)
        items = _open_items(env["state_dir"])
        assert [item["dedup_key"] for item in items] == [
            f"review-cap:{PID}:{verdict.verdict_id}:blocker-{n}" for n in (1, 2)]
        assert [item["severity"] for item in items] == ["blocker", "warn"]
        assert list(verdict.oi_ids) == [item["id"] for item in items]
        assert [receipt["open_items_written"] for receipt in _records(env["state_dir"])] == [2]
        assert _deliverable_row(env["state_dir"])[0] == "ready", "nothing goes back to plan"
        assert _pr_state(gh) == "OPEN" and _gh_calls(gh, "close") == [], "the PR stays open"
        assert f"still needs an operator approval bound to verdict {verdict.verdict_id}, PR #{PR} " in out
        assert f"and head {verdict.head_sha}" in out

        assert _cli("--for-dispatch", X, "--json") == 0
        repeat = json.loads(capsys.readouterr().out)
        assert len(model["instructions"]) == 1, "the verdict stands: the repeat asks no model"
        assert _open_items(env["state_dir"]) == items
        assert [receipt["open_items_written"] for receipt in _records(env["state_dir"])] == [2]
        assert repeat["aftermath"]["steps"][-1]["restored"] is True


class TestTheCountedRecordIsTheOnlySource:
    @pytest.mark.parametrize("how", ["gone", "another-head"])
    def test_a_record_that_is_not_the_counted_one_refuses_and_writes_no_oi(self, env, model, gh, capsys, how):
        import review_cap_verdicts as rcv

        _world(env)
        path = env["results_dir"] / f"pr-{PR}-codex_gate.json"
        if how == "gone":
            path.unlink()
        else:
            path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "commit_sha": "e" * 40}))
        model["answers"].append(_answer(MERGEN))

        assert _cli("--for-dispatch", X) == 3

        (verdict,) = rcv.read_verdicts(env["state_dir"], PR)
        assert (verdict.outcome, verdict.oi_ids) == (MERGEN, ())
        assert _open_items(env["state_dir"]) == [] and _records(env["state_dir"]) == []
        err = capsys.readouterr().err
        assert "aftermath is incomplete" in err
        assert ("no open items" if how == "gone" else "not read from another verdict") in err


class TestTheApprovalIsPerUse:
    def test_an_approval_bound_to_verdict_pr_and_head_is_found(self, tmp_path):
        import review_cap_verdicts as rcv

        verdict = _verdict()
        record = rcv.record_merge_approval(tmp_path, verdict, "vincent", note="ok for this head")

        (line,) = [json.loads(raw) for raw in (tmp_path / "operator_approvals.ndjson").read_text().splitlines()]
        assert (line["event"], line["action"]) == ("operator_approval", "merge_pr")
        assert line["decision_id"] == verdict.verdict_id
        assert (line["verdict_id"], line["pr_number"], line["head_sha"]) == (verdict.verdict_id, PR, CAP)
        assert rcv.merge_approval(tmp_path, verdict) == record

    @pytest.mark.parametrize("field,value", [
        ("verdict_id", _vid(HEAD)), ("pr_number", PR + 1), ("head_sha", HEAD),
    ], ids=["another-verdict", "another-pr", "another-head"])
    def test_an_approval_of_another_verdict_pr_or_head_is_no_approval(self, tmp_path, field, value):
        import review_cap_verdicts as rcv

        verdict = _verdict()
        rcv.record_merge_approval(tmp_path, verdict, "vincent")

        assert rcv.merge_approval(tmp_path, replace(verdict, **{field: value})) is None

    def test_no_ledger_or_a_malformed_line_is_no_approval(self, tmp_path):
        import review_cap_verdicts as rcv

        verdict = _verdict()

        assert rcv.merge_approval(tmp_path, verdict) is None, "no ledger: nothing is cleared"
        (tmp_path / "operator_approvals.ndjson").write_text("not json\n", encoding="utf-8")
        assert rcv.merge_approval(tmp_path, verdict) is None
