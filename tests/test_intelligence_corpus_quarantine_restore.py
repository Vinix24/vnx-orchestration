"""Q3 split 2: restore puts a quarantined corpus back with one command and proves it before COMMIT."""
import hashlib
import importlib
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
CLI = ROOT / "scripts/vnx_intelligence_corpus_quarantine.py"
AUDIT = "intelligence_corpus_quarantine_audit.ndjson"
ROWIDS = [2, 3, 19946]  # 19946 % 9973 == 0: the modulus sample, beyond the first and last 20

contract = importlib.import_module("intelligence_corpus_quarantine.contract")
store = importlib.import_module("intelligence_corpus_quarantine.store")
table_digest = importlib.import_module("intelligence_corpus_quarantine.digest").table_digest


def cli(*args, cwd, state=None):
    env = dict(os.environ, **({"VNX_STATE_DIR": str(state)} if state else {}))
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True, cwd=cwd, env=env)


def content(db, table):
    with closing(sqlite3.connect(db)) as conn:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        kinds = ", ".join(f"{c}, typeof({c})" for c in columns)
        return conn.execute(f"SELECT rowid, {kinds} FROM {table} ORDER BY rowid").fetchall()


def counts(db):
    with closing(sqlite3.connect(db)) as conn:
        return [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in contract.CORPUS_TABLES]


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
    result = cli("quarantine", "--db", db, "--out", tmp_path / "permit", cwd=tmp_path, state=state)  # default state dir
    assert result.returncode == 0, result.stderr
    before = {t: content(db, t) for t in contract.CORPUS_TABLES}
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DELETE FROM code_snippets")
        conn.execute("DELETE FROM snippet_metadata")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='snippet_metadata'")  # restore must re-seed it itself
        conn.commit()
    return dict(db=db, q=tmp_path / "permit/artifact/quarantine.json", state=state, before=before, tmp=tmp_path)


def restore(p, *extra, state=None):
    return cli("restore", "--db", p["db"], "--quarantine", p["q"], "--state-dir", state or p["state"], *extra,
               cwd=p["tmp"])


def audit_lines(state, stage):
    lines = (state / AUDIT).read_text().splitlines() if (state / AUDIT).is_file() else []
    return [r for r in map(json.loads, lines) if r["stage"] == stage]


def test_restore_puts_rows_rowids_join_digest_and_index_back(permit):
    result = restore(permit)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["rows"] == {"code_snippets": 3, "snippet_metadata": 3}
    with closing(sqlite3.connect(permit["db"])) as conn:
        for table in contract.load_quarantine(permit["q"])["tables"]:  # rows, rowids, storage classes, digest
            assert content(permit["db"], table["name"]) == permit["before"][table["name"]]
            assert table_digest(conn, table["name"], table["columns"], "rowid")["digest_sha256"] == table["content_digest"]
        assert conn.execute("SELECT rowid FROM code_snippets WHERE code_snippets MATCH 'parse' "
                            "ORDER BY rowid").fetchall() == [(r,) for r in ROWIDS]
        assert conn.execute("SELECT count(*) FROM snippet_metadata m JOIN code_snippets s "
                            "ON s.rowid = m.snippet_rowid").fetchone() == (3,)
        assert conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path) VALUES (2,'n')").lastrowid > 20046
        assert conn.execute("INSERT INTO code_snippets(title) VALUES ('n')").lastrowid > 19946
    [line] = audit_lines(permit["state"], "restored")
    assert len(audit_lines(permit["state"], "quarantined")) == 1  # the fixture's default: the resolved VNX_STATE_DIR
    manifest = permit["q"].parent.parent / "manifest.json"
    assert line["permit_sha256"] == hashlib.sha256(manifest.read_bytes() + permit["q"].read_bytes()).hexdigest()
    assert (line["status"], line["code_snippets_rows"], line["snippet_metadata_rows"]) == ("ok", 3, 3)


