"""Q5: purge empties the live corpus in one transaction that re-proves the permit and the session invariant."""
import hashlib
import importlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/lib"), str(ROOT / "tests")]
from test_intelligence_corpus_quarantine_verify import AUDIT, CLI, argv, build_permit, cli, jset, tamper  # noqa: E402

purge_module = importlib.import_module("intelligence_corpus_quarantine.purge")
audit_module = importlib.import_module("intelligence_corpus_quarantine.audit")


@pytest.fixture
def permit(tmp_path):
    p = build_permit(tmp_path)
    assert cli.main(argv(p)) == 0  # verify, as the T0 does before purge
    return p


def run(p):
    return cli.main(["purge", "--db", str(p.db), "--manifest", str(p.manifest), "--quarantine", str(p.folder),
                     "--state-dir", str(p.state)])


def count(p, table):
    with closing(sqlite3.connect(p.db)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def lines(p, stage):
    return [json.loads(x) for x in (p.state / AUDIT).read_text().splitlines() if json.loads(x)["stage"] == stage]


def digest_of(p):
    with closing(sqlite3.connect(p.db)) as conn:
        return conn.execute("SELECT group_concat(id || session_id || project_id, '|') FROM session_analytics").fetchone()[0]


def test_valid_permit_empties_the_corpus_and_changes_nothing_else(permit):
    sessions, others = digest_of(permit), {}
    with closing(sqlite3.connect(permit.db)) as conn:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'code\\_snippets%' ESCAPE '\\' "
                                    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name != 'snippet_metadata'").fetchall():
            others[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    assert run(permit) == 0
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata")) == (0, 0)
    assert digest_of(permit) == sessions
    with closing(sqlite3.connect(permit.db)) as conn:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] >= k for n, k in others.items())
    sha = hashlib.sha256(permit.manifest.read_bytes() + permit.quarantine.read_bytes()).hexdigest()
    deleted, vacuumed = lines(permit, "deleted"), lines(permit, "vacuumed")
    assert [(x["status"], x["permit_sha256"]) for x in deleted] == [("intent", sha), ("ok", sha)]
    assert [(x["status"], x["permit_sha256"]) for x in vacuumed] == [("intent", sha), ("ok", sha)]
    assert deleted[1]["session_in_transaction_equal"] is True and deleted[1]["baseline_max_id"] == 2
    assert vacuumed[1]["corpus_family_bytes_before"] >= vacuumed[1]["corpus_family_bytes_after"] >= 0
    with closing(sqlite3.connect(permit.artifact.as_uri() + "?mode=ro", uri=True)) as art:  # moved, not deleted
        assert art.execute("SELECT COUNT(*) FROM corpus_code_snippets").fetchone()[0] == 3
        assert art.execute("SELECT COUNT(*) FROM corpus_snippet_metadata").fetchone()[0] == 3


def test_a_broken_permit_is_exit_3_and_the_rows_stay(permit):
    tamper(permit.manifest, jset("invariants.session_analytics.digest_sha256", "0" * 64))
    assert run(permit) == 3
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata")) == (3, 3)
    assert [x["status"] for x in lines(permit, "deleted")] == ["intent", "refused"]


def test_a_row_added_between_verify_and_purge_names_its_rowid(permit, capsys):
    with closing(sqlite3.connect(permit.db)) as conn:
        conn.execute("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (50,'t','c','/p','x')")
        conn.commit()
    assert run(permit) == 3
    assert "rowid 50" in capsys.readouterr().err
    assert count(permit, "code_snippets") == 4 and lines(permit, "deleted")[-1]["status"] == "refused"


def test_a_verdict_that_covers_only_requirements_1_to_5_is_a_refusal(permit, monkeypatch):
    real = purge_module.check_permit
    monkeypatch.setattr(purge_module, "check_permit", lambda *a, **k: {**real(*a, **k), "requirements_checked": [1, 2, 3, 4, 5]})
    assert run(permit) == 3
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata")) == (3, 3)
    assert lines(permit, "deleted")[-1]["status"] == "refused"


