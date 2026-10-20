"""R2b (track review-stopregel-en-lichte-stoelen): the tiebreaker carries out ``terug-naar-plan``.

Plan ``claudedocs/specs/review-stopregel-en-lichte-stoelen/plan.md`` section 2, ADR-041.
Dispatch 20261020-rs-r2b-terug-naar-plan. Pinned here:

- the red run: a B3.1 cap where the injected tiebreaker answers ``terug-naar-plan`` stores
  that outcome, makes one open item per remaining codex blocker (from the result record of the
  cap head), sends the deliverable back to ``proposed`` and closes the PR once;
- the aftermath is idempotent on the verdict id: a crash after any step, then a repeat of the
  command, ends in exactly one of each (open items, coordination event, close call, receipt);
- a PR that links to no deliverable says so loudly and the other steps still run; a close that
  fails exits 3 and a repeat finishes it; a PR that is closed already is left as it is;
- the merge door: a ``terug-naar-plan`` verdict that stands counts no PASS, override or not;
- OI-2207: a receipt of another project, or of a count before the last attested opening, is
  never booked; a commit message that is no UTF-8 is a logged "no grant"; the writer looks up
  no git facts for a ``verkleinen`` of a count that no longer stands;
- ``mergen-met-OI`` still lands as ``outcome_unavailable_in_r2a`` (``test_r2a_cap_tiebreaker``).

No provider lane and no network: the tiebreaker's lane is injected, and ``gh`` is a fake
binary on ``PATH`` that records every call.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from review_stack_support import BRANCH, PR, isolate_config_registry
# The R2a world, reused as it is; ``door``, ``env`` and ``model`` are fixtures, found by name.
from test_r2a_cap_tiebreaker import (
    CAP, HEAD, PID, X, _answer, _cap_receipts, _capped, _commit, _door_record, _git_spy, _line, _opened,
    _store_verkleinen, _vid, _write_ledger, _write_result, _verdict, door, env, model,
)

DELIVERABLE = "dlv-r2b"
OUTPUT_REF = "pr:dlv-r2b"
TRACK = "review-stopregel-en-lichte-stoelen"
BUILDER = BRANCH.split("/", 1)[1]
_FINDINGS = [
    {"severity": "error", "message": "The cap check reads the wrong ledger.\nIt must read the store's own.",
     "file_path": "scripts/lib/review_rounds.py", "line": 812},
    {"severity": "error", "message": "A second verdict on one head is booked twice.",
     "file_path": "scripts/lib/review_cap_verdicts.py", "line": 233},
    {"severity": "warning", "message": "The refusal text names no way out."},
]


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


# ---------------------------------------------------------------------------
# The world of an aftermath: a result record, a deliverable plane, a fake gh
# ---------------------------------------------------------------------------


_FAKE_GH = '''
import json, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
with open(here / "calls.ndjson", "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
state = here / "pr_state"
if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "state":
    print(json.dumps({"state": state.read_text().strip() if state.exists() else "OPEN"}))
    sys.exit(0)
if args[:2] == ["pr", "close"]:
    if (here / "close_fails").exists():
        print("GraphQL: could not close", file=sys.stderr)
        sys.exit(1)
    state.write_text("CLOSED")
    sys.exit(0)
print("unexpected gh call", file=sys.stderr)
sys.exit(1)
'''


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake ``gh`` first on PATH: ``pr view --json state`` and ``pr close``, every call logged."""
    bin_dir = tmp_path / "fake-gh"
    bin_dir.mkdir()
    (bin_dir / "gh_fake.py").write_text(_FAKE_GH, encoding="utf-8")
    script = bin_dir / "gh"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "gh_fake.py"}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


