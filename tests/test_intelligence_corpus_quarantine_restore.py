"""Q3 split 2: restore puts a quarantined corpus back with one command, between an intent and an outcome line."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
CLI, AUDIT = ROOT / "scripts/vnx_intelligence_corpus_quarantine.py", "intelligence_corpus_quarantine_audit.ndjson"
ROWIDS, SHA = [2, 3, 19946], "a" * 64  # 19946 % 9973 == 0: the modulus sample, beyond the first and last 20
META = "INSERT INTO snippet_metadata(id,snippet_rowid,file_path) VALUES ({},2,'x');"
BETWEEN, MERGE = "INSERT INTO code_snippets(rowid,title) VALUES (500,'between');", ("--allow-existing",)

audit, contract, digest, store = (importlib.import_module(f"intelligence_corpus_quarantine.{name}")
                                  for name in ("audit", "contract", "digest", "store"))
quarantine_cli = importlib.util.module_from_spec(importlib.util.spec_from_file_location("quarantine_cli", CLI))
quarantine_cli.__loader__.exec_module(quarantine_cli)  # the real main() in this process: a monkeypatched seam holds


def content(db, table):
    with closing(sqlite3.connect(db)) as conn:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        kinds = ", ".join(f"{c}, typeof({c})" for c in columns)
        return conn.execute(f"SELECT rowid, {kinds} FROM {table} ORDER BY rowid").fetchall()


def counts(db):
    return [len(content(db, t)) for t in contract.CORPUS_TABLES]


@pytest.fixture
def permit(tmp_path):
    """A store from the real DDL, quarantined by split 1's command, then emptied of its corpus."""
    db, state = tmp_path / "store ?#.db", tmp_path / "state"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        conn.executemany("INSERT INTO code_snippets(rowid,title,code,file_path,language,quality_score,last_updated) "
                         "VALUES (?,?,?,?,?,?,?)", [(r, f"parse feed {r}", f"def f{r}(): pass", f"/src/{r}.py",
                                                     "python", 85.5 if r % 2 else 70, None) for r in ROWIDS])
        conn.executemany("INSERT INTO snippet_metadata(id,snippet_rowid,file_path,quality_score,usage_count) "
                         "VALUES (?,?,?,?,?)", [(r + 100, r, f"/src/{r}.py", 0.5, None) for r in ROWIDS])
        conn.commit()
    (tmp_path / "permit").mkdir()
    (tmp_path / "permit/manifest.json").write_text('{"baseline": "fixture"}\n')
    state.mkdir()
    result = subprocess.run([sys.executable, CLI, "quarantine", "--db", db, "--out", tmp_path / "permit"], cwd=tmp_path,
                            capture_output=True, text=True, env=dict(os.environ, VNX_STATE_DIR=str(state)))
    assert result.returncode == 0, result.stderr  # no --state-dir: the default, the resolved VNX_STATE_DIR
    before = {t: content(db, t) for t in contract.CORPUS_TABLES}
    with closing(sqlite3.connect(db)) as conn:  # sqlite_sequence too: restore must re-seed it itself
        conn.executescript("DELETE FROM code_snippets; DELETE FROM snippet_metadata; DELETE FROM sqlite_sequence;")
    return dict(db=db, q=tmp_path / "permit/artifact/quarantine.json", state=state, before=before, tmp=tmp_path)


def restore(p, capsys, *extra, state=None):
    return (quarantine_cli.main(["restore", "--db", str(p["db"]), "--quarantine", str(p["q"]),
                                 "--state-dir", str(state or p["state"]), *extra]), *capsys.readouterr())


def outcome(p, *statuses, stage="restored"):
    """Assert the audit lines of a stage carry exactly these statuses, in order, all bound to this permit."""
    lines = [r for r in map(json.loads, (p["state"] / AUDIT).read_text().splitlines()) if r["stage"] == stage]
    sha = hashlib.sha256((p["q"].parent.parent / "manifest.json").read_bytes() + p["q"].read_bytes()).hexdigest()
    assert [(r["status"], r["permit_sha256"]) for r in lines] == [(s, sha) for s in statuses]
    return lines


def insert_then_raise(conn, tables, insert=store._insert_and_prove):
    insert(conn, tables)
    raise ValueError("injected after the insert")


def test_restore_puts_rows_rowids_join_digest_and_index_back(permit, capsys):
    rc, out, err = restore(permit, capsys)
    assert rc == 0 and json.loads(out)["rows"] == {"code_snippets": 3, "snippet_metadata": 3}, err
    with closing(sqlite3.connect(permit["db"])) as conn:
        for t in contract.load_quarantine(permit["q"])["tables"]:  # rows, rowids, storage classes, digest
            assert content(permit["db"], t["name"]) == permit["before"][t["name"]]
            assert digest.table_digest(conn, t["name"], t["columns"], "rowid")["digest_sha256"] == t["content_digest"]
        assert conn.execute("SELECT rowid FROM code_snippets WHERE code_snippets MATCH 'parse' "
                            "ORDER BY rowid").fetchall() == [(r,) for r in ROWIDS]
        assert conn.execute("SELECT count(*) FROM snippet_metadata m JOIN code_snippets s "
                            "ON s.rowid = m.snippet_rowid").fetchone() == (3,)
        assert conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path) VALUES (2,'n')").lastrowid > 20046
        assert conn.execute("INSERT INTO code_snippets(title) VALUES ('n')").lastrowid > 19946
    intent, ok = outcome(permit, "intent", "ok")
    outcome(permit, "ok", stage="quarantined")  # the fixture's quarantine wrote to the resolved VNX_STATE_DIR
    assert [(r["code_snippets_rows"], r["snippet_metadata_rows"]) for r in (intent, ok)] == [(3, 3), (3, 3)]


