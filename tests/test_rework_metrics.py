"""rework_metrics (herwerk H6): merge facts, identity, the three measures, maturity and the n rule."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_branch_contradicting_receipt_pr_is_pr_join_conflict(tmp_path):
    w = World(tmp_path)
    w.build("dlv-ffff00000006", 40)
    w.merge(40, "dlv-9999aaaa0007", 30)
    out = w.run()
    assert out["unattributed"] == [{"delivery": "dlv-ffff00000006", "reason": "pr_join_conflict"}]


def test_module_has_no_write_path():
    text = (REPO_ROOT / "scripts" / "lib" / "rework_metrics.py").read_text(encoding="utf-8")
    assert not re.search(r"open\([^)]*[\"'][wax]\+?b?[\"']|write_text|INSERT|UPDATE|DELETE|\.commit\(", text)
