"""Q1: independent SQLite measurements and the shared equality contract."""
import copy
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
    db = tmp_path / "quality ?#.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        conn.executemany("INSERT INTO code_snippets(title, code, language) VALUES (?, ?, ?)",
                         [(f"snippet-{i}", f"print({i})", "python") for i in range(3)])
        conn.execute("PRAGMA user_version=35")
        conn.commit()
    return db


def run_baseline(db, out):
    return subprocess.run([sys.executable, str(CLI), "baseline", "--db", str(db),
                           "--out", str(out)], cwd=out.parent, capture_output=True, text=True)


def test_contract_imports_from_scripts_lib_in_fresh_interpreter(database, tmp_path):
    target = tmp_path / "manifest.json"
    script = "\n".join([
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "from intelligence_corpus_quarantine import contract",
        "from intelligence_corpus_quarantine.baseline import measure_baseline",
        "contract.write_manifest(sys.argv[3], measure_baseline(sys.argv[2]))",
    ])
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "scripts/lib"), str(database), str(target)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert target.is_file()
    assert json.loads(target.read_text())["db"]["path"] == str(database)


def test_baseline_matches_sqlite(database, tmp_path):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = run_baseline(database, tmp_path)
    assert result.returncode == 0, result.stderr
    manifest_path = tmp_path / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        assert len(names) == 34
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='view'").fetchone()[0] == 6
        expected_db = {"path": str(database), "bytes": database.stat().st_size}
        for pragma in ("page_size", "page_count", "freelist_count", "user_version",
                       "journal_mode", "integrity_check"):
            expected_db[pragma] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        assert manifest["db"] == expected_db
        assert manifest["db"]["integrity_check"] == "ok"
        assert [entry["name"] for entry in manifest["tables"]] == [name for name, in names]
        for entry in manifest["tables"]:
            name = entry["name"]
            assert entry["rows"] == conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            size = conn.execute("SELECT sum(pgsize) FROM dbstat WHERE name=?", (name,)).fetchone()[0]
            assert entry["dbstat_bytes"] == (0 if size is None else size)
    from intelligence_corpus_quarantine.contract import load_manifest, validate_manifest
    assert validate_manifest(manifest) == manifest == load_manifest(manifest_path)
    assert manifest["track"] == "intelligence-corpus-quarantine"
    assert set(manifest) == {"schema", "track", "generated_at", "db", "tables"}
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_baseline_ignores_stale_manifest_scratch_file(database, tmp_path):
    from intelligence_corpus_quarantine.contract import load_manifest
    stale = tmp_path / "manifest.json.tmp"
    content = b"interrupted writer's bytes\n"
    stale.write_bytes(content)
    before = stale.stat()
    result = run_baseline(database, tmp_path)
    assert result.returncode == 0, result.stderr
    assert load_manifest(tmp_path / "manifest.json")["db"]["path"] == str(database)
    assert stale.stat() == before and stale.read_bytes() == content


@pytest.mark.parametrize("kind,code,reason", [
    ("empty", 3, "0-byte"), ("no_snippets", 3, "code_snippets"),
    ("no_out", 2, "directory"), ("unreadable", 1, "regular file"),
    ("corrupt", 1, "not a database"), ("permissions", 1, "unable to open"),
])
def test_refusals(database, tmp_path, kind, code, reason):
    db, out = database, tmp_path
    if kind == "empty":
        db = tmp_path / "empty.db"
        db.touch()
    elif kind == "no_snippets":
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("DROP TABLE code_snippets")
            conn.commit()
    elif kind == "no_out":
        out = tmp_path / "absent"
    elif kind == "unreadable":
        db = tmp_path
    elif kind == "corrupt":
        db.write_bytes(b"not SQLite")
    elif kind == "permissions":
        db.chmod(0)
    try:
        result = run_baseline(db, out)
    finally:
        if kind == "permissions":
            db.chmod(0o600)
    assert result.returncode == code, result.stderr
    assert reason in result.stderr and len(result.stderr.splitlines()) == 1
    assert not (out / "manifest.json").exists()
    if kind == "no_out":
        assert not out.exists()


def test_digest_fixtures_and_affinity(database, tmp_path):
    from intelligence_corpus_quarantine.digest import table_digest
    twin = tmp_path / "twin.db"
    twin.write_bytes(database.read_bytes())
    with closing(sqlite3.connect(database)) as left, closing(sqlite3.connect(twin)) as right:
        columns = [row[1] for row in left.execute("PRAGMA table_info(code_snippets)")]
        original = table_digest(left, "code_snippets", columns, "rowid")
        assert (original["rows"], original["row_min"], original["row_max"]) == (3, 1, 3)
        assert original == table_digest(right, "code_snippets", columns, "rowid")
        right.execute("UPDATE code_snippets SET description='changed' WHERE rowid=2")
        assert original["digest_sha256"] != table_digest(right, "code_snippets", columns, "rowid")["digest_sha256"]
        for table, affinity in (("typed", "TEXT"), ("untyped", "")):
            left.execute(f"CREATE TABLE {table}(value {affinity})")
            left.execute(f"INSERT INTO {table} VALUES (?)", (42,))
        assert table_digest(left, "typed", ["value"], "rowid") != table_digest(left, "untyped", ["value"], "rowid")


