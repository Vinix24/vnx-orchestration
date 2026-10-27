"""tests/test_k6_cost_backfill.py — the harness-transcript backfill (K6, ADR-043).

Track ``kosten-per-model-in-het-grootboek``, deliverable K6. Covers
``scripts/cost_ledger_backfill.py`` against real files (fixture transcripts in
``tmp_path``, a real ledger written through ``cost_ledger.record_run``):

- a transcript whose dispatch already has a live line becomes one *correction*
  on that run's ``run_id``, with ``supersedes`` and ``reason``, never a new run;
- a transcript without a live line becomes one *charge* under the backfill
  producer, marked ``backfill: true``; the aggregate counts each run once;
- two unbooked sessions of one dispatch get ``:1`` and ``:2`` (attempts are
  reserved across the whole plan, in transcript-end-then-path order), and a
  live backfill line pushes the new attempts past its own number;
- a second ``--apply`` writes nothing and changes no run id (idempotent on
  ``run_id`` and tokens);
- a run across the DeepSeek peak boundary carries two segments;
- ``--dry-run`` (the default) writes no line and no spool file, and prints the
  per-provider counts;
- a transcript whose ``cwd`` names no dispatch is counted, not dropped, with
  ``role`` ``unknown:no_dispatch_row``;
- a message rewritten by the harness counts once (its last rewrite wins);
- an identical live line is not corrected (silently skipped).

The ledger accepts ``unknown:<reason>`` as a role so a run without a dispatch
row is booked with the reason it has, never under an invented purpose role.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts" / "lib"), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cost_ledger  # noqa: E402
import cost_ledger_backfill  # noqa: E402
import harness_run_cost  # noqa: E402

PROJECT = "vnx-dev"
FULL_USAGE = {
    "input_tokens": 1000,
    "cache_read_input_tokens": 5000,
    "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 200,
}


def _usage(input_tokens=1000, output_tokens=200, cache_read=5000, write_5m=100, write_1h=0):
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        },
        "output_tokens": output_tokens,
    }


def _entry(message_id: str, timestamp: str, usage: dict, cwd: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {"role": "assistant", "id": message_id, "usage": usage},
    }


def _tokens(**over) -> dict:
    tokens = {"input_uncached": 0, "cache_hit": 0, "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}
    tokens.update(over)
    return tokens


def _lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Lab:
    """The fixture store: dispatch rows under the pinned dispatch dir, transcripts in tmp_path."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
        self.tmp_path = tmp_path
        self.dispatch_dir = Path(os.environ["VNX_DISPATCH_DIR"])
        self.ledger = tmp_path / "events" / "provider_costs.ndjson"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    def lane_root(self, lane: str) -> Path:
        root = self.tmp_path / "harness" / lane
        (root / "projects").mkdir(parents=True, exist_ok=True)
        return root

    def dispatch(self, dispatch_id: str, role: str = "backend-developer",
                 provider: str = "deepseek-harness", model: str = "deepseek-flash") -> None:
        target = self.dispatch_dir / "pending" / dispatch_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "dispatch-spec.json").write_text(json.dumps({
            "dispatch_id": dispatch_id, "role": role, "provider": provider, "model": model,
        }), encoding="utf-8")

    def transcript(self, lane: str, session: str, cwd: str, entries: list) -> Path:
        project_dir = self.lane_root(lane) / "projects" / "-tmp-fixture-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session}.jsonl"
        path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
        return path

    def live(self, run_id: str, provider: str, model: str, role: str, tokens: dict,
             occurred_from: str = "2026-10-05T00:00:00+00:00",
             occurred_to: str = "2026-10-05T00:30:00+00:00",
             segments: list | None = None) -> dict:
        record = cost_ledger.record_run(cost_ledger.RunCost(
            run_id=run_id, project_id=PROJECT, role=role, provider=provider, model=model,
            occurred_from=occurred_from, occurred_to=occurred_to, tokens=tokens,
            usage_completeness=harness_run_cost.usage_completeness(tokens),
            billing=harness_run_cost.BILLING_BY_PROVIDER[provider], segments=segments,
        ), ledger_path=self.ledger)
        return dict(record)

    def apply(self, *lane_roots: Path, apply: bool = True) -> int:
        argv = ["--ledger", str(self.ledger)]
        for root in lane_roots:
            argv.extend(["--harness-root", str(root)])
        if apply:
            argv.append("--apply")
        return cost_ledger_backfill.main(argv)