def test_non_empty_target_without_allow_existing_inserts_nothing(permit):
    with closing(sqlite3.connect(permit["db"])) as conn:
        conn.execute("INSERT INTO snippet_metadata(id,snippet_rowid,file_path) VALUES (900,2,'later')")
        conn.commit()
    result = restore(permit)
    assert result.returncode == 3 and "snippet_metadata is not empty" in result.stderr, result.stderr
    assert counts(permit["db"]) == [0, 1] and audit_lines(permit["state"], "restored") == []


@pytest.mark.parametrize("clash,rc,after", [(True, 3, [1, 1]), (False, 0, [4, 3])], ids=["collision", "merge"])
def test_allow_existing_names_a_rowid_collision_or_merges_around_existing_rows(permit, clash, rc, after):
    with closing(sqlite3.connect(permit["db"])) as conn:
        conn.execute("INSERT INTO code_snippets(rowid,title) VALUES (500,'between')")  # inside the restored range
        if clash:  # the second table collides: one transaction means the first table gets nothing either
            conn.execute("INSERT INTO snippet_metadata(id,snippet_rowid,file_path) VALUES (103,500,'clash')")
        conn.commit()
    result = restore(permit, "--allow-existing")
    assert result.returncode == rc, result.stderr
    assert not clash or "collision in snippet_metadata: 1 rowids [103]" in result.stderr
    assert counts(permit["db"]) == after


@pytest.mark.parametrize("seam,value,reason", [
    ("table_digest", lambda *a, **k: dict(rows=3, digest_sha256="0" * 64), "code_snippets digest"),
    ("_probe_query", lambda values: '"absentword"', "missed [2, 3, 19946]"),
], ids=["digest", "match-probe"])
def test_failed_proof_rolls_back_and_exits_3(permit, monkeypatch, capsys, seam, value, reason):
    spec = importlib.util.spec_from_file_location("quarantine_cli", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(store, seam, value)
    rc = module.main(["restore", "--db", str(permit["db"]), "--quarantine", str(permit["q"]),
                      "--state-dir", str(permit["state"])])
    assert rc == 3 and reason in capsys.readouterr().err
    assert counts(permit["db"]) == [0, 0] and audit_lines(permit["state"], "restored") == []


def test_flipped_artifact_byte_refuses_before_any_write(permit):
    artifact = permit["q"].parent / contract.load_quarantine(permit["q"])["artifact"]
    permit["q"].parent.chmod(0o755)
    artifact.chmod(0o644)
    data = bytearray(artifact.read_bytes())
    data[len(data) // 2] ^= 0xFF
    artifact.write_bytes(bytes(data))
    before = permit["db"].read_bytes()
    result = restore(permit)
    assert result.returncode == 3 and "artifact does not match quarantine.json" in result.stderr, result.stderr
    assert permit["db"].read_bytes() == before and audit_lines(permit["state"], "restored") == []


def test_unwritable_audit_path_is_exit_4_without_a_success_line(permit):
    blocked = permit["tmp"] / "state-is-a-file"
    blocked.write_text("")
    result = restore(permit, state=blocked)
    assert result.returncode == 4 and "audit line was not written" in result.stderr, result.stderr
    assert result.stdout == "" and blocked.read_text() == ""
    assert counts(permit["db"]) == [3, 3]  # committed: exit 4 says the rows are back, only the record is missing


def test_audit_state_dir_inside_the_permit_dir_is_refused(permit):
    result = restore(permit, state=permit["q"].parent.parent)
    assert result.returncode == 3 and "B4" in result.stderr
    assert counts(permit["db"]) == [0, 0]


@pytest.mark.parametrize("stage,permit,status,reason", [("purged", "a" * 64, "ok", "stage"), (None, "a" * 64, "ok", "stage"),
    ("Restored", "a" * 64, "ok", "stage"), ("restored", "abc", "ok", "permit"), ("restored", None, "ok", "permit"),
    ("restored", "a" * 64, "", "status")])
def test_audit_append_refuses_an_unknown_stage_or_bad_field_and_writes_nothing(tmp_path, stage, permit, status, reason):
    audit = importlib.import_module("intelligence_corpus_quarantine.audit")
    (tmp_path / "state").mkdir()
    with pytest.raises(ValueError, match=reason):
        audit.append(tmp_path / "state", stage, permit, status, rows=1)
    assert list((tmp_path / "state").iterdir()) == []
