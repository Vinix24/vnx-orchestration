"""Characterisation tests for golf-schoon R16 (extract-method refactor).

Pins the observable behaviour of ``simulate_feedback_loop``,
``backfill_dispatch_experiments`` (scripts/retroactive_backfill.py) and
``validate_installation`` (scripts/vnx_install.py) so the refactor can be
shown behaviour-preserving.
"""

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import retroactive_backfill as rb
import vnx_install as vi


# ---------------------------------------------------------------------------
# retroactive_backfill fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def qi(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    db = state / "quality_intelligence.db"
    monkeypatch.setattr(rb, "QI_DB", db)
    monkeypatch.setattr(rb, "RECEIPTS_PATH", state / "t0_receipts.ndjson")
    monkeypatch.setattr(rb, "ARCHIVE_DIR", tmp_path / "archive")
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE dispatch_metadata (
            dispatch_id TEXT, terminal TEXT, role TEXT, cognition TEXT,
            outcome_status TEXT, cqs REAL, instruction_char_count INTEGER,
            pattern_count INTEGER, intelligence_json TEXT,
            dispatched_at TEXT, completed_at TEXT
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT, pattern_title TEXT, used_count INTEGER,
            success_count INTEGER, failure_count INTEGER, confidence REAL,
            last_offered TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert_dispatch(db, did, **kw):
    row = dict(terminal="T1", role="backend-developer", cognition="normal",
               outcome_status="success", cqs=80.0, instruction_char_count=1000,
               pattern_count=3, intelligence_json="{}",
               dispatched_at="2026-01-01T10:00:00Z",
               completed_at="2026-01-01T10:30:00Z")
    row.update(kw)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatch_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (did, row["terminal"], row["role"], row["cognition"],
         row["outcome_status"], row["cqs"], row["instruction_char_count"],
         row["pattern_count"], row["intelligence_json"],
         row["dispatched_at"], row["completed_at"]),
    )
    conn.commit()
    conn.close()


def _insert_pattern(db, pid, last_offered, used=0, succ=0, fail=0, conf=0.5):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO pattern_usage VALUES (?,?,?,?,?,?,?,?,NULL)",
        (pid, pid, used, succ, fail, conf, last_offered, "2026-01-01"),
    )
    conn.commit()
    conn.close()


def _patterns(db):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT pattern_id, used_count, success_count, failure_count, "
        "confidence, updated_at FROM pattern_usage ORDER BY pattern_id"
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# simulate_feedback_loop
# ---------------------------------------------------------------------------

@pytest.fixture
def upcf_calls(monkeypatch):
    import intelligence_persist
    calls = []

    def fake(db_path, did, terminal, out):
        calls.append((Path(db_path), did, terminal, out))
        return {"boosted": 1 if out == "success" else 0,
                "decayed": 1 if out == "failure" else 0}

    monkeypatch.setattr(intelligence_persist, "update_confidence_from_outcome", fake)
    return calls


def test_feedback_dry_run(qi, capsys):
    _insert_dispatch(qi, "d1")
    _insert_dispatch(qi, "d2", intelligence_json=None)
    _insert_pattern(qi, "p1", "2026-01-01T10:10:00Z")
    result = rb.simulate_feedback_loop(dry_run=True)
    assert result == {"dispatches_processed": 1, "patterns_available": 1}
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[step5] Simulating retroactive feedback loop...",
        "[step5] Dispatches with intel + outcome: 1",
        "[step5] Pattern usage rows with last_offered: 1",
        "[dry-run] Would process 1 dispatches against 1 patterns",
    ]
    assert _patterns(qi)[0][1] == 0


