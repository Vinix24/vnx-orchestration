"""Q2 snapshot, corpus and all-column session evidence on real SQLite fixtures."""
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
def test_snapshot(database, tmp_path, journal_mode):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    snapshot = tmp_path / "snapshot.db"
    result = run(database, tmp_path, "--snapshot", snapshot)
    assert result.returncode == 0, result.stderr
    assert snapshot.stat().st_mode & 0o777 == 0o400
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["invariants"]["session_analytics"]["digest_sha256"]
    assert manifest["corpus"]["languages"]
    assert manifest["snapshot"] == dict(path=str(snapshot), bytes=snapshot.stat().st_size,
        sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(), integrity_check="ok")
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


@pytest.mark.parametrize("extra_column", [False, True])
def test_reuse_and_all_column_digest(database, tmp_path, extra_column):
    column = "extension_metric" if extra_column else "deep_analysis_json"
    if extra_column:
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("ALTER TABLE session_analytics ADD COLUMN extension_metric TEXT")
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / "copy.db"
    assert run(database, tmp_path, "--snapshot", snapshot).returncode == 0
    target = tmp_path / "manifest.json"
    original = target.read_bytes()
    before = json.loads(original)
    files = list(snapshot_dir.iterdir())
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"UPDATE session_analytics SET {column}='changed' WHERE id=1")
        conn.commit()
    result = run(database, tmp_path, "--reuse-snapshot", snapshot)
    assert result.returncode == 0, result.stderr
    assert snapshot.stat().st_mode & 0o777 == 0o400
    after = json.loads(target.read_bytes())
    assert target.read_bytes() != original and after["snapshot"] == before["snapshot"]
    assert list(snapshot_dir.iterdir()) == files
    assert len(after["invariants"]["session_analytics"]["columns"]) == (40 if extra_column else 39)
    assert after["invariants"]["session_analytics"]["digest_sha256"] != before["invariants"]["session_analytics"]["digest_sha256"]
    with closing(sqlite3.connect(snapshot)) as conn:
        assert conn.execute(f"SELECT {column} FROM session_analytics WHERE id=1").fetchone() == (None,)


@pytest.mark.parametrize("sql,reason", [
    ("INSERT INTO code_snippets(file_path,language) VALUES ('/new','python')", "code_snippets rows"),
    ("UPDATE code_snippets SET code='changed' WHERE rowid=2", "code_snippets digest"),
    ("UPDATE snippet_metadata SET file_path='/changed' WHERE snippet_rowid=2", "snippet_metadata digest"),
    ("UPDATE session_analytics SET deep_analysis_json='changed' WHERE id=1", None),
], ids=["snippet-insert", "snippet-update", "metadata-update", "session-only"])
def test_reuse_requires_unchanged_corpus(database, tmp_path, sql, reason):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / "copy ?#.db"
    assert run(database, tmp_path, "--snapshot", snapshot).returncode == 0
    target = tmp_path / "manifest.json"
    before = target.read_bytes()
    files = list(snapshot_dir.iterdir())
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(sql)
        conn.commit()
    result = run(database, tmp_path, "--reuse-snapshot", snapshot)
    if reason is not None:
        assert result.returncode == 3, f"{result.stderr}; manifest changed={target.read_bytes() != before}"
        assert reason in result.stderr
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(before).hexdigest()
    else:
        assert result.returncode == 0, result.stderr
        after, original = json.loads(target.read_bytes()), json.loads(before)
        assert after["snapshot"] == original["snapshot"]
        assert after["invariants"]["session_analytics"]["digest_sha256"] != original["invariants"]["session_analytics"]["digest_sha256"]
    assert list(snapshot_dir.iterdir()) == files


@pytest.mark.parametrize("case,code,reason", [("existing", 3, "already exists"), ("both", 2, "not allowed"),
    ("hash", 3, "mismatch"), ("other", 3, "path differs"), ("missing", 3, "No such file"),
    ("absent_snapshot", 3, "No such file"), ("alias", 3, "overwrite")])
def test_refusals(database, tmp_path, case, code, reason):
    snapshot = tmp_path / "copy.db"
    assert run(database, tmp_path, "--snapshot", snapshot).returncode == 0
    target = tmp_path / "manifest.json"
    before = target.read_bytes()
    flags, out = ["--reuse-snapshot", snapshot], tmp_path
    if case == "existing":
        flags = ["--snapshot", snapshot]
    elif case == "both":
        flags += ["--snapshot", tmp_path / "new.db"]
    elif case == "hash":
        snapshot.chmod(0o600)
        with snapshot.open("r+b") as stream:
            byte = stream.read(1)
            stream.seek(0)
            stream.write(bytes([byte[0] ^ 1]))
    elif case in {"other", "alias"}:
        flags = ["--reuse-snapshot", tmp_path / "other.db" if case == "other" else target]
    elif case == "missing":
        out = tmp_path / "no-manifest"
        out.mkdir()
    elif case == "absent_snapshot":
        snapshot.rename(tmp_path / "moved.db")
    result = run(database, out, *flags)
    assert result.returncode == code and reason in result.stderr and len(result.stderr.splitlines()) == 1
    assert target.read_bytes() == before and not (tmp_path / "new.db").exists()


