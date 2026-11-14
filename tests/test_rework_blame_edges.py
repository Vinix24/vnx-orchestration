"""Blame edges into rework_edges, from a watermark (herwerk H4, ADR-071 besluit 1, 4, 13)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import quality_db_init as qdi  # noqa: E402
import rework_blame_edges as rbe  # noqa: E402

PID = "tproj"
_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}
_RC = """
CREATE TABLE provenance_registry (dispatch_id TEXT PRIMARY KEY, commit_sha TEXT, pr_number INTEGER,
    registered_at TEXT NOT NULL DEFAULT '2026-01-01');
CREATE TABLE dispatches (dispatch_id TEXT PRIMARY KEY, metadata_json TEXT);
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_ENV, "HOME": str(repo.parent)}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, text=True).stdout.strip()


class Fixture:
    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        self.rc = sqlite3.connect(tmp_path / "rc.db")
        self.rc.executescript(_RC)
        qi = tmp_path / "qi.db"
        assert qdi.bootstrap_qi_db(qi) is True
        self.qi = sqlite3.connect(qi)
        self.wm = tmp_path / "wm.json"
        self.base = self.commit("chore: base", "base.txt", "x\n")

    def commit(self, subject: str, name: str, text: str, dispatch: str | None = None, task_class: str | None = None) -> str:
        (self.repo / name).write_text(text)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", subject)
        sha = _git(self.repo, "rev-parse", "HEAD")
        if dispatch:
            self.rc.execute("INSERT OR IGNORE INTO provenance_registry (dispatch_id, commit_sha) VALUES (?, ?)", (dispatch, sha))
            meta = json.dumps({"task_class": task_class} if task_class else {})
            self.rc.execute("INSERT OR IGNORE INTO dispatches (dispatch_id, metadata_json) VALUES (?, ?)", (dispatch, meta))
            self.rc.commit()
        return sha

    def run(self, project_id: str = PID, persist: bool = True, since: str | None = None, git=None) -> dict:
        since = since or (None if self.wm.exists() else self.base)
        return rbe.run_blame_edges(self.repo, self.rc, self.qi, project_id, watermark_path=self.wm,
                                   since=since, persist=persist, git=git)

    def edges(self, project_id: str = PID) -> list:
        return self.qi.execute("SELECT rework_dispatch_id, origin_dispatch_id, kind, class, source, evidence_json "
                               "FROM rework_edges WHERE project_id = ? ORDER BY rework_dispatch_id", (project_id,)).fetchall()


@pytest.fixture
def fx(tmp_path):
    f = Fixture(tmp_path)
    yield f
    f.rc.close()
    f.qi.close()


def _origin_then(fx: Fixture, subject: str, **kw) -> str:
    fx.commit("feat: origin", "a.txt", "one\ntwo\nthree\n", dispatch="O")
    return fx.commit(subject, "a.txt", "one\nTWO\nthree\n", dispatch="R", **kw)


def test_fix_over_feat_lines_gives_one_rework_blame_edge_with_evidence(fx):
    r = _origin_then(fx, "fix: repair")
    summary = fx.run()
    assert summary["stopped"] is None and summary["edges_written"] == 1
    [(rw, org, kind, cls, source, ev)] = fx.edges()
    assert (rw, org, kind, cls, source) == ("R", "O", "blame", "rework", "blame")
    assert json.loads(ev) == {"commit": r, "files": ["a.txt"], "lines": 1}
    assert fx.edges("other") == []  # a second project_id in the same database sees nothing


def test_refactor_over_the_same_lines_is_churn(fx):
    _origin_then(fx, "refactor(x): tidy")
    fx.run()
    assert [e[3] for e in fx.edges()] == ["churn"]


def test_untyped_subject_is_churn_and_counted(fx):
    _origin_then(fx, "tweak the thing")
    assert fx.run()["untyped_subject"] >= 1 and [e[3] for e in fx.edges()] == ["churn"]


def test_debugging_task_class_and_revert_are_rework(fx):
    _origin_then(fx, "chore: tweak", task_class="05_debugging")
    fx.run()
    assert [e[3] for e in fx.edges()] == ["rework"]
    assert rbe.classify('Revert "feat: a"', None) == "rework" and rbe.classify("docs: a", None) == "churn"


def test_live_shape_debugging_class_in_metadata_json_is_rework(fx):
    """OI-2761: the live dispatches table has no task_class column; the class sits in metadata_json."""
    _origin_then(fx, "chore: tweak", task_class="05_debugging")
    summary = fx.run()
    assert summary["stopped"] is None and [e[3] for e in fx.edges()] == ["rework"]


def test_live_shape_without_a_class_in_metadata_json_is_churn(fx):
    _origin_then(fx, "chore: tweak")
    summary = fx.run()
    assert summary["stopped"] is None and [e[3] for e in fx.edges()] == ["churn"]


def test_column_shape_works_and_the_column_wins_over_metadata(fx):
    fx.rc.execute("ALTER TABLE dispatches ADD COLUMN task_class TEXT")
    fx.commit("feat: origin", "a.txt", "one\ntwo\nthree\n", dispatch="O")
    fx.commit("chore: tweak", "a.txt", "one\nTWO\nthree\n", dispatch="R")
    fx.rc.execute("UPDATE dispatches SET task_class = '05_debugging', metadata_json = ? WHERE dispatch_id = 'R'",
                  (json.dumps({"task_class": "01_feature"}),))
    fx.rc.commit()
    fx.run()
    assert [e[3] for e in fx.edges()] == ["rework"]