def test_feedback_updates_confidence_and_counts(qi, upcf_calls, capsys):
    _insert_dispatch(qi, "d1", outcome_status="success")
    _insert_dispatch(qi, "d2", outcome_status="failed", terminal=None,
                     dispatched_at="2026-01-01T10:05:00Z", completed_at=None)
    _insert_dispatch(qi, "d3", outcome_status="no_confirmation")
    _insert_dispatch(qi, "d4", outcome_status="success",
                     dispatched_at="2026-03-01T10:00:00Z",
                     completed_at="2026-03-01T10:10:00Z")
    _insert_pattern(qi, "p_in", "2026-01-01T10:10:00Z", used=2, succ=1, conf=0.5)
    _insert_pattern(qi, "p_out", "2025-06-01T00:00:00Z", conf=0.5)
    _insert_pattern(qi, "p_naive", "2026-01-01T10:20:00", conf=0.0)
    _insert_pattern(qi, "p_bad", "garbage", conf=0.5)
    result = rb.simulate_feedback_loop()
    assert result == {"updated": 2, "boosted": 2, "decayed": 2}
    rows = {r[0]: r for r in _patterns(qi)}
    # p_in: +success (d1), then failure (d2) => 0.5 +0.05 -0.10
    assert rows["p_in"][1:5] == (4, 2, 1, 0.45)
    assert rows["p_in"][5] is not None
    # p_naive: confidence 0.0 falsy -> 0.5 default; success then failure
    assert rows["p_naive"][1:5] == (2, 1, 1, 0.45)
    assert rows["p_out"][1:5] == (0, 0, 0, 0.5)
    assert rows["p_out"][5] is None
    assert rows["p_bad"][1] == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-2] == "[step5] Applied confidence updates to 3 dispatches' patterns"
    assert out[-1] == ("[step5] Feedback loop: 2 pattern_usage rows updated "
                       "(2 boosted, 2 decayed)")
    # d3 (no_confirmation) is excluded by the SQL filter; order is by dispatched_at
    assert upcf_calls == [
        (qi, "d1", "T1", "success"),
        (qi, "d2", "unknown", "failure"),
        (qi, "d4", "T1", "success"),
    ]


def test_feedback_skips_bad_dispatch_times(qi, upcf_calls):
    _insert_dispatch(qi, "dn", dispatched_at=None)
    _insert_dispatch(qi, "dbad", dispatched_at="not-a-date")
    _insert_dispatch(qi, "dbadend", completed_at="nope",
                     dispatched_at="2026-01-01T10:00:00Z")
    _insert_pattern(qi, "p", "2026-01-01T10:20:00Z")
    result = rb.simulate_feedback_loop()
    # dn is None => ORDER BY puts it first and it is skipped; dbad skipped;
    # dbadend falls back to t_end = t_start and matches within +-30 min.
    assert result == {"updated": 1, "boosted": 1, "decayed": 0}
    assert _patterns(qi)[0][1:5] == (1, 1, 0, 0.55)


def test_feedback_confidence_clamped(qi, upcf_calls):
    for i in range(4):
        _insert_dispatch(qi, f"s{i}", dispatched_at=f"2026-01-01T10:0{i}:00Z")
    _insert_pattern(qi, "hi", "2026-01-01T10:00:00Z", conf=0.98)
    rb.simulate_feedback_loop()
    assert _patterns(qi)[0][4] == 1.0


def test_feedback_import_error_message(qi, monkeypatch, capsys):
    _insert_dispatch(qi, "d1")
    _insert_pattern(qi, "p", "2026-01-01T10:10:00Z")
    monkeypatch.setitem(sys.modules, "intelligence_persist", None)
    result = rb.simulate_feedback_loop()
    captured = capsys.readouterr()
    assert result == {"updated": 1, "boosted": 1, "decayed": 0}
    assert "[step5] Could not import intelligence_persist:" in captured.err
    assert "Applied confidence updates" not in captured.out
    assert _patterns(qi)[0][1] == 1  # commit still happened


# ---------------------------------------------------------------------------
# backfill_dispatch_experiments
# ---------------------------------------------------------------------------

def _experiments(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatch_experiments ORDER BY dispatch_id"
    ).fetchall()
    conn.close()
    return {r["dispatch_id"]: dict(r) for r in rows}


def test_backfill_dry_run(qi, capsys):
    _insert_dispatch(qi, "d1")
    assert rb.backfill_dispatch_experiments(dry_run=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "[step1] Loading dispatch_metadata...",
        "[step1] Found 1 dispatches. Loading receipts and archives...",
        "[dry-run] Would process 1 dispatch rows",
        "[dry-run]   receipts available: 0",
        "[dry-run]   archives available: 0",
    ]
    conn = sqlite3.connect(str(qi))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name='dispatch_experiments'")]
    conn.close()
    assert tables == []


