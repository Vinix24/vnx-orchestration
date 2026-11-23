"""Q3: the sealed corpus artefact, proven against the source before the seal."""
import hashlib
import importlib
import json
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
CLI = ROOT / "scripts/vnx_intelligence_corpus_quarantine.py"
AUDIT = "intelligence_corpus_quarantine_audit.ndjson"

contract = importlib.import_module("intelligence_corpus_quarantine.contract")
table_digest = importlib.import_module("intelligence_corpus_quarantine.digest").table_digest

SNIPPETS = [(2, "parse feed", "def parse(): pass", "/src/a.py", "python", 85, 0.5, None),
            (3, "store rows", "INSERT INTO t", "/src/b.py", "python", 70.25, 3, "2026-06-01"),
            (19946, "rowid % 9973", "x = 1", "/src/c.py", "python", None, 0, "2026-06-02")]


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "source ?#.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        conn.executemany("INSERT INTO code_snippets(rowid,title,code,file_path,language,quality_score,usage_count,"
                         "last_updated) VALUES (?,?,?,?,?,?,?,?)", SNIPPETS)
        conn.executemany("INSERT INTO snippet_metadata(snippet_rowid,file_path,quality_score,usage_count) "
                         "VALUES (?,?,?,?)", [(2, "/src/a.py", 0.5, 1), (3, "/src/b.py", 1, None),
                                              (19946, "/src/c.py", None, 7)])
        conn.commit()
    (tmp_path / "permit").mkdir()
    (tmp_path / "permit/manifest.json").write_text('{"baseline": "fixture"}\n')
    (tmp_path / "state").mkdir()
    return db


def run(db, out, state=None):
    return subprocess.run([sys.executable, str(CLI), "quarantine", "--db", str(db), "--out", str(out),
                           "--state-dir", str(state or out.parent / "state")], capture_output=True, text=True,
                          cwd=out.parent)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_quarantine_writes_a_proven_sealed_artifact(database, tmp_path, journal_mode):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    before = sha(database)
    result = run(database, tmp_path / "permit")
    assert result.returncode == 0, result.stderr
    folder = tmp_path / "permit/artifact"
    document = contract.load_quarantine(folder / "quarantine.json")  # decision 17: command writes, reader reads
    assert document["track"] == "intelligence-corpus-quarantine"
    assert sha(database) == before
    assert folder.stat().st_mode & 0o222 == 0 and folder.stat().st_mode & 0o555 == 0o555
    artifact = folder / document["artifact"]
    assert sorted(p.name for p in folder.iterdir()) == sorted([artifact.name, "quarantine.json"])
    assert all(p.stat().st_mode & 0o777 == 0o444 for p in folder.iterdir())
    assert (document["artifact_sha256"], document["artifact_bytes"]) == (sha(artifact), artifact.stat().st_size)
    assert document["restore_proof"]["ok"] is True and document["restore_proof"]["match_probe"] == 3
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".quarantine-probe-")]
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(artifact.as_uri() + "?mode=ro", uri=True)) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert copy.execute("SELECT type, name FROM sqlite_master ORDER BY name").fetchall() == [
            ("table", "corpus_code_snippets"), ("table", "corpus_snippet_metadata")]
        for table in document["tables"]:
            name, columns = table["name"], table["columns"]
            target = "corpus_" + name
            assert columns == [r[1] for r in source.execute(f"PRAGMA table_info({name})")]
            assert {r[2] for r in copy.execute(f"PRAGMA table_info({target})")} == {""}  # decision 19
            kinds = ", ".join(f"typeof({c})" for c in columns)
            assert copy.execute(f"SELECT rowid, {kinds} FROM {target} ORDER BY rowid").fetchall() == \
                source.execute(f"SELECT rowid, {kinds} FROM {name} ORDER BY rowid").fetchall()
            found = table_digest(source, name, columns, "rowid")
            assert table["content_digest"] == found["digest_sha256"]
            assert (table["rows"], table["row_min"], table["row_max"]) == (3, found["row_min"], found["row_max"])
        assert copy.execute("SELECT typeof(quality_score) FROM corpus_code_snippets ORDER BY rowid").fetchall() == [
            ("integer",), ("real",), ("null",)]
    readme = tmp_path / "permit/LEESMIJ.md"
    assert readme.stat().st_mode & 0o777 == 0o444
    quoted = [shlex.quote(str(p.resolve())) for p in (database, folder / "quarantine.json")]
    assert "restore --db {} --quarantine {}".format(*quoted) in readme.read_text()
    [line] = [json.loads(x) for x in (tmp_path / "state" / AUDIT).read_text().splitlines()]
    permit = hashlib.sha256((tmp_path / "permit/manifest.json").read_bytes() + (folder / "quarantine.json").read_bytes())
    assert (line["stage"], line["status"], line["permit_sha256"]) == ("quarantined", "ok", permit.hexdigest())
    assert (line["code_snippets_rows"], line["snippet_metadata_rows"]) == (3, 3)
    assert line["artifact_sha256"] == document["artifact_sha256"]


def test_unwritable_audit_path_after_the_seal_is_exit_4_and_keeps_the_artifact(database, tmp_path):
    blocked = tmp_path / "state-is-a-file"
    blocked.write_text("")
    result = run(database, tmp_path / "permit", state=blocked)
    assert result.returncode == 4 and "artifact sealed and valid" in result.stderr, result.stderr
    folder = tmp_path / "permit/artifact"
    document = contract.load_quarantine(folder / "quarantine.json")
    assert sha(folder / document["artifact"]) == document["artifact_sha256"] and folder.stat().st_mode & 0o777 == 0o555
    assert blocked.read_text() == "" and result.stdout == ""