@pytest.mark.parametrize("verdict", [None, {}, {"requirements_checked": None}, {"requirements_checked": [True, 2, 3, 4, 5, 6, 7, 8, 9]}],
                         ids=["none", "missing", "field-none", "bool-for-int"])
def test_a_malformed_verdict_never_deletes(permit, monkeypatch, verdict):
    monkeypatch.setattr(purge_module, "check_permit", lambda *a, **k: verdict)
    assert run(permit) == 3
    assert count(permit, "code_snippets") == 3


def test_a_delete_that_touches_session_analytics_rolls_back(permit, monkeypatch):
    monkeypatch.setattr(purge_module, "DELETE_SQL", (*purge_module.DELETE_SQL, "DELETE FROM session_analytics WHERE id = 2"))
    assert run(permit) == 3
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata"), count(permit, "session_analytics")) == (3, 3, 2)
    last = lines(permit, "deleted")[-1]
    assert last["status"] == "rolled_back" and "digest B != digest A" in last["reason"]


def _sql(p, statement):
    with closing(sqlite3.connect(p.db)) as conn:
        conn.execute(statement)
        conn.commit()


def _insert_sql(p, new_id="(SELECT max(id) + 1 FROM session_analytics)"):
    with closing(sqlite3.connect(p.db)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(session_analytics)") if r[1] != "id"]
    picked = ", ".join(f"'probe-' || {new_id}" if c == "session_id" else c for c in cols)
    return f"INSERT INTO session_analytics(id, {', '.join(cols)}) SELECT {new_id}, {picked} FROM session_analytics WHERE id = 1"


def _trigger(p, body):
    _sql(p, f"CREATE TRIGGER purge_probe AFTER DELETE ON snippet_metadata BEGIN {body}; END")


def _row_10_after_verdict(p, monkeypatch):
    """Row 10 appears after the permit verdict, so the transaction itself starts with it."""
    verdict = purge_module.check_permit(p.db, p.manifest, p.folder)
    _sql(p, _insert_sql(p, "10"))
    monkeypatch.setattr(purge_module, "check_permit", lambda *a, **k: verdict)


@pytest.mark.parametrize("kind, body, rows", [
    ("rows added", None, 2),
    ("rows changed", "UPDATE session_analytics SET project_id = project_id || 'x' WHERE id = 10", 3),
    ("rows removed", "DELETE FROM session_analytics WHERE id = 10", 3)])
def test_a_trigger_that_changes_session_rows_above_the_baseline_rolls_back(permit, monkeypatch, kind, body, rows):
    body and _row_10_after_verdict(permit, monkeypatch)
    _trigger(permit, body or _insert_sql(permit))
    assert run(permit) == 3
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata"), count(permit, "session_analytics")) == (3, 3, rows)
    last = lines(permit, "deleted")[-1]
    assert last["status"] == "rolled_back" and "digest B != digest A" in last["reason"] and kind in last["reason"]


def test_a_session_row_above_the_baseline_does_not_fail_the_after_measurement(permit):
    manifest = json.loads(permit.manifest.read_text())
    assert run(permit) == 0
    _sql(permit, _insert_sql(permit, "10"))
    assert purge_module._after_measure(permit.db, manifest, manifest["invariants"]["session_analytics"])["corpus_family_bytes_after"] >= 0


def test_an_unwritable_audit_path_from_the_start_is_exit_3_with_the_rows_still_there(permit):
    (permit.state / AUDIT).chmod(0o444)
    assert run(permit) == 3
    assert (count(permit, "code_snippets"), count(permit, "snippet_metadata")) == (3, 3)


def test_an_audit_failure_after_the_commit_is_exit_4_and_no_success_line(permit, monkeypatch, capsys):
    real = audit_module.append

    def append(state, stage, sha, status, **numbers):
        if status == "ok":
            raise OSError("disk full")
        return real(state, stage, sha, status, **numbers)

    monkeypatch.setattr(audit_module, "append", append)
    assert run(permit) == 4
    assert "restore path" in capsys.readouterr().err
    assert count(permit, "code_snippets") == 0
    assert not [x for x in lines(permit, "deleted") + lines(permit, "vacuumed") if x["status"] == "ok"]


def test_resume_on_zero_zero_finishes_only_the_vacuum_on_the_deleted_line(permit):
    assert run(permit) == 0
    assert run(permit) == 0
    assert [x["status"] for x in lines(permit, "deleted")] == ["intent", "ok"]
    assert [x["status"] for x in lines(permit, "vacuumed")] == ["intent", "ok", "intent", "ok"]


@pytest.mark.parametrize("intent_only", [False, True], ids=["no-line", "intent-without-ok"])
def test_zero_zero_without_a_deleted_ok_line_is_exit_3(permit, intent_only):
    with closing(sqlite3.connect(permit.db)) as conn:
        conn.executescript("DELETE FROM code_snippets; DELETE FROM snippet_metadata")
    if intent_only:
        sha = hashlib.sha256(permit.manifest.read_bytes() + permit.quarantine.read_bytes()).hexdigest()
        audit_module.append(permit.state, "deleted", sha, "intent")
    assert run(permit) == 3
    assert lines(permit, "deleted")[-1]["status"] == "refused" and not lines(permit, "vacuumed")


def test_a_fresh_interpreter_with_only_scripts_lib_runs_purge(permit):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from intelligence_corpus_quarantine.purge import purge; "
            "purge(*sys.argv[2:5], state_dir=sys.argv[5])")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "scripts/lib"), str(permit.db),
                             str(permit.manifest), str(permit.folder), str(permit.state)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert count(permit, "code_snippets") == 0