def _deepseek_entry(message_id="m1", timestamp="2026-10-05T02:00:00Z", usage=None, cwd=None) -> dict:
    return _entry(message_id, timestamp, usage or dict(FULL_USAGE), cwd or "unused")


def test_a_live_run_is_corrected_and_superseded(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a1")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a1"
    lab.transcript("deepseek-harness", "sess-a1", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
        _deepseek_entry("m1", "2026-10-05T02:00:05Z", dict(FULL_USAGE), cwd),
    ])
    live = lab.live("vnx-dev:envelope_govern:dlv-a1:1", "deepseek-harness", "deepseek-flash",
                    "builder", _tokens(input_uncached=7, output=3))

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 2
    correction = lines[-1]
    assert correction["kind"] == "correction"
    assert correction["run_id"] == live["run_id"]
    assert correction["supersedes"] == live["record_id"]
    assert correction["reason"]
    assert correction["backfill"] is True
    assert correction["role"] == "builder"
    assert correction["provider"] == "deepseek-harness"
    assert correction["model"] == "deepseek-flash"
    assert correction["tokens"] == {
        "input_uncached": 1000, "cache_hit": 5000, "cache_write_5m": 100,
        "cache_write_1h": 0, "output": 200,
    }
    assert correction["cost_usd"] > 0
    assert correction["cost_source"].startswith("price_table@")
    totals = cost_ledger.aggregate_runs(lab.ledger)
    assert list(totals.runs) == [live["run_id"]]
    assert totals.runs[live["run_id"]]["record_id"] == correction["record_id"]
    assert capsys.readouterr().out.strip()