def _gh_calls(gh_dir: Path, verb: str) -> List[List[str]]:
    log = gh_dir / "calls.ndjson"
    calls = [json.loads(raw) for raw in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return [call for call in calls if call[:2] == ["pr", verb]]


def _view_sql() -> str:
    text = (Path(__file__).resolve().parent.parent / "schemas" / "migrations"
            / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8")
    return re.search(r"CREATE VIEW IF NOT EXISTS deliverables AS.*?;", text, re.S).group(0)


def _deliverable_plane(state_dir: Path, *, builder_ref: str = OUTPUT_REF, state: str = "ready") -> None:
    """The dispatches and coordination_events of a store, with deliverable R2b and the PR's builder."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
                track TEXT, pr_ref TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                metadata_json TEXT DEFAULT '{}', operator_approved_at TEXT, output_ref TEXT, output_kind TEXT,
                UNIQUE(dispatch_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL, from_state TEXT,
                to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime', reason TEXT, metadata_json TEXT DEFAULT '{}',
                occurred_at TEXT NOT NULL, project_id TEXT
            );
        """)
        conn.execute(_view_sql())
        rows = [
            (DELIVERABLE, state, OUTPUT_REF, json.dumps({"deliverable": True, "title": "R2b"}), "2026-10-20T07:00:00Z"),
            (BUILDER, "completed", builder_ref or None, json.dumps({"target_slot": "T1"}), None),
        ]
        conn.executemany(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, output_ref, output_kind, metadata_json, "
            "operator_approved_at) VALUES (?, ?, ?, ?, ?, 'pr', ?, ?)",
            [(did, PID, st, TRACK, ref, meta, approved) for did, st, ref, meta, approved in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _cap_record(store, head: str, findings: List[Dict[str, Any]] = _FINDINGS) -> None:
    """The codex result record of the cap head, as the gate wrote it (report = the counted line's)."""
    record = {
        "gate": "codex_gate", "pr_number": PR, "pr_id": str(PR), "status": "completed", "commit_sha": head,
        "branch": BRANCH, "report_path": f"/r/20261020-000001-HEADLESS-codex_gate-pr-{PR}-x.md",
        "blocking_findings": findings, "advisory_findings": [],
    }
    (store["results_dir"] / f"pr-{PR}-codex_gate.json").write_text(json.dumps(record), encoding="utf-8")


def _aftermath_world(env, gh, **plane: Any) -> None:
    _capped(env)
    _cap_record(env, env["cap_head"])
    _deliverable_plane(env["state_dir"], **plane)


def _cli(*argv: str) -> int:
    import review_rounds_cli

    return review_rounds_cli.main(["cap-verdict", "--pr", str(PR), *argv])


def _open_items(state_dir: Path) -> List[Dict[str, Any]]:
    path = state_dir / "open_items.json"
    items = json.loads(path.read_text(encoding="utf-8"))["items"] if path.exists() else []
    return [item for item in items if item.get("source") == "review_cap_tiebreaker"]


def _deliverable_row(state_dir: Path) -> tuple:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return conn.execute(
            "SELECT state, operator_approved_at FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            (DELIVERABLE, PID),
        ).fetchone()
    finally:
        conn.close()


def _returned_events(state_dir: Path) -> List[tuple]:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        return conn.execute(
            "SELECT entity_id, from_state, to_state, actor, reason, project_id FROM coordination_events "
            "WHERE event_type = 'deliverable_returned_to_plan'"
        ).fetchall()
    finally:
        conn.close()


def _step_receipts(state_dir: Path) -> List[Dict[str, Any]]:
    path = state_dir / "t0_receipts.ndjson"
    lines = [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    return [line for line in lines if line.get("event_type") == "review_cap_aftermath"]


# ---------------------------------------------------------------------------
# Red run: terug-naar-plan is carried out
# ---------------------------------------------------------------------------


class TestTerugNaarPlanIsCarriedOut:
    def test_a_b31_terug_naar_plan_stores_the_outcome_makes_the_ois_returns_the_deliverable_and_closes_once(
        self, env, model, gh, capsys,
    ):
        import planning_cli
        import review_cap_verdicts

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))

        code = _cli("--for-dispatch", X)

        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert (verdict.cap_rule, verdict.outcome, verdict.chosen_outcome) == ("B3.1", "terug-naar-plan", "terug-naar-plan")
        assert code == 0, capsys.readouterr()
        items = _open_items(env["state_dir"])
        assert [item["severity"] for item in items] == ["blocker", "blocker", "warn"]
        assert items[0]["title"] == (
            f"PR #{PR} codex blocker 1/3 at scripts/lib/review_rounds.py:812: The cap check reads the wrong ledger."
        )
        assert items[2]["title"] == f"PR #{PR} codex blocker 3/3: The refusal text names no way out."
        assert "Severity codex gave: error" in items[0]["details"] and items[0]["pr_id"] == str(PR)
        assert list(verdict.oi_ids) == [item["id"] for item in items]
        assert _deliverable_row(env["state_dir"]) == ("proposed", None), "it needs the operator's promote again"
        assert [(r["deliverable_ref"], r["derived_status"]) for r in planning_cli._fetch_deliverable_records(
            env["state_dir"], PID)] == [(OUTPUT_REF, "proposed")]
        ((entity, from_state, to_state, actor, reason, project),) = _returned_events(env["state_dir"])
        assert (entity, from_state, to_state, actor, project) == (DELIVERABLE, "ready", "proposed", verdict.verdict_id, PID)
        assert "B3.1" in reason
        ((_pr, _close, number, _flag, comment),) = _gh_calls(gh, "close")
        assert number == str(PR)
        for part in (verdict.verdict_id, "B3.1", *verdict.oi_ids, DELIVERABLE):
            assert part in comment
        assert [(r["step"], r["result"]) for r in _step_receipts(env["state_dir"])] == [
            ("open_items", "created"), ("deliverable", "returned"), ("pr_close", "closed"),
        ]
        assert "terug-naar-plan: the PR goes back to plan; aftermath complete" in capsys.readouterr().out

    def test_the_rule_alone_sends_b31_back_after_a_failed_extra_round(self, env, gh, monkeypatch):
        import review_cap_tiebreaker
        import review_cap_verdicts

        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        vid = _store_verkleinen(env["state_dir"], env["cap_head"])
        _capped(env, _line("codex_gate", x1, "fail", 1, at=5, granted_by=vid,
                           report=f"/r/20261020-000005-HEADLESS-codex_gate-pr-{PR}-x.md"))
        record = {"commit_sha": x1, "report_path": f"/r/20261020-000005-HEADLESS-codex_gate-pr-{PR}-x.md",
                  "blocking_findings": [_FINDINGS[1]]}
        (env["results_dir"] / f"pr-{PR}-codex_gate.json").write_text(json.dumps(record), encoding="utf-8")
        _deliverable_plane(env["state_dir"])
        monkeypatch.setattr(review_cap_tiebreaker, "_default_dispatcher", lambda *_a: pytest.fail("no model"))

        assert _cli("--for-dispatch", X) == 0

        forced = [v for v in review_cap_verdicts.read_verdicts(env["state_dir"], PR) if v.head_sha == x1]
        assert [(v.outcome, v.tiebreaker_model) for v in forced] == [("terug-naar-plan", "")]
        assert len(forced[0].oi_ids) == 1 and len(_gh_calls(gh, "close")) == 1


# ---------------------------------------------------------------------------
# Idempotence: a crash after any step, then a repeat
# ---------------------------------------------------------------------------


class _Crash(BaseException):
    """A process that dies: no handler of the aftermath catches it."""


class TestTheAftermathIsIdempotent:
    @pytest.mark.parametrize("step", ["open_items", "deliverable", "pr_close"])
    @pytest.mark.parametrize("when", ["before_its_receipt", "after_its_receipt"])
    def test_a_crash_after_a_step_then_a_repeat_ends_in_exactly_one_of_each(self, env, model, gh, monkeypatch, step, when):
        import review_cap_aftermath
        import review_cap_verdicts

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        real = review_cap_aftermath._receipt_once

        def crashing(state_dir, verdict, result):
            if result.step == step and when == "before_its_receipt":
                raise _Crash(step)
            real(state_dir, verdict, result)
            if result.step == step:
                raise _Crash(step)

        with monkeypatch.context() as patch:
            patch.setattr(review_cap_aftermath, "_receipt_once", crashing)
            with pytest.raises(_Crash):
                _cli("--for-dispatch", X)
        repeated = _cli("--for-dispatch", X)

        assert repeated == 0
        assert len(model["instructions"]) == 1, "the verdict stands: the repeat asks no model"
        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert len(_open_items(env["state_dir"])) == 3 and len(verdict.oi_ids) == 3
        assert len(_returned_events(env["state_dir"])) == 1
        assert len(_gh_calls(gh, "close")) == 1
        assert sorted(r["step"] for r in _step_receipts(env["state_dir"])) == ["deliverable", "open_items", "pr_close"]
        assert len(_cap_receipts(env["state_dir"])) == 1

    def test_a_close_that_fails_exits_3_and_a_repeat_finishes_it(self, env, model, gh, capsys):
        import review_rounds_cli

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        (gh / "close_fails").write_text("", encoding="utf-8")

        failed = _cli("--for-dispatch", X)
        err = capsys.readouterr().err
        steps_after_failure = [r["step"] for r in _step_receipts(env["state_dir"])]
        (gh / "close_fails").unlink()
        finished = _cli("--for-dispatch", X)

        assert failed == review_rounds_cli.EXIT_AFTERMATH_INCOMPLETE == 3
        assert "aftermath is incomplete" in err and "gh pr close" in err
        assert steps_after_failure == ["open_items", "deliverable"], "the steps before it stand, receipted"
        assert finished == 0
        assert (gh / "pr_state").read_text() == "CLOSED" and len(_gh_calls(gh, "close")) == 2
        assert [r["step"] for r in _step_receipts(env["state_dir"])] == ["open_items", "deliverable", "pr_close"]
        assert len(_returned_events(env["state_dir"])) == 1 and len(_open_items(env["state_dir"])) == 3

    def test_a_pr_that_is_closed_already_is_left_as_it_is(self, env, model, gh):
        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        (gh / "pr_state").write_text("CLOSED", encoding="utf-8")

        assert _cli("--for-dispatch", X) == 0

        assert _gh_calls(gh, "close") == []
        (receipt,) = [r for r in _step_receipts(env["state_dir"]) if r["step"] == "pr_close"]
        assert (receipt["result"], receipt["pr_state"]) == ("already_closed", "CLOSED")


# ---------------------------------------------------------------------------
# No linked deliverable: loud, and the other steps still run
# ---------------------------------------------------------------------------


class TestNoLinkedDeliverable:
    def test_a_builder_without_output_ref_is_said_loudly_and_the_pr_still_closes(self, env, model, gh, capsys):
        _aftermath_world(env, gh, builder_ref="")
        model["answers"].append(_answer("terug-naar-plan"))

        code = _cli("--for-dispatch", X)
        out = capsys.readouterr()

        assert code == 0
        assert "WARNING" in out.err and "links to no deliverable" in out.err and "OI-1969" in out.err
        assert f"open deliverables on track {TRACK}: {DELIVERABLE} (ready)" in out.err
        assert _deliverable_row(env["state_dir"])[0] == "ready", "nothing is inferred from the track"
        assert _returned_events(env["state_dir"]) == []
        (receipt,) = [r for r in _step_receipts(env["state_dir"]) if r["step"] == "deliverable"]
        assert receipt["result"] == "no_linked_deliverable" and "OI-1969" in receipt["detail"]
        ((*_args, comment),) = _gh_calls(gh, "close")
        assert "No deliverable is linked to this PR" in comment
        assert len(_open_items(env["state_dir"])) == 3

    def test_a_deliverable_that_is_not_ready_is_not_moved(self, env, model, gh, capsys):
        _aftermath_world(env, gh, state="completed")
        model["answers"].append(_answer("terug-naar-plan"))

        assert _cli("--for-dispatch", X) == 0

        assert "did not go back to plan" in capsys.readouterr().err
        assert _deliverable_row(env["state_dir"])[0] == "completed" and _returned_events(env["state_dir"]) == []

    def test_the_counted_record_is_the_only_source_of_the_open_items(self, env, model, gh):
        import review_cap_verdicts

        _aftermath_world(env, gh)
        _cap_record(env, "e" * 40)
        model["answers"].append(_answer("terug-naar-plan"))

        assert _cli("--for-dispatch", X) == 3

        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert verdict.outcome == "terug-naar-plan" and verdict.oi_ids == ()
        assert _open_items(env["state_dir"]) == [] and _gh_calls(gh, "close") == []
        assert _step_receipts(env["state_dir"]) == []


# ---------------------------------------------------------------------------
# The merge door: sent back to plan, no PASS counts
# ---------------------------------------------------------------------------


def _store_returned(state_dir: Path, head: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(
            "INSERT INTO review_cap_verdicts (project_id, pr_number, verdict_id, head_sha, cap_rule, outcome, "
            "chosen_outcome, tiebreaker_model, created_at) "
            "VALUES (?, ?, ?, ?, 'B3.1', 'terug-naar-plan', 'terug-naar-plan', 'deepseek-v4-pro', ?)",
            (PID, PR, _vid(head), head, "2026-10-20T09:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return _vid(head)


def _overridden_door(door) -> Dict[str, Any]:
    """Codex at the cap on CAP, then a codex PASS and a kimi PASS on HEAD, judged with --override-b3."""
    import closure_verifier

    codex = _door_record(door, "codex_gate", tag="01")
    kimi = _door_record(door, "kimi_gate", tag="02")
    _write_ledger(door["ledger"], [
        _opened(), _line("codex_gate", CAP, "fail", 3, at=1),
        _line("codex_gate", HEAD, "pass", 0, at=2, report=codex["report_path"]),
        _line("kimi_gate", HEAD, "pass", 0, at=3, report=kimi["report_path"]),
    ])
    return closure_verifier.check_review_gate_for_merge(
        str(PR), "codex_gate", door["results"], branch=BRANCH, head_sha=HEAD, b3_override="operator: go on",
    )


class TestTheMergeDoorRefusesAPrSentBackToPlan:
    def test_without_a_verdict_the_override_lets_the_capped_pr_through(self, door):
        verdict = _overridden_door(door)

        assert verdict["verdict"] == "GO", verdict["message"]

    @pytest.mark.parametrize("on", [CAP, HEAD], ids=["verdict-on-the-cap-head", "verdict-on-the-merged-head"])
    def test_a_standing_terug_naar_plan_counts_no_pass_override_or_not(self, door, on):
        vid = _store_returned(door["state"], on)

        verdict = _overridden_door(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "terug naar plan" in verdict["message"] and vid in verdict["message"]
        assert verdict["review_seats"]["filled_by"] == {}, "no seat counts, the second seat neither"

    def test_a_verdict_store_that_cannot_be_read_counts_no_pass_of_a_capped_pr(self, door):
        _store_returned(door["state"], CAP)
        conn = sqlite3.connect(str(door["state"] / "runtime_coordination.db"))
        conn.execute("UPDATE review_cap_verdicts SET oi_ids = 'not json'")
        conn.commit()
        conn.close()

        verdict = _overridden_door(door)

        assert verdict["verdict"] == "NO-GO" and "onleesbaar" in verdict["message"], verdict["message"]

    def test_a_verdict_before_the_last_attested_opening_does_not_stand(self, door):
        import review_cap_verdicts
        import review_rounds

        _store_returned(door["state"], CAP)
        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(_line("codex_gate", CAP, "fail", 3, at=1))], PR)

        assert review_cap_verdicts.door_return(door["state"], cap, since="2026-10-20T09:30:00Z") is None
        assert review_cap_verdicts.door_return(door["state"], cap, since="2026-10-20T08:30:00Z").verdict_id == _vid(CAP)


# ---------------------------------------------------------------------------
# OI-2207: recovery keys on project_id and since; git that is no UTF-8; stale grants
# ---------------------------------------------------------------------------


def _emit_cap_receipt(state_dir: Path, **fields: Any) -> None:
    """A ``review_cap_verdict`` line in the store's receipt ledger, as another writer left it there."""
    values = {
        "timestamp": "2026-10-20T09:00:00Z", "event_type": "review_cap_verdict", "receipt_kind": "state_mutation",
        "project_id": PID, "pr_number": PR, "verdict_id": "", "head_sha": "", "cap_rule": "B3.1",
        "outcome": "verkleinen", "chosen_outcome": "verkleinen", "intervention": "planted elsewhere",
        "oi_ids": [], "granted_dispatch_id": "20261020-foreign-dispatch", "tiebreaker_model": "elsewhere",
        "rationale": "decided elsewhere", "created_at": "2026-10-20T09:00:00Z", **fields,
    }
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(values) + "\n")


def _attested(at: str) -> Dict[str, Any]:
    return {
        "kind": "opened", "project_id": "", "pr_number": PR, "basis": "attested", "evidence": 1,
        "recorded_at": at, "source": "operator", "actor": "operator", "reason": "release after a cut-off publication",
    }


class TestReceiptRecoveryKeysOnProjectAndCount:
    def test_a_receipt_of_another_project_with_the_same_pr_and_verdict_id_is_never_booked(self, env, model):
        import review_cap_verdicts

        _capped(env)
        _emit_cap_receipt(env["state_dir"], project_id="another-project", verdict_id=_vid(env["cap_head"]),
                          head_sha=env["cap_head"])
        model["answers"].append(_answer("verkleinen", "split the parser out of the gate"))

        assert _cli("--for-dispatch", X) == 0

        assert len(model["instructions"]) == 1, "the foreign receipt decided nothing here: the model is asked"
        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert (verdict.project_id, verdict.granted_dispatch_id, verdict.intervention) == (
            PID, X, "split the parser out of the gate",
        )

    def test_a_receipt_from_before_the_last_attested_opening_books_nothing(self, env, model):
        import review_cap_verdicts

        line = {**_line("codex_gate", env["cap_head"], "fail", 3, at=1), "recorded_at": "2026-10-20T09:40:00Z"}
        _write_ledger(env["ledger"], [_attested("2026-10-20T09:30:00Z"), line])
        _emit_cap_receipt(env["state_dir"], verdict_id=_vid(env["cap_head"]), head_sha=env["cap_head"])
        model["answers"].append(_answer("verkleinen", "split the parser out of the gate"))

        assert _cli("--for-dispatch", X) == 0

        assert len(model["instructions"]) == 1, "a receipt of a count that no longer stands decides nothing"
        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert verdict.intervention == "split the parser out of the gate"

    def test_the_lookup_matches_project_and_count_and_only_then_returns_the_receipt(self, env):
        import review_cap_verdicts

        vid = _vid(CAP)
        _emit_cap_receipt(env["state_dir"], project_id="another-project", verdict_id=vid, head_sha=CAP)
        _emit_cap_receipt(env["state_dir"], verdict_id=vid, head_sha=CAP, created_at="2026-10-20T08:00:00Z",
                          intervention="decided here")

        def found(**kw):
            return review_cap_verdicts.receipted_verdict(env["state_dir"], PR, vid, **kw)

        assert found(project_id="nobody") is None
        assert found(project_id=PID, since="2026-10-20T08:30:00Z") is None
        assert found(project_id=PID, since="2026-10-20T07:30:00Z").intervention == "decided here"
        assert found(project_id="another-project").intervention == "planted elsewhere"


class TestGitAndStaleGrants:
    def test_a_commit_message_that_is_no_utf8_is_a_logged_no_grant(self, door, tmp_path, monkeypatch, caplog):
        import review_cap_verdicts
        import review_rounds

        vid = _store_verkleinen(door["state"], CAP)
        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(_line("codex_gate", CAP, "fail", 3, at=1))], PR)
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        monkeypatch.setattr(review_cap_verdicts, "subprocess", _git_spy([], fail={"log": error}))

        with caplog.at_level(logging.WARNING, logger="review_cap_verdicts"):
            grant = review_cap_verdicts.grant_or_none(door["state"], cap, cap.rounds, head_sha=HEAD, repo=tmp_path)

        assert grant == review_rounds.Grant(vid, X, CAP)
        assert "no granted round" in caplog.text and "invalid start byte" in caplog.text

    def test_the_writer_looks_up_no_git_for_a_verkleinen_of_a_count_that_no_longer_stands(self, env, monkeypatch):
        import review_cap_verdicts

        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        _store_verkleinen(env["state_dir"], env["cap_head"])
        line = {**_line("codex_gate", env["cap_head"], "fail", 3, at=1), "recorded_at": "2026-10-20T09:40:00Z"}
        _write_ledger(env["ledger"], [_attested("2026-10-20T09:30:00Z"), line])
        verbs: List[str] = []

        def run(args, **kwargs):
            verbs.append(args[3])
            return subprocess.run(args, **kwargs)

        spy = _git_spy([])
        spy.run = run
        monkeypatch.setattr(review_cap_verdicts, "subprocess", spy)

        _on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="61", head=x1))

        assert landed is False, "the verkleinen is from before the release: the cap stays closed"
        assert verbs == [], "no git for a grant that no longer stands"