def test_backfill_inserts_rows(qi, tmp_path, capsys):
    _insert_dispatch(qi, "ok", outcome_status="done", cognition="deep")
    _insert_dispatch(qi, "bad", outcome_status="Blocked", role=None,
                     terminal=None, pattern_count=None,
                     instruction_char_count=None, cqs=None)
    _insert_dispatch(qi, "noconf", outcome_status="no_confirmation", cqs=None,
                     dispatched_at="2026-01-01T11:00:00Z",
                     completed_at="2026-01-01T11:10:00Z")
    _insert_dispatch(qi, "pending", outcome_status=None,
                     dispatched_at=None, completed_at=None)
    _insert_dispatch(qi, "", outcome_status="success")
    rb.RECEIPTS_PATH.write_text("\n".join([
        json.dumps({"event_type": "task_complete", "dispatch_id": "noconf",
                    "status": "success", "cqs": {"cqs": 77.5}}),
        json.dumps({"event_type": "task_complete", "dispatch_id": "pending",
                    "status": "failed", "cqs": 61}),
        "not json",
        json.dumps({"event_type": "other", "dispatch_id": "ok"}),
        "",
    ]) + "\n")
    assert rb.backfill_dispatch_experiments() == 4
    rows = _experiments(qi)
    assert set(rows) == {"ok", "bad", "noconf", "pending"}
    ok = rows["ok"]
    assert (ok["instruction_chars"], ok["context_items"], ok["role"],
            ok["terminal"], ok["model"], ok["success"], ok["cqs"],
            ok["completion_minutes"], ok["test_count"], ok["committed"],
            ok["lines_changed"], ok["file_count"], ok["repo_map_symbols"],
            ok["timestamp"]) == (
        1000, 3, "backend-developer", "T1", "sonnet", 1, 80.0, 30.0, 0, 0,
        0, 0, 0, "2026-01-01T10:00:00Z")
    bad = rows["bad"]
    assert (bad["success"], bad["role"], bad["terminal"], bad["context_items"],
            bad["instruction_chars"], bad["cqs"]) == (
        0, "unknown", "unknown", 0, 0, None)
    assert rows["noconf"]["success"] == 1
    assert rows["noconf"]["cqs"] == 77.5
    assert rows["pending"]["success"] == 0
    assert rows["pending"]["cqs"] == 80.0  # row cqs wins over receipt
    assert rows["pending"]["completion_minutes"] == 0.0
    assert rows["pending"]["timestamp"] not in (None, "")
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "[step1] Already in dispatch_experiments: 0"
    assert out[-1] == ("[step1] Inserted 4 dispatch experiments "
                       "(skipped existing: 0)")


def test_backfill_skips_existing_and_is_idempotent(qi, capsys):
    _insert_dispatch(qi, "d1")
    _insert_dispatch(qi, "d2")
    assert rb.backfill_dispatch_experiments() == 2
    capsys.readouterr()
    assert rb.backfill_dispatch_experiments() == 0
    out = capsys.readouterr().out.splitlines()
    assert "[step1] Already in dispatch_experiments: 2" in out
    assert out[-1] == ("[step1] Inserted 0 dispatch experiments "
                       "(skipped existing: 2)")
    assert len(_experiments(qi)) == 2


def test_backfill_uses_archive_summary(qi, tmp_path, monkeypatch):
    _insert_dispatch(qi, "arch", outcome_status="success")
    summary = rb.ArchiveSummary(committed=True, test_count=9, lines_changed=42,
                                unique_files=5)
    monkeypatch.setattr(rb, "_build_archive_index",
                        lambda: {"arch": tmp_path / "x.ndjson"})
    seen = []

    def fake_parse(path):
        seen.append(path)
        return summary

    monkeypatch.setattr(rb, "_parse_archive", fake_parse)
    assert rb.backfill_dispatch_experiments() == 1
    row = _experiments(qi)["arch"]
    assert seen == [tmp_path / "x.ndjson"]
    assert (row["test_count"], row["committed"], row["lines_changed"],
            row["file_count"]) == (9, 1, 42, 5)


# ---------------------------------------------------------------------------
# validate_installation
# ---------------------------------------------------------------------------

def _full_install(root: Path, executable=True):
    home = root / ".vnx"
    for d in ["bin", "scripts/lib", "templates/terminals"]:
        (home / d).mkdir(parents=True, exist_ok=True)
    for f in ["bin/vnx", "scripts/vnx_init.py", "scripts/vnx_doctor.py",
              "scripts/lib/vnx_paths.py", "scripts/lib/vnx_paths.sh",
              "scripts/lib/vnx_mode.py"]:
        (home / f).write_text("x")
    for t in ["T0", "T1", "T2", "T3"]:
        (home / "templates" / "terminals" / f"{t}.md").write_text("t")
    mode = home / "bin" / "vnx"
    mode.chmod(0o755 if executable else 0o644)
    return home


def _summ(results):
    return [(r.name, r.category, r.status, r.message, r.remediation)
            for r in results]