def test_empty_column_falls_back_to_metadata_json(fx):
    fx.rc.execute("ALTER TABLE dispatches ADD COLUMN task_class TEXT")
    _origin_then(fx, "chore: tweak", task_class="05_debugging")
    fx.run()
    assert [e[3] for e in fx.edges()] == ["rework"]


@pytest.mark.parametrize("ddl", ["CREATE TABLE dispatches (dispatch_id TEXT PRIMARY KEY, state TEXT)", None])
def test_ledger_with_neither_class_source_stops_the_run(fx, ddl):
    _origin_then(fx, "fix: repair")
    fx.rc.execute("DROP TABLE dispatches")
    if ddl:
        fx.rc.execute(ddl)
    summary = fx.run()
    assert summary["stopped"] and "dispatches" in summary["stopped"]
    assert fx.edges() == [] and not fx.wm.exists()


def test_neither_source_keeps_an_existing_watermark_unchanged(fx):
    _origin_then(fx, "fix: repair")
    fx.wm.write_text(json.dumps({"commit": fx.base}))
    fx.rc.execute("DROP TABLE dispatches")
    fx.rc.execute("CREATE TABLE dispatches (dispatch_id TEXT PRIMARY KEY)")
    before = fx.wm.read_text()
    summary = fx.run()
    assert "task_class" in summary["stopped"] and fx.edges() == [] and fx.wm.read_text() == before


def test_second_run_changes_nothing(fx):
    _origin_then(fx, "fix: repair")
    fx.run()
    first = fx.edges()
    again = fx.run(since=fx.base)
    fx.wm.unlink()
    third = fx.run(since=fx.base)
    assert fx.edges() == first and again["scanned"] == 0 and third["edges_existing"] == 1


def test_failure_on_second_commit_keeps_watermark_and_first_commits_edges(fx):
    fx.commit("feat: origin", "a.txt", "one\ntwo\nthree\n", dispatch="O")
    fx.commit("fix: first", "a.txt", "one\nTWO\nthree\n", dispatch="R1")
    c2 = fx.commit("fix: second", "a.txt", "one\nTWO\nTHREE\n", dispatch="R2")

    def flaky(repo, args):
        if c2 in args and "show" in args:
            raise rbe.BlameRunError("boom")
        return rbe._git_strict(repo, args)

    summary = fx.run(git=flaky)
    assert "boom" in summary["stopped"]
    assert json.loads(fx.wm.read_text())["commit"] != c2
    assert [e[0] for e in fx.edges()] == ["R1"]
    resumed = fx.run()
    assert resumed["stopped"] is None and json.loads(fx.wm.read_text())["commit"] == c2
    assert [e[0] for e in fx.edges()] == ["R1", "R2"]


def test_commit_without_a_dispatch_is_unattributed(fx):
    fx.commit("feat: origin", "a.txt", "one\ntwo\nthree\n", dispatch="O")
    fx.commit("fix: unknown pr (#999)", "a.txt", "one\nTWO\nthree\n")
    summary = fx.run()
    assert fx.edges() == [] and summary["unattributed"] == 1


def test_squash_subject_pr_number_maps_to_the_dispatch(fx):
    fx.commit("feat: origin (#10)", "a.txt", "one\ntwo\nthree\n")
    fx.commit("fix: repair (#11)", "a.txt", "one\nTWO\nthree\n")
    fx.rc.executemany("INSERT INTO provenance_registry (dispatch_id, pr_number) VALUES (?, ?)", [("O", 10), ("R", 11)])
    fx.rc.commit()
    fx.run()
    assert [(e[0], e[1]) for e in fx.edges()] == [("R", "O")]


@pytest.mark.parametrize("content", [None, "not json", '{"commit": "abc"}'])
def test_missing_or_malformed_watermark_stops_the_run(fx, content):
    if content is not None:
        fx.wm.write_text(content)
    summary = rbe.run_blame_edges(fx.repo, fx.rc, fx.qi, PID, watermark_path=fx.wm)
    assert "watermark" in summary["stopped"] and fx.edges() == []


@pytest.mark.parametrize("table,reason", [("provenance_registry", "provenance_registry unreadable"), ("dispatches", "ledger unreadable")])
def test_unreadable_registry_or_ledger_stops_before_any_edge(fx, table, reason):
    _origin_then(fx, "fix: repair")
    fx.rc.execute(f"DROP TABLE {table}")
    summary = fx.run()
    assert reason in summary["stopped"] and fx.edges() == [] and not fx.wm.exists()


def test_dry_run_writes_nothing(fx):
    _origin_then(fx, "fix: repair")
    summary = fx.run(persist=False)
    assert summary["edges_written"] == 1 and fx.edges() == [] and not fx.wm.exists()


def test_cli_stops_with_exit_2_and_names_the_reason(tmp_path, monkeypatch, capsys):
    import attribute_rework as cli
    monkeypatch.setattr(cli, "resolve_central_data_dir", lambda pid: tmp_path)
    assert cli.main(["--persist", "--project-id", PID, "--json"]) == cli.EXIT_STOPPED
    assert "quality_intelligence.db" in json.loads(capsys.readouterr().out)["stopped"]


def test_a_missing_runtime_connection_stops_the_run_and_never_raises(tmp_path):
    qi = sqlite3.connect(":memory:")
    summary = rbe.run_blame_edges(tmp_path, None, qi, PID, watermark_path=tmp_path / "wm.json",
                                  since="a" * 40, git=lambda repo, args: "")
    assert "no runtime_coordination connection" in summary["stopped"]
    assert not (tmp_path / "wm.json").exists()
