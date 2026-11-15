"""CLI ledger B4b (ADR-075 decisions 7, 8, 10; L4), on tmp_path stores only."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "lib")]

import cli_invocations as ci

PID = "vnx-dev"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    for name in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_DATA_HOME"):  # the conftest's too
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".vnx-data" / PID).mkdir(parents=True)
    return tmp_path / ".vnx-data" / PID / "state"


def test_no_argument_value_reaches_the_ledger(store):
    token, flag_value = "ghp_" + "Q7" * 18, "uniqueflagvalue-8c1d2e"
    line = ci.record(["deliverable", "show", token, "--reason", flag_value], 0, 5, env={}, isatty=True)
    text = (store / ci.LEDGER).read_text()
    assert token not in text and flag_value not in text
    assert line["vorm"] == "vnx deliverable show <arg> --reason"
    assert set(json.loads(text)) == set(ci.FIELDS)
    assert ci.build_vorm(["deliverable", "promote", "dlv-4856e810fb14"]) == "vnx deliverable promote <dlv>"
    assert ci.build_vorm(["review", "--pr=2959", "x", "5816d5d3a"]) == "vnx review <arg> <sha> --pr"


@pytest.mark.parametrize("env,tty,expected", [
    ({"CLAUDE_ROLE": "orchestrator"}, False, "t0"), ({"VNX_DISPATCH_ID": "dlv-1"}, False, "worker"),
    ({}, True, "operator")])
def test_actor_class_is_derived(store, env, tty, expected):
    assert ci.record(["status"], 0, 1, env=env, isatty=tty)["actor_class"] == expected


def test_recorder_failure_leaves_an_error_in_the_coverage(store, monkeypatch):
    append, write, cov = ci.pe._append_all, ci.pe.write_coverage, store / "permission_coverage.json"
    monkeypatch.setattr(ci.pe, "_append_all", lambda *a: (_ for _ in ()).throw(OSError("/Users/x/secret-arg")))
    assert ci.record(["status"], 0, 1, env={}, isatty=True) is None
    assert not (store / ci.LEDGER).exists() and "secret-arg" not in cov.read_text()  # class name only
    entry = ci.pe._load_coverage(cov)[0]["B4b"]
    assert (entry["state"], entry["unreadable_reason"], ci.coverage(PID)) == ("error", "record failed: OSError", entry)
    monkeypatch.setattr(ci.pe, "write_coverage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cov")))
    assert ci.record(["status"], 0, 1, env={}, isatty=True) is None  # a failing trace raises nothing either
    monkeypatch.setattr(ci.pe, "_append_all", append)
    monkeypatch.setattr(ci.pe, "write_coverage", write)
    assert ci.record(["status"], 0, 1, env={}, isatty=True)  # a later line: only now ok with the count
    assert (ci.coverage(PID)["state"], ci.coverage(PID)["count"]) == ("ok", 1)


def test_a_failed_launch_keeps_the_exit_code_and_leaves_an_error(store, monkeypatch):
    import vnx_cli.main as m
    monkeypatch.delenv("VNX_CLI_RECORDED", raising=False)
    real = subprocess.Popen  # only the recorder's launch fails; git under the path resolver still runs
    monkeypatch.setattr(m.subprocess, "Popen", lambda a, *r, **k: (_ for _ in ()).throw(PermissionError("x"))
                        if "record" in a else real(a, *r, **k))
    with pytest.raises(SystemExit) as raised:
        m._recorded(lambda: sys.exit(4))()
    assert (raised.value.code, ci.coverage(PID)["unreadable_reason"]) == (4, "launch failed: PermissionError")


def _run(tmp_path, *args, recorder_body=None, **extra_env):
    """bin/vnx copied into a VNX_HOME (it derives VNX_HOME from its own place) whose slot_cli exits 3."""
    home = tmp_path / "vnxhome"
    lib = home / "scripts" / "lib"
    lib.mkdir(parents=True)
    if recorder_body:
        (lib / "cli_invocations.py").write_text(recorder_body)
    (home / "scripts" / "slot_cli.py").write_text(
        "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n")
    for item in (REPO / "scripts" / "lib").iterdir():
        if not (lib / item.name).exists():
            (lib / item.name).symlink_to(item)
    (home / "vnx_cli").symlink_to(REPO / "vnx_cli")
    (home / "bin").mkdir()
    shutil.copy2(REPO / "bin" / "vnx", home / "bin" / "vnx")
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_CANONICAL_ROOT", "VNX_DATA_DIR", "VNX_DATA_HOME")}
    return subprocess.run([str(home / "bin" / "vnx"), *args], cwd=tmp_path, capture_output=True, text=True,
                          env=dict(env, HOME=str(tmp_path), VNX_PROJECT_ID=PID, **extra_env), timeout=60)


def _wait_lines(state):
    path, deadline = state / ci.LEDGER, time.time() + 15
    while time.time() < deadline and not path.exists():
        time.sleep(0.1)
    time.sleep(1.5)  # a second line, if any, lands within this settle time
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_exec_branch_keeps_exit_code_and_output_and_records_once(tmp_path, store):
    result = _run(tmp_path, "slot", "--token", "s3cr3tvalue")
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")
    assert [(x["vorm"], x["exit_code"]) for x in _wait_lines(store)] == [("vnx slot --token", 3)]


def test_an_explicit_data_dir_gets_the_line_and_home_gets_no_store(tmp_path):
    data = tmp_path / "explicit-data"
    assert _run(tmp_path, "slot", VNX_DATA_DIR=str(data), VNX_DATA_DIR_EXPLICIT="1").returncode == 3
    assert [x["vorm"] for x in _wait_lines(data / "state")] == ["vnx slot"]
    assert not (tmp_path / ".vnx-data").exists()


def test_raising_recorder_changes_nothing(tmp_path):
    result = _run(tmp_path, "slot", recorder_body="raise RuntimeError('recorder broke')\n")
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_exec_into_vnx_cli_main_gives_one_line(tmp_path, store):
    assert _run(tmp_path, "preflight", "--help").returncode == 0
    assert [x["vorm"] for x in _wait_lines(store)] == ["vnx preflight --help"]


def test_known_subcommands_cover_both_parsers_and_door_is_outside(monkeypatch):
    import vnx_cli.main as m
    import vnx_cli._reexec as r
    monkeypatch.setattr(r, "maybe_reexec_pinned", lambda: None)
    parsers, names = [], set()
    monkeypatch.setattr(m._SuggestionArgumentParser, "parse_args", lambda self, *a: parsers.append(self) or sys.exit(0))
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        m.main()
    while parsers:  # every subparser name, at every depth
        for action in parsers.pop()._actions:
            if isinstance(action, argparse._SubParsersAction):
                names.update(action.choices)
                parsers.extend(action.choices.values())
    src = (REPO / "bin" / "vnx").read_text()
    body = src[src.index("\nmain() {"):]
    body = body[:body.index("\n}\n")]
    for label in re.findall(r"^\s+([a-z][\w|-]*)\)", body, re.M):
        names.update(x for x in label.split("|") if not x.startswith("-"))
    assert names - set(ci.KNOWN_SUBCOMMANDS) == set()
    # the door runs before, and so outside, the subshell hook: its process tree is unchanged
    assert src.index('if [ "${1:-help}" = dispatch ]; then main "$@"; exit $?; fi') < src.index('(set -e; main "$@")')


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_coverage_unknown_without_a_readable_ledger(tmp_path, damage):
    state = tmp_path / "state"
    state.mkdir()
    if damage == "unreadable":
        (state / ci.LEDGER).write_bytes(b"\xff\xfe\n")
    entry = ci.coverage(PID, state)
    assert entry["state"] == "unknown" and entry["unreadable_reason"] and entry["count"] is None
    assert json.loads((state / "permission_coverage.json").read_text())["sources"]["B4b"] == entry


def test_coverage_ok_with_a_readable_ledger(store):
    ci.record(["status"], 0, 1, env={}, isatty=True)
    assert (ci.coverage(PID)["state"], ci.coverage(PID)["count"]) == ("ok", 1)
