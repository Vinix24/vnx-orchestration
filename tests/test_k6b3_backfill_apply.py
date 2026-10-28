"""tests/test_k6b3_backfill_apply.py — the backfill writes its plan, and the CLI (K6b-3).

Covers the writer half of ``scripts/cost_ledger_backfill.py`` and ``main`` (ADR-049):
``apply_runs`` books exactly what K6b-2's ``plan_runs`` decided — a charge as a new
ledger line, a correction through the ledger's correction path, a skip as nothing —
and ``--apply`` is idempotent on the same transcripts. Every transcript and every
ledger is a real file under ``tmp_path``; only the paths are pinned (the project id
and ``vnx_paths.resolve_paths``), so the reader, the planner, the writer and the
command all run for real. The last two groups pin the ledger's role vocabulary: the
two ``unknown:<reason>`` roles are a closed set, and a refused line fails the call
without touching the lines already written.

K6b-3 round 2 adds the reservation guard: a backfill charge's attempt number is
checked against the ledger under the writer's own lock, so two backfills of one
dispatch — two plans from one snapshot, or two real processes — never write two
transcripts' spend under one ``run_id`` (which the aggregate would collapse to the
last line). The tests below drive that in one process and in two, past the retry
bound, and pin what the guard leaves alone (a correction, a live charge). The last
one makes the lock file impossible to create, which is how ``main`` reaches its
``CostLedgerWriteError`` branch: the unknown write outcome stops the run.

K6b-3b (OI-2455) makes the identity check compare with the *effective* line: the
newest line of the ``supersedes`` chain, in the ledger and in the spool that waits
for the replay, that no other line supersedes. A repeated backfill of a grown
transcript therefore skips against the correction already written instead of adding
another one, and a transcript whose usage went back to an earlier value is corrected
back to it instead of being reported as booked. The tests at the end of the file pin
both, and pin a correction waiting in the spool as the effective line.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import multiprocessing
import os
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import cost_ledger_backfill as backfill  # noqa: E402
import harness_run_cost  # noqa: E402
import provider_costs  # noqa: E402
import vnx_paths  # noqa: E402
from token_harvest import MessageUsage  # noqa: E402

PROJECT = "vnx-dev"
DISPATCH = "dlv-b3"
LANE = "deepseek-harness"
GLM_LANE = "glm-harness"
MODEL = "deepseek-v4-pro"
ENCODED_CWD = "-repo-fixture-project"
WORKTREE_CWD = f"/repo/.vnx-data/worktrees/dispatch-{DISPATCH}"
TOKENS_A = {"input_uncached": 1000, "cache_hit": 5000, "cache_write_5m": 100,
            "cache_write_1h": 0, "output": 200}
TOKENS_B = {"input_uncached": 2000, "cache_hit": 0, "cache_write_5m": 0,
            "cache_write_1h": 0, "output": 400}
# A second transcript whose spend is an order of magnitude apart from ``TOKENS_A``,
# whatever the price window: the aggregate cannot look right by booking one twice.
TOKENS_HEAVY = {"input_uncached": 50_000, "cache_hit": 100_000, "cache_write_5m": 2_000,
                "cache_write_1h": 0, "output": 20_000}
WINDOW_A = "2026-10-05T02:00:00Z"
WINDOW_B = "2026-10-06T04:00:00Z"
LEGACY_RUN = f"{PROJECT}:envelope_govern:{DISPATCH}:1"


def _entry(message_id, timestamp, tokens, cwd=WORKTREE_CWD, model=MODEL):
    """One assistant message as Claude Code writes it, usage included."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": tokens["input_uncached"],
                "cache_read_input_tokens": tokens["cache_hit"],
                "cache_creation": {"ephemeral_5m_input_tokens": tokens["cache_write_5m"],
                                   "ephemeral_1h_input_tokens": tokens["cache_write_1h"]},
                "output_tokens": tokens["output"],
            },
        },
    }


def _segments(tokens, window=WINDOW_A):
    """The live line's segments for one message: what the writer prices, as the plan does."""
    messages = [MessageUsage("m", window, dict(tokens))]
    return harness_run_cost.price_segments(LANE, MODEL, messages, tokens)


