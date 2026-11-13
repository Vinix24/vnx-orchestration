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

    def run(self, github=None, ledger=None):
        from rework_metrics import compute_metrics
        self.receipts.write_text("".join(json.dumps(x) + "\n" for x in self.lines), encoding="utf-8")
        self.rounds.write_text("".join(json.dumps(x) + "\n" for x in self.round_lines), encoding="utf-8")
        return compute_metrics(PID, self.receipts, self.rounds, self.db, AS_OF, github, ledger_path=ledger)


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


# --- cost axis (herwerk-per-model-en-reviewer) ---------------------------------------------------------

def _run_line(producer, ref, attempt=1, role="builder", usd=1.0, kind="charge", source="price_table@2026-09-29",
              execution_id=None, project=PID, supersedes=None):
    rec = {"schema": "provider_cost_run/1", "record_id": f"{producer}-{ref}-{attempt}-{kind}-{usd}",
           "run_id": f"{project}:{producer}:{ref}:{attempt}", "project_id": project, "kind": kind,
           "role": role, "provider": "claude", "model": "sonnet",
           "tokens": {"input_uncached": 10, "cache_hit": 20, "cache_write_5m": 0, "cache_write_1h": 0, "output": 5},
           "usage_completeness": "complete", "cost_usd": 0.0 if source.startswith("price") else None,
           "cost_source": source, "billing": "subscription", "api_equivalent_usd": usd if source.startswith("price") else None}
    if execution_id:
        rec["execution_id"] = execution_id
    if supersedes:
        rec["supersedes"] = supersedes
    return rec


def _ledger(tmp_path, *records):
    path = tmp_path / "events" / "provider_costs.ndjson"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _gate_result(tmp_path, pr, seat, execution_id):
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    (results / f"pr-{pr}-{seat}.json").write_text(
        json.dumps({"pr_number": pr, "gate": seat, "execution_id": execution_id}), encoding="utf-8")


def _delivery(w, did, pr, days_ago=30, **kw):
    w.build(did, pr, **kw)
    w.merge(pr, did, days_ago)


def _cost(out, builder="claude/sonnet"):
    return [c for c in out["cost"]["cells"] if c["builder"] == builder]