# ---------------------------------------------------------------------------
# Round 2 (dispatch 20261020-rs-r2b-r2): the codex findings on 28a186a8
# ---------------------------------------------------------------------------


def _returned(head: str, **fields: Any):
    """A ``terug-naar-plan`` verdict on ``head`` as the tiebreaker books it."""
    import review_cap_verdicts

    values = {
        "project_id": "", "pr_number": PR, "verdict_id": _vid(head), "head_sha": head, "cap_rule": "B3.1",
        "outcome": review_cap_verdicts.TERUG_NAAR_PLAN, "chosen_outcome": review_cap_verdicts.TERUG_NAAR_PLAN,
        "tiebreaker_model": "deepseek-v4-pro", "rationale": "a design problem", "created_at": "2026-10-20T09:00:00Z",
        **fields,
    }
    return review_cap_verdicts.CapVerdict(**values)


def _receipt_without_row(state_dir: Path, verdict) -> None:
    """Book ``verdict`` as the tiebreaker does, with the row's commit failing after its receipt landed."""
    import review_cap_verdicts

    real = review_cap_verdicts._book_row

    def commit_fails(path, booked, *, commit):
        if commit:
            raise review_cap_verdicts.CapVerdictError("database or disk is full")
        return real(path, booked, commit=commit)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(review_cap_verdicts, "_book_row", commit_fails)
        with pytest.raises(review_cap_verdicts.CapVerdictError, match="receipt of .* stands"):
            review_cap_verdicts.record_verdict(state_dir, verdict)


