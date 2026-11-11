"""Q6: rehearse proves quarantine, verify, purge and restore on a copy of the store and touches nothing live."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/lib"), str(ROOT / "tests")]
from test_intelligence_corpus_quarantine_verify import AUDIT, CLI, build_permit, cli  # noqa: E402

BOOLEANS = ["rows_equal", "rowids_equal", "digests_equal", "fts_match_ok", "session_equal", "purge_empty", "live_unchanged"]


@pytest.fixture
def permit(tmp_path):
    return build_permit(tmp_path)


def argv(p, *extra):
    return ["rehearse", "--db", str(p.db), "--manifest", str(p.manifest), "--quarantine", str(p.folder),
            "--state-dir", str(p.state), *extra]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def sealed(folder):
    return {str(p): (p.stat().st_mode & 0o777, sha(p)) for p in [folder, *folder.iterdir()] if p.is_file()} | \
        {"folder": folder.stat().st_mode & 0o777}


def run(p, capsys, *extra):
    rc = cli.main(argv(p, *extra))
    return rc, capsys.readouterr()


def test_rehearse_on_a_copy_is_exit_0_with_every_flag_true_and_the_source_untouched(permit, capsys):
    db_before, sealed_before = sha(permit.db), sealed(permit.folder)
    rc, out = run(permit, capsys)
    assert rc == 0, out.err
    verdict = json.loads(out.out)
    assert all(verdict[name] is True for name in BOOLEANS) and verdict["ok"] is True and verdict["orphans"] == 0
    assert verdict["counts"]["before"] == {"code_snippets": 3, "snippet_metadata": 3}
    assert verdict["counts"]["purged"] == {"code_snippets": 0, "snippet_metadata": 0}
    assert verdict["counts"]["after"] == verdict["counts"]["before"]
    assert set(verdict["family_bytes"]) == {"before", "purged", "after"}
    copy = Path(verdict["copy"])
    assert copy.is_file() and copy.parent == Path(verdict["work_dir"]) and permit.folder not in copy.parents
    assert verdict["live_sha256_before"] == verdict["live_sha256_after"] == db_before == sha(permit.db)
    assert sealed(permit.folder) == sealed_before
    with closing(sqlite3.connect(copy)) as conn:  # the copy was purged and restored: it carries the rows again
        assert conn.execute("SELECT count(*) FROM code_snippets").fetchone() == (3,)
    live_lines = [json.loads(x) for x in (permit.state / AUDIT).read_text().splitlines()]
    assert [x["stage"] for x in live_lines] == ["quarantined", "rehearsed"]
    assert live_lines[-1]["status"] == "ok" and live_lines[-1]["copy"] == str(copy)
    assert not any(x["stage"] in ("deleted", "vacuumed", "restored") for x in live_lines)  # those went to the work audit


def test_an_artifact_of_another_database_is_rows_equal_false_and_exit_4(permit, tmp_path, capsys):
    (tmp_path / "other").mkdir()
    other = build_permit(tmp_path / "other")  # a second fixture; one row more goes in before its own quarantine
    with closing(sqlite3.connect(other.db)) as conn:
        conn.execute("INSERT INTO code_snippets(rowid,title,code,file_path,language) VALUES (7,'extra','x = 2','/src/d.py','python')")
        conn.execute("INSERT INTO snippet_metadata(snippet_rowid,file_path,created_at) VALUES (7,'/src/d.py','2026-06-01')")
        conn.commit()
    out, state = tmp_path / "foreign", tmp_path / "foreign-state"
    out.mkdir(), state.mkdir()
    assert cli.main(["baseline", "--db", str(other.db), "--out", str(out), "--snapshot", str(tmp_path / "foreign-snap.db")]) == 0
    assert cli.main(["quarantine", "--db", str(other.db), "--out", str(out), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = cli.main(["rehearse", "--db", str(permit.db), "--manifest", str(out / "manifest.json"),
                   "--quarantine", str(out / "artifact"), "--state-dir", str(permit.state)])
    verdict = json.loads(capsys.readouterr().out)
    assert rc == 4 and verdict["rows_equal"] is False and verdict["ok"] is False
    assert verdict["digests_equal"] is False and verdict["rowids_equal"] is False
    last = json.loads((permit.state / AUDIT).read_text().splitlines()[-1])
    assert (last["stage"], last["status"]) == ("rehearsed", "refused")


def test_discard_copy_removes_the_copy_and_nothing_else(permit, capsys, tmp_path):
    work = tmp_path / "work"
    rc, out = run(permit, capsys, "--work", str(work))
    assert rc == 0, out.err
    kept = tree(work)
    assert "copy.db" in kept
    fresh = tmp_path / "work2"
    rc, out = run(permit, capsys, "--work", str(fresh), "--discard-copy")
    assert rc == 0, out.err
    verdict = json.loads(out.out)
    assert verdict["copy_discarded"] is True and not Path(verdict["copy"]).exists()
    assert tree(fresh) == [x for x in kept if x != "copy.db"]
    assert (fresh / "permit/artifact").is_dir() and (fresh / "snapshot.db").is_file()


def test_the_live_artifact_and_the_given_manifest_are_never_written(permit, capsys):
    before = {p: (p.stat().st_mode & 0o777, sha(p)) for p in (permit.manifest, permit.quarantine, permit.artifact)}
    mtimes = {p: p.stat().st_mtime_ns for p in before}
    assert run(permit, capsys)[0] == 0
    assert {p: (p.stat().st_mode & 0o777, sha(p)) for p in before} == before
    assert {p: p.stat().st_mtime_ns for p in before} == mtimes
    assert sorted(x.name for x in permit.folder.iterdir()) == sorted([permit.artifact.name, "quarantine.json"])


def test_a_work_dir_inside_the_sealed_artifact_or_a_filled_one_is_exit_3_and_writes_no_copy(permit, capsys, tmp_path):
    rc, out = run(permit, capsys, "--work", str(permit.folder / "work"))
    assert rc == 3 and "inside the permit dir" in out.err and not (permit.folder / "work").exists()
    filled = tmp_path / "filled"
    filled.mkdir()
    (filled / "x").write_text("x")
    rc, out = run(permit, capsys, "--work", str(filled))
    assert rc == 3 and "not an empty directory" in out.err and sorted(p.name for p in filled.iterdir()) == ["x"]


def test_a_missing_or_broken_permit_is_exit_3_before_any_copy(permit, capsys, tmp_path):
    work = tmp_path / "work"
    permit.quarantine.parent.chmod(0o755)
    permit.quarantine.chmod(0o644)
    permit.quarantine.write_text("{not json")
    rc, out = run(permit, capsys, "--work", str(work))
    assert rc == 3 and not work.exists()


def test_a_stage_that_fails_on_the_copy_is_a_false_verdict_and_exit_4(permit, capsys, monkeypatch):
    import intelligence_corpus_quarantine.rehearse as module

    def broken(*args, **kwargs):
        raise ValueError("purge refused on the copy")
    monkeypatch.setattr(module, "purge", broken)
    rc, out = run(permit, capsys)
    verdict = json.loads(out.out)
    assert rc == 4 and verdict["ok"] is False and verdict["failed_stage"] == "purge"
    assert verdict["rows_equal"] is False and verdict["purge_empty"] is False and verdict["orphans"] is None


def test_a_live_database_that_changes_during_the_run_is_not_ok(permit, capsys, monkeypatch):
    import intelligence_corpus_quarantine.rehearse as module
    real = module.file_sha256
    calls = []

    def drifting(path):
        calls.append(path)
        return real(path) if len(calls) < 2 or Path(path) != permit.db else "0" * 64
    monkeypatch.setattr(module, "file_sha256", drifting)
    rc, out = run(permit, capsys)
    assert rc == 4 and json.loads(out.out)["live_unchanged"] is False


def test_a_fresh_interpreter_with_only_scripts_lib_on_the_path_rehearses(permit):
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from intelligence_corpus_quarantine.rehearse import rehearse; "
            "print(json.dumps(rehearse(*sys.argv[2:5], state_dir=sys.argv[5])))")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "scripts/lib"), str(permit.db),
                             str(permit.manifest), str(permit.folder), str(permit.state)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
