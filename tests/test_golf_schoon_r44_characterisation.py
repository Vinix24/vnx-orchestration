"""Golf schoon R44 (dlv-d4eb492ff32e): pin log_dispatch_metadata.main and pre_merge_gate.check_ci_workflow
(return values, exit codes, stdout/stderr, SQLite rows, order and arguments of external calls)."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location("pre_merge_gate_r44_char", SCRIPTS_DIR / "pre_merge_gate.py")
PMG = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PMG)
SKIP = PMG.SKIPPED_UNVERIFIED


# ---------------------------------------------------------------------------
# log_dispatch_metadata.py::main — run as the dispatcher runs it (a subprocess)
# ---------------------------------------------------------------------------

def _make_db(db_path: Path, *, project: bool, provider: bool) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT", "dispatch_id TEXT NOT NULL"]
    cols += ["project_id TEXT NOT NULL DEFAULT 'vnx-dev'"] if project else []
    cols += ["terminal TEXT NOT NULL", "track TEXT NOT NULL"]
    cols += ["provider TEXT", "model TEXT"] if provider else []
    cols += [f"{c} TEXT" for c in ("role", "skill_name", "gate", "pr_id", "dispatched_at",
                                    "target_open_items", "intelligence_json", "cognition", "priority")]
    cols += [f"{c} INTEGER DEFAULT 0" for c in ("pattern_count", "prevention_rule_count",
                                                 "instruction_char_count", "context_file_count")]
    cols += ["UNIQUE (project_id, dispatch_id)" if project else "UNIQUE (dispatch_id)"]
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"CREATE TABLE dispatch_metadata ({', '.join(cols)})")
    conn.commit()
    conn.close()


def _run_ldm(db_path: Path, *args: str, project_id: str | None = "vnx-dev"):
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_PROJECT_ID", "VNX_CANONICAL_ROOT")}
    env.update(VNX_STATE_DIR=str(db_path.parent), VNX_DATA_DIR=str(db_path.parent.parent))
    if project_id is not None:
        env["VNX_PROJECT_ID"] = project_id
    r = subprocess.run([sys.executable, str(SCRIPTS_DIR / "log_dispatch_metadata.py"), *args],
                       env=env, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _rows(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM dispatch_metadata ORDER BY id")]
    conn.close()
    return rows


_BASE = ("--dispatch-id", "r44-d1", "--terminal", "T1", "--track", "A")
_FULL = _BASE + (
    "--role", "backend-developer", "--skill-name", "sk", "--gate", "g1",
    "--cognition", "deep", "--priority", "P0", "--pr-id", "PR-9",
    "--pattern-count", "3", "--prevention-rule-count", "4",
    "--intelligence-json", '{"a": 1}', "--instruction-char-count", "500",
    "--context-file-count", "2", "--target-open-items", "OI-1, OI-2,,",
)
_OK = (0, "Logged dispatch metadata: r44-d1\n", "")


def test_ldm_missing_db_warns_and_exits_1(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    db.parent.mkdir(parents=True)
    assert _run_ldm(db, *_BASE) == (1, "", f"WARNING: DB not found: {db}\n")


def test_ldm_project_store_insert_then_update(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    _make_db(db, project=True, provider=True)
    assert _run_ldm(db, *_FULL, "--provider", "claude", "--model", "opus") == _OK
    [row] = _rows(db)
    dispatched_at = row.pop("dispatched_at")
    assert dispatched_at
    assert row == {
        "id": 1, "dispatch_id": "r44-d1", "project_id": "vnx-dev",
        "terminal": "T1", "track": "A", "provider": "claude", "model": "opus",
        "role": "backend-developer", "skill_name": "sk", "gate": "g1", "pr_id": "PR-9",
        "target_open_items": '["OI-1", "OI-2"]', "pattern_count": 3,
        "prevention_rule_count": 4, "intelligence_json": '{"a": 1}',
        "instruction_char_count": 500, "context_file_count": 2,
        "cognition": "deep", "priority": "P0",
    }
    # Re-call: other fields refresh, role/provider/model/dispatched_at stay.
    assert _run_ldm(db, "--dispatch-id", "r44-d1", "--terminal", "T2", "--track", "B", "--provider",
                    "codex", "--model", "gpt", "--target-open-items", '["OI-7"]') == _OK
    [r2] = _rows(db)
    assert (r2["dispatched_at"], r2["terminal"], r2["track"], r2["role"]) == (dispatched_at, "T2", "B", "backend-developer")
    assert (r2["provider"], r2["model"], r2["target_open_items"]) == ("claude", "opus", '["OI-7"]')
    assert (r2["skill_name"], r2["gate"], r2["pr_id"], r2["intelligence_json"]) == (None,) * 4
    assert (r2["cognition"], r2["priority"], r2["pattern_count"]) == ("normal", "P1", 0)


def test_ldm_project_store_update_scoped_to_project(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    _make_db(db, project=True, provider=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track) "
                     "VALUES ('r44-d1', 'other-proj', 'T9', 'Z')")
    assert _run_ldm(db, *_BASE, "--provider", "claude", "--model", "opus") == _OK
    rows = {row["project_id"]: row for row in _rows(db)}
    assert (rows["other-proj"]["terminal"], rows["other-proj"]["provider"]) == ("T9", None)
    mine = rows["vnx-dev"]
    assert (mine["terminal"], mine["provider"], mine["model"], mine["target_open_items"]) == ("T1", "claude", "opus", None)


def test_ldm_legacy_store_without_project_or_provider(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    _make_db(db, project=False, provider=False)
    assert _run_ldm(db, *_FULL, "--provider", "claude", "--model", "opus", project_id=None) == _OK
    [row] = _rows(db)
    assert (row["terminal"], row["role"], row["target_open_items"]) == ("T1", "backend-developer", '["OI-1", "OI-2"]')
    assert _run_ldm(db, "--dispatch-id", "r44-d1", "--terminal", "T3", "--track", "C", project_id=None) == _OK
    [row2] = _rows(db)
    assert (row2["terminal"], row2["track"], row2["role"]) == ("T3", "C", "backend-developer")


def test_ldm_legacy_store_with_provider_columns(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    _make_db(db, project=False, provider=True)
    assert _run_ldm(db, *_BASE, "--model", "opus", project_id=None) == _OK
    assert [(r["provider"], r["model"]) for r in _rows(db)] == [(None, "opus")]
    _run_ldm(db, *_BASE, "--provider", "kimi", "--model", "other", project_id=None)
    assert [(r["provider"], r["model"]) for r in _rows(db)] == [("kimi", "opus")]


def test_ldm_tenant_unresolved_refuses_write(tmp_path):
    db = tmp_path / "state" / "quality_intelligence.db"
    _make_db(db, project=True, provider=True)
    rc, out, err = _run_ldm(db, *_BASE, project_id="Not A Valid Id!")
    assert (rc, out) == (1, "")
    assert err.startswith(f"ERROR: tenant_stamp_skip log_dispatch_metadata db_path={db} "
                          "dispatch_id=r44-d1 terminal=T1 reason=")
    assert _rows(db) == []


# ---------------------------------------------------------------------------
# pre_merge_gate.py::check_ci_workflow — in process, only git/gh/forge patched
# ---------------------------------------------------------------------------

@pytest.fixture
def gate(monkeypatch):
    def _install(*responses, fetch=(None, "")):
        calls, fetches, queue = [], [], list(responses)

        def _run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            resp = queue.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            return subprocess.CompletedProcess(cmd, *resp)

        monkeypatch.setattr(PMG.subprocess, "run", _run)
        monkeypatch.setattr(PMG, "fetch_ci_workflow_from_main",
                            lambda project_root, **kw: fetches.append(project_root) or fetch)
        return calls, fetches

    return _install


def _skip(detail):
    return {"check": "ci_workflow", "status": SKIP, "detail": detail, "ci_conclusion": None, "ci_ran_on_sha": False}


SHA = "abcdef1234567890"
P = Path("/p")
GIT_KW = {"cwd": "/p", "capture_output": True, "text": True, "timeout": 10}
GH_KW = dict(GIT_KW, timeout=15)
GIT_HEAD, GIT_BRANCH = ["git", "rev-parse", "HEAD"], ["git", "rev-parse", "--abbrev-ref", "HEAD"]
_FAILS = [(1, "", "x"), FileNotFoundError(), subprocess.TimeoutExpired("git", 10)]


def _gh(branch, wf):
    return ["gh", "run", "list", "--branch", branch, "--workflow", wf, "--limit", "10",
            "--json", "conclusion,headSha,status,databaseId"]


def test_ci_workflow_error_reading_main(gate):
    calls, fetches = gate(fetch=(None, "404 boom"))
    assert PMG.check_ci_workflow(P) == _skip(
        "could not read the project's branch_protection.yaml on main (404 boom); CI workflow could not be verified")
    assert (calls, fetches) == ([], [P])


@pytest.mark.parametrize("resp", _FAILS)
def test_ci_head_sha_unresolved(gate, resp):
    calls, _ = gate(resp)
    assert PMG.check_ci_workflow(P) == _skip("could not resolve HEAD SHA — CI workflow could not be verified")
    assert calls == [(GIT_HEAD, GIT_KW)]


@pytest.mark.parametrize("resp", _FAILS)
def test_ci_branch_unresolved(gate, resp):
    calls, _ = gate((0, SHA + "\n", ""), resp)
    assert PMG.check_ci_workflow(P) == _skip("could not resolve branch name — CI workflow could not be verified")
    assert calls == [(GIT_HEAD, GIT_KW), (GIT_BRANCH, GIT_KW)]


@pytest.mark.parametrize("resp,detail", [
    (FileNotFoundError(), "gh CLI not available — CI workflow could not be verified"),
    (subprocess.TimeoutExpired("gh", 15), "gh CLI not available — CI workflow could not be verified"),
    ((1, "", "  " + "e" * 200), "gh run list failed for workflow 'W' — CI workflow could not be verified: " + "e" * 120),
    ((1, "", None), "gh run list failed for workflow 'W' — CI workflow could not be verified: "),
    ((0, "not json", ""), "gh output unparseable — CI workflow could not be verified"),
])
def test_ci_gh_unverifiable(gate, resp, detail):
    calls, fetches = gate(resp, fetch=(None, "would fail"))
    assert PMG.check_ci_workflow(P, branch="b", head_sha=SHA, workflow_name="W") == _skip(detail)
    assert (calls, fetches) == ([(_gh("b", "W"), GH_KW)], [])


def test_ci_success_on_sha_resolves_from_git_and_project_workflow(gate):
    runs = [{"headSha": "zzz", "conclusion": "failure", "databaseId": 1},
            {"headSha": SHA, "conclusion": "success", "databaseId": 42},
            {"headSha": SHA, "conclusion": "failure", "databaseId": 43}]
    calls, fetches = gate((0, SHA + "\n", ""), (0, "feat/x\n", ""), (0, json.dumps(runs), ""), fetch=("Proj CI", ""))
    assert PMG.check_ci_workflow(P) == {
        "check": "ci_workflow", "status": "GO", "detail": "Proj CI succeeded on abcdef123456 (run 42)",
        "ci_conclusion": "success", "ci_ran_on_sha": True, "ci_head_sha": SHA, "ci_run_id": 42}
    assert calls == [(GIT_HEAD, GIT_KW), (GIT_BRANCH, GIT_KW), (_gh("feat/x", "Proj CI"), GH_KW)]
    assert fetches == [P]


@pytest.mark.parametrize("conclusion,shown", [("failure", "failure"), (None, ""), ("", "")])
def test_ci_non_success_on_sha_holds(gate, conclusion, shown):
    gate((0, json.dumps([{"headSha": SHA, "conclusion": conclusion, "databaseId": 7}]), ""))
    assert PMG.check_ci_workflow(P, branch="b", head_sha=SHA, workflow_name="W") == {
        "check": "ci_workflow", "status": "HOLD",
        "detail": f"W conclusion is '{shown}' on abcdef123456 (run 7) — must be 'success'",
        "ci_conclusion": shown, "ci_ran_on_sha": True, "ci_head_sha": SHA, "ci_run_id": 7}


@pytest.mark.parametrize("latest,sha,concl", [
    ({"headSha": "1234567890abcdef99", "conclusion": "success"}, "1234567890ab", "success"),
    ({"headSha": None, "conclusion": None}, "", "unknown"),
])
def test_ci_ran_on_other_sha_holds(gate, latest, sha, concl):
    gate((0, json.dumps([latest, {"headSha": "other"}]), ""))
    assert PMG.check_ci_workflow(P, branch="b", head_sha=SHA, workflow_name="W") == {
        "check": "ci_workflow", "status": "HOLD",
        "detail": (f"W has NOT run on HEAD (abcdef123456). Latest run on branch 'b' was on {sha} "
                   f"(conclusion: {concl}). The PR may show green checks from a prior run — re-run CI."),
        "ci_conclusion": None, "ci_ran_on_sha": False, "ci_head_sha": SHA, "ci_latest_run_sha": sha}


def test_ci_never_ran_holds_and_ignores_the_deleted_env_workflow_name(gate, monkeypatch):
    calls, fetches = gate((0, "[]", ""))
    monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", "Env CI")
    assert PMG.check_ci_workflow(P, branch="b", head_sha=SHA) == {
        "check": "ci_workflow", "status": "HOLD",
        "detail": ("VNX CI has NEVER run on branch 'b'. No runs found for HEAD abcdef123456. PR checks may "
                   "show green from other workflows — this is the 'green-lie' that OI-931 guards against."),
        "ci_conclusion": None, "ci_ran_on_sha": False, "ci_head_sha": SHA}
    assert (calls, fetches) == ([(_gh("b", "VNX CI"), GH_KW)], [P])
