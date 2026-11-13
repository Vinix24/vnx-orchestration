"""rework_metrics (herwerk H6): merge facts, identity, the three measures, maturity and the n rule."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init as qdi  # noqa: E402
import rework_attribution as ra  # noqa: E402

PID = "tproj"
AS_OF = datetime(2026, 11, 12, tzinfo=timezone.utc)
HEAD = "a" * 40


def _iso(days_before_as_of: float) -> str:
    return (AS_OF - timedelta(days=days_before_as_of)).strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """Receipts, rounds and an edge store in tmp_path; nothing outside it is touched."""

    def __init__(self, tmp: Path):
        self.receipts, self.rounds, self.db = tmp / "t0_receipts.ndjson", tmp / "rounds.ndjson", tmp / "qi.db"
        assert qdi.bootstrap_qi_db(self.db) is True
        self.lines: list = []
        self.round_lines: list = []

    def build(self, did, pr, model="sonnet", provider="claude", task="01_code_generation", risk="medium"):
        self.lines.append({"dispatch_id": did, "provider": provider, "model": model, "pr_number": pr,
                           "task_class": task, "risk_class": risk, "receipt_kind": "dispatch",
                           "event_type": "task_complete", "timestamp": "2026-01-01T00:00:00Z"})

    def merge(self, pr, did, days_ago, head=HEAD):
        self.lines.append({"event_type": "pr_merged", "pr_number": pr, "branch": f"dispatch/{did}",
                           "timestamp": _iso(days_ago), "review_seats": {"head_sha": head}})

    def edge(self, rework, origin, kind="oi_fix", cls="rework"):
        with closing(sqlite3.connect(self.db)) as conn:
            ra.record_edge(conn, PID, rework, origin, kind, cls, source="spec")
            conn.commit()

    def verdict(self, pr, reviewer="codex_gate", verdict="pass", blockers=0, head=HEAD, model="gpt-x"):
        self.round_lines.append({"project_id": PID, "pr_number": pr, "head_sha": head, "seat": reviewer,
                                 "reviewer": reviewer, "takeover_from": "", "verdict": verdict,
                                 "blocking_count": blockers, "report_path": "", "dispatch_id": "",
                                 "recorded_at": "2026-11-01T00:00:00Z", "reviewer_model": model})

    def run(self, github=None):
        from rework_metrics import compute_metrics
        self.receipts.write_text("".join(json.dumps(x) + "\n" for x in self.lines), encoding="utf-8")
        self.rounds.write_text("".join(json.dumps(x) + "\n" for x in self.round_lines), encoding="utf-8")
        return compute_metrics(PID, self.receipts, self.rounds, self.db, AS_OF, github)


def _cell(out, key, **match):
    rows = [r for r in out[key] if all(r.get(k) == v for k, v in match.items())]
    assert len(rows) == 1, (key, match, out[key])
    return rows[0]


def _origin_with_fix(w, days_gap, fix_kind="oi_fix", fix_cls="rework", origin_age=30):
    w.build("dlv-aaaa00000001", 10)
    w.merge(10, "dlv-aaaa00000001", origin_age)
    w.build("dlv-bbbb00000002", 11)
    w.merge(11, "dlv-bbbb00000002", origin_age - days_gap)
    w.edge("dlv-bbbb00000002", "dlv-aaaa00000001", fix_kind, fix_cls)
    w.verdict(10)


def test_passed_through_one_when_fix_merges_within_14_days(tmp_path):
    w = World(tmp_path)
    _origin_with_fix(w, days_gap=5)
    out = w.run()
    seat = _cell(out, "passed_through", reviewer="codex_gate")
    assert (seat["count"], seat["n"], seat["pct"]) == (1, 1, None)
    assert _cell(out, "rework_14d", builder="claude/sonnet")["count"] == 1
    assert "upper bound" in out["passed_through_note"]


def test_passed_through_zero_when_fix_merges_after_14_days(tmp_path):
    w = World(tmp_path)
    _origin_with_fix(w, days_gap=15)
    out = w.run()
    assert _cell(out, "passed_through", reviewer="codex_gate")["count"] == 0
    assert _cell(out, "rework_14d", builder="claude/sonnet")["count"] == 0


def test_origin_merged_10_days_ago_is_immature_and_not_counted(tmp_path):
    w = World(tmp_path)
    _origin_with_fix(w, days_gap=2, origin_age=10)
    out = w.run()
    cell = _cell(out, "rework_14d", builder="claude/sonnet")
    assert (cell["n"], cell["count"], cell["immature"]) == (0, 0, 2)
    seat = _cell(out, "passed_through", reviewer="codex_gate")
    assert (seat["n"], seat["count"], seat["immature"]) == (0, 0, 1)


def test_churn_edge_is_not_rework(tmp_path):
    w = World(tmp_path)
    _origin_with_fix(w, days_gap=3, fix_kind="blame", fix_cls="churn")
    out = w.run()
    assert _cell(out, "rework_14d", builder="claude/sonnet")["count"] == 0
    assert _cell(out, "passed_through", reviewer="codex_gate")["count"] == 0


def test_two_models_for_one_dispatch_give_conflict_cell(tmp_path):
    w = World(tmp_path)
    w.build("dlv-cccc00000003", 12, model="sonnet")
    w.build("dlv-cccc00000003", 12, model="opus")
    w.merge(12, "dlv-cccc00000003", 30)
    out = w.run()
    assert _cell(out, "first_pass_yield", builder="model=conflict")["n"] == 1
    assert not [r for r in out["first_pass_yield"] if r["builder"] == "claude/sonnet"]


def test_n_rule_19_has_no_percentage_20_has(tmp_path):
    for count, expected in ((19, None), (20, 100.0)):
        (tmp_path / f"n{count}").mkdir()
        w = World(tmp_path / f"n{count}")
        for i in range(count):
            did = f"dlv-{i:012x}"
            w.build(did, 100 + i)
            w.merge(100 + i, did, 30)
        cell = _cell(w.run(), "first_pass_yield", builder="claude/sonnet")
        assert (cell["n"], cell["pct"]) == (count, expected)


def test_wilson_interval_values_and_edges():
    from rework_metrics import wilson_interval
    assert wilson_interval(10, 20) == pytest.approx((0.299, 0.701), abs=0.001)
    assert wilson_interval(0, 20)[0] == 0.0
    assert wilson_interval(20, 20)[1] == 1.0
    assert wilson_interval(0, 0) is None
    assert wilson_interval(2, 20) == (0.028, 0.301)


def test_ci95_comes_from_the_same_count_and_n_as_pct_and_only_from_n_20(tmp_path):
    from rework_metrics import wilson_interval
    for count in (19, 20):
        (tmp_path / f"n{count}").mkdir()
        w = World(tmp_path / f"n{count}")
        for i in range(count):
            did = f"dlv-{i:012x}"
            w.build(did, 100 + i)
            w.merge(100 + i, did, 30)
        cell = _cell(w.run(), "first_pass_yield", builder="claude/sonnet")
        if count < 20:
            assert cell["pct"] is None and "ci95" not in cell
        else:
            assert cell["ci95"] == list(wilson_interval(cell["count"], cell["n"]))


def test_benchmark_dispatch_is_excluded_and_counted(tmp_path):
    w = World(tmp_path)
    w.build("bench-1", 20)
    w.merge(20, "bench-1", 30)
    with closing(sqlite3.connect(w.db)) as conn:
        conn.execute("INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track) "
                     "VALUES ('bench-1', ?, 'headless', 'headless')", (PID,))
        conn.commit()
    out = w.run()
    assert out["exclusions"] == {"benchmark": 1}
    assert out["first_pass_yield"] == []


def test_other_exclusions_are_counted_per_reason(tmp_path):
    w = World(tmp_path)
    w.build("kimi-gate-pr5-1", 5)
    w.build("panel-x", 6)
    w.build("plan-gate-y", 7)
    w.build("dlv-dddd00000004", None)
    out = w.run()
    assert out["exclusions"] == {"gate": 1, "not_build": 2, "no_pr": 1}


def test_pr_without_merge_fact_is_unattributed_and_github_fills_it(tmp_path):
    w = World(tmp_path)
    w.build("dlv-eeee00000005", 30)
    out = w.run()
    assert out["unattributed"] == [{"delivery": "dlv-eeee00000005", "reason": "no_merge_fact"}]
    assert out["first_pass_yield"] == []
    fact = {"mergedAt": _iso(30), "headRefOid": HEAD, "headRefName": "dispatch/dlv-eeee00000005"}
    out = w.run(github=lambda pr: fact if pr == 30 else None)
    assert out["unattributed"] == [] and out["merge_sources"] == {"receipt": 0, "github": 1}
    assert _cell(out, "first_pass_yield", builder="claude/sonnet")["n"] == 1


def test_malformed_github_fact_stays_unattributed(tmp_path):
    w = World(tmp_path)
    w.build("dlv-eeee00000005", 30)
    for bad in ({"mergedAt": None, "headRefOid": HEAD}, {"mergedAt": _iso(3)}, {}, "x"):
        assert w.run(github=lambda pr, bad=bad: bad)["unattributed"][0]["reason"] == "no_merge_fact"


def test_branch_contradicting_receipt_pr_is_pr_join_conflict(tmp_path):
    w = World(tmp_path)
    w.build("dlv-ffff00000006", 40)
    w.merge(40, "dlv-9999aaaa0007", 30)
    out = w.run()
    assert out["unattributed"] == [{"delivery": "dlv-ffff00000006", "reason": "pr_join_conflict"}]


def test_round_folds_into_delivery_and_breaks_first_pass(tmp_path):
    w = World(tmp_path)
    w.build("dlv-1111aaaa0008", 50)
    w.build("dlv-1111aaaa0008-r2", 50)
    w.merge(50, "dlv-1111aaaa0008", 30)
    w.build("dlv-2222bbbb0009", 51)
    w.merge(51, "dlv-2222bbbb0009", 30)
    out = w.run()
    cell = _cell(out, "first_pass_yield", builder="claude/sonnet")
    assert (cell["n"], cell["count"]) == (2, 1)
    assert out["exclusions"] == {}


def test_round_edge_breaks_first_pass_but_not_rework_after_merge(tmp_path):
    w = World(tmp_path)
    _origin_with_fix(w, days_gap=3, fix_kind="round")
    out = w.run()
    assert _cell(out, "first_pass_yield", builder="claude/sonnet")["count"] == 1
    assert _cell(out, "rework_14d", builder="claude/sonnet")["count"] == 0


def test_seat_model_unknown_is_its_own_cell(tmp_path):
    w = World(tmp_path)
    w.build("dlv-aaaa00000001", 10)
    w.merge(10, "dlv-aaaa00000001", 30)
    w.verdict(10, model="")
    assert _cell(w.run(), "passed_through", reviewer="codex_gate")["model"] == "model=unknown"


def test_seat_verdicts_dedupe_last_wins_and_blockers_do_not_count(tmp_path):
    w = World(tmp_path)
    w.build("dlv-aaaa00000001", 10)
    w.merge(10, "dlv-aaaa00000001", 30)
    w.verdict(10, verdict="fail", blockers=2)
    w.round_lines[-1]["recorded_at"] = "2026-10-01T00:00:00Z"
    w.verdict(10)
    w.verdict(10, reviewer="kimi_gate", blockers=1)
    out = w.run()
    assert _cell(out, "passed_through", reviewer="codex_gate")["n"] == 1
    assert not [r for r in out["passed_through"] if r["reviewer"] == "kimi_gate"]


def test_unreadable_rounds_ledger_marks_run_incomplete(tmp_path):
    w = World(tmp_path)
    w.build("dlv-aaaa00000001", 10)
    w.merge(10, "dlv-aaaa00000001", 30)
    w.run()
    w.rounds.unlink()
    w.rounds.mkdir()
    from rework_metrics import compute_metrics
    out = compute_metrics(PID, w.receipts, w.rounds, w.db, AS_OF)
    assert out["complete"] is False and out["passed_through"] == []


def test_module_has_no_write_path():
    text = (REPO_ROOT / "scripts" / "lib" / "rework_metrics.py").read_text(encoding="utf-8")
    assert not re.search(r"open\([^)]*[\"'][wax]\+?b?[\"']|write_text|INSERT|UPDATE|DELETE|\.commit\(", text)
