"""Q2 corpus and all-column session evidence on real SQLite fixtures."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
CLI = ROOT / "scripts/vnx_intelligence_corpus_quarantine.py"


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "source ?#.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        paths = ["/one/two/three/four/five/six/seven", "/one/two/three", "/one/two/three"]
        for i, path in enumerate(paths, 1):
            conn.execute("INSERT INTO code_snippets(rowid,file_path,language) VALUES (?,?,?)",
                         (i * 2, path, "python" if i < 3 else "markdown"))
            conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path,created_at) VALUES (?,?,?)",
                         (i * 2, path, f"2026-06-0{i} 00:00:00"))
        conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path,created_at) VALUES (99,'/orphan','2026-06-04')")
        conn.executemany("INSERT INTO session_analytics(session_id,project_id,project_path,session_date) "
                         "VALUES (?,'fixture','/fixture','2026-06-01')", [("a",), ("b",)])
        conn.commit()
    return db


def run(db, out, *flags):
    return subprocess.run([sys.executable, str(CLI), "baseline", "--db", str(db), "--out", str(out),
                           *map(str, flags)], capture_output=True, text=True, cwd=out.parent)


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_corpus_baseline(database, tmp_path, journal_mode):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = run(database, tmp_path, "--corpus")
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["invariants"]["session_analytics"]["digest_sha256"]
    assert manifest["corpus"]["languages"]
    assert "snapshot" not in manifest
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(database)) as conn:
        corpus = manifest["corpus"]
        assert corpus["languages"] == dict(conn.execute("SELECT language,count(*) FROM code_snippets GROUP BY language"))
        assert corpus["distinct_file_path"] == 2 and corpus["join"] == dict(orphans=1, duplicate_snippet_rowid=0)
        assert corpus["created_at"] == dict(min="2026-06-01 00:00:00", max="2026-06-04")
        assert corpus["file_path_roots"] == {"/one/two/three/four/five": 1, "/one/two/three": 2}
        assert corpus["tables"] == ["code_snippets", "snippet_metadata"]
        for table in corpus["tables"]:
            assert corpus["rowid_range"][table] == dict(zip(("min", "max"), conn.execute(
                f"SELECT min(rowid),max(rowid) FROM {table}").fetchone()))
            assert corpus["rows"][table] == conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert corpus["dbstat_bytes"][table] == conn.execute(
                "SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name=?", (table,)).fetchone()[0]
        session = manifest["invariants"]["session_analytics"]
        assert session["columns"] == [r[1] for r in conn.execute("PRAGMA table_info(session_analytics)")]
        assert len(session["columns"]) == 39  # Checked-in DDL; live inventory reports 40.
        assert (session["rows"], session["max_id"], session["sum_id"]) == (2, 2, 3)


@pytest.mark.parametrize("field", ["invariants", "invariants.session_analytics", *[f"invariants.session_analytics.{k}" for k in
    ("rows", "max_id", "sum_id", "columns", "digest_sha256")], "corpus", *[f"corpus.{k}" for k in
    ("tables", "rows", "dbstat_bytes", "rowid_range", "distinct_file_path", "file_path_roots", "languages", "created_at", "join")],
    "corpus.rows.code_snippets", "corpus.dbstat_bytes.snippet_metadata", "corpus.rowid_range.code_snippets.min",
    "corpus.created_at.min", "corpus.join.orphans", "corpus.join.duplicate_snippet_rowid", "corpus.languages.python"])
@pytest.mark.parametrize("bad", ["missing", "null", "malformed", "unknown"])
def test_invalid_evidence(database, tmp_path, field, bad):
    from atomic_io import atomic_write_text
    from intelligence_corpus_quarantine.baseline import measure_baseline
    from intelligence_corpus_quarantine.contract import load_manifest, write_manifest
    manifest = measure_baseline(database, corpus=True)
    owner = manifest
    for key in field.split(".")[:-1]:
        owner = owner[key]
    key = field.split(".")[-1]
    if bad == "missing":
        del owner[key]
    else:
        owner[key] = {"null": None, "malformed": [], "unknown": -1 if type(owner[key]) is int else "unknown"}[bad]
    target = tmp_path / "manifest.json"
    with pytest.raises(ValueError):
        write_manifest(target, manifest)
    assert not target.exists()
    atomic_write_text(target, json.dumps(manifest))
    with pytest.raises(ValueError):
        load_manifest(target)


def test_empty_and_duplicate_corpus(database, tmp_path):
    from intelligence_corpus_quarantine.baseline import measure_baseline
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path) VALUES (2,'/duplicate')")
        conn.commit()
        assert measure_baseline(database, corpus=True)["corpus"]["join"]["duplicate_snippet_rowid"] == 1
        for table in ("code_snippets", "snippet_metadata", "session_analytics"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    manifest = measure_baseline(database, corpus=True)
    assert manifest["corpus"]["languages"] == {} and manifest["corpus"]["rowid_range"]["code_snippets"] == dict(min=None, max=None)
    assert manifest["invariants"]["session_analytics"]["max_id"] is None


@pytest.mark.parametrize("field", ["file_path", "language"])
def test_missing_corpus_evidence(database, tmp_path, field):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"UPDATE code_snippets SET {field}=NULL WHERE rowid=2")
        conn.commit()
    result = run(database, tmp_path, "--corpus")
    assert result.returncode == 3 and "invalid" in result.stderr
    assert not (tmp_path / "manifest.json").exists()


def test_session_digest_covers_all_rows_without_where(database):
    from intelligence_corpus_quarantine.baseline import _session
    from intelligence_corpus_quarantine.digest import table_digest
    with closing(sqlite3.connect(database)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(session_analytics)")]
        assert _session(conn)["digest_sha256"] == table_digest(
            conn, "session_analytics", columns, "id", where="id <= 2")["digest_sha256"]
