#!/usr/bin/env python3
"""Tests for `backfill_pr_merged_receipts.py --all-projects` (deur D7b).

The fleet runner enumerates the union of the project registry and the ledger-health
labels, runs the D7a audit once per project in its own subprocess (own cwd, own VNX_*
paths) and exits with the maximum severity. No real launchctl, gh or central store.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import backfill_pr_merged_receipts as bf


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _report(outcome: str, rc: int, missing: int = 0) -> str:
    return json.dumps({"outcome": outcome, "exit_code": rc, "reason": "",
                       "missing": [{"pr_number": n} for n in range(missing)]})


def _project(tmp_path: Path, pid: str, repo: bool = True) -> Dict[str, Any]:
    root = tmp_path / pid
    root.mkdir()
    if repo:
        (root / ".git").mkdir()
    return {"project_id": pid, "path": str(root), "name": pid}


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    """Registry on tmp_path, stubbed labels, recorded child calls."""
    state: Dict[str, Any] = {"labels": set(), "calls": [], "children": {}, "projects": []}
    registry = tmp_path / "projects.json"

    def setup(projects: List[Dict[str, Any]], labels):
        state["projects"] = projects
        state["labels"] = set(labels)
        registry.write_text(json.dumps({"projects": projects}))

    def fake_run(cmd, **kwargs):
        state["calls"].append({"cmd": cmd, "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        child = state["children"].get(kwargs.get("cwd"), _Done(0, _report("clean", 0)))
        if isinstance(child, Exception):
            raise child
        return child

    monkeypatch.setattr(bf, "_installed_ledger_health_ids", lambda: set(state["labels"]), raising=False)
    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "datahome"))
    state["setup"], state["registry"] = setup, registry
    return state


def _run(fleet, *extra, capsys=None):
    code = bf.main(["--all-projects", "--registry", str(fleet["registry"]), "--json", *extra])
    return code


def _rows(capsys) -> Dict[str, Dict[str, Any]]:
    return {r["project_id"]: r for r in json.loads(capsys.readouterr().out)["rows"]}


def test_unknown_project_is_exit_2_with_text(fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha")], {"alpha"})
    with pytest.raises(SystemExit) as raised:
        code = _run(fleet, "--project", "nope")
        raise SystemExit(code)
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "onbekend project: nope" in captured.out + captured.err
    assert fleet["calls"] == []


def test_readable_and_repo_less_project_gives_exit_2_one_row_each(fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha"), _project(tmp_path, "norepo", repo=False)], {"alpha", "norepo"})
    assert _run(fleet) == 2
    rows = _rows(capsys)
    assert set(rows) == {"alpha", "norepo"}
    assert rows["alpha"]["outcome"] == "clean" and rows["alpha"]["exit_code"] == 0
    assert rows["norepo"]["outcome"] == "unmeasurable" and rows["norepo"]["exit_code"] == 2
    assert len(fleet["calls"]) == 1


def test_empty_enumeration_is_exit_2(fleet, capsys):
    fleet["setup"]([], set())
    assert _run(fleet) == 2
    assert fleet["calls"] == []


@pytest.mark.parametrize("failure", [OSError("boom"), subprocess.TimeoutExpired("x", 1),
                                     _Done(0, "not json"), _Done(1, _report("clean", 0)),
                                     _Done(7, _report("gaps", 1)), _Done(0, "")])
def test_crash_of_a_leaves_b_measured(fleet, capsys, tmp_path, failure):
    a, b = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    fleet["setup"]([a, b], {"alpha", "beta"})
    fleet["children"][a["path"]] = failure
    assert _run(fleet) == 2
    rows = _rows(capsys)
    assert rows["alpha"]["outcome"] == "unmeasurable"
    assert rows["beta"]["outcome"] == "clean"
    assert len(fleet["calls"]) == 2


def test_child_exit_2_is_unmeasurable_and_gaps_are_counted(fleet, capsys, tmp_path):
    a, b = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    fleet["setup"]([a, b], {"alpha", "beta"})
    fleet["children"][a["path"]] = _Done(1, _report("gaps", 1, missing=3))
    assert _run(fleet) == 1
    rows = _rows(capsys)
    assert rows["alpha"]["outcome"] == "gaps" and rows["alpha"]["gaps"] == 3
    fleet["children"][b["path"]] = _Done(2, _report("unknown", 2))
    assert _run(fleet) == 2


def test_registry_only_project_is_job_missing(fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha"), _project(tmp_path, "beta")], {"alpha"})
    assert _run(fleet) == 1
    rows = _rows(capsys)
    assert rows["beta"]["finding"] == "job_missing" and rows["beta"]["exit_code"] == 1
    assert rows["alpha"]["finding"] == ""


def test_label_only_project_is_unregistered_and_unmeasurable(fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha")], {"alpha", "ghost"})
    assert _run(fleet) == 2
    row = _rows(capsys)["ghost"]
    assert row["finding"] == "unregistered_project" and row["outcome"] == "unmeasurable"


def test_unreadable_registry_is_exit_2(fleet, capsys, tmp_path):
    fleet["registry"].write_text("{broken")
    assert _run(fleet) == 2
    assert fleet["calls"] == []


def test_launchctl_failure_is_exit_2(monkeypatch, fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha")], set())
    monkeypatch.undo()

    def boom(*a, **k):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(bf.subprocess, "run", boom)
    assert bf.main(["--all-projects", "--registry", str(fleet["registry"])]) == 2


def test_project_flag_narrows_the_run(fleet, capsys, tmp_path):
    fleet["setup"]([_project(tmp_path, "alpha"), _project(tmp_path, "beta")], {"alpha", "beta"})
    assert _run(fleet, "--project", "beta") == 0
    assert set(_rows(capsys)) == {"beta"}
    assert len(fleet["calls"]) == 1


def test_project_without_all_projects_is_rejected():
    with pytest.raises(SystemExit) as raised:
        bf.main(["--project", "alpha"])
    assert raised.value.code == 2


def test_each_child_gets_its_own_cwd_and_env(fleet, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "leak")
    monkeypatch.setenv("VNX_LOGS_DIR", "/elsewhere/logs")
    a, b = _project(tmp_path, "alpha"), _project(tmp_path, "beta")
    fleet["setup"]([a, b], {"alpha", "beta"})
    assert _run(fleet, "--since", "2026-11-01") == 0
    by_cwd = {c["cwd"]: c for c in fleet["calls"]}
    assert set(by_cwd) == {a["path"], b["path"]}
    for project in (a, b):
        call = by_cwd[project["path"]]
        root = Path(project["path"]).resolve()
        env = call["env"]
        assert call["cmd"][1].endswith("backfill_pr_merged_receipts.py")
        assert "--report-only" in call["cmd"] and "--since" in call["cmd"]
        assert env["VNX_PROJECT_ID"] == project["project_id"]
        assert env["VNX_PROJECT_ROOT"] == str(root)
        assert env["VNX_DATA_DIR"].endswith(f"/{project['project_id']}")
        assert env["VNX_STATE_DIR"] == env["VNX_DATA_DIR"] + "/state"
        assert env["VNX_DATA_DIR_EXPLICIT"] == "1"
        assert "VNX_LOGS_DIR" not in env


def test_same_gap_twice_is_one_open_item_with_project_dedup_key(tmp_path, monkeypatch):
    import open_items_manager as oim
    state = tmp_path / "oi_state"
    state.mkdir()
    for attr, name in (("OPEN_ITEMS_FILE", "open_items.json"), ("DIGEST_FILE", "open_items_digest.json"),
                       ("MARKDOWN_FILE", "open_items.md"), ("AUDIT_LOG", "open_items_audit.jsonl")):
        monkeypatch.setattr(oim, attr, state / name)
    monkeypatch.setattr(oim, "STATE_DIR", state)
    pr = {"pr_number": 41, "pr_title": "x", "merged_at": "2026-11-07T00:00:00Z"}
    first = bf._file_open_item(pr, "alpha")
    second = bf._file_open_item(pr, "alpha")
    assert first[1] is True and second == (first[0], False)
    items = oim.load_items()["items"]
    assert [i["dedup_key"] for i in items] == ["door-audit:alpha:41"]
    assert bf._file_open_item(pr, "beta")[1] is True


def test_plist_is_one_machine_wide_daily_job():
    path = VNX_ROOT / "scripts" / "launchd" / "com.vnx.door-audit.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.vnx.door-audit"
    assert "${VNX_PROJECT_ID}" not in path.read_text().replace("<!--", "").split("-->", 1)[1]
    assert "--all-projects" in " ".join(data["ProgramArguments"])
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 30}