def _returned_receipt(state_dir: Path, head: str, **fields: Any) -> None:
    _emit_cap_receipt(
        state_dir, verdict_id=_vid(head), head_sha=head, outcome="terug-naar-plan", chosen_outcome="terug-naar-plan",
        intervention="", granted_dispatch_id="", **fields,
    )


class TestAReceiptWhoseRowDidNotLandStillSendsThePrBack:
    """BLOCKING: the door read only SQLite, so an interrupted projection erased the terminal decision."""

    @pytest.mark.parametrize("on", [CAP, HEAD], ids=["receipt-on-the-cap-head", "receipt-on-the-merged-head"])
    def test_a_terug_naar_plan_receipt_without_its_row_refuses_like_its_row_override_included(self, door, on):
        import review_cap_verdicts

        _receipt_without_row(door["state"], _returned(on))

        verdict = _overridden_door(door)

        assert review_cap_verdicts.read_verdicts(door["state"], PR) == (), "the row did not land"
        assert [r["verdict_id"] for r in _cap_receipts(door["state"])] == [_vid(on)], "its receipt did"
        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "terug naar plan" in verdict["message"] and _vid(on) in verdict["message"]
        assert verdict["review_seats"]["filled_by"] == {}, "no seat counts, the second seat neither"

    def test_only_a_receipt_of_this_project_and_the_current_count_refuses(self, door):
        import review_cap_verdicts
        import review_rounds

        _returned_receipt(door["state"], CAP, project_id="another-project")
        _returned_receipt(door["state"], HEAD, created_at="2026-10-20T08:10:00Z")
        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(_line("codex_gate", CAP, "fail", 3, at=1))], PR)

        assert review_cap_verdicts.door_return(door["state"], cap, since="2026-10-20T08:30:00Z") is None
        assert review_cap_verdicts.door_return(door["state"], cap, since="2026-10-20T08:00:00Z").verdict_id == _vid(HEAD)

    def test_a_receipt_in_a_store_that_names_no_project_counts_no_pass(self, tmp_path):
        import review_cap_verdicts
        import review_rounds

        state = tmp_path / "no-store" / "state"
        state.mkdir(parents=True)
        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(_line("codex_gate", CAP, "fail", 3, at=1))], PR)

        assert review_cap_verdicts.door_return(state, cap) is None, "no store and no receipt: nothing stands"
        _returned_receipt(state, CAP)
        with pytest.raises(review_cap_verdicts.CapVerdictError, match="names no project"):
            review_cap_verdicts.door_return(state, cap)


