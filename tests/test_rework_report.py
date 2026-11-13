"""rework_report (herwerk H7): JSON and Markdown render, stratified proposals, no write path to config."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rework_report.py"
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init as qdi  # noqa: E402
import rework_attribution as ra  # noqa: E402
import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402

PID = "tproj"
AS_OF = datetime(2026, 11, 12, tzinfo=timezone.utc)
HEAD = "a" * 40
FORBIDDEN = ("config_registry", "project_config", "routing_recommendations", "provider_constraints")


def _iso(days_before: float) -> str:
    return (AS_OF - timedelta(days=days_before)).strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """data_dir/state layout in tmp_path; the CLI reads it through --data-dir."""

    def __init__(self, tmp: Path):
        self.data = tmp / "data"
        self.state = self.data / "state"
        (self.state / "review_gates").mkdir(parents=True)
        self.db = self.state / "quality_intelligence.db"
        assert qdi.bootstrap_qi_db(self.db) is True
        self.lines: list = []
        self.seq = 0

    def delivery(self, model, task, risk, age=30, fixed_by=None):
        self.seq += 1
        did, pr = f"dlv-{self.seq:012x}", 1000 + self.seq
        self.lines.append({"dispatch_id": did, "provider": "claude", "model": model, "pr_number": pr,
                           "task_class": task, "risk_class": risk, "receipt_kind": "dispatch",
                           "event_type": "task_complete", "timestamp": "2026-01-01T00:00:00Z"})
        self.lines.append({"event_type": "pr_merged", "pr_number": pr, "branch": f"dispatch/{did}",
                           "timestamp": _iso(age), "review_seats": {"head_sha": HEAD}})
        return did

    def cohort(self, model, task, risk, n, reworked):
        for i in range(n):
            origin = self.delivery(model, task, risk)
            if i < reworked:
                fix = self.delivery("fixer", "05_debugging", risk, age=20)
                with closing(sqlite3.connect(self.db)) as conn:
                    ra.record_edge(conn, PID, fix, origin, "oi_fix", "rework", source="spec")
                    conn.commit()

    def args(self):
        self.state.joinpath("t0_receipts.ndjson").write_text(
            "".join(json.dumps(x) + "\n" for x in self.lines), encoding="utf-8")
        self.state.joinpath("review_gates", "rounds.ndjson").write_text("", encoding="utf-8")
        return ["--project-id", PID, "--data-dir", str(self.data), "--as-of", AS_OF.isoformat()]

    def cli(self, fmt, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), fmt, *self.args(), *extra],
                              capture_output=True, text=True)


def test_json_is_the_metrics_result_unchanged(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 3, 1)
    proc = w.cli("--json")
    assert proc.returncode == 0, proc.stderr
    from rework_metrics import compute_metrics
    expected = compute_metrics(PID, w.state / "t0_receipts.ndjson", w.state / "review_gates" / "rounds.ndjson",
                               w.db, AS_OF)
    assert json.loads(proc.stdout) == json.loads(json.dumps(expected, sort_keys=True))
    assert list(json.loads(proc.stdout)) == sorted(json.loads(proc.stdout))


def test_md_shows_n_in_every_cell_and_no_percentage_below_20(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 5, 2)
    proc = w.cli("--md")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "sonnet" in ln]
    assert rows and all("n=" in ln for ln in rows)
    assert "%" not in out
    assert "## Voorstellen" in out and "geen voorstel" in out
    assert "bovengrens" in out and "Uitgesloten per reden" in out and "immature" in out


def test_no_proposal_across_task_classes(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 25, 20)
    w.cohort("opus", "02_refactoring", "medium", 25, 0)
    out = w.cli("--md").stdout
    assert out.count("Bouwer:") == 0
    assert "geen voorstel" in out


def test_one_proposal_inside_one_stratum(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 25, 20)
    w.cohort("opus", "01_code_generation", "medium", 25, 0)
    proc = w.cli("--md")
    assert proc.returncode == 0, proc.stderr
    props = [ln for ln in proc.stdout.splitlines() if ln.startswith("- Bouwer:")]
    assert len(props) == 1
    assert "taakklasse 01_code_generation" in props[0] and "risico medium" in props[0]
    assert "claude/sonnet" in props[0] and "claude/opus" in props[0]
    assert "naar claude/opus" in props[0]


def test_no_proposal_below_n_20_or_small_difference(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 19, 19)
    w.cohort("opus", "01_code_generation", "medium", 25, 0)
    assert w.cli("--md").stdout.count("Bouwer:") == 0


def _builder_props(out: str) -> list:
    return [ln for ln in out.splitlines() if ln.startswith("- Bouwer:")]


def test_no_proposal_when_the_intervals_overlap_at_the_edge(tmp_path):
    from rework_metrics import wilson_interval
    assert wilson_interval(10, 20)[0] < wilson_interval(2, 20)[1]  # 0.299 < 0.301: they overlap
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 20, 2)
    w.cohort("opus", "01_code_generation", "medium", 20, 10)
    out = w.cli("--md").stdout
    assert "10.0% (3-30%, n=20)" in out and "50.0% (30-70%, n=20)" in out
    assert _builder_props(out) == []
    assert "geen voorstel" in out


def test_proposal_names_both_intervals_when_they_separate(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 20, 0)
    w.cohort("opus", "01_code_generation", "medium", 20, 10)
    props = _builder_props(w.cli("--md").stdout)
    assert len(props) == 1
    assert "naar claude/sonnet" in props[0]
    assert "0.0% (0-16%, n=20)" in props[0] and "50.0% (30-70%, n=20)" in props[0]
    assert "overlappen niet" in props[0]


def test_below_n_20_carries_counts_only_and_no_proposal(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 19, 0)
    w.cohort("opus", "01_code_generation", "medium", 19, 19)
    proc = w.cli("--json")
    rows = [r for r in json.loads(proc.stdout)["rework_14d"] if r["task_class"] == "01_code_generation"]
    assert rows and all(r["pct"] is None and "ci95" not in r for r in rows)
    out = w.cli("--md").stdout
    assert _builder_props(out) == [] and "(n=19" not in out and "%" not in out.split("## Per bouwer")[1]


def test_builder_row_without_ci95_never_proposes():
    import rework_report
    rows = [{"builder": "a", "task_class": "t", "risk_class": "medium", "n": 25, "count": 0, "pct": 0.0},
            {"builder": "b", "task_class": "t", "risk_class": "medium", "n": 25, "count": 25, "pct": 100.0}]
    assert rework_report._builder_proposals(rows) == []


def _seat(reviewer, model, n, count):
    return {"reviewer": reviewer, "model": model, "task_class": "t", "risk_class": "medium", "n": n, "count": count}


def test_seat_overlap_gives_no_proposal_and_separation_names_both_intervals():
    import rework_report
    assert rework_report._seat_proposals([_seat("codex_gate", "m1", 20, 2), _seat("kimi_gate", "m2", 20, 10)]) == []
    props = rework_report._seat_proposals([_seat("codex_gate", "m1", 20, 0), _seat("kimi_gate", "m2", 20, 10)])
    assert len(props) == 1
    assert "kimi_gate/m2 meer door dan codex_gate/m1" in props[0]
    assert "50.0% (30-70%, n=20)" in props[0] and "0.0% (0-16%, n=20)" in props[0]


def test_seat_below_n_20_gives_no_proposal():
    import rework_report
    assert rework_report._seat_proposals([_seat("codex_gate", "m1", 19, 0), _seat("kimi_gate", "m2", 19, 19)]) == []


def test_min_diff_pp_is_gone_from_scripts():
    import rework_report
    assert not hasattr(rework_report, "MIN_DIFF_PP")
    for path in (REPO_ROOT / "scripts").rglob("*.py"):
        assert "MIN_DIFF_PP" not in path.read_text(encoding="utf-8"), path


def _violations(source: str) -> list:
    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module or ""] + [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else []
        found += [n for n in names for bad in FORBIDDEN if bad in n]
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name != "_write_atomic"]:
        for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
            if getattr(call.func, "id", "") == "open" and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) and set(a.value) & set("wax+")
                    for a in call.args[1:] + [k.value for k in call.keywords]):
                found.append(f"open-write in {fn.name}")
            if getattr(call.func, "attr", "") in ("write_text", "write_bytes"):
                found.append(f"{call.func.attr} in {fn.name}")
    return found


def test_module_has_no_write_path_and_the_grep_bites():
    source = SCRIPT.read_text(encoding="utf-8")
    assert _violations(source) == []
    assert _violations("import config_registry\n" + source) == ["config_registry"]
    planted = source.replace("def render_markdown", "def evil():\n    open('x', 'w')\n\n\ndef render_markdown")
    assert _violations(planted) == ["open-write in evil"]


def test_out_writes_atomically(tmp_path, monkeypatch):
    import rework_report
    calls = []
    real = rework_report.os.replace
    monkeypatch.setattr(rework_report.os, "replace", lambda a, b: (calls.append((Path(a), Path(b))), real(a, b)))
    target = tmp_path / "report.md"
    target.write_text("old", encoding="utf-8")
    rework_report._write_atomic(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert calls == [(tmp_path / "report.md.tmp", target)]
    assert not (tmp_path / "report.md.tmp").exists()


def test_out_flag_writes_file_and_prints_nothing(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 2, 0)
    out = tmp_path / "r.md"
    proc = w.cli("--md", "--out", str(out))
    assert proc.returncode == 0 and proc.stdout == ""
    assert out.read_text(encoding="utf-8").startswith("# Herwerkrapport")


@pytest.mark.parametrize("remove", ["t0_receipts.ndjson", "quality_intelligence.db"])
def test_missing_input_fails_closed(tmp_path, remove):
    w = World(tmp_path)
    args = w.args()
    (w.state / remove).unlink()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--json", *args], capture_output=True, text=True)
    assert proc.returncode == 1 and "missing input" in proc.stderr and proc.stdout == ""


def _cost_ledger(w, *deliveries):
    events = w.data / "events"
    events.mkdir(exist_ok=True)
    rows = [{"schema": "provider_cost_run/1", "record_id": f"r{i}", "run_id": f"{PID}:envelope_govern:{did}:1",
             "project_id": PID, "kind": "charge", "role": "builder", "provider": "claude", "model": "sonnet",
             "tokens": {"input_uncached": 1, "cache_hit": 2, "cache_write_5m": 0, "cache_write_1h": 0, "output": 3},
             "usage_completeness": "complete", "cost_usd": 0.0, "cost_source": "price_table@2026-09-29",
             "billing": "subscription", "api_equivalent_usd": 1.5} for i, did in enumerate(deliveries)]
    (events / "provider_costs.ndjson").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_json_carries_cost(tmp_path):
    w = World(tmp_path)
    did = w.delivery("sonnet", "01_code_generation", "medium")
    w.delivery("sonnet", "01_code_generation", "medium")
    _cost_ledger(w, did)
    proc = w.cli("--json")
    assert proc.returncode == 0, proc.stderr
    cost = json.loads(proc.stdout)["cost"]
    assert cost["cells"][0]["n"] == 2 and cost["cells"][0]["n_complete"] == 1
    assert cost["gaps"]["incomplete_deliveries"][0]["delivery"] != did


def test_json_names_a_missing_cost_ledger(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 2, 0)
    cost = json.loads(w.cli("--json").stdout)["cost"]
    assert cost["cells"] == [] and "cost ledger not found" in cost["ledger_error"]


def test_md_renders_the_cost_section(tmp_path):
    w = World(tmp_path)
    did = w.delivery("sonnet", "01_code_generation", "medium")
    w.delivery("sonnet", "01_code_generation", "medium")
    _cost_ledger(w, did)
    md = w.cli("--md")
    assert md.returncode == 0, md.stderr
    assert "## Cost per delivery" in md.stdout
    row = next(ln for ln in md.stdout.splitlines() if ln.startswith("| claude/sonnet") and "$1.5" in ln)
    assert "unmeasured" in row and "nee (n < 20)" in row
    assert "Incomplete deliveries (buiten de medianen): 1" in md.stdout and "without builder run" in md.stdout
    assert "Reviewruns zonder koppeling" in md.stdout


def test_md_names_a_missing_cost_ledger_instead_of_a_table(tmp_path):
    w = World(tmp_path)
    w.cohort("sonnet", "01_code_generation", "medium", 2, 0)
    out = w.cli("--md").stdout
    assert "## Cost per delivery" in out and "Geen kostenas: cost ledger not found" in out
    assert "bouw p50 / p75" not in out
