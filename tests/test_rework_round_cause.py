"""Round cause (ADR-071 besluit 15): staging, cause per member, first pass, report, suffix."""
from __future__ import annotations

import importlib
import json
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

import dispatch_bridge as db  # noqa: E402
import quality_db_init as qdi  # noqa: E402
import rework_attribution as ra  # noqa: E402
import rework_metrics as rm  # noqa: E402
import rework_report  # noqa: E402

PID = "tproj"
AS_OF = datetime(2026, 11, 12, tzinfo=timezone.utc)
HEAD = "a" * 40
BASE = "dlv-abc123"


@pytest.fixture
def store(tmp_path):
    assert qdi.bootstrap_qi_db(tmp_path / "state" / "quality_intelligence.db") is True
    return tmp_path


@pytest.fixture(autouse=True)
def seams(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(db, "policy_digest_for_role", lambda role: "digest")


def _stage(store, dispatch_id="dlv-new", **kw):
    return db.stage_spec_bundle(
        instruction_text="do it", dispatch_id=dispatch_id, role="backend-developer",
        target_slot="T1", project_id=PID, data_dir=store, **kw,
    )


def _edges(store):
    with closing(sqlite3.connect(store / "state" / "quality_intelligence.db")) as conn:
        return conn.execute(
            "SELECT rework_dispatch_id, origin_dispatch_id, kind, evidence_json FROM rework_edges WHERE project_id = ?",
            (PID,)).fetchall()


# ---- staging -------------------------------------------------------------------------------

def test_valid_cause_is_on_spec_and_in_edge_evidence(store):
    spec = json.loads(_stage(store, parent_dispatch="dlv-p", round_cause=" CI ").read_text())
    assert spec["round_cause"] == "ci"
    rows = _edges(store)
    assert json.loads(rows[0][3]) == {"parent_dispatch": "dlv-p", "round_cause": "ci"}


def test_round_suffix_without_parent_writes_edge_to_delivery(store):
    spec = json.loads(_stage(store, dispatch_id="dlv-abc123-r2", round_cause="review").read_text())
    assert spec["parent_dispatch"] is None
    rows = _edges(store)
    assert [r[:3] for r in rows] == [("dlv-abc123-r2", "dlv-abc123", "round")]
    assert json.loads(rows[0][3])["round_cause"] == "review"


def test_split_suffix_without_parent_writes_edge_without_cause(store):
    _stage(store, dispatch_id="dlv-abc123-c")
    rows = _edges(store)
    assert [r[:3] for r in rows] == [("dlv-abc123-c", "dlv-abc123", "round")]
    assert "round_cause" not in json.loads(rows[0][3])


@pytest.mark.parametrize("bad", ["style", "grenzen", "x"])
def test_unknown_cause_raises_before_any_file_exists(store, bad):
    with pytest.raises(ValueError) as exc:
        _stage(store, round_cause=bad)
    assert "review, ci, grens, operator" in str(exc.value)
    assert not (store / "dispatches").exists()
    assert _edges(store) == []


def test_cli_round_cause_reaches_the_spec(store, tmp_path, capsys):
    instr = tmp_path / "i.md"
    instr.write_text("do it")
    rc = db.main(["stage", "--instruction", str(instr), "--dispatch-id", "dlv-cli", "--role", "backend-developer",
                  "--slot", "T1", "--round-cause", "operator", "--data-dir", str(store)])
    assert rc == 0
    assert json.loads(Path(capsys.readouterr().out.strip()).read_text())["round_cause"] == "operator"


def test_cli_unknown_cause_is_rejected(store, tmp_path, capsys):
    instr = tmp_path / "i.md"
    instr.write_text("do it")
    rc = db.main(["stage", "--instruction", str(instr), "--dispatch-id", "dlv-cli", "--role", "backend-developer",
                  "--slot", "T1", "--round-cause", "nope", "--data-dir", str(store)])
    assert rc == 1
    assert not (store / "dispatches").exists()


# ---- suffix ---------------------------------------------------------------------------------

def test_delivery_of_any_one_letter_split_suffix():
    assert rm.delivery_of("dlv-abc-c") == "dlv-abc"
    assert rm.delivery_of("dlv-abc-b-r3") == "dlv-abc"
    assert rm.delivery_of("dlv-abc-a") == "dlv-abc"
    assert rm.delivery_of("dlv-abc") == "dlv-abc"


# ---- cause per member and first pass --------------------------------------------------------

class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.receipts, self.rounds, self.db = tmp / "t0_receipts.ndjson", tmp / "rounds.ndjson", tmp / "qi.db"
        assert qdi.bootstrap_qi_db(self.db) is True
        self.lines: list = []

    def member(self, did, pr=10, title=None, path=None):
        rec = {"dispatch_id": did, "provider": "claude", "model": "sonnet", "pr_number": pr,
               "task_class": "01_code_generation", "risk_class": "medium", "receipt_kind": "dispatch",
               "event_type": "task_complete", "timestamp": "2026-01-01T00:00:00Z"}
        if title is not None:
            prompt = self.tmp / f"{did}.md"
            prompt.write_text(f"intro\n# {title}\nbody\n", encoding="utf-8")
            rec["final_prompt_path"] = str(prompt)
        elif path is not None:
            rec["final_prompt_path"] = path
        self.lines.append(rec)

    def merge(self, pr=10, did=BASE):
        self.lines.append({"event_type": "pr_merged", "pr_number": pr, "branch": f"dispatch/{did}",
                           "timestamp": (AS_OF - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "review_seats": {"head_sha": HEAD}})

    def edge(self, rework, cause=None, raw=None):
        evidence = raw if raw is not None else ({"round_cause": cause} if cause else {})
        with closing(sqlite3.connect(self.db)) as conn:
            ra.record_edge(conn, PID, rework, BASE, "round", "rework", source="spec", evidence=evidence)
            conn.commit()

    def run(self):
        self.receipts.write_text("".join(json.dumps(x) + "\n" for x in self.lines), encoding="utf-8")
        self.rounds.write_text("", encoding="utf-8")
        return rm.compute_metrics(PID, self.receipts, self.rounds, self.db, AS_OF)


def _round(out):
    cells = out["round_causes"]["cells"]
    assert len(cells) == 1, cells
    return cells[0]


def _fpy(out):
    return out["first_pass_yield"][0]


def _world(tmp_path, *members, title=None):
    w = World(tmp_path)
    w.member(BASE)
    for did in members:
        w.member(did, title=title)
    w.merge()
    return w


def test_declared_beats_the_title_rule(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2", title="fix a codex finding")
    w.edge(f"{BASE}-r2", "ci")
    out = w.run()
    assert _round(out)["ci"] == 1 and _round(out)["review"] == 0
    assert out["round_causes"]["sources"] == {"declared": 1, "title": 0, "unknown": 0}


def test_declared_review_beats_a_ci_title(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2", title="CI red: mode tier")
    w.edge(f"{BASE}-r2", "review")
    assert _round(w.run())["review"] == 1


def test_split_part_is_grens_without_a_title(tmp_path):
    out = _world(tmp_path, f"{BASE}-c").run()
    assert _round(out)["grens"] == 1
    assert out["round_causes"]["sources"]["title"] == 1


def test_split_then_round_follows_the_title(tmp_path):
    out = _world(tmp_path, f"{BASE}-b-r2", title="fix the finding").run()
    assert _round(out)["review"] == 1


@pytest.mark.parametrize("title,cause", [
    ("CI fix: ratchet", "ci"), ("move the rest to -b over 450 lines", "grens"),
    ("fix the codex finding", "review"), ("CI red and split", "grens")])
def test_round_title_rule(tmp_path, title, cause):
    assert _round(_world(tmp_path, f"{BASE}-r2", title=title).run())[cause] == 1


def test_title_is_read_after_the_dispatch_instruction_marker(tmp_path):
    w = _world(tmp_path)
    final = tmp_path / "final_prompt.md"
    final.write_text("# Base Worker Context\n\nDISPATCH INSTRUCTION:\n\n# fix: CI red, mode tier\nbody\n", encoding="utf-8")
    w.member(f"{BASE}-r2", path=str(final))
    assert _round(w.run())["ci"] == 1


def test_no_final_prompt_path_is_unknown(tmp_path):
    out = _world(tmp_path, f"{BASE}-r2").run()
    assert _round(out)["unknown"] == 1
    assert out["round_causes"]["sources"]["unknown"] == 1


def test_unreadable_prompt_file_is_unknown(tmp_path):
    w = _world(tmp_path)
    w.member(f"{BASE}-r2", path=str(tmp_path / "missing.md"))
    assert _round(w.run())["unknown"] == 1


@pytest.mark.parametrize("raw", [{"round_cause": "bogus"}, {"round_cause": None}, {"round_cause": 3}])
def test_invalid_declared_cause_is_ignored(tmp_path, raw):
    w = _world(tmp_path, f"{BASE}-r2")
    w.edge(f"{BASE}-r2", raw=raw)
    assert _round(w.run())["unknown"] == 1


def test_malformed_evidence_json_declares_nothing(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2")
    w.edge(f"{BASE}-r2")
    with closing(sqlite3.connect(w.db)) as conn:
        conn.execute("UPDATE rework_edges SET evidence_json = 'not json'")
        conn.commit()
    assert _round(w.run())["unknown"] == 1


def test_round_edge_without_suffix_or_cause_is_unknown(tmp_path):
    w = _world(tmp_path)
    w.member("dlv-def456", pr=11)
    w.edge("dlv-def456")
    out = w.run()
    assert _round(out)["unknown"] == 1 and _fpy(out)["count"] == 0


def test_delivery_with_only_ci_and_grens_rounds_is_a_hit(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2", f"{BASE}-c")
    w.edge(f"{BASE}-r2", "ci")
    out = w.run()
    assert _fpy(out)["count"] == 1 and _fpy(out)["n"] == 1


def test_operator_round_is_a_hit(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2")
    w.edge(f"{BASE}-r2", "operator")
    assert _fpy(w.run())["count"] == 1


@pytest.mark.parametrize("cause", ["review", None])
def test_review_or_unknown_round_is_a_miss(tmp_path, cause):
    w = _world(tmp_path, f"{BASE}-r2", f"{BASE}-r3")
    w.edge(f"{BASE}-r2", "ci")
    if cause:
        w.edge(f"{BASE}-r3", cause)
    assert _fpy(w.run())["count"] == 0


def test_delivery_without_rounds_is_a_hit_and_has_no_cause_row(tmp_path):
    out = _world(tmp_path).run()
    assert _fpy(out)["count"] == 1 and out["round_causes"]["cells"] == []


# ---- report ---------------------------------------------------------------------------------

def test_report_renders_the_table_and_the_basis_line(tmp_path):
    w = _world(tmp_path, f"{BASE}-r2", f"{BASE}-c")
    w.edge(f"{BASE}-r2", "ci")
    text = rework_report.render_markdown(w.run())
    assert "### Rondes per oorzaak" in text
    assert "| builder | task_class | risk_class | review | ci | grens | operator | unknown |" in text
    assert "| claude/sonnet | 01_code_generation | medium | 0 | 1 | 1 | 0 | 0 |" in text
    assert '- Oorzaak van rondes per bron: {"declared": 1, "title": 1, "unknown": 0}.' in text
    assert "First-pass yield (telt alleen reviewrondes)" in text
    assert text.index("### First-pass yield") < text.index("### Rondes per oorzaak") < text.index("### Herwerk binnen")