class TestTheDeliverableMoveIsJournaledBeforeItCommits:
    """ADVISORY 1 (ADR-005): the move back to plan committed before its receipt."""

    def test_a_deliverable_receipt_that_does_not_land_leaves_the_deliverable_where_it_stood(
        self, env, model, gh, monkeypatch,
    ):
        import governance_receipts

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        real = governance_receipts.emit_governance_receipt

        def full_disk_on_the_deliverable(event, **fields):
            if fields.get("step") == "deliverable":
                raise OSError(28, "No space left on device")
            return real(event, **fields)

        with monkeypatch.context() as patch:
            patch.setattr(governance_receipts, "emit_governance_receipt", full_disk_on_the_deliverable)
            failed = _cli("--for-dispatch", X)
        after_failure = (
            _deliverable_row(env["state_dir"]), _returned_events(env["state_dir"]),
            [r["step"] for r in _step_receipts(env["state_dir"])],
        )
        finished = _cli("--for-dispatch", X)

        assert failed == 3
        assert after_failure == (("ready", "2026-10-20T07:00:00Z"), [], ["open_items"]), (
            "no receipt, no move: the deliverable stands ready, approved, without an event"
        )
        assert finished == 0
        assert _deliverable_row(env["state_dir"]) == ("proposed", None) and len(_returned_events(env["state_dir"])) == 1
        assert [r["step"] for r in _step_receipts(env["state_dir"])] == ["open_items", "deliverable", "pr_close"]

    def test_a_move_whose_commit_did_not_land_is_applied_from_its_receipt_on_the_next_run(
        self, env, model, gh, monkeypatch, capsys,
    ):
        import review_cap_aftermath

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        real = review_cap_aftermath._receipt_once

        def dies_after_the_deliverable_receipt(state_dir, verdict, result):
            real(state_dir, verdict, result)
            if result.step == "deliverable":
                raise _Crash("the process dies between the receipt and the commit")

        with monkeypatch.context() as patch:
            patch.setattr(review_cap_aftermath, "_receipt_once", dies_after_the_deliverable_receipt)
            with pytest.raises(_Crash):
                _cli("--for-dispatch", X)
        after_crash = (_deliverable_row(env["state_dir"]), _returned_events(env["state_dir"]))
        capsys.readouterr()
        finished = _cli("--for-dispatch", X, "--json")

        assert after_crash == (("ready", "2026-10-20T07:00:00Z"), []), "the receipt landed first, the move did not"
        assert finished == 0
        steps = {step["step"]: step for step in json.loads(capsys.readouterr().out)["aftermath"]["steps"]}
        assert (steps["deliverable"]["result"], steps["deliverable"]["restored"]) == ("returned", True)
        assert steps["pr_close"]["restored"] is False
        assert _deliverable_row(env["state_dir"]) == ("proposed", None)
        ((entity, from_state, to_state, actor, _reason, _project),) = _returned_events(env["state_dir"])
        assert (entity, from_state, to_state, actor) == (DELIVERABLE, "ready", "proposed", _vid(env["cap_head"]))
        assert [r["step"] for r in _step_receipts(env["state_dir"])] == ["open_items", "deliverable", "pr_close"]


