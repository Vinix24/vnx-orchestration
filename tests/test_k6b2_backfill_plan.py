"""tests/test_k6b2_backfill_plan.py — what a backfill would book, decided without writing (K6b-2).

Covers the planning half of ``scripts/cost_ledger_backfill.py`` (ADR-049, OI-2419):
``plan_runs`` maps each transcript to a correction on a live line, a charge beside
it, or a skip. Every ledger lives under ``tmp_path``; the transcripts are built in
memory. The two rules this file pins down:

- a skip needs the identity: a line that matches on every field but carries no
  ``transcript_identity`` is written as a correction that stamps it (codex finding
  on #2304 round 3), so a later ``--since`` cannot let another session claim it;
- a legacy line needs evidence: exact tokens or an overlapping window, never
  "the nearest line" — an unrelated record is not this session's spend (advisory).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import cost_ledger_backfill as backfill  # noqa: E402
import harness_run_cost  # noqa: E402
from token_harvest import MessageUsage  # noqa: E402

PROJECT = "vnx-dev"
DISPATCH = "dlv-b2"
PROVIDER = "deepseek-harness"
MODEL = "deepseek-flash"
LEGACY_RUN = f"{PROJECT}:envelope_govern:{DISPATCH}:1"

TOKENS_A = {"input_uncached": 1000, "cache_hit": 5000, "cache_write_5m": 100,
            "cache_write_1h": 0, "output": 200}
TOKENS_B = {"input_uncached": 2000, "cache_hit": 0, "cache_write_5m": 0,
            "cache_write_1h": 0, "output": 400}
WINDOW_A = ("2026-10-05T02:00:00Z", "2026-10-05T02:10:00Z")
WINDOW_B = ("2026-10-06T04:00:00Z", "2026-10-06T04:10:00Z")
SEPTEMBER = ("2026-09-01T00:00:00Z", "2026-09-01T00:30:00Z")


def _run(session, tokens, window=WINDOW_A, dispatch=DISPATCH, provider=PROVIDER):
    """One transcript read as a run: a single assistant message carrying ``tokens``."""
    started, ended = window
    message = MessageUsage(message_id=f"m-{session}", timestamp=started, tokens=dict(tokens))
    return backfill.TranscriptRun(
        provider=provider, session_id=session, path=f"/transcripts/{session}.jsonl",
        dispatch_id=dispatch, messages=[message], reported_model=MODEL,
        occurred_from=started, occurred_to=ended,
    )


class Ledger:
    """A ledger under ``tmp_path`` plus the dispatch rows the plan reads."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "events" / "provider_costs.ndjson"
        self.path.parent.mkdir(parents=True)
        self.rows = {}

    def dispatch(self, dispatch_id=DISPATCH, role="backend-developer", model=MODEL):
        self.rows[dispatch_id] = backfill.DispatchRow(role, model)

    def book(self, run_id, tokens, window=WINDOW_A, *, identity=None, provider=PROVIDER,
             model=MODEL, role="builder", kind="charge", supersedes=None, reason=None,
             segments=None):
        record = cost_ledger.record_run(cost_ledger.RunCost(
            run_id=run_id, project_id=PROJECT, role=role, provider=provider, model=model,
            occurred_from=window[0], occurred_to=window[1], tokens=tokens,
            usage_completeness=harness_run_cost.usage_completeness(tokens),
            billing=harness_run_cost.BILLING_BY_PROVIDER[provider], segments=segments,
            kind=kind, supersedes=supersedes, reason=reason, transcript_identity=identity,
        ), ledger_path=self.path)
        return dict(record)

    def write(self, run):
        """Write a planned line, as K6b-3's writer will: the plan itself never writes."""
        return dict(cost_ledger.record_run(run, ledger_path=self.path))

    def plan(self, *runs):
        index = backfill.load_live_runs(self.path, PROJECT)
        return backfill.plan_runs(list(runs), self.rows, index, PROJECT)

    def lines(self):
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _only(plans, kind):
    assert len(plans) == 1, plans
    assert plans[0].run is not None and plans[0].run.kind == kind
    return plans[0].run