def test_the_cli_prints_the_result_as_json(permit):
    result = subprocess.run([sys.executable, str(CLI), "purge", "--db", str(permit.db), "--manifest", str(permit.manifest),
                             "--quarantine", str(permit.folder), "--state-dir", str(permit.state)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["rows_deleted"] == {"code_snippets_rows": 3, "snippet_metadata_rows": 3}


def _empty_store_with_audit(p, *records):
    with closing(sqlite3.connect(p.db)) as conn:
        conn.executescript("DELETE FROM code_snippets; DELETE FROM snippet_metadata")
    sha = hashlib.sha256(p.manifest.read_bytes() + p.quarantine.read_bytes()).hexdigest()
    ok = json.dumps({"stage": "deleted", "status": "ok", "permit_sha256": sha})
    (p.state / AUDIT).write_text("\n".join(r.replace("SHA", sha).replace("OK", ok) for r in records) + "\n", encoding="utf-8")
    return sha


def test_a_malformed_line_after_the_matching_line_refuses_the_resume(permit, capsys):
    sha = _empty_store_with_audit(permit, "OK", '{"stage": "vacuumed", ')
    with pytest.raises(ValueError, match="audit line 2"):
        purge_module._deleted_ok(permit.state, sha)
    capsys.readouterr()
    assert run(permit) == 3
    assert "audit line 2" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ['{"stage": "x", ', "[1]", ""], ids=["truncated", "not-object", "empty-middle"])
def test_a_malformed_line_before_the_matching_line_refuses(permit, bad):
    sha = _empty_store_with_audit(permit, bad, "OK")
    with pytest.raises(ValueError, match="audit line 1"):
        purge_module._deleted_ok(permit.state, sha)


def test_a_valid_file_without_a_matching_line_refuses(permit):
    sha = _empty_store_with_audit(permit, '{"stage": "deleted", "status": "intent", "permit_sha256": "SHA"}')
    assert purge_module._deleted_ok(permit.state, sha) is False
    assert run(permit) == 3


def test_a_raw_u2028_inside_a_json_string_does_not_cut_the_line(permit):
    sha = _empty_store_with_audit(permit, '{"stage": "note", "message": "a b\u0085c d"}', "OK")
    assert purge_module._deleted_ok(permit.state, sha) is True
    assert run(permit) == 0


def _bulk_permit(tmp_path, rows=4000):
    """A permit over a store whose code_snippets FTS5 index has several segments."""
    p = build_permit(tmp_path)
    with closing(sqlite3.connect(p.db)) as conn:
        for start in range(100, 100 + rows, 500):
            conn.executemany("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (?,?,?,?,?)",
                             [(i, f"title {i}", f"def f{i}(): return '{i}' * 40 # {i * 7919}", f"/src/{i}.py", "python")
                              for i in range(start, start + 500)])
            conn.commit()
    out, state = tmp_path / "permit2", tmp_path / "state2"
    out.mkdir()
    state.mkdir()
    assert cli.main(["baseline", "--db", str(p.db), "--out", str(out), "--snapshot", str(tmp_path / "snap2.db")]) == 0
    assert cli.main(["quarantine", "--db", str(p.db), "--out", str(out), "--state-dir", str(state)]) == 0
    folder = out / "artifact"
    p.state, p.folder, p.manifest, p.quarantine = state, folder, out / "manifest.json", folder / "quarantine.json"
    p.artifact, p.snapshot = next(folder.glob("*.db")), tmp_path / "snap2.db"
    assert cli.main(argv(p)) == 0
    return p


def _shadow_pages(p, table="code_snippets"):
    with closing(sqlite3.connect(p.db)) as conn:
        return conn.execute("SELECT count(*) FROM dbstat WHERE name = ?", (f"{table}_data",)).fetchone()[0]


def _empty_without_optimize(p):
    """The old behaviour: delete the rows and write the deleted/ok line, no optimize, no VACUUM."""
    sha = hashlib.sha256(p.manifest.read_bytes() + p.quarantine.read_bytes()).hexdigest()
    with closing(sqlite3.connect(p.db)) as conn:
        conn.executescript("DELETE FROM code_snippets; DELETE FROM snippet_metadata")
        conn.commit()
    audit_module.append(p.state, "deleted", sha, "intent")
    audit_module.append(p.state, "deleted", sha, "ok")
    return sha


def test_purge_merges_the_fts_index_so_the_file_shrinks_by_the_index_size(tmp_path):
    p = _bulk_permit(tmp_path)
    size_before, index_pages = p.db.stat().st_size, _shadow_pages(p)
    assert index_pages > purge_module.FTS_DATA_PAGES_MAX * 10
    assert run(p) == 0
    assert _shadow_pages(p) <= purge_module.FTS_DATA_PAGES_MAX
    with closing(sqlite3.connect(p.db)) as conn:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert size_before - p.db.stat().st_size >= (index_pages - purge_module.FTS_DATA_PAGES_MAX) * page


def test_resume_after_an_unoptimized_delete_gives_the_shrink_and_a_second_vacuumed_line(tmp_path):
    p = _bulk_permit(tmp_path)
    sha = _empty_without_optimize(p)
    assert _shadow_pages(p) > purge_module.FTS_DATA_PAGES_MAX
    assert run(p) == 0
    assert _shadow_pages(p) <= purge_module.FTS_DATA_PAGES_MAX
    vacuumed = lines(p, "vacuumed")
    assert [x["status"] for x in vacuumed] == ["intent", "ok"]
    assert {x["permit_sha256"] for x in vacuumed} == {sha}
    assert run(p) == 0
    assert [x["status"] for x in lines(p, "vacuumed")] == ["intent", "ok", "intent", "ok"]


def test_a_non_fts5_corpus_table_gets_no_optimize_statement(permit, monkeypatch):
    executed, real = [], sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(executed.append)
        return conn

    monkeypatch.setattr(purge_module.sqlite3, "connect", traced)
    assert run(permit) == 0
    optimize = [x for x in executed if "'optimize'" in x]
    assert optimize == ['INSERT INTO "code_snippets"("code_snippets") VALUES(\'optimize\')']
    assert not [x for x in optimize if "snippet_metadata" in x]


def test_a_shadow_table_above_the_bound_is_exit_4(tmp_path, monkeypatch, capsys):
    p = _bulk_permit(tmp_path)
    monkeypatch.setattr(purge_module, "_optimize_fts", lambda conn: None)
    assert run(p) == 4
    assert "code_snippets_data holds" in capsys.readouterr().err
    assert not [x for x in lines(p, "vacuumed") if x["status"] == "ok"]


def test_session_analytics_is_unchanged_after_the_purge_with_optimize(tmp_path):
    p = _bulk_permit(tmp_path)
    before = digest_of(p)
    assert run(p) == 0
    assert digest_of(p) == before and count(p, "session_analytics") == 2