@pytest.mark.parametrize("field", ["snapshot", "snapshot.path", "snapshot.bytes", "snapshot.sha256", "snapshot.integrity_check",
    "invariants", "invariants.session_analytics", *[f"invariants.session_analytics.{k}" for k in
    ("rows", "max_id", "sum_id", "columns", "digest_sha256")], "corpus", *[f"corpus.{k}" for k in
    ("tables", "rows", "dbstat_bytes", "rowid_range", "distinct_file_path", "file_path_roots", "languages", "created_at", "join")],
    "corpus.rows.code_snippets", "corpus.dbstat_bytes.snippet_metadata", "corpus.rowid_range.code_snippets.min",
    "corpus.created_at.min", "corpus.join.orphans", "corpus.join.duplicate_snippet_rowid", "corpus.languages.python"])
@pytest.mark.parametrize("bad", ["missing", "null", "malformed", "unknown"])
def test_invalid_evidence(database, tmp_path, field, bad):
    from atomic_io import atomic_write_text
    from intelligence_corpus_quarantine.baseline import measure_baseline
    from intelligence_corpus_quarantine.contract import load_manifest, write_manifest
    manifest = measure_baseline(database, snapshot=tmp_path / "copy.db")
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
    before = target.read_bytes()
    with pytest.raises(ValueError):
        load_manifest(target)
    assert run(database, tmp_path, "--reuse-snapshot", tmp_path / "copy.db").returncode == 3
    assert target.read_bytes() == before


def test_snapshot_integrity_refusal_removes_copy(tmp_path):
    from intelligence_corpus_quarantine.snapshot import create_snapshot
    db, target = tmp_path / "bad.db", tmp_path / "copy.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript("CREATE VIRTUAL TABLE bad USING fts5(content); INSERT INTO bad VALUES ('broken index'); DELETE FROM bad_docsize;")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        with pytest.raises(ValueError, match="snapshot integrity_check"):
            create_snapshot(conn, target)
    assert not target.exists()


def test_isolated_interpreter(database, tmp_path):
    script = ("import sys, runpy; sys.path.insert(0, sys.argv[1]); "
        "from intelligence_corpus_quarantine import baseline; "
        "sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')")
    result = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT / "scripts/lib"), str(CLI),
        "baseline", "--db", str(database), "--out", str(tmp_path), "--snapshot", str(tmp_path / "copy.db")],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "manifest.json").read_text())["snapshot"]["integrity_check"] == "ok"


def test_empty_and_duplicate_corpus(database, tmp_path):
    from intelligence_corpus_quarantine.baseline import measure_baseline
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path) VALUES (2,'/duplicate')")
        conn.commit()
        assert measure_baseline(database, snapshot=tmp_path / "duplicate.db")["corpus"]["join"]["duplicate_snippet_rowid"] == 1
        for table in ("code_snippets", "snippet_metadata", "session_analytics"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    manifest = measure_baseline(database, snapshot=tmp_path / "empty.db")
    assert manifest["corpus"]["languages"] == {} and manifest["corpus"]["rowid_range"]["code_snippets"] == dict(min=None, max=None)
    assert manifest["invariants"]["session_analytics"]["max_id"] is None


@pytest.mark.parametrize("field", ["file_path", "language"])
def test_missing_corpus_evidence(database, tmp_path, field):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"UPDATE code_snippets SET {field}=NULL WHERE rowid=2")
        conn.commit()
    result = run(database, tmp_path, "--snapshot", tmp_path / "copy.db")
    assert result.returncode == 3 and "invalid" in result.stderr
    assert not (tmp_path / "copy.db").exists() and not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE code_snippets SET code='late' WHERE rowid=2", "code_snippets digest"),
    ("INSERT INTO snippet_metadata(snippet_rowid,file_path) VALUES (2,'/late')", "snippet_metadata rows"),
    ("UPDATE session_analytics SET deep_analysis_json='late' WHERE id=1", "session_analytics digest"),
], ids=["snippet-update", "metadata-insert", "session-update"])
def test_fresh_snapshot_refused_when_source_changes_before_copy(database, tmp_path, sql, reason):
    from intelligence_corpus_quarantine.baseline import measure_baseline

    def writer():
        with closing(sqlite3.connect(database)) as other:
            other.execute(sql)
            other.commit()

    target = tmp_path / "copy.db"
    with pytest.raises(ValueError, match=f"fresh snapshot differs from the measured corpus: {reason}"):
        measure_baseline(database, snapshot=target, _before_snapshot=writer)
    assert not target.exists()


def test_session_digest_covers_all_rows_without_where(database):
    from intelligence_corpus_quarantine.baseline import _session
    from intelligence_corpus_quarantine.digest import table_digest
    with closing(sqlite3.connect(database)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(session_analytics)")]
        assert _session(conn)["digest_sha256"] == table_digest(
            conn, "session_analytics", columns, "id", where="id <= 2")["digest_sha256"]