def test_a_legacy_line_identical_but_for_the_identity_is_corrected_not_skipped(tmp_path):
    """The codex finding: the write that matches a legacy line is what persists the identity."""
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    session_a = _run("sess-a", TOKENS_A)
    legacy = ledger.book(LEGACY_RUN, TOKENS_A, segments=harness_run_cost.price_segments(
        PROVIDER, MODEL, session_a.messages, TOKENS_A))
    assert "transcript_identity" not in legacy
    assert legacy["segments"] == harness_run_cost.price_segments(
        PROVIDER, MODEL, session_a.messages, TOKENS_A)

    correction = _only(ledger.plan(session_a), "correction")
    assert correction.supersedes == legacy["record_id"]
    assert correction.transcript_identity == "deepseek-harness:sess-a"

    stamped = ledger.write(correction)
    # A later plan whose --since leaves session A out: B adds its own spend beside it.
    charge = _only(ledger.plan(_run("sess-b", TOKENS_B, WINDOW_B)), "charge")
    assert charge.run_id == f"{PROJECT}:backfill:{DISPATCH}:1"
    assert charge.supersedes is None
    ledger.write(charge)

    assert [line for line in ledger.lines() if line.get("supersedes") == stamped["record_id"]] == []
    aggregate = cost_ledger.aggregate_runs(ledger.path)
    assert set(aggregate.runs) == {LEGACY_RUN, f"{PROJECT}:backfill:{DISPATCH}:1"}
    assert aggregate.runs[LEGACY_RUN]["transcript_identity"] == "deepseek-harness:sess-a"
    assert sum(record["tokens"]["input_uncached"] for record in aggregate.runs.values()) == 3000