@pytest.mark.parametrize("sql,seam,flag,after,status,reason", [
    (META.format(900), None, (), [0, 1], "refused", "snippet_metadata is not empty"),
    (BETWEEN + META.format(103), None, MERGE, [1, 1], "refused", "collision in snippet_metadata: 1 rowids [103]"),
    (BETWEEN, None, MERGE, [4, 3], "ok", ""),
    ("", ("table_digest", lambda *a, **k: dict(rows=3, digest_sha256=SHA)), (), [0, 0], "rolled_back", "digest"),
    ("", ("_probe_query", lambda values: '"absentword"'), (), [0, 0], "rolled_back", "missed [2, 3, 19946]"),
    ("", ("_insert_and_prove", insert_then_raise), (), [0, 0], "rolled_back", "injected after the insert"),
], ids=["not-empty", "collision", "merge", "digest", "match-probe", "insert-raises"])
def test_intent_then_refused_rolled_back_or_ok(permit, monkeypatch, capsys, sql, seam, flag, after, status, reason):
    with closing(sqlite3.connect(permit["db"])) as conn:  # the second table refuses: the first gets nothing either
        conn.executescript(sql)
    if seam:
        monkeypatch.setattr(store, *seam)
    rc, _, err = restore(permit, capsys, *flag)
    assert rc == (0 if status == "ok" else 3) and reason in err and counts(permit["db"]) == after, err
    assert reason in outcome(permit, "intent", status)[1].get("reason", "")


@pytest.mark.parametrize("fails,rc,after,message", [("ok", 4, [3, 3], "committed, but the audit line was not written"),
                                                     ("rolled_back", 3, [0, 0], "rolled_back outcome line is missing")])
def test_failed_outcome_line_keeps_the_intent(permit, monkeypatch, capsys, fails, rc, after, message):
    def append(state_dir, stage, permit_sha256, status, real=audit.append, **numbers):
        if status == fails:
            raise OSError(f"injected: no {status} line")
        return real(state_dir, stage, permit_sha256, status, **numbers)
    monkeypatch.setattr(audit, "append", append)
    if fails == "rolled_back":  # the transaction fails too: its outcome line is the one that cannot be written
        monkeypatch.setattr(store, "_insert_and_prove", insert_then_raise)
    code, out, err = restore(permit, capsys)
    assert (code, out) == (rc, "") and message in err and counts(permit["db"]) == after, err
    outcome(permit, "intent")


@pytest.mark.parametrize("case,reason", [("flipped-byte", "artifact does not match quarantine.json"),
                                         ("state-is-a-file", "intent line was not written"), ("state-in-permit", "B4")])
def test_refusal_before_the_intent_line_leaves_the_store_unopened(permit, capsys, case, reason):
    state = {"state-is-a-file": permit["tmp"] / case, "state-in-permit": permit["q"].parent.parent}.get(case)
    (permit["tmp"] / "state-is-a-file").write_text("")
    if case == "flipped-byte":
        artifact = permit["q"].parent / contract.load_quarantine(permit["q"])["artifact"]
        artifact.chmod(0o644)  # the sealed folder stays 0555: rewriting a file needs only the file's own mode
        data = bytearray(artifact.read_bytes())
        data[len(data) // 2] ^= 0xFF
        artifact.write_bytes(bytes(data))
    before = permit["db"].read_bytes()
    rc, out, err = restore(permit, capsys, state=state)
    assert rc == 3 and reason in err and out == "" and permit["db"].read_bytes() == before, err
    assert counts(permit["db"]) == [0, 0] and outcome(permit) == [] and not (state and (state / AUDIT).exists())


@pytest.mark.parametrize("stage,permit,status,reason", [
    ("purged", SHA, "ok", "stage"), (None, SHA, "ok", "stage"), ("Restored", SHA, "ok", "stage"),
    ("restored", "abc", "ok", "permit"), ("restored", None, "ok", "permit"), ("restored", SHA, "", "status"),
    ("restored", SHA, None, "status"), ("restored", SHA, "done", "status"), ("restored", SHA, "OK", "status")])
def test_audit_append_refuses_an_unknown_stage_status_or_field(tmp_path, stage, permit, status, reason):
    (tmp_path / "state").mkdir()
    with pytest.raises(ValueError, match=reason):
        audit.append(tmp_path / "state", stage, permit, status, rows=1)
    assert list((tmp_path / "state").iterdir()) == []