def test_digest_byte_stream_and_invalid_inputs():
    from intelligence_corpus_quarantine.digest import table_digest
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute('CREATE TABLE sample(id INTEGER, n, i, f, "t""x", b)')
        conn.execute("INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?)", (-2, None, -42, 1.25, "é", b"\x00\xff"))
        columns = ["n", "i", "f", 't"x', "b"]
        stream = b"R" + (-2).to_bytes(8, "big", signed=True) + b"NI-42;F1.25;T"
        stream += (2).to_bytes(8, "big") + "é".encode() + b"B" + (2).to_bytes(8, "big") + b"\x00\xff\n"
        assert table_digest(conn, "sample", columns, "id") == {
            "rows": 1, "row_min": -2, "row_max": -2, "columns": columns,
            "digest_sha256": hashlib.sha256(stream).hexdigest()}
        empty = table_digest(conn, "sample", columns, "id", where="id > 0")
        assert empty["rows"] == 0 and empty["row_min"] is None and empty["row_max"] is None
        assert empty["digest_sha256"] == hashlib.sha256(b"").hexdigest()
        with pytest.raises(sqlite3.OperationalError):
            table_digest(conn, "sample", ["missing"], "id")
        conn.execute("INSERT INTO sample(id) VALUES (NULL)")
        with pytest.raises(ValueError, match="key"):
            table_digest(conn, "sample", columns, "id")
        for invalid in (None, "n", [], [None]):
            with pytest.raises(ValueError):
                table_digest(conn, "sample", invalid, "id")
        for invalid in ("text", 1.5, -2):
            conn.execute("DELETE FROM sample WHERE id IS NULL OR id != -2")
            conn.execute("INSERT INTO sample(id) VALUES (?)", (invalid,))
            with pytest.raises(ValueError, match="key"):
                table_digest(conn, "sample", columns, "id")


@pytest.mark.parametrize("field", ["schema", "track", "generated_at", "db", "tables",
    "db.path", "db.bytes", "db.page_size", "db.page_count", "db.freelist_count",
    "db.user_version", "db.journal_mode", "db.integrity_check", "table.name", "table.rows", "table.dbstat_bytes"])
@pytest.mark.parametrize("bad", ["missing", "null", "malformed", "unknown"])
def test_contract_rejects_bad_evidence(database, tmp_path, field, bad):
    from intelligence_corpus_quarantine.baseline import measure_baseline
    from intelligence_corpus_quarantine.contract import load_manifest, validate_manifest, write_manifest
    manifest = copy.deepcopy(measure_baseline(database))
    table = next(entry for entry in manifest["tables"] if entry["name"] == "code_snippets")
    owner = manifest["db"] if field.startswith("db.") else table if field.startswith("table.") else manifest
    name = field.split(".")[-1]
    original = owner[name]
    if bad == "missing":
        del owner[name]
    else:
        unknown = 2 if field == "schema" else -1 if type(original) is int and name != "user_version" else "unknown"
        owner[name] = {"null": None, "malformed": [], "unknown": unknown}[bad]
    target = tmp_path / "manifest.json"
    for action in (lambda: validate_manifest(manifest), lambda: write_manifest(target, manifest)):
        with pytest.raises(ValueError):
            action()
    assert not target.exists()
    target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_manifest(target)


def test_atomic_replacement_and_failure(database, tmp_path):
    from intelligence_corpus_quarantine.baseline import measure_baseline
    from intelligence_corpus_quarantine.contract import load_manifest, write_manifest
    manifest = measure_baseline(database)
    target = tmp_path / "manifest.json"
    write_manifest(target, manifest)
    first = target.read_bytes()
    write_manifest(target, manifest)
    assert target.read_bytes() == first and load_manifest(target) == manifest
    assert first.decode() == json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    blocked = tmp_path / "blocked.json"
    blocked.mkdir()
    with pytest.raises(OSError):
        write_manifest(blocked, manifest)
    assert blocked.is_dir() and not list(tmp_path.glob("*.tmp"))


def test_refuses_database_as_output(database, tmp_path):
    db = tmp_path / "manifest.json"
    database.rename(db)
    before = db.read_bytes()
    result = run_baseline(db, tmp_path)
    assert result.returncode == 3 and "overwrite" in result.stderr
    assert db.read_bytes() == before