def test_validate_no_installation(tmp_path):
    assert _summ(vi.validate_installation(tmp_path)) == [
        ("install", "install", vi.FAIL, "No VNX installation to validate",
         "Run install.sh first")]


def test_validate_full_install_without_data_dir(tmp_path):
    _full_install(tmp_path)
    res = vi.validate_installation(tmp_path)
    names = [r.name for r in res]
    assert names == [
        "dir-bin", "dir-scripts", "dir-scripts/lib", "dir-templates",
        "dir-templates/terminals",
        "file-bin/vnx", "file-scripts/vnx_init.py",
        "file-scripts/vnx_doctor.py", "file-scripts/lib/vnx_paths.py",
        "file-scripts/lib/vnx_paths.sh", "file-scripts/lib/vnx_mode.py",
        "executable", "template-T0", "template-T1", "template-T2",
        "template-T3", "runtime-data",
    ]
    assert [r.status for r in res[:-1]] == [vi.PASS] * 16
    assert res[0].message == "CLI entrypoint: bin"
    assert res[5].message == "CLI entrypoint script"
    assert res[11].message == "bin/vnx is executable"
    assert res[12].message == "Terminal template: T0"
    last = res[-1]
    assert (last.status, last.message, last.remediation) == (
        vi.WARN, "No .vnx-data/ (not yet initialized)",
        "Run: vnx init (or vnx setup)")


def test_validate_missing_pieces_fail(tmp_path):
    home = _full_install(tmp_path, executable=False)
    (home / "templates" / "terminals" / "T2.md").unlink()
    (home / "scripts" / "vnx_doctor.py").unlink()
    (home / "scripts" / "lib" / "vnx_paths.sh").unlink()
    (home / "scripts" / "lib" / "vnx_paths.py").unlink()
    res = {r.name: r for r in vi.validate_installation(tmp_path)}
    assert res["executable"].status == vi.FAIL
    assert res["executable"].message == "bin/vnx exists but is not executable"
    assert res["executable"].remediation == f"Run: chmod +x {home / 'bin' / 'vnx'}"
    assert res["template-T2"].message == "Missing template: templates/terminals/T2.md"
    assert res["template-T2"].remediation == "Re-run install.sh to repair"
    assert res["file-scripts/vnx_doctor.py"].message == (
        "Missing: scripts/vnx_doctor.py (Python doctor)")
    assert res["file-scripts/lib/vnx_paths.sh"].status == vi.FAIL
    assert res["template-T1"].status == vi.PASS


def test_validate_missing_dir_fails(tmp_path):
    home = _full_install(tmp_path)
    for p in (home / "templates" / "terminals").iterdir():
        p.unlink()
    (home / "templates" / "terminals").rmdir()
    res = {r.name: r for r in vi.validate_installation(tmp_path)}
    assert res["dir-templates/terminals"].status == vi.FAIL
    assert res["dir-templates/terminals"].message == (
        "Missing: templates/terminals (Terminal CLAUDE.md templates)")
    assert res["template-T0"].status == vi.FAIL


def test_validate_mode_states(tmp_path):
    _full_install(tmp_path)
    data = tmp_path / ".vnx-data"
    data.mkdir()
    res = vi.validate_installation(tmp_path)
    assert [r.name for r in res][-2:] == ["runtime-data", "mode"]
    assert res[-2].status == vi.PASS
    assert res[-2].message == f"Runtime data: {data}"
    assert (res[-1].status, res[-1].message) == (
        vi.WARN, "No mode.json (run 'vnx init --starter' or 'vnx init --operator')")
    (data / "mode.json").write_text('{"mode": "operator"}')
    res = vi.validate_installation(tmp_path)
    assert (res[-1].status, res[-1].message) == (vi.PASS, "Mode: operator")
    (data / "mode.json").write_text("{}")
    assert vi.validate_installation(tmp_path)[-1].message == "Mode: unknown"
    (data / "mode.json").write_text("{broken")
    res = vi.validate_installation(tmp_path)
    assert (res[-1].status, res[-1].message) == (
        vi.WARN, "mode.json exists but is unreadable")


def test_validate_legacy_layout(tmp_path):
    home = tmp_path / ".claude" / "vnx-system"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "vnx").write_text("x")
    res = vi.validate_installation(tmp_path)
    assert res[0].name == "dir-bin" and res[0].status == vi.PASS
    assert {r.name: r.status for r in res}["dir-scripts"] == vi.FAIL
