"""Q4: verify proves the nine-point permit and changes nothing."""
import hashlib
import importlib.util
import json
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
BOOLEANS = ["rows_equal", "digests_equal", "seal_ok"]
DROP = object()
spec = importlib.util.spec_from_file_location("corpus_cli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
verify_module = importlib.import_module("intelligence_corpus_quarantine.verify")


@pytest.fixture
def permit(tmp_path):
    db = tmp_path / "source.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        conn.executemany("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (?,?,?,?,?)",
                         [(2, "parse feed", "def parse(): pass", "/src/a.py", "python"),
                          (3, "store rows", "INSERT INTO t", "/src/b.py", "python"),
                          (19946, "rowid mod", "x = 1", "/src/c.py", "python")])
        conn.executemany("INSERT INTO snippet_metadata(snippet_rowid,file_path,created_at) VALUES (?,?,'2026-06-01')",
                         [(2, "/src/a.py"), (3, "/src/b.py"), (19946, "/src/c.py")])
        conn.commit()
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
    assert verdict["rows"] == {"code_snippets": 3, "snippet_metadata": 3}; assert verdict["requirements_checked"] == [1, 2, 3, 4]
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
]


@pytest.mark.parametrize("requirement,target,case,change", MATRIX, ids=[f"r{m[0]}-{m[2]}" for m in MATRIX])
def test_every_broken_requirement_refuses_with_exit_3(permit, capsys, requirement, target, case, change):
    tamper(getattr(permit, target), change)
    refused(permit, capsys, requirement)


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