def test_a_line_that_names_the_session_and_says_the_same_thing_is_skipped(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    ledger.book(LEGACY_RUN, TOKENS_A)
    written = ledger.write(ledger.plan(_run("sess-a", TOKENS_A))[0].run)

    [again] = ledger.plan(_run("sess-a", TOKENS_A))

    assert again.skipped is True
    assert again.run is None
    assert again.live["record_id"] == written["record_id"]
    assert len(ledger.lines()) == 2


def test_a_legacy_line_with_an_unrelated_window_and_other_tokens_is_not_claimed(tmp_path):
    """The advisory: no evidence means no claim — the run books its own charge."""
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    ledger.book(LEGACY_RUN, TOKENS_B, SEPTEMBER)

    [planned] = ledger.plan(_run("sess-a", TOKENS_A, WINDOW_A))

    assert planned.live is None
    assert planned.skipped is False
    assert planned.run.kind == "charge"
    assert planned.run.supersedes is None
    assert planned.run.transcript_identity == "deepseek-harness:sess-a"


def test_a_legacy_line_with_an_overlapping_window_is_claimed_once(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    legacy = ledger.book(LEGACY_RUN, TOKENS_B, ("2026-10-05T02:00:00Z", "2026-10-05T02:30:00Z"))
    first = _run("sess-a", TOKENS_A, ("2026-10-05T02:05:00Z", "2026-10-05T02:10:00Z"))
    second = _run("sess-b", TOKENS_A, ("2026-10-05T02:20:00Z", "2026-10-05T02:25:00Z"))

    plans = ledger.plan(first, second)

    correction = _only([plan for plan in plans if plan.run is not None and plan.run.kind == "correction"],
                       "correction")
    assert correction.supersedes == legacy["record_id"]
    assert correction.transcript_identity == "deepseek-harness:sess-a"
    charges = [plan.run for plan in plans if plan.run is not None and plan.run.kind == "charge"]
    assert [charge.transcript_identity for charge in charges] == ["deepseek-harness:sess-b"]


def test_a_legacy_line_with_exactly_equal_tokens_is_claimed_without_an_overlap(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    legacy = ledger.book(LEGACY_RUN, TOKENS_A, SEPTEMBER)

    correction = _only(ledger.plan(_run("sess-a", TOKENS_A, WINDOW_A)), "correction")

    assert correction.supersedes == legacy["record_id"]
    assert correction.transcript_identity == "deepseek-harness:sess-a"


def test_a_follow_up_session_does_not_take_the_live_line(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    legacy = ledger.book(LEGACY_RUN, TOKENS_A, ("2026-10-05T02:00:00Z", "2026-10-05T02:30:00Z"))
    big = _run("sess-big", TOKENS_A, ("2026-10-05T02:00:00Z", "2026-10-05T02:30:00Z"))
    tiny = _run("sess-tiny", TOKENS_B, WINDOW_B)

    plans = ledger.plan(big, tiny)

    assert plans[0].run.kind == "correction" and plans[0].run.supersedes == legacy["record_id"]
    assert plans[1].run.kind == "charge"
    assert plans[1].run.run_id == f"{PROJECT}:backfill:{DISPATCH}:1"
    assert plans[1].run.tokens["input_uncached"] == 2000


def test_two_unbooked_sessions_of_one_dispatch_get_two_run_ids(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    late = _run("sess-a-late", TOKENS_B, WINDOW_B)
    early = _run("sess-z-early", TOKENS_A, WINDOW_A)

    plans = ledger.plan(late, early)

    assert [plan.run.run_id for plan in plans] == [
        f"{PROJECT}:backfill:{DISPATCH}:1", f"{PROJECT}:backfill:{DISPATCH}:2"]
    assert [plan.run.tokens["input_uncached"] for plan in plans] == [1000, 2000]


def test_attempts_continue_after_a_live_backfill_line(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    ledger.book(f"{PROJECT}:backfill:{DISPATCH}:1", TOKENS_B,
                provider="glm-harness", model="glm-5.2")

    plans = ledger.plan(_run("sess-early", TOKENS_A, WINDOW_A), _run("sess-late", TOKENS_B, WINDOW_B))

    assert [plan.run.run_id for plan in plans] == [
        f"{PROJECT}:backfill:{DISPATCH}:2", f"{PROJECT}:backfill:{DISPATCH}:3"]


def test_a_later_session_of_the_same_dispatch_adds_spend_never_replaces_it(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    booked_a = ledger.write(ledger.plan(_run("sess-a", TOKENS_A, WINDOW_A))[0].run)

    [planned_b] = ledger.plan(_run("sess-b", TOKENS_B, WINDOW_B))
    assert planned_b.run.kind == "charge"
    assert planned_b.run.run_id == f"{PROJECT}:backfill:{DISPATCH}:2"
    ledger.write(planned_b.run)

    aggregate = cost_ledger.aggregate_runs(ledger.path)
    assert set(aggregate.runs) == {f"{PROJECT}:backfill:{DISPATCH}:1",
                                   f"{PROJECT}:backfill:{DISPATCH}:2"}
    assert aggregate.runs[f"{PROJECT}:backfill:{DISPATCH}:1"]["record_id"] == booked_a["record_id"]
    assert sum(record["tokens"]["input_uncached"] for record in aggregate.runs.values()) == 3000


def test_a_correction_stamps_the_transcript_identity_on_the_live_run(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    legacy = ledger.book(LEGACY_RUN, TOKENS_B, ("2026-10-05T02:00:00Z", "2026-10-05T02:30:00Z"))

    plans = ledger.plan(_run("sess-c2", TOKENS_A, WINDOW_A), _run("sess-c2-other", TOKENS_A, WINDOW_B))

    assert plans[0].run.kind == "correction"
    assert plans[0].run.run_id == LEGACY_RUN
    assert plans[0].run.supersedes == legacy["record_id"]
    assert plans[0].run.transcript_identity == "deepseek-harness:sess-c2"
    assert plans[1].run.kind == "charge"
    assert plans[1].run.run_id == f"{PROJECT}:backfill:{DISPATCH}:1"


def test_identity_is_the_shape_harness_run_cost_owns():
    sample = _run("sess-identity", TOKENS_A)

    assert sample.identity == harness_run_cost.transcript_identity(PROVIDER, "sess-identity")
    assert sample.identity == "deepseek-harness:sess-identity"


@pytest.mark.parametrize("window", [("not-a-timestamp", "also-not-one"), (None, None), ()])
def test_a_line_with_an_unreadable_window_is_no_evidence(tmp_path, window):
    """A malformed or missing window claims nothing: the line stays and the run books a charge.

    The writer refuses such a line, so the index is built in memory here, the way
    a hand-edited or half-written line would reach the plan.
    """
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    record = {"record_id": "abc", "run_id": LEGACY_RUN, "provider": PROVIDER, "role": "builder",
              "model": MODEL, "tokens": dict(TOKENS_B)}
    record.update(zip(("occurred_from", "occurred_to"), window))

    plans = backfill.plan_runs([_run("sess-a", TOKENS_A, WINDOW_A)], ledger.rows,
                               {DISPATCH: [record]}, PROJECT)

    assert plans[0].live is None
    assert plans[0].run.kind == "charge"


def test_a_line_of_another_lane_is_never_claimed(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    ledger.book(LEGACY_RUN, TOKENS_A, WINDOW_A, provider="glm-harness", model="glm-5.2")

    [planned] = ledger.plan(_run("sess-a", TOKENS_A, WINDOW_A))

    assert planned.live is None
    assert planned.run.kind == "charge"


def test_a_line_that_names_another_session_is_never_claimed(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.dispatch()
    ledger.book(LEGACY_RUN, TOKENS_A, WINDOW_A, identity="deepseek-harness:sess-elsewhere")

    [planned] = ledger.plan(_run("sess-a", TOKENS_A, WINDOW_A))

    assert planned.live is None
    assert planned.run.kind == "charge"
