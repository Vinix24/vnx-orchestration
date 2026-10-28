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
"""

from __future__ import annotations

import json
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