def test_a_run_without_a_live_line_becomes_a_backfill_charge(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a2")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a2"
    lab.transcript("deepseek-harness", "sess-a2", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 1
    charge = lines[0]
    assert charge["kind"] == "charge"
    assert charge["run_id"] == "vnx-dev:backfill:dlv-a2:1"
    assert charge["backfill"] is True
    assert charge["role"] == "builder"
    assert charge["tokens"]["input_uncached"] == 1000
    assert cost_ledger.aggregate_runs(lab.ledger).runs[charge["run_id"]]["record_id"] == charge["record_id"]


def test_a_second_apply_writes_nothing(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a3")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a3"
    lab.transcript("deepseek-harness", "sess-a3", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
    ])
    lane = lab.lane_root("deepseek-harness")

    assert lab.apply(lane) == 0
    first = lab.ledger.read_bytes()
    assert lab.apply(lane) == 0

    assert lab.ledger.read_bytes() == first
    assert not (lab.ledger.parent / cost_ledger.SPOOL_DIR_NAME).exists()


def test_a_run_across_the_peak_boundary_has_two_segments(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a4")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a4"
    lab.transcript("deepseek-harness", "sess-a4", cwd, [
        _deepseek_entry("m1", "2026-10-05T00:30:00Z", _usage(input_tokens=100, output_tokens=10), cwd),
        _deepseek_entry("m2", "2026-10-05T02:00:00Z", _usage(input_tokens=50, output_tokens=5), cwd),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    charge = _lines(lab.ledger)[0]
    segments = charge["segments"]
    assert len(segments) == 2
    assert {segment["peak"] for segment in segments} == {True, False}
    assert sum(segment["tokens"]["input_uncached"] for segment in segments) == 150
    assert sum(segment["tokens"]["output"] for segment in segments) == 15
    assert charge["tokens"]["input_uncached"] == 150


def test_dry_run_writes_nothing_and_reports_each_provider(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a5")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a5"
    lab.transcript("deepseek-harness", "sess-a5", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
    ])
    lab.live("vnx-dev:envelope_govern:dlv-a5:1", "deepseek-harness", "deepseek-flash",
             "builder", _tokens(input_uncached=7, output=3))
    before = lab.ledger.read_bytes()

    assert lab.apply(lab.lane_root("deepseek-harness"), apply=False) == 0

    out = capsys.readouterr().out
    assert "deepseek-harness:" in out
    assert "runs_found=1" in out
    assert "with_live=1" in out
    assert "corrections=1" in out
    assert "charges=0" in out
    assert lab.ledger.read_bytes() == before
    assert not (lab.ledger.parent / cost_ledger.SPOOL_DIR_NAME).exists()


def test_a_transcript_without_a_dispatch_row_is_counted_not_dropped(tmp_path, monkeypatch, capsys):
    lab = Lab(tmp_path, monkeypatch)
    lab.transcript("deepseek-harness", "sess-a6", "/", [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), "/"),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 1
    assert lines[0]["run_id"] == "vnx-dev:backfill:sess-a6:1"
    assert lines[0]["role"] == "unknown:no_dispatch_row"
    assert lines[0]["backfill"] is True
    assert "runs_found=1" in capsys.readouterr().out
    assert cost_ledger.aggregate_runs(lab.ledger).runs[lines[0]["run_id"]]["role"] == "unknown:no_dispatch_row"


def test_a_rewritten_message_counts_once(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a7")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a7"
    lab.transcript("deepseek-harness", "sess-a7", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", _usage(input_tokens=10, output_tokens=1), cwd),
        _deepseek_entry("m1", "2026-10-05T02:00:02Z", _usage(input_tokens=100, output_tokens=7), cwd),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    charge = _lines(lab.ledger)[0]
    assert charge["tokens"]["input_uncached"] == 100
    assert charge["tokens"]["output"] == 7
    assert charge["usage_completeness"] == "complete"


def test_an_identical_live_line_is_not_corrected(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a8", role="review-gate", provider="glm-harness", model="glm-5.2")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a8"
    lab.transcript("glm-harness", "sess-a8", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
    ])
    lab.live("vnx-dev:gate_run:dlv-a8:1", "glm-harness", "glm-5.2", "review",
             _tokens(input_uncached=1000, cache_hit=5000, cache_write_5m=100, output=200))
    before = lab.ledger.read_bytes()

    assert lab.apply(lab.lane_root("glm-harness")) == 0

    assert lab.ledger.read_bytes() == before
    assert len(_lines(lab.ledger)) == 1


def test_the_ledger_accepts_an_unknown_role_with_a_reason(tmp_path):
    tokens = _tokens(input_uncached=1)
    base = cost_ledger.RunCost(
        run_id="vnx-dev:backfill:ref:1", project_id="vnx-dev", role="unknown:no_dispatch_row",
        provider="deepseek-harness", model="deepseek-flash",
        occurred_from="2026-10-05T00:00:00+00:00", occurred_to="2026-10-05T00:30:00+00:00",
        tokens=tokens, usage_completeness=harness_run_cost.usage_completeness(tokens),
        billing="metered",
    )
    assert cost_ledger.build_record(base)["role"] == "unknown:no_dispatch_row"
    for bad in ("unknown:", "unknownish", "constructor"):
        with pytest.raises(cost_ledger.InvalidRecord):
            cost_ledger.build_record(dataclasses.replace(base, role=bad))


def test_the_pip_cli_wiring_carries_every_flag(tmp_path, monkeypatch):
    import argparse

    from vnx_cli.commands import costs as costs_cli

    assert costs_cli.resolve_backfill_script(Path("/engine")) == Path("/engine/scripts/cost_ledger_backfill.py")

    script = tmp_path / "cost_ledger_backfill.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(costs_cli, "resolve_backfill_script", lambda root=None: script)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(costs_cli.subprocess, "run", fake_run)
    args = argparse.Namespace(costs_command="backfill", since="2026-10-02",
                              harness_root=["/a", "/b"], apply=True, dry_run=False,
                              ledger="/ledger")
    assert costs_cli.vnx_costs(args) == 0
    argv = seen["argv"]
    assert argv[1] == str(script)
    assert argv[2:4] == ["--since", "2026-10-02"]
    assert argv.count("--harness-root") == 2
    assert argv[argv.index("--harness-root") + 1] == "/a"
    assert "--apply" in argv
    assert argv[argv.index("--ledger") + 1] == "/ledger"
    assert "--dry-run" not in argv


def test_a_follow_up_session_does_not_take_the_live_line(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-a9")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-a9"
    lab.transcript("deepseek-harness", "sess-big", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", dict(FULL_USAGE), cwd),
    ])
    lab.transcript("deepseek-harness", "sess-tiny", cwd, [
        _deepseek_entry("m1", "2026-10-05T04:00:00Z",
                        _usage(input_tokens=10, output_tokens=1, cache_read=0, write_5m=0), cwd),
    ])
    tokens = _tokens(input_uncached=1000, cache_hit=5000, cache_write_5m=100, output=200)
    live = lab.live("vnx-dev:envelope_govern:dlv-a9:1", "deepseek-harness", "deepseek-flash",
                    "builder", tokens,
                    segments=[{"tokens": tokens, "peak": True, "ts": "2026-10-05T02:00:00Z"}])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 2
    assert lines[0]["record_id"] == live["record_id"]
    assert lines[1]["kind"] == "charge"
    assert lines[1]["run_id"] == "vnx-dev:backfill:dlv-a9:1"
    assert lines[1]["tokens"]["input_uncached"] == 10
    assert cost_ledger.aggregate_runs(lab.ledger).runs[live["run_id"]]["record_id"] == live["record_id"]


def test_two_unbooked_sessions_of_one_dispatch_get_two_run_ids(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-b1")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-b1"
    # Written earlier in the store, but it *ends* later: numbering follows the plan's
    # stable key (occurred_to, then transcript path), never the scan or creation order.
    lab.transcript("deepseek-harness", "sess-a-late", cwd, [
        _deepseek_entry("m1", "2026-10-05T04:00:00Z", _usage(input_tokens=200, output_tokens=20), cwd),
    ])
    lab.transcript("deepseek-harness", "sess-z-early", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", _usage(input_tokens=100, output_tokens=10), cwd),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 2
    by_input = {line["tokens"]["input_uncached"]: line for line in lines}
    assert by_input[100]["run_id"] == "vnx-dev:backfill:dlv-b1:1"
    assert by_input[200]["run_id"] == "vnx-dev:backfill:dlv-b1:2"
    aggregate = cost_ledger.aggregate_runs(lab.ledger)
    assert set(aggregate.runs) == {"vnx-dev:backfill:dlv-b1:1", "vnx-dev:backfill:dlv-b1:2"}
    assert sum(record["tokens"]["input_uncached"] for record in aggregate.runs.values()) == 300


def test_a_second_apply_keeps_the_run_ids_of_two_unbooked_sessions(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-b2")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-b2"
    lab.transcript("deepseek-harness", "sess-b2-early", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", _usage(input_tokens=100, output_tokens=10), cwd),
    ])
    lab.transcript("deepseek-harness", "sess-b2-late", cwd, [
        _deepseek_entry("m1", "2026-10-05T04:00:00Z", _usage(input_tokens=200, output_tokens=20), cwd),
    ])
    lane = lab.lane_root("deepseek-harness")

    assert lab.apply(lane) == 0
    first = lab.ledger.read_bytes()
    first_ids = sorted(line["run_id"] for line in _lines(lab.ledger))
    assert first_ids == ["vnx-dev:backfill:dlv-b2:1", "vnx-dev:backfill:dlv-b2:2"]

    assert lab.apply(lane) == 0

    assert lab.ledger.read_bytes() == first
    assert sorted(line["run_id"] for line in _lines(lab.ledger)) == first_ids
    assert not (lab.ledger.parent / cost_ledger.SPOOL_DIR_NAME).exists()


def test_attempts_continue_after_a_live_backfill_line(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.dispatch("dlv-b3")
    cwd = "/repo/.vnx-data/worktrees/dispatch-dlv-b3"
    live = lab.live("vnx-dev:backfill:dlv-b3:1", "glm-harness", "glm-5.2", "builder",
                    _tokens(input_uncached=7, output=3))
    lab.transcript("deepseek-harness", "sess-b3-early", cwd, [
        _deepseek_entry("m1", "2026-10-05T02:00:00Z", _usage(input_tokens=100, output_tokens=10), cwd),
    ])
    lab.transcript("deepseek-harness", "sess-b3-late", cwd, [
        _deepseek_entry("m1", "2026-10-05T04:00:00Z", _usage(input_tokens=200, output_tokens=20), cwd),
    ])

    assert lab.apply(lab.lane_root("deepseek-harness")) == 0

    lines = _lines(lab.ledger)
    assert len(lines) == 3
    live_line = [line for line in lines if line["record_id"] == live["record_id"]][0]
    assert live_line["run_id"] == "vnx-dev:backfill:dlv-b3:1"
    booked = sorted(line["run_id"] for line in lines if line["record_id"] != live["record_id"])
    assert booked == ["vnx-dev:backfill:dlv-b3:2", "vnx-dev:backfill:dlv-b3:3"]
    aggregate = cost_ledger.aggregate_runs(lab.ledger)
    assert set(aggregate.runs) == {
        "vnx-dev:backfill:dlv-b3:1", "vnx-dev:backfill:dlv-b3:2", "vnx-dev:backfill:dlv-b3:3",
    }
