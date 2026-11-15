"""CLI ledger B4b (ADR-075 decisions 7, 8, 10; L4): no argument values, exit code unchanged, one
line per call, actor_class derived, asked-to-executed pairing, coverage unknown without a ledger."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "lib")]

import cli_invocations as ci
import permission_events as pe

PID = "vnx-dev"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    return tmp_path / ".vnx-data" / PID / "state"


def _lines(state, name=ci.LEDGER):
    path = state / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_no_argument_value_reaches_the_ledger(store):
    token, flag_value = "ghp_" + "Q7" * 18, "uniqueflagvalue-8c1d2e"
    line = ci.record(["deliverable", "show", token, "--reason", flag_value], 0, 5, env={}, isatty=True)
    text = (store / ci.LEDGER).read_text()
    assert token not in text and flag_value not in text
    assert line["vorm"] == "vnx deliverable show <arg> --reason"
    assert set(json.loads(text)) == set(ci.FIELDS)


def test_promote_vorm_and_placeholders():
    assert ci.build_vorm(["deliverable", "promote", "dlv-4856e810fb14"]) == "vnx deliverable promote <dlv>"
    assert ci.build_vorm(["review", "--pr=2959", "x", "5816d5d3a"]) == "vnx review <arg> <sha> --pr"


@pytest.mark.parametrize("env,tty,expected", [
    ({"CLAUDE_ROLE": "orchestrator"}, False, "t0"), ({"VNX_DISPATCH_ID": "dlv-1"}, False, "worker"),
    ({}, True, "operator")])
def test_actor_class_is_derived(store, env, tty, expected):
    assert ci.record(["status"], 0, 1, env=env, isatty=tty)["actor_class"] == expected


def test_recorder_failure_is_swallowed(store, monkeypatch):
    monkeypatch.setattr(ci.pe, "_append_all", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert ci.record(["status"], 0, 1, env={}, isatty=True) is None
    assert not (store / ci.LEDGER).exists()


def _fake_home(tmp_path, recorder_body=None, exit_code=3):
    home = tmp_path / "vnxhome"
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "slot_cli.py").write_text(
        f"import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit({exit_code})\n")
    for item in (REPO / "scripts" / "lib").iterdir():
        if item.name != "cli_invocations.py":
            (home / "scripts" / "lib" / item.name).symlink_to(item)
    rec = home / "scripts" / "lib" / "cli_invocations.py"
    rec.write_text(recorder_body) if recorder_body else rec.symlink_to(REPO / "scripts/lib/cli_invocations.py")
    (home / "vnx_cli").symlink_to(REPO / "vnx_cli")
    (home / "bin").mkdir()
    (home / "bin" / "vnx").write_bytes((REPO / "bin" / "vnx").read_bytes())
    (home / "bin" / "vnx").chmod(0o755)  # bin/vnx derives VNX_HOME from its own place
    return home


def _run(home, tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_CANONICAL_ROOT", "VNX_DATA_DIR")}
    env.update(HOME=str(tmp_path), VNX_PROJECT_ID=PID)
    return subprocess.run([str(home / "bin" / "vnx"), *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def _wait_lines(state, n, settle=1.5):
    deadline = time.time() + 15
    while time.time() < deadline and len(_lines(state)) < n:
        time.sleep(0.1)
    time.sleep(settle)
    return _lines(state)


def test_exec_branch_keeps_exit_code_and_output_and_records_once(tmp_path, store):
    result = _run(_fake_home(tmp_path), tmp_path, "slot", "--token", "s3cr3tvalue")
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")
    lines = _wait_lines(store, 1)
    assert [(x["vorm"], x["exit_code"]) for x in lines] == [("vnx slot --token", 3)]


def test_raising_recorder_changes_nothing(tmp_path):
    home = _fake_home(tmp_path, recorder_body="raise RuntimeError('recorder broke')\n")
    result = _run(home, tmp_path, "slot")
    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")


def test_exec_into_vnx_cli_main_gives_one_line(tmp_path, store):
    result = _run(_fake_home(tmp_path), tmp_path, "preflight", "--help")
    assert result.returncode == 0
    assert [x["vorm"] for x in _wait_lines(store, 1)] == ["vnx preflight --help"]


def test_dispatch_door_is_outside_the_subshell_hook():
    src = (REPO / "bin" / "vnx").read_text()
    door, hook = src.index('if [ "${1:-help}" = dispatch ]; then\n  main "$@"'), src.index('(set -e; main "$@")')
    assert door < hook


def test_known_subcommands_cover_both_parsers(monkeypatch):
    import vnx_cli.main as m
    import vnx_cli._reexec as r
    monkeypatch.setattr(r, "maybe_reexec_pinned", lambda: None)
    captured = {}

    def grab(self, *a, **k):
        captured["parser"] = self
        raise SystemExit(0)
    monkeypatch.setattr(m._SuggestionArgumentParser, "parse_args", grab)
    monkeypatch.setattr(m, "_record_cli_call", lambda *a: None)
    with pytest.raises(SystemExit):
        m.main()
    names = set()

    def walk(parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    names.add(name)
                    walk(sub)
    walk(captured["parser"])
    src = (REPO / "bin" / "vnx").read_text()
    body = src[src.index("\nmain() {"):]
    body = body[:body.index("\n}\n")]
    for label in re.findall(r"^\s+([a-z][\w|-]*)\)", body, re.M):
        names.update(x for x in label.split("|") if not x.startswith("-"))
    assert names - set(ci.KNOWN_SUBCOMMANDS) == set()


def _asked(state, ts, command="bin/vnx deliverable promote dlv-4856e810fb14"):
    event = pe.build_event(project_id=PID, session_id="s1", ts=ts, account="vnx", origin="t0",
                           bron="B4a", besluit="asked", command=command)
    pe.write_events([event], PID, state)


def _call(state, ts, actor="operator", vorm="vnx deliverable promote <dlv>"):
    state.mkdir(parents=True, exist_ok=True)
    with (state / ci.LEDGER).open("a") as fh:
        fh.write(json.dumps({"schema": ci.SCHEMA, "ts": ts, "invocation_id": ts[-6:-1], "project_id": PID,
                             "vorm": vorm, "actor_class": actor, "exit_code": 0, "duration_ms": 9}) + "\n")


def _executed(state):
    return [e for e in _lines(state, "permission_events.ndjson") if e["besluit"] == "executed"]


@pytest.mark.parametrize("call_ts,actor,pairs", [
    ("2026-11-14T10:10:00Z", "operator", 1), ("2026-11-14T10:31:00Z", "operator", 0),
    ("2026-11-14T10:10:00Z", "t0", 0)])
def test_pair_window_and_actor(tmp_path, call_ts, actor, pairs):
    state = tmp_path / "state"
    _asked(state, "2026-11-14T10:00:00Z")
    _call(state, call_ts, actor)
    assert ci.pair(PID, state) == pairs
    executed = _executed(state)
    assert len(executed) == pairs and all(e["bron"] == "B4b" for e in executed)
    before = (state / "permission_events.ndjson").read_bytes()
    ci.pair(PID, state)
    assert (state / "permission_events.ndjson").read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_coverage_unknown_without_a_readable_ledger(tmp_path, damage):
    state = tmp_path / "state"
    _asked(state, "2026-11-14T10:00:00Z")
    if damage == "unreadable":
        (state / ci.LEDGER).write_bytes(b"\xff\xfe\n")
    assert ci.pair(PID, state) is None
    entry = json.loads((state / "permission_coverage.json").read_text())["sources"]["B4b"]
    assert entry["state"] == "unknown" and entry["unreadable_reason"] and entry["count"] is None