def test_build_cost_sums_both_members_and_a_correction_replaces_its_run(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    w.build("dlv-aaaa00000001-r2", 10)
    ledger = _ledger(
        tmp_path,
        _run_line("envelope_govern", "dlv-aaaa00000001", usd=1.0),
        _run_line("envelope_govern", "dlv-aaaa00000001-r2", usd=5.0),
        _run_line("envelope_govern", "dlv-aaaa00000001-r2", usd=2.0, kind="correction", supersedes="x"))
    out = w.run(ledger=ledger)
    (d,) = out["cost"]["deliveries"]
    assert d["incomplete"] == [] and d["build"]["runs"] == 2
    assert d["build"]["api_equivalent_usd"] == 3.0
    assert d["build"]["tokens"]["cache_hit"] == 40


def test_member_without_builder_run_is_incomplete_and_out_of_the_medians(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    w.build("dlv-aaaa00000001-r2", 10)
    _delivery(w, "dlv-bbbb00000002", 11)
    ledger = _ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001", usd=9.0),
                     _run_line("envelope_govern", "dlv-bbbb00000002", usd=1.0))
    out = w.run(ledger=ledger)
    gaps = out["cost"]["gaps"]["incomplete_deliveries"]
    assert [g["delivery"] for g in gaps] == ["dlv-aaaa00000001"]
    assert "dlv-aaaa00000001-r2" in gaps[0]["why"][0]
    (cell,) = _cost(out)
    assert (cell["n"], cell["n_complete"], cell["build_usd_median"]) == (2, 1, 1.0)


def test_unpriced_builder_run_is_a_gap_not_zero(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    ledger = _ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001", source="unavailable:no_usage"))
    out = w.run(ledger=ledger)
    (cell,) = _cost(out)
    assert cell["n_complete"] == 0 and cell["build_usd_median"] is None
    assert "without price" in out["cost"]["gaps"]["incomplete_deliveries"][0]["why"][0]


def test_review_runs_per_seat_unavailable_and_other_pr_not_attributed(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    _gate_result(tmp_path, 10, "deepseek_gate", "e" * 32)
    ledger = _ledger(
        tmp_path,
        _run_line("envelope_govern", "dlv-aaaa00000001"),
        _run_line("codex_gate", "pr-10", role="review", usd=0.2),
        _run_line("codex_gate", "pr-10", attempt=2, role="review", usd=0.4),
        _run_line("codex_gate", "pr-99", role="review", usd=7.0),
        _run_line("deepseek_gate", "e" * 32, role="review", source="unavailable:no_usage"),
        _run_line("deepseek_gate", "f" * 32, role="review", source="unavailable:no_usage"))
    out = w.run(ledger=ledger)
    (d,) = out["cost"]["deliveries"]
    assert d["review"]["codex_gate"] == {"runs": 2, "unavailable_runs": 0, "api_equivalent_usd": 0.6}
    assert d["review"]["deepseek_gate"] == {"runs": 1, "unavailable_runs": 1, "api_equivalent_usd": None}
    gaps = out["cost"]["gaps"]
    assert gaps["other_pr_review_runs"] == {"codex_gate": 1}
    assert gaps["unattributed_review_runs"] == {"deepseek_gate": 1}
    assert gaps["unavailable_review_runs"] == {"deepseek_gate": 1}
    seat = _cost(out)[0]["review"]["deepseek_gate"]
    assert seat["api_equivalent_usd_median"] is None and seat["unavailable_runs"] == 1
    assert _cost(out)[0]["review"]["codex_gate"]["api_equivalent_usd_median"] == 0.6


def test_gate_result_of_another_pr_or_malformed_links_nothing(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    results = tmp_path / "results"
    results.mkdir()
    (results / "pr-10-deepseek_gate.json").write_text(
        json.dumps({"pr_number": 11, "execution_id": "e" * 32}), encoding="utf-8")
    (results / "pr-10-kimi_gate.json").write_text("{not json", encoding="utf-8")
    ledger = _ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001"),
                     _run_line("deepseek_gate", "e" * 32, role="review"),
                     _run_line("kimi_gate", "d" * 32, role="review"))
    out = w.run(ledger=ledger)
    assert out["cost"]["deliveries"][0]["review"] == {}
    assert out["cost"]["gaps"]["unattributed_review_runs"] == {"deepseek_gate": 1, "kimi_gate": 1}


def test_lead_time_is_first_member_receipt_to_merge_in_minutes(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10, days_ago=1)
    w.build("dlv-aaaa00000001-r2", 10)
    w.lines[0]["timestamp"] = (AS_OF - timedelta(days=1, minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    w.lines[-1]["timestamp"] = (AS_OF - timedelta(days=1, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger = _ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001"),
                     _run_line("envelope_govern", "dlv-aaaa00000001-r2"))
    out = w.run(ledger=ledger)
    assert out["cost"]["deliveries"][0]["lead_time_min"] == 90.0
    assert _cost(out)[0]["lead_time_median_min"] == 90.0


def test_changed_lines_from_the_merge_fact_and_gap_when_absent(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    _delivery(w, "dlv-bbbb00000002", 11)
    w.lines[1]["additions"], w.lines[1]["deletions"] = 300, 100
    ledger = _ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001", usd=2.0),
                     _run_line("envelope_govern", "dlv-bbbb00000002", usd=3.0))
    _delivery(w, "dlv-cccc00000003", 12, model="opus")
    ledger = _ledger(tmp_path, *[json.loads(x) for x in ledger.read_text().splitlines()],
                     _run_line("envelope_govern", "dlv-cccc00000003"))
    out = w.run(ledger=ledger)
    assert [c["quota_share"]["status"] for c in out["cost"]["cells"]] == ["unmeasured"] * 2
    (cell,) = _cost(out)
    assert cell["per_1000_n"] == 1 and cell["build_usd_per_1000_lines_median"] == 5.0
    assert out["cost"]["gaps"]["no_changed_lines"] == 2  # the sonnet delivery without lines and the opus one


@pytest.mark.parametrize("n,comparable", [(19, False), (20, True)])
def test_cell_is_comparable_from_n_20_and_shows_medians_below(tmp_path, n, comparable):
    w = World(tmp_path)
    runs = []
    for i in range(n):
        did = f"dlv-{i + 1:012x}"
        _delivery(w, did, 100 + i)
        runs.append(_run_line("envelope_govern", did, usd=float(i + 1)))
    out = w.run(ledger=_ledger(tmp_path, *runs))
    (cell,) = _cost(out)
    assert cell["n_complete"] == n and cell["comparable"] is comparable
    assert cell["quota_share"] == {"status": "unmeasured", "reason": "no per-run weekly quota source"}
    assert cell["build_usd_median"] == (n + 1) / 2 and cell["build_usd_p75"] is not None


def test_other_project_runs_and_missing_ledger_are_not_counted(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    out = w.run(ledger=_ledger(tmp_path, _run_line("envelope_govern", "dlv-aaaa00000001", project="other")))
    assert out["cost"]["deliveries"][0]["incomplete"]
    missing = w.run(ledger=tmp_path / "nope.ndjson")
    assert missing["cost"]["cells"] == [] and "not found" in missing["cost"]["ledger_error"]
    assert missing["cost"]["quota_share"]["status"] == "unmeasured"


def test_metered_run_counts_its_cost_usd_not_a_zero_api_equivalent(tmp_path):
    w = World(tmp_path)
    _delivery(w, "dlv-aaaa00000001", 10)
    metered = _run_line("envelope_govern", "dlv-aaaa00000001", usd=0.0)
    metered.update(billing="metered", cost_usd=0.5, api_equivalent_usd=None)
    review = _run_line("codex_gate", "pr-10", role="review")
    review.update(billing="metered", cost_usd=0.25, api_equivalent_usd=None)
    out = w.run(ledger=_ledger(tmp_path, metered, review))
    (d,) = out["cost"]["deliveries"]
    assert d["build"]["api_equivalent_usd"] == 0.5
    assert d["review"]["codex_gate"]["api_equivalent_usd"] == 0.25