@pytest.mark.parametrize("case", ["no-manifest", "state-in-permit"])
def test_refusals_before_any_write(database, tmp_path, case):
    if case == "no-manifest":
        (tmp_path / "permit/manifest.json").unlink()
    result = run(database, tmp_path / "permit", state=tmp_path / "permit" if case == "state-in-permit" else None)
    assert result.returncode == 3 and ("manifest.json" if case == "no-manifest" else "B4") in result.stderr
    assert not (tmp_path / "permit/artifact").exists() and not (tmp_path / "state" / AUDIT).exists()


@pytest.mark.parametrize("field,value,reason", [
    ("artifact", "/tmp/quality_intelligence-corpus.db", "relative"),
    ("artifact", "../quality_intelligence-corpus.db", "relative"),
    ("artifact", "..", "relative"),
    ("track", "x", "track"), ("schema", 2, "schema"), ("track", None, "field"), ("seal", None, "field"),
    ("restore_proof", dict(match_probe=3, ok=False), "MATCH"), ("artifact_sha256", "abc", "sha256"),
    ("tables", [], "corpus tables"), (None, None, "expected fields"),
])
def test_validate_quarantine_refuses_what_the_writer_never_writes(database, tmp_path, field, value, reason):
    assert run(database, tmp_path / "permit").returncode == 0
    document = json.loads((tmp_path / "permit/artifact/quarantine.json").read_text())
    assert contract.validate_quarantine(json.loads(json.dumps(document))) == document
    if field is None:
        del document["source_ddl_sha256"]
    else:
        document[field] = value
    with pytest.raises(ValueError, match=reason):
        contract.validate_quarantine(document)


def test_existing_artifact_directory_is_never_touched(database, tmp_path):
    folder = tmp_path / "permit/artifact"
    folder.mkdir()
    (folder / "quarantine.json").write_text("earlier")
    result = run(database, tmp_path / "permit")
    assert result.returncode == 3 and "refusing to overwrite" in result.stderr
    assert [(p.name, p.read_text()) for p in folder.iterdir()] == [("quarantine.json", "earlier")]


@pytest.mark.parametrize("sql,reason", [
    ("DELETE FROM code_snippets", "MATCH probe tried 0"),
    ("UPDATE code_snippets SET title='--', code=NULL, file_path='..', language=NULL, quality_score=NULL, "
     "usage_count=NULL, last_updated=NULL WHERE rowid=3", "missed [3]"),
    ("DROP TABLE snippet_metadata", "no snippet_metadata table"),
], ids=["empty-corpus", "unprovable-row", "missing-table"])
def test_failed_proof_exits_3_and_removes_only_its_own_files(database, tmp_path, sql, reason):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(sql)
        conn.commit()
    (tmp_path / "permit/manifest.json").write_text("{}")
    listing = sorted(p.name for p in tmp_path.iterdir())
    result = run(database, tmp_path / "permit")
    assert result.returncode == 3 and reason in result.stderr, result.stderr
    assert [p.name for p in (tmp_path / "permit").iterdir()] == ["manifest.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == listing


def test_decoy_database_is_refused(tmp_path):
    (tmp_path / "permit").mkdir()
    (tmp_path / "decoy.db").touch()
    result = run(tmp_path / "decoy.db", tmp_path / "permit")
    assert result.returncode == 3 and "decoy" in result.stderr
    assert list((tmp_path / "permit").iterdir()) == []


def test_typed_copy_is_caught_by_the_proof(database, tmp_path):
    """Decision 19: a TEXT column turns integer 85 into '85'; the digest and typeof check refuse it."""
    store = importlib.import_module("intelligence_corpus_quarantine.store")
    target = tmp_path / "typed.db"
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(target)) as copy:
        columns = [r[1] for r in source.execute("PRAGMA table_info(code_snippets)")]
        found = table_digest(source, "code_snippets", columns, "rowid")
        copy.execute(f"CREATE TABLE corpus_code_snippets ({', '.join(c + ' TEXT' for c in columns)})")
        copy.executemany(f"INSERT INTO corpus_code_snippets (rowid, {', '.join(columns)}) VALUES "
                         f"({', '.join('?' * (len(columns) + 1))})",
                         source.execute(f"SELECT rowid, {', '.join(columns)} FROM code_snippets"))
        copy.commit()
        table = dict(name="code_snippets", rows=3, columns=columns, content_digest=found["digest_sha256"])
        with pytest.raises(ValueError, match="code_snippets digest"):
            store._prove_artifact(source, target, [table])
    with pytest.raises(ValueError, match="FTS5"):
        store.match_probe(target, "CREATE TABLE code_snippets (title)", ["title"], tmp_path)


def test_fresh_interpreter_with_only_scripts_lib(database, tmp_path):
    script = "\n".join(["import sys", "sys.path.insert(0, sys.argv[1])",
                        "from intelligence_corpus_quarantine.store import quarantine",
                        "from intelligence_corpus_quarantine.contract import load_quarantine",
                        "print(load_quarantine(quarantine(*sys.argv[2:4], state_dir=sys.argv[4]))['track'])"])
    result = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT / "scripts/lib"), str(database),
                             str(tmp_path / "permit"), str(tmp_path / "state")], cwd=tmp_path, capture_output=True,
                            text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "intelligence-corpus-quarantine"
