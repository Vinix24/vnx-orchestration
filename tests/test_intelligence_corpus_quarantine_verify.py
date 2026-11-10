"""Q4: verify proves the nine-point permit and changes nothing."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
CLI = ROOT / "scripts/vnx_intelligence_corpus_quarantine.py"
AUDIT = "intelligence_corpus_quarantine_audit.ndjson"
BOOLEANS = ["rows_equal", "digests_equal", "ddl_equal", "snapshot_ok", "seal_ok", "session_equal"]
DROP = object()
spec = importlib.util.spec_from_file_location("corpus_cli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
verify_module = importlib.import_module("intelligence_corpus_quarantine.verify")


@pytest.fixture
def permit(tmp_path):
    return build_permit(tmp_path)


def keep_only(conn, kept):
    """Drop every view and every table outside kept (FTS shadow tables go with their table); the documents keep all fields."""
    def names(sql):
        return [row[0] for row in conn.execute(sql).fetchall()]

    for view in names("SELECT name FROM sqlite_master WHERE type='view'"):
        conn.execute(f'DROP VIEW "{view}"')
    for vtab in names("SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'"):
        if vtab not in kept:
            conn.execute(f'DROP TABLE "{vtab}"')
    for table in names("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if not table.startswith(kept):
            conn.execute(f'DROP TABLE "{table}"')


def build_permit(tmp_path, kept=None):
    db = tmp_path / "source.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        if kept:
            keep_only(conn, kept)
        conn.executemany("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (?,?,?,?,?)",
                         [(2, "parse feed", "def parse(): pass", "/src/a.py", "python"),
                          (3, "store rows", "INSERT INTO t", "/src/b.py", "python"),
                          (19946, "rowid mod", "x = 1", "/src/c.py", "python")])
        conn.executemany("INSERT INTO snippet_metadata(snippet_rowid,file_path,created_at) VALUES (?,?,'2026-06-01')",
                         [(2, "/src/a.py"), (3, "/src/b.py"), (19946, "/src/c.py")])
        conn.executemany("INSERT INTO session_analytics(session_id,project_id,project_path,session_date) "
                         "VALUES (?,'fixture','/fixture','2026-06-01')", [("a",), ("b",)])
        conn.commit()
        if kept:
            conn.execute("VACUUM")
    out, state = tmp_path / "permit", tmp_path / "state"
    out.mkdir()
    state.mkdir()
    assert cli.main(["baseline", "--db", str(db), "--out", str(out), "--snapshot", str(tmp_path / "snap.db")]) == 0
    assert cli.main(["quarantine", "--db", str(db), "--out", str(out), "--state-dir", str(state)]) == 0
    folder = out / "artifact"
    return SimpleNamespace(db=db, state=state, folder=folder, manifest=out / "manifest.json",
                           quarantine=folder / "quarantine.json", artifact=next(folder.glob("*.db")),
                           snapshot=tmp_path / "snap.db")


def argv(p, *extra):
    return ["verify", "--db", str(p.db), "--manifest", str(p.manifest), "--quarantine", str(p.folder),
            "--state-dir", str(p.state), *extra]


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def audit_lines(p):
    lines = [json.loads(line) for line in (p.state / AUDIT).read_text().splitlines()]
    return [line for line in lines if line["stage"] == "verified"]


def tamper(path, change):
    """Rewrite a sealed file, then put its modes back so only the content differs."""
    parent_mode, mode = path.parent.stat().st_mode & 0o777, path.stat().st_mode & 0o777
    path.parent.chmod(0o755)
    path.chmod(0o644)
    path.write_bytes(change(path.read_bytes()))
    path.chmod(mode)
    path.parent.chmod(parent_mode)


def jset(dotted, value):
    def change(raw):
        doc = node = json.loads(raw)
        *head, last = [int(k) if k.isdigit() else k for k in dotted.split(".")]
        for key in head:
            node = node[key]
        if value is DROP:
            del node[last]
        else:
            node[last] = value
        return json.dumps(doc).encode()
    return change


def refused(p, capsys, requirement):
    before = fingerprint(p.db)
    assert cli.main(argv(p)) == 3
    assert f"requirement {requirement}:" in capsys.readouterr().err
    assert fingerprint(p.db) == before
    last = audit_lines(p)[-1]
    assert (last["stage"], last["status"], last["requirement"]) == ("verified", "refused", requirement)


def test_valid_permit_is_exit_0_with_a_true_verdict_and_changes_nothing(permit):
    before = {f: fingerprint(f) for f in (permit.db, permit.manifest, permit.quarantine, permit.artifact)}
    result = subprocess.run([sys.executable, str(CLI), *argv(permit)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    verdict = json.loads(result.stdout)
    assert all(verdict[name] is True for name in BOOLEANS)
    expected = hashlib.sha256(permit.manifest.read_bytes() + permit.quarantine.read_bytes()).hexdigest()
    assert verdict["permit_sha256"] == expected
    assert verdict["rows"] == {"code_snippets": 3, "snippet_metadata": 3}
    assert verdict["session_rows"] == 2
    assert verdict["requirements_checked"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert isinstance(verdict["user_version"], int)
    assert {f: fingerprint(f) for f in before} == before
    assert permit.folder.stat().st_mode & 0o777 == 0o555 and permit.artifact.stat().st_mode & 0o777 == 0o444
    (line,) = audit_lines(permit)
    assert (line["stage"], line["status"], line["permit_sha256"]) == ("verified", "ok", expected)
    assert not (permit.folder / AUDIT).exists()


def test_second_verify_and_human_output(permit, capsys):
    assert cli.main(argv(permit)) == 0 and cli.main(argv(permit, "--human")) == 0  # B4: the seal survives
    assert "rows_equal: True" in capsys.readouterr().out
    assert [l["status"] for l in audit_lines(permit)] == ["ok", "ok"]


MATRIX = [  # (requirement, file, name, change)
    (1, "manifest", "schema missing", jset("schema", DROP)),
    (1, "quarantine", "track None", jset("track", None)),
    (1, "manifest", "truncated file", lambda raw: raw[:20]),
    (1, "quarantine", "schema 2", jset("schema", 2)),
    (1, "manifest", "nested deeper than the JSON parser's stack", lambda raw: b"[" * 100000 + b"]" * 100000),
    (2, "quarantine", "bytes missing", jset("artifact_bytes", DROP)),
    (2, "quarantine", "sha None", jset("artifact_sha256", None)),
    (2, "quarantine", "sha not hex", jset("artifact_sha256", "xyz")),
    (2, "quarantine", "path with ..", jset("artifact", "../x.db")),
    (2, "artifact", "truncated artifact", lambda raw: raw[:100]),
    (3, "quarantine", "seal missing", jset("seal", DROP)),
    (3, "quarantine", "seal unknown mode", jset("seal.dir_mode", "0777")),
    (4, "quarantine", "digest changed", jset("tables.0.content_digest", "0" * 64)),
    (4, "quarantine", "columns malformed", jset("tables.0.columns", "x")),
    (4, "quarantine", "table entry missing", jset("tables.1", DROP)),
    (5, "quarantine", "ddl hash missing", jset("source_ddl_sha256", DROP)),
    (5, "quarantine", "ddl hash None", jset("source_ddl_sha256", None)),
    (5, "quarantine", "ddl hash truncated", jset("source_ddl_sha256", "abc")),
    (5, "quarantine", "ddl hash unknown", jset("source_ddl_sha256", "0" * 64)),
    (6, "manifest", "session digest missing", jset("invariants.session_analytics.digest_sha256", DROP)),
    (6, "manifest", "session rows None", jset("invariants.session_analytics.rows", None)),
    (6, "manifest", "session columns malformed", jset("invariants.session_analytics.columns", "x")),
    (6, "manifest", "session sum_id unknown", jset("invariants.session_analytics.sum_id", 999)),
    (7, "manifest", "corpus rows missing", jset("corpus.rows", DROP)),
    (7, "manifest", "corpus count None", jset("corpus.rows.code_snippets", None)),
    (7, "manifest", "corpus rows malformed", jset("corpus.rows", "x")),
    (7, "manifest", "corpus count unknown", jset("corpus.rows.code_snippets", 99)),
    (8, "manifest", "snapshot missing", jset("snapshot", DROP)),
    (8, "manifest", "snapshot sha None", jset("snapshot.sha256", None)),
    (8, "manifest", "snapshot bytes malformed", jset("snapshot.bytes", "x")),
    (8, "snapshot", "snapshot truncated", lambda raw: raw[:100]),
    (9, "manifest", "db bytes missing", jset("db.bytes", DROP)),
    (9, "manifest", "db bytes None", jset("db.bytes", None)),
    (9, "manifest", "db bytes malformed", jset("db.bytes", "x")),
    (9, "manifest", "db bytes zero", jset("db.bytes", 0)),
]


@pytest.mark.parametrize("requirement,target,case,change", MATRIX, ids=[f"r{m[0]}-{m[2]}" for m in MATRIX])
def test_every_broken_requirement_refuses_with_exit_3(permit, capsys, requirement, target, case, change):
    tamper(getattr(permit, target), change)
    refused(permit, capsys, requirement)


def test_a_changed_session_column_names_its_id(permit, capsys):
    with closing(sqlite3.connect(permit.db)) as conn:
        conn.execute("UPDATE session_analytics SET deep_analysis_json='changed' WHERE id=2")
        conn.commit()
    refused(permit, capsys, 6)
    assert "id 2 is missing" in audit_lines(permit)[-1]["reason"]


def test_a_row_added_after_quarantine_names_its_rowid(permit, capsys):
    with closing(sqlite3.connect(permit.db)) as conn:
        conn.execute("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (50,'t','c','/p','x')")
        conn.commit()
    refused(permit, capsys, 4)
    assert (permit.state / AUDIT).read_text().count("rowid 50") == 1


def reseal_artifact(p, change):  # rewrite the artifact through sqlite, then make quarantine.json describe the new bytes
    p.folder.chmod(0o755)
    p.artifact.chmod(0o644)
    with closing(sqlite3.connect(p.artifact)) as conn:
        change(conn)
        conn.commit()
    p.artifact.chmod(0o444)
    p.folder.chmod(0o555)
    raw = p.artifact.read_bytes()
    tamper(p.quarantine, lambda doc: json.dumps({**json.loads(doc), "artifact_bytes": len(raw),
                                                  "artifact_sha256": hashlib.sha256(raw).hexdigest()}).encode())


@pytest.mark.parametrize("change", [lambda conn: conn.executescript("DROP TABLE corpus_code_snippets; DROP TABLE corpus_snippet_metadata"), lambda conn: conn.execute("DELETE FROM corpus_code_snippets WHERE rowid = 3")],
                         ids=["valid database without corpus tables", "one row fewer"])
def test_an_artifact_that_differs_refuses_on_the_artifact_even_with_matching_bytes_and_hash(permit, capsys, change):
    reseal_artifact(permit, change)
    refused(permit, capsys, 4)
    assert "artifact" in audit_lines(permit)[-1]["reason"]


@pytest.mark.parametrize("name", [[], 7, "x", None, {}], ids=["list", "int", "unknown", "none", "dict"])
def test_a_malformed_table_name_is_a_refusal_never_a_traceback(permit, capsys, name):
    tamper(permit.quarantine, jset("tables.0.name", name))
    refused(permit, capsys, 4)


def test_garbage_artifact_with_a_matching_hash_is_not_a_database(permit, capsys):
    size = permit.artifact.stat().st_size
    tamper(permit.artifact, lambda raw: b"x" * size)
    tamper(permit.quarantine, jset("artifact_sha256", hashlib.sha256(b"x" * size).hexdigest()))
    refused(permit, capsys, 3)


def test_broken_seal_and_decoy_db_refuse(permit, capsys):
    permit.folder.chmod(0o755)
    refused(permit, capsys, 3)
    permit.folder.chmod(0o555)
    permit.db.write_bytes(b"")
    assert cli.main(argv(permit)) == 3 and "0 bytes" in capsys.readouterr().err


def test_audit_failures_are_never_a_silent_success(permit, capsys, tmp_path):
    assert cli.main(argv(permit)[:-2] + ["--state-dir", str(tmp_path / "missing")]) == 1
    assert "audit line was not written" in capsys.readouterr().err
    assert cli.main(argv(permit)[:-2] + ["--state-dir", str(permit.folder / "inside")]) == 3


@pytest.mark.parametrize("where", ["permit dir itself", "sibling of the sealed folder"])
def test_verify_refuses_an_audit_dir_anywhere_in_the_permit_dir(permit, where):
    state = permit.folder.parent if where == "permit dir itself" else permit.folder.parent / "audit"
    state.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="B4"):
        verify_module.verify(permit.db, permit.manifest, permit.folder, state_dir=state)
    assert not (state / AUDIT).exists()


def test_a_fresh_interpreter_with_only_scripts_lib_runs_verify(permit):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from intelligence_corpus_quarantine.verify import verify; "
            "v = verify(*sys.argv[2:5], state_dir=sys.argv[5]); "
            "sys.exit(0 if v['rows_equal'] is True else 1)")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "scripts/lib"), str(permit.db),
                             str(permit.manifest), str(permit.folder), str(permit.state)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_check_permit_runs_on_the_callers_connection(permit):
    with closing(sqlite3.connect(permit.db.as_uri() + "?mode=ro", uri=True)) as conn:
        verdict = verify_module.check_permit(permit.db, permit.manifest, permit.folder, conn=conn)
    assert verdict["digests_equal"] is True


@pytest.fixture
def relative_snapshot(permit, monkeypatch):
    """A manifest whose snapshot.path is relative but resolves to the real snapshot from the working directory."""
    monkeypatch.chdir(permit.snapshot.parent)
    tamper(permit.manifest, jset("snapshot.path", permit.snapshot.name))
    return permit


def test_relative_snapshot_path_refuses_requirement_8(relative_snapshot):
    p = relative_snapshot
    with pytest.raises(verify_module.Refusal) as info:
        verify_module.check_permit(p.db, p.manifest, p.folder)
    assert info.value.requirement == 8
    assert "snap.db" in str(info.value)


def test_verify_writes_one_refused_audit_line_for_a_relative_snapshot_path(relative_snapshot):
    p = relative_snapshot
    with pytest.raises(verify_module.Refusal) as info:
        verify_module.verify(p.db, p.manifest, p.folder, state_dir=p.state)
    assert info.value.requirement == 8
    (line,) = audit_lines(p)
    assert (line["status"], line["requirement"]) == ("refused", 8)


def test_snapshot_path_with_nul_refuses_requirement_8(permit):
    tamper(permit.manifest, jset("snapshot.path", str(permit.snapshot) + "\u0000x"))
    with pytest.raises(verify_module.Refusal) as info:
        verify_module.check_permit(permit.db, permit.manifest, permit.folder)
    assert info.value.requirement == 8


def test_open_ro_refuses_a_relative_path_for_its_own_requirement():
    with pytest.raises(verify_module.Refusal) as info:
        verify_module._open_ro("rel.db", 3, "artifact")
    assert info.value.requirement == 3
    assert "artifact" in str(info.value)


MALFORMED = [None, {}, [], [{}], 0, -1, "x", True, "x\u0000"]  # the last: a NUL reaching os.stat is a ValueError
COLUMN_ELEMENT = {"manifest": ("invariants", "session_analytics", "columns", 0), "quarantine": ("tables", 0, "columns", 0)}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """One valid permit per module, on the three tables the permit reads; a case writes only its own copy of one document."""
    p = build_permit(tmp_path_factory.mktemp("corpus"), kept=("code_snippets", "snippet_metadata", "session_analytics"))
    p.docs = {"manifest": json.loads(p.manifest.read_text()), "quarantine": json.loads(p.quarantine.read_text())}
    return p


def key_paths(node, prefix=()):
    """Every key path below node, list elements and the containers themselves included."""
    children = node.items() if type(node) is dict else enumerate(node) if type(node) is list else ()
    for key, child in children:
        yield prefix + (key,)
        yield from key_paths(child, prefix + (key,))


def mutated(doc, path, value):
    """A deep copy of doc with path set to value, or None when that is the value it already holds."""
    copy = node = json.loads(json.dumps(doc))
    for key in path[:-1]:
        node = node[key]
    if type(node[path[-1]]) is type(value) and node[path[-1]] == value:
        return None
    node[path[-1]] = value
    return copy


def case_files(p, case, target, document):
    """Manifest and quarantine dir of one case: a mutated quarantine.json gets its own sealed folder beside a hard link."""
    if target == "manifest":
        case.write_text(json.dumps(document))
        return case, p.folder
    folder = case / "artifact"
    folder.mkdir(parents=True)
    os.link(p.artifact, folder / p.artifact.name)
    (folder / "quarantine.json").write_text(json.dumps(document))
    (folder / "quarantine.json").chmod(0o444)
    folder.chmod(0o555)
    return p.manifest, folder


def outcome(call):
    try:
        call()
    except verify_module.Refusal as refusal:
        return "refused", refusal.requirement
    except Exception as exc:
        return "raised", f"{type(exc).__name__}: {exc}"
    return "ok", None


def appended(log, start):
    raw = log.read_bytes()[start:] if log.exists() else b""
    return [(line["stage"], line["status"], line.get("requirement")) for line in map(json.loads, raw.splitlines())]


@pytest.mark.parametrize("value", MALFORMED, ids=repr)
@pytest.mark.parametrize("target", ["manifest", "quarantine"])
def test_every_malformed_field_of_both_documents_is_a_refusal_or_a_verdict(corpus, tmp_path, target, value):
    paths = list(key_paths(corpus.docs[target]))
    assert COLUMN_ELEMENT[target] in paths  # the walk reaches list elements, so a field added later is covered too
    state, failures, cases = tmp_path / "state", [], 0
    state.mkdir()
    log = state / AUDIT
    for n, path in enumerate(paths):
        document = mutated(corpus.docs[target], path, value)
        if document is None:
            continue
        cases += 1
        manifest, folder = case_files(corpus, tmp_path / f"case{n}", target, document)
        checked = outcome(lambda: verify_module.check_permit(corpus.db, manifest, folder))
        start = log.stat().st_size if log.exists() else 0
        verified = outcome(lambda: verify_module.verify(corpus.db, manifest, folder, state_dir=state))
        lines = appended(log, start)
        if checked[0] == "raised" or verified != checked or lines != [("verified", *checked)]:
            failures.append(f"{target}.{'.'.join(map(str, path))} = {value!r}: check_permit {checked}, "
                            f"verify {verified}, audit {lines}")
    print(f"{target} = {value!r}: {cases} cases")
    assert not failures, f"{len(failures)} of {cases} cases:\n" + "\n".join(failures)


def recreate_session_table(p, sql):
    with closing(sqlite3.connect(p.db)) as conn:
        conn.executescript(f"DROP TABLE session_analytics; {sql}")


@pytest.mark.parametrize("sql,columns", [
    ("CREATE TABLE session_analytics(id, session_id); INSERT INTO session_analytics VALUES (1,'s'),(1,'t');",
     ["id", "session_id"]),
    ("CREATE TABLE session_analytics(session_id TEXT); INSERT INTO session_analytics VALUES ('s');", ["session_id"]),
    ("", []),
], ids=["id not a unique integer", "no id column", "table gone, columns empty"])
def test_a_session_table_that_cannot_be_measured_refuses_requirement_6(permit, sql, columns):
    recreate_session_table(permit, sql)
    tamper(permit.manifest, jset("invariants.session_analytics.columns", columns))
    with pytest.raises(verify_module.Refusal) as info:
        verify_module.verify(permit.db, permit.manifest, permit.folder, state_dir=permit.state)
    assert info.value.requirement == 6 and "session_analytics" in str(info.value)
    (line,) = audit_lines(permit)
    assert (line["status"], line["requirement"]) == ("refused", 6)


def test_a_database_without_read_permission_refuses_requirement_9(permit):
    permit.db.chmod(0)
    try:
        with pytest.raises(verify_module.Refusal) as info:
            verify_module.verify(permit.db, permit.manifest, permit.folder, state_dir=permit.state)
    finally:
        permit.db.chmod(0o644)
    assert info.value.requirement == 9 and "does not open" in str(info.value)
    (line,) = audit_lines(permit)
    assert (line["status"], line["requirement"]) == ("refused", 9)


def test_a_callers_connection_with_a_missing_database_path_refuses_requirement_9(permit, tmp_path):
    with closing(sqlite3.connect(permit.db.as_uri() + "?mode=ro", uri=True)) as conn:
        with pytest.raises(verify_module.Refusal) as info:
            verify_module.check_permit(tmp_path / "gone.db", permit.manifest, permit.folder, conn=conn)
    assert info.value.requirement == 9