class Lab:
    """A harness store, a ledger and a dispatch dir under ``tmp_path``, with only paths pinned."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.data = tmp_path / "data"
        self.lane = self.data / "harness-config" / LANE
        (self.lane / "projects").mkdir(parents=True)
        self.ledger = self.data / "events" / "provider_costs.ndjson"
        self.ledger.parent.mkdir(parents=True)
        self.dispatch_dir = self.data / "dispatches"
        self.dispatch_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
        monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {
            "VNX_DATA_DIR": str(self.data),
            "VNX_DISPATCH_DIR": str(self.dispatch_dir),
        })

    def transcript(self, session, window=WINDOW_A, tokens=TOKENS_A, cwd=WORKTREE_CWD, lane=LANE):
        """One session transcript: a single assistant message, so its tokens are ``tokens`` exactly."""
        project = self.data / "harness-config" / lane / "projects" / ENCODED_CWD
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{session}.jsonl"
        path.write_text(json.dumps(_entry(f"m-{session}", window, tokens, cwd=cwd)) + "\n", encoding="utf-8")
        return path

    def dispatch(self, dispatch_id=DISPATCH, role="backend-developer", model=MODEL):
        path = self.dispatch_dir / dispatch_id / "dispatch-spec.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"dispatch_id": dispatch_id, "role": role, "model": model}),
                        encoding="utf-8")

    def book(self, run_id, tokens=TOKENS_A, window=WINDOW_A, *, identity=None, role="builder",
             kind="charge", supersedes=None, reason=None):
        """A live line written through the ledger's own writer, as an earlier run left it."""
        record = cost_ledger.record_run(cost_ledger.RunCost(
            run_id=run_id, project_id=PROJECT, role=role, provider=LANE, model=MODEL,
            occurred_from=window, occurred_to=window, tokens=dict(tokens),
            usage_completeness=harness_run_cost.usage_completeness(tokens),
            billing=harness_run_cost.BILLING_BY_PROVIDER[LANE],
            segments=_segments(tokens, window), kind=kind, supersedes=supersedes, reason=reason,
            transcript_identity=identity,
        ), ledger_path=self.ledger)
        return dict(record)

    def run_cli(self, *args):
        """``vnx costs backfill`` in this lab; the lanes are found under this store's harness-config."""
        return backfill.main([*args, "--ledger", str(self.ledger)])

    def lines(self):
        text = self.ledger.read_text(encoding="utf-8") if self.ledger.exists() else ""
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def summary(self, capsys, provider=LANE):
        out = capsys.readouterr().out
        return next(line for line in out.splitlines() if line.startswith(f"{provider}:"))


