"""Characterisation tests for golf-schoon R16 (extract-method refactor).

Pins the observable behaviour of
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
