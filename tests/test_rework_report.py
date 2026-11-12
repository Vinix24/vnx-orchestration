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