def test_a_dry_run_prints_one_summary_per_provider_and_writes_nothing(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a")
    lab.transcript("sess-g", tokens=TOKENS_B, lane=GLM_LANE)
    legacy = lab.book(LEGACY_RUN)
    before = lab.ledger.read_bytes()

    assert lab.run_cli("--harness-root", str(lab.data / "harness-config")) == 0

    assert lab.ledger.read_bytes() == before
    out = capsys.readouterr().out
    assert "dry run: no line written; pass --apply to write" in out
    assert f"{LANE}: runs=1 new_lines=0 corrections=1 skips=0 tokens={sum(TOKENS_A.values())}" in out
    # the glm transcript is unbooked: the dry run prices it as one new line, and writes none of it
    assert f"{GLM_LANE}: runs=1 new_lines=1 corrections=0 skips=0" in out
    assert legacy  # the earlier line is still the only ledger line


def test_apply_writes_the_planned_correction_and_charge(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a")
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_B)
    legacy = lab.book(LEGACY_RUN)

    assert lab.run_cli("--apply") == 0

    written = lab.lines()
    assert len(written) == 3, written
    assert written[0] == legacy
    by_session = {line.get("transcript_identity"): line for line in written[1:]}
    correction = by_session[f"{LANE}:sess-a"]
    assert correction["kind"] == "correction"
    assert correction["supersedes"] == legacy["record_id"]
    assert correction["reason"] == backfill.CORRECTION_REASON
    assert correction["run_id"] == legacy["run_id"]
    assert correction["backfill"] is True
    charge = by_session[f"{LANE}:sess-b"]
    assert charge["kind"] == "charge"
    assert charge["run_id"] == f"{PROJECT}:backfill:{DISPATCH}:1"
    assert charge["backfill"] is True
    line = lab.summary(capsys)
    assert "runs=2 new_lines=1 corrections=1 skips=0" in line
    assert f"tokens={sum(TOKENS_A.values()) + sum(TOKENS_B.values())}" in line


def test_a_second_apply_over_the_same_transcripts_writes_nothing(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a")
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_B)
    lab.book(LEGACY_RUN)
    assert lab.run_cli("--apply") == 0
    capsys.readouterr()
    before = lab.ledger.read_bytes()

    assert lab.run_cli("--apply") == 0

    assert lab.ledger.read_bytes() == before
    line = lab.summary(capsys)
    assert "runs=2 new_lines=0 corrections=0 skips=2 tokens=0" in line


def test_a_run_without_a_dispatch_row_books_as_unknown_no_dispatch_row(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a")  # no dispatch-spec.json for this dispatch id at all

    assert lab.run_cli("--apply") == 0

    line, = lab.lines()
    assert line["role"] == "unknown:no_dispatch_row"
    assert line["transcript_identity"] == f"{LANE}:sess-a"
    assert line["cost_usd"] is not None


def test_a_dispatch_row_without_a_ledger_purpose_books_as_unknown_no_ledger_role(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch(role="carrier-pigeon")
    lab.transcript("sess-a")

    assert lab.run_cli("--apply") == 0

    line, = lab.lines()
    assert line["role"] == "unknown:no_ledger_role"


def test_the_unknown_roles_are_the_ledger_closed_set():
    assert cost_ledger.UNKNOWN_ROLES == {backfill.UNKNOWN_ROLE, backfill.UNKNOWN_ROLE_NO_PURPOSE}


def _cost(role, run_id=f"{PROJECT}:backfill:{DISPATCH}:1"):
    return cost_ledger.RunCost(
        run_id=run_id, project_id=PROJECT, role=role, provider=LANE, model=MODEL,
        occurred_from=WINDOW_A, occurred_to=WINDOW_A, tokens=dict(TOKENS_A),
        usage_completeness="complete", billing="metered",
        transcript_identity=f"{LANE}:sess-a",
    )


@pytest.mark.parametrize("role", [backfill.UNKNOWN_ROLE, backfill.UNKNOWN_ROLE_NO_PURPOSE])
def test_the_two_unknown_reasons_are_booked(tmp_path, role):
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    ledger.parent.mkdir(parents=True)

    written = cost_ledger.record_run(_cost(role), ledger_path=ledger)

    assert written["role"] == role
    assert [json.loads(line)["role"] for line in ledger.read_text(encoding="utf-8").splitlines()] == [role]


@pytest.mark.parametrize("role", ["unknown:other", "unknown:", "unknownx", "unknown"])
def test_any_other_role_is_refused_with_nothing_written(tmp_path, role):
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    ledger.parent.mkdir(parents=True)

    with pytest.raises(cost_ledger.InvalidRecord):
        cost_ledger.record_run(_cost(role), ledger_path=ledger)

    assert not ledger.exists()


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_a_refused_line_fails_the_call_and_leaves_the_earlier_line_untouched(
    tmp_path, monkeypatch, capsys, dry_run,
):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a")
    planned = backfill.plan_runs(backfill.scan([lab.lane], date(2026, 10, 1)), {}, {}, PROJECT)
    assert len(planned) == 1
    bad = backfill.PlannedRun(LANE, _cost("unknown:other", f"{PROJECT}:backfill:{DISPATCH}:9"), None, False)

    summaries = backfill.apply_runs(planned + [bad], lab.ledger, dry_run=dry_run)

    assert summaries[LANE].new_lines == 1
    assert summaries[LANE].failures == 1
    assert backfill._exit_code(summaries) == backfill.EXIT_FAILED
    assert "unknown:other" in capsys.readouterr().err
    written = lab.lines()
    if dry_run:
        assert written == []
    else:
        assert len(written) == 1
        assert written[0]["run_id"] == f"{PROJECT}:backfill:{DISPATCH}:1"
        assert written[0]["role"] == planned[0].run.role
        assert written[0]["tokens"] == planned[0].run.tokens


def test_a_harness_root_that_holds_no_lane_is_refused(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    (tmp_path / "empty").mkdir()

    assert lab.run_cli("--harness-root", str(tmp_path / "empty")) == backfill.EXIT_USAGE
    assert "no harness lane under" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Two backfills of one dispatch: the reservation is checked under the writer's lock
# (K6b-3 round 2)
# ---------------------------------------------------------------------------


def _lines(ledger_path):
    """Every line of a ledger that may not exist yet."""
    if not Path(ledger_path).is_file():
        return []
    text = Path(ledger_path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run_of(lab, session):
    """The one scanned transcript of a session."""
    runs = [run for run in backfill.scan([lab.lane], date(2026, 10, 1)) if run.session_id == session]
    assert len(runs) == 1, runs
    return runs[0]


def _plans_from_one_snapshot(lab, *sessions, rows=None):
    """One plan per session, every plan numbered on the same ledger read.

    This is what two backfills see when they run one after the other: each reads the
    ledger before either writes, so each numbers its charge from the same snapshot.
    """
    snapshot = backfill.load_live_runs(lab.ledger, PROJECT)
    plans = []
    for session in sessions:
        planned = backfill.plan_runs([_run_of(lab, session)], rows or {}, snapshot, PROJECT)
        assert len(planned) == 1, planned
        plans.append(planned[0])
    return plans


def test_two_plans_from_one_snapshot_book_both_transcripts_spends(tmp_path, monkeypatch):
    """The round-2 blocker: both plans reserve attempt 1; both spends must still be counted."""
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a", window=WINDOW_A, tokens=TOKENS_A)
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_HEAVY)
    rows = {DISPATCH: backfill.DispatchRow("backend-developer", MODEL)}
    first, second = _plans_from_one_snapshot(lab, "sess-a", "sess-b", rows=rows)
    assert first.run.run_id == second.run.run_id, "both plans must have reserved the same attempt"

    backfill.apply_runs([first], lab.ledger, dry_run=False)
    backfill.apply_runs([second], lab.ledger, dry_run=False)

    written = lab.lines()
    assert len(written) == 2, written
    assert len({line["run_id"] for line in written}) == 2, written
    assert {line["transcript_identity"] for line in written} == {f"{LANE}:sess-a", f"{LANE}:sess-b"}
    assert written[0]["cost_usd"] != written[1]["cost_usd"], "two transcripts' spend, not one booked twice"
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert len(totals.runs) == 2
    assert {record["transcript_identity"] for record in totals.runs.values()} == {
        f"{LANE}:sess-a", f"{LANE}:sess-b",
    }
    assert totals.cost_usd == pytest.approx(sum(line["cost_usd"] for line in written))


def test_a_renumbered_charge_never_lands_on_an_attempt_the_same_plan_holds(tmp_path, monkeypatch):
    """One plan holds attempt 2 for its second charge; the first charge renumbered past 1 takes 3."""
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a", window=WINDOW_A, tokens=TOKENS_A)
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_HEAVY)
    planned = backfill.plan_runs([_run_of(lab, "sess-a"), _run_of(lab, "sess-b")], {}, {}, PROJECT)
    assert [cost_ledger.split_run_id(item.run.run_id)[3] for item in planned] == [1, 2]
    # Another backfill wrote attempt 1 from a snapshot without this plan's line.
    lab.book(f"{PROJECT}:backfill:{DISPATCH}:1", tokens=TOKENS_B, window=WINDOW_A,
             identity=f"{LANE}:another-session")

    summaries = backfill.apply_runs(planned, lab.ledger, dry_run=False)

    assert summaries[LANE].new_lines == 2 and summaries[LANE].failures == 0
    attempts = {line["transcript_identity"]: cost_ledger.split_run_id(line["run_id"])[3]
                for line in lab.lines() if line.get("transcript_identity")}
    assert attempts == {f"{LANE}:another-session": 1, f"{LANE}:sess-a": 3, f"{LANE}:sess-b": 2}
    assert len(cost_ledger.aggregate_runs(lab.ledger).runs) == 3


def _apply_in_a_child(payload):
    """A child process: write one plan on the shared ledger once both children are ready."""
    ledger_path, plans, gate, results = payload
    gate.wait(timeout=30)
    summaries = backfill.apply_runs(plans, Path(ledger_path), dry_run=False)
    results.put({provider: summary.failures for provider, summary in summaries.items()})


def test_two_processes_backfilling_one_dispatch_keep_both_spends(tmp_path, monkeypatch):
    """Two real processes, one dispatch, one ledger: neither write may drop the other's spend."""
    lab = Lab(tmp_path, monkeypatch)
    lab.ledger.parent.mkdir(parents=True, exist_ok=True)
    lab.transcript("sess-a", window=WINDOW_A, tokens=TOKENS_A)
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_HEAVY)
    first, second = _plans_from_one_snapshot(lab, "sess-a", "sess-b")
    assert first.run.run_id == second.run.run_id, "both children must start from the same attempt"

    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_apply_in_a_child,
                        args=((str(lab.ledger), [planned], gate, results),))
        for planned in (first, second)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0, f"child exited with {process.exitcode}"

    assert [results.get(timeout=10) for _ in processes] == [{LANE: 0}, {LANE: 0}]
    written = lab.lines()
    assert len(written) == 2, written
    assert len({line["run_id"] for line in written}) == 2, written
    assert {line["transcript_identity"] for line in written} == {f"{LANE}:sess-a", f"{LANE}:sess-b"}
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert len(totals.runs) == 2
    assert {record["transcript_identity"] for record in totals.runs.values()} == {
        f"{LANE}:sess-a", f"{LANE}:sess-b",
    }
    assert totals.cost_usd == pytest.approx(sum(line["cost_usd"] for line in written))


def test_a_backfill_charge_on_an_existing_run_id_is_refused_and_writes_nothing(tmp_path):
    """Another transcript holds the attempt: the ledger refuses the charge, nothing written."""
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    ledger.parent.mkdir(parents=True)
    live = dataclasses.replace(_cost("builder", f"{PROJECT}:backfill:{DISPATCH}:1"),
                               transcript_identity=f"{LANE}:another-session")
    cost_ledger.record_run(live, ledger_path=ledger)

    with pytest.raises(cost_ledger.BackfillRunIdTaken):
        cost_ledger.record_run(dataclasses.replace(live, backfill=True,
                                                   transcript_identity=f"{LANE}:sess-a"),
                               ledger_path=ledger)

    assert [line["run_id"] for line in _lines(ledger)] == [live.run_id]


def test_the_ledger_refuses_a_backfill_charge_whose_transcript_is_booked(tmp_path):
    """The same transcript is booked already: the refusal carries that line, nothing written."""
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    ledger.parent.mkdir(parents=True)
    live = _cost("builder", f"{PROJECT}:backfill:{DISPATCH}:1")
    booked = cost_ledger.record_run(live, ledger_path=ledger)

    with pytest.raises(cost_ledger.BackfillTranscriptBooked) as refused:
        cost_ledger.record_run(dataclasses.replace(live, backfill=True,
                                                   run_id=f"{PROJECT}:backfill:{DISPATCH}:2"),
                               ledger_path=ledger)

    assert refused.value.record["record_id"] == booked["record_id"]
    assert refused.value.record["transcript_identity"] == f"{LANE}:sess-a"
    assert [line["record_id"] for line in _lines(ledger)] == [booked["record_id"]]


def test_a_correction_on_an_existing_line_is_still_written_as_before(tmp_path, monkeypatch):
    """A correction keeps its target's ``run_id``: the guard never renumbers it."""
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a")
    legacy = lab.book(LEGACY_RUN)
    planned = backfill.plan_runs([_run_of(lab, "sess-a")], {},
                                 backfill.load_live_runs(lab.ledger, PROJECT), PROJECT)
    assert planned[0].run.kind == "correction"
    assert planned[0].run.run_id == legacy["run_id"], "the correction carries the live run's id"

    summaries = backfill.apply_runs(planned, lab.ledger, dry_run=False)

    assert summaries[LANE].corrections == 1 and summaries[LANE].failures == 0
    correction = lab.lines()[-1]
    assert correction["kind"] == "correction"
    assert correction["run_id"] == legacy["run_id"]
    assert correction["supersedes"] == legacy["record_id"]


def test_a_live_charge_with_an_existing_run_id_is_written_as_before(tmp_path):
    """A live producer decides its own ``run_id``: the guard is a backfill charge's alone."""
    ledger = tmp_path / "events" / "provider_costs.ndjson"
    ledger.parent.mkdir(parents=True)
    live = _cost("builder", f"{PROJECT}:envelope_govern:{DISPATCH}:1")

    cost_ledger.record_run(live, ledger_path=ledger)
    cost_ledger.record_run(live, ledger_path=ledger)

    assert [line["run_id"] for line in _lines(ledger)] == [live.run_id, live.run_id]


def test_a_charge_taken_past_the_retry_bound_is_a_failure_and_writes_nothing(
    tmp_path, monkeypatch, capsys,
):
    """A ledger that keeps saying taken must stop the charge, not loop on it or write it blindly."""
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a")
    planned = backfill.plan_runs([_run_of(lab, "sess-a")], {}, {}, PROJECT)
    calls = []

    def always_taken(run, *, ledger_path=None):
        calls.append(run.run_id)
        raise cost_ledger.BackfillRunIdTaken(f"run_id {run.run_id} is booked already")

    monkeypatch.setattr(cost_ledger, "record_run", always_taken)

    summaries = backfill.apply_runs(planned, lab.ledger, dry_run=False)

    assert len(calls) == backfill.BACKFILL_RETRY_LIMIT + 1
    assert summaries[LANE].failures == 1 and summaries[LANE].new_lines == 0
    assert backfill._exit_code(summaries) == backfill.EXIT_FAILED
    assert lab.lines() == []
    error = capsys.readouterr().err
    assert DISPATCH in error and f"{backfill.BACKFILL_RETRY_LIMIT} renumberings" in error


def test_a_lock_file_that_cannot_be_created_stops_the_run_with_nothing_written(
    tmp_path, monkeypatch, capsys,
):
    """The ``CostLedgerWriteError`` branch of ``main``: an unknown write outcome stops the run.

    The read may go on without the lock file (it says so, ``unlocked_read``), so the
    run reaches its first write; there the lock file is the write's precondition and
    cannot be created. The lock path is a dangling symlink into a directory that does
    not exist rather than a directory: ``open(dir, O_RDONLY|O_CREAT)`` and ``flock``
    both succeed on macOS, so a directory is not a lock that cannot be taken.
    """
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a")
    lock = provider_costs.ledger_lock_path(lab.ledger)
    os.symlink(str(lab.data / "missing" / lock.name), lock)

    assert lab.run_cli("--apply") == backfill.EXIT_FAILED

    assert not lab.ledger.exists(), "nothing landed"
    assert not cost_ledger.spool_dir_for(lab.ledger).exists(), "nothing went to the spool either"
    assert "the outcome of a write is unknown, stopping" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The same transcript is booked once: identity comes before the run id under the
# writer's lock (K6b-3 round 3)
# ---------------------------------------------------------------------------


def test_the_same_charge_plan_applied_twice_books_the_spend_once(tmp_path, monkeypatch, capsys):
    """Codex's reproduction: a second apply of one plan must not book the same transcript twice."""
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a", window=WINDOW_A, tokens=TOKENS_A)
    planned, = _plans_from_one_snapshot(lab, "sess-a")
    assert backfill.apply_runs([planned], lab.ledger, dry_run=False)[LANE].new_lines == 1
    capsys.readouterr()

    summaries = backfill.apply_runs([planned], lab.ledger, dry_run=False)

    assert summaries[LANE].skips == 1, "the second apply is already booked, not a new line"
    assert summaries[LANE].new_lines == 0 and summaries[LANE].corrections == 0
    assert summaries[LANE].failures == 0
    assert backfill._exit_code(summaries) == 0, "already booked is not a failure"
    written = lab.lines()
    assert len(written) == 1, written
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert len(totals.runs) == 1, "one transcript is one run, however many times it is applied"
    assert totals.cost_usd == pytest.approx(written[0]["cost_usd"])


def test_the_same_transcript_with_changed_usage_is_corrected_once(tmp_path, monkeypatch, capsys):
    """A second plan of one transcript with other usage writes one correction, not a second charge."""
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a", tokens=TOKENS_A)
    snapshot = backfill.load_live_runs(lab.ledger, PROJECT)
    first = backfill.plan_runs([_run_of(lab, "sess-a")], {}, snapshot, PROJECT)
    backfill.apply_runs(first, lab.ledger, dry_run=False)
    lab.transcript("sess-a", tokens=TOKENS_HEAVY)
    second = backfill.plan_runs([_run_of(lab, "sess-a")], {}, snapshot, PROJECT)
    assert second[0].run.kind == "charge", "both plans were numbered on the same, empty snapshot"
    assert second[0].run.run_id == first[0].run.run_id
    capsys.readouterr()

    summaries = backfill.apply_runs(second, lab.ledger, dry_run=False)

    charge, correction = lab.lines()
    assert len(lab.lines()) == 2
    assert correction["kind"] == "correction"
    assert correction["run_id"] == charge["run_id"], "the correction keeps the booked run's id"
    assert correction["supersedes"] == charge["record_id"]
    assert correction["reason"] == backfill.CORRECTION_REASON
    assert correction["transcript_identity"] == f"{LANE}:sess-a"
    assert summaries[LANE].corrections == 1 and summaries[LANE].new_lines == 0
    assert summaries[LANE].failures == 0
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert len(totals.runs) == 1
    effective, = totals.runs.values()
    assert effective["tokens"] == correction["tokens"], "the aggregate shows the new usage once"
    assert totals.cost_usd == pytest.approx(correction["cost_usd"])
    assert totals.cost_usd != pytest.approx(charge["cost_usd"]), "not the usage it replaced"


def test_two_processes_backfilling_the_same_transcript_book_it_once(tmp_path, monkeypatch):
    """Two real processes, one transcript, one ledger: the transcript's spend counts once."""
    lab = Lab(tmp_path, monkeypatch)
    lab.ledger.parent.mkdir(parents=True, exist_ok=True)
    lab.transcript("sess-a", window=WINDOW_A, tokens=TOKENS_A)
    first, = _plans_from_one_snapshot(lab, "sess-a")
    second, = _plans_from_one_snapshot(lab, "sess-a")
    assert first.run.run_id == second.run.run_id, "both children start from the same attempt"

    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(target=_apply_in_a_child,
                        args=((str(lab.ledger), [planned], gate, results),))
        for planned in (first, second)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0, f"child exited with {process.exitcode}"

    assert [results.get(timeout=10) for _ in processes] == [{LANE: 0}, {LANE: 0}]
    written = lab.lines()
    assert len(written) == 1, written
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert len(totals.runs) == 1
    assert totals.cost_usd == pytest.approx(written[0]["cost_usd"])


def test_every_planned_charge_names_its_transcript_identity(tmp_path, monkeypatch):
    """Point 3's measurement: the planner cannot produce an identity-less charge.

    Both references are covered: a transcript whose ``cwd`` names a dispatch (its ref is
    the dispatch id) and one whose ``cwd`` names none (its ref is the session id).
    """
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a", cwd=WORKTREE_CWD)
    lab.transcript("sess-b", window=WINDOW_B, tokens=TOKENS_B, cwd="/")

    planned = backfill.plan_runs(backfill.scan([lab.lane], date(2026, 10, 1)), {}, {}, PROJECT)

    assert len(planned) == 2, planned
    assert {planned_item.run.transcript_identity for planned_item in planned} == {
        f"{LANE}:sess-a", f"{LANE}:sess-b",
    }


def test_an_identityless_charge_on_a_taken_run_id_fails_and_writes_nothing(
    tmp_path, monkeypatch, capsys,
):
    """A charge that cannot name its transcript is never renumbered onto another one's line."""
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("sess-a")
    planned, = backfill.plan_runs([_run_of(lab, "sess-a")], {}, {}, PROJECT)
    blind = dataclasses.replace(planned.run, transcript_identity=None)
    lab.book(blind.run_id, tokens=TOKENS_HEAVY, identity=f"{LANE}:another-session")

    summaries = backfill.apply_runs(
        [backfill.PlannedRun(LANE, blind, None, False)], lab.ledger, dry_run=False,
    )

    assert summaries[LANE].failures == 1 and summaries[LANE].new_lines == 0
    assert backfill._exit_code(summaries) == backfill.EXIT_FAILED
    assert [line["transcript_identity"] for line in lab.lines()] == [f"{LANE}:another-session"]
    error = capsys.readouterr().err
    assert blind.run_id in error
    assert "no transcript identity" in error


# ---------------------------------------------------------------------------
# The identity check compares with the effective line of the supersedes chain
# (K6b-3b, OI-2455)
# ---------------------------------------------------------------------------


def _plan_on(lab, snapshot, session="sess-a"):
    """One session's plan, numbered on ``snapshot``: the ledger as an earlier backfill read it."""
    planned = backfill.plan_runs([_run_of(lab, session)], {}, snapshot, PROJECT)
    assert len(planned) == 1, planned
    return planned[0]


def _spooled_records(lab):
    """The records waiting in the spool for this lab's backfill run, in order."""
    run_id = f"{PROJECT}:backfill:{DISPATCH}:1"
    path = cost_ledger.spool_file_for(cost_ledger.spool_dir_for(lab.ledger), run_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _apply_a_then_b(tmp_path, monkeypatch):
    """A, then B: one charge and one correction on it, both planned on the empty snapshot."""
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    snapshot = backfill.load_live_runs(lab.ledger, PROJECT)
    lab.transcript("sess-a", tokens=TOKENS_A)
    first = _plan_on(lab, snapshot)
    backfill.apply_runs([first], lab.ledger, dry_run=False)
    lab.transcript("sess-a", tokens=TOKENS_B)
    second = _plan_on(lab, snapshot)
    backfill.apply_runs([second], lab.ledger, dry_run=False)
    charge, correction = lab.lines()
    assert (charge["kind"], correction["kind"]) == ("charge", "correction")
    assert correction["supersedes"] == charge["record_id"]
    return lab, snapshot, first, charge, correction


def test_a_repeated_backfill_after_a_correction_writes_no_second_correction(tmp_path, monkeypatch):
    """A, then B, then B: the correction already says B, so the third apply is a skip."""
    lab, snapshot, _first, charge, correction = _apply_a_then_b(tmp_path, monkeypatch)
    third = _plan_on(lab, snapshot)

    summaries = backfill.apply_runs([third], lab.ledger, dry_run=False)

    assert summaries[LANE].skips == 1, "the correction that is the effective line already says B"
    assert summaries[LANE].corrections == 0 and summaries[LANE].new_lines == 0
    assert summaries[LANE].failures == 0
    written = lab.lines()
    assert [line["kind"] for line in written] == ["charge", "correction"], written
    assert written[-1]["supersedes"] == charge["record_id"]
    assert written[-1]["record_id"] == correction["record_id"], "no second correction beside the first"
    totals = cost_ledger.aggregate_runs(lab.ledger)
    effective, = totals.runs.values()
    assert effective["tokens"] == dict(TOKENS_B), "the aggregate stays B"


def test_a_transcript_back_at_its_older_usage_is_corrected_back_not_skipped(tmp_path, monkeypatch):
    """A, then B, then A: the newest plan wins, so the effective line is corrected back to A."""
    lab, snapshot, _first, charge, correction = _apply_a_then_b(tmp_path, monkeypatch)
    lab.transcript("sess-a", tokens=TOKENS_A)
    third = _plan_on(lab, snapshot)

    summaries = backfill.apply_runs([third], lab.ledger, dry_run=False)

    assert summaries[LANE].corrections == 1, "A differs from the effective B line: one correction"
    assert summaries[LANE].skips == 0, "a superseded line that happens to say A is not a booking"
    assert summaries[LANE].new_lines == 0 and summaries[LANE].failures == 0
    written = lab.lines()
    assert [line["kind"] for line in written] == ["charge", "correction", "correction"], written
    back_to_a = written[-1]
    assert back_to_a["supersedes"] == correction["record_id"], "the effective line, never the original"
    assert back_to_a["run_id"] == charge["run_id"], "a correction keeps the run it corrects"
    assert back_to_a["tokens"] == dict(TOKENS_A)
    totals = cost_ledger.aggregate_runs(lab.ledger)
    effective, = totals.runs.values()
    assert effective["tokens"] == dict(TOKENS_A)
    assert totals.cost_usd == pytest.approx(back_to_a["cost_usd"])


def test_a_correction_waiting_in_the_spool_is_the_effective_line(tmp_path, monkeypatch):
    """B's correction waits for the replay: the check must read that line, not the superseded charge."""
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    snapshot = backfill.load_live_runs(lab.ledger, PROJECT)
    lab.transcript("sess-a", tokens=TOKENS_A)
    first = _plan_on(lab, snapshot)
    backfill.apply_runs([first], lab.ledger, dry_run=False)
    charge, = lab.lines()
    lab.transcript("sess-a", tokens=TOKENS_B)
    second = _plan_on(lab, snapshot)
    real_append, failed = cost_ledger._append_record, []

    def append_failing_once(fh, record):
        if not failed:
            failed.append(record)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return real_append(fh, record)

    monkeypatch.setattr(cost_ledger, "_append_record", append_failing_once)
    assert backfill.apply_runs([second], lab.ledger, dry_run=False)[LANE].corrections == 1
    spooled = _spooled_records(lab)
    assert [record["kind"] for record in spooled] == ["correction"], "the correction waits in the spool"
    assert len(lab.lines()) == 1, "the ledger still holds only the charge"
    monkeypatch.setattr(cost_ledger, "_append_record", real_append)
    before = lab.ledger.read_bytes()

    summaries = backfill.apply_runs([second], lab.ledger, dry_run=False)

    assert summaries[LANE].skips == 1, "the spooled correction already says B"
    assert summaries[LANE].corrections == 0 and summaries[LANE].failures == 0
    assert lab.ledger.read_bytes() == before
    assert [record["record_id"] for record in _spooled_records(lab)] == [spooled[0]["record_id"]]

    summaries = backfill.apply_runs([first], lab.ledger, dry_run=False)

    assert summaries[LANE].corrections == 1, "A differs from the spooled effective line"
    assert summaries[LANE].skips == 0
    back_to_a, = _spooled_records(lab)[1:]
    assert back_to_a["supersedes"] == spooled[0]["record_id"], "superseding the waiting correction, not the charge"
    assert back_to_a["run_id"] == charge["run_id"]
    assert len(lab.lines()) == 1, "both corrections wait behind the charge in the spool"


def test_a_correction_without_an_identity_still_holds_the_transcript(tmp_path, monkeypatch):
    """The chain is followed by ``supersedes``: a correction that repeats no identity is still effective.

    Reading only identity-carrying lines would find the superseded charge replaced and
    take the transcript for unbooked, booking its spend a second time.
    """
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a", tokens=TOKENS_A)
    planned, = backfill.plan_runs([_run_of(lab, "sess-a")], {}, {}, PROJECT)
    assert backfill.apply_runs([planned], lab.ledger, dry_run=False)[LANE].new_lines == 1
    charge, = lab.lines()
    correction = cost_ledger.record_run(dataclasses.replace(
        planned.run, kind="correction", supersedes=charge["record_id"],
        reason="a manual correction", transcript_identity=None,
    ), ledger_path=lab.ledger)
    assert "transcript_identity" not in correction

    summaries = backfill.apply_runs([planned], lab.ledger, dry_run=False)

    assert summaries[LANE].skips == 1, "the identity-less correction is the effective line, not 'not booked'"
    assert summaries[LANE].new_lines == 0 and summaries[LANE].corrections == 0
    assert len(lab.lines()) == 2
    assert len(cost_ledger.aggregate_runs(lab.ledger).runs) == 1, "one transcript stays one run"


def test_a_line_with_a_malformed_supersedes_does_not_break_the_check(tmp_path, monkeypatch):
    """A hand-edited line that names no record in ``supersedes`` is not part of a chain, and never raises."""
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch()
    lab.transcript("sess-a", tokens=TOKENS_A)
    planned, = backfill.plan_runs([_run_of(lab, "sess-a")], {}, {}, PROJECT)
    assert backfill.apply_runs([planned], lab.ledger, dry_run=False)[LANE].new_lines == 1
    charge, = lab.lines()
    corrupt = dict(charge, kind="correction", supersedes=[], reason="hand-edited", record_id="0" * 32)
    with lab.ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(corrupt) + "\n")

    summaries = backfill.apply_runs([planned], lab.ledger, dry_run=False)

    assert summaries[LANE].skips == 1, "the charge still holds its transcript"
    assert summaries[LANE].new_lines == 0 and summaries[LANE].corrections == 0
    assert summaries[LANE].failures == 0