class TestAReceiptedStepIsNotRunAgain:
    """ADVISORY 2: a retry re-ran receipted steps and reread a result record that a later gate run replaced."""

    def test_a_retry_after_a_failed_close_closes_without_reading_the_replaced_result_record(self, env, model, gh):
        import review_cap_verdicts

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        (gh / "close_fails").write_text("", encoding="utf-8")
        failed = _cli("--for-dispatch", X)
        (before,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        _cap_record(env, "e" * 40, findings=[{"severity": "error", "message": "a finding on another head"}])
        (gh / "close_fails").unlink()

        retried = _cli("--for-dispatch", X)

        assert failed == 3 and retried == 0
        assert (gh / "pr_state").read_text() == "CLOSED"
        (after,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert after.oi_ids == before.oi_ids and len(after.oi_ids) == 3
        assert len(_open_items(env["state_dir"])) == 3 and len(_returned_events(env["state_dir"])) == 1
        assert [r["step"] for r in _step_receipts(env["state_dir"])] == ["open_items", "deliverable", "pr_close"]

    def test_a_crash_after_the_open_items_receipt_books_its_ids_from_the_receipt(self, env, model, gh, monkeypatch):
        import review_cap_aftermath
        import review_cap_verdicts

        _aftermath_world(env, gh)
        model["answers"].append(_answer("terug-naar-plan"))
        real = review_cap_aftermath._receipt_once

        def dies_after_the_open_items_receipt(state_dir, verdict, result):
            real(state_dir, verdict, result)
            if result.step == "open_items":
                raise _Crash("the process dies after the receipt")

        with monkeypatch.context() as patch:
            patch.setattr(review_cap_aftermath, "_receipt_once", dies_after_the_open_items_receipt)
            with pytest.raises(_Crash):
                _cli("--for-dispatch", X)
        (receipt,) = _step_receipts(env["state_dir"])
        _cap_record(env, "e" * 40, findings=[{"severity": "error", "message": "a finding on another head"}])

        assert _cli("--for-dispatch", X) == 0

        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert list(verdict.oi_ids) == receipt["oi_ids"] and len(verdict.oi_ids) == 3
        assert len(_open_items(env["state_dir"])) == 3 and len(_gh_calls(gh, "close")) == 1
        assert [r["step"] for r in _step_receipts(env["state_dir"])] == ["open_items", "deliverable", "pr_close"]
