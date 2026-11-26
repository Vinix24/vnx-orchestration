"""OI-3095: real stores and subprocesses keep both provenance readers in agreement."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import config_store_db

KEYS = ("VNX_PROVENANCE_ENFORCEMENT", "VNX_PROVENANCE_LEGACY_ACCEPTED")
OVERRIDES = ({}, {0: "0"}, {1: "1"}, {0: "0", 1: "1"}, {0: "1", 1: "0"})


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    python_bin = tmp_path / "python-bin"
    python_bin.mkdir()
    (python_bin / "python3").symlink_to(sys.executable)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VNX_", "GIT_"))}
    env.update(PATH=str(python_bin) + os.pathsep + env["PATH"],
               PYTHONPATH=str(ROOT / "scripts/lib"), VNX_PROJECT_ROOT=str(repo),
               VNX_DATA_DIR=str(tmp_path), VNX_DATA_DIR_EXPLICIT="1",
               VNX_STATE_DIR=str(tmp_path / "state"),
               VNX_GLOBAL_CONFIG_FILE=str(tmp_path / "global.json"))
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    Path(env["VNX_STATE_DIR"]).mkdir()
    return repo, env


def read_pair(checkout, expected, cause):
    repo, env = checkout
    code = ("import json, trace_token_validator as t, config_runtime as r; "
            "print(json.dumps([t._provenance_config(), t.get_enforcement_mode().value, "
            "r.wire_status().cause]))")
    python = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                            capture_output=True, text=True, check=True)
    pair, mode, actual_cause = json.loads(python.stdout)
    shell = subprocess.run(["bash", "-euc", 'source "$1/scripts/lib/provenance_config.sh"; '
                            'vnx_read_provenance_config "$1"; '
                            'printf "%s\\n%s\\n" "$ENFORCEMENT" "$LEGACY_ACCEPTED"',
                            "oi3095", str(ROOT)], cwd=repo, env=env,
                           capture_output=True, text=True, check=True)
    assert actual_cause == cause, python.stderr
    assert pair == expected, python.stderr
    assert mode == ("enforced" if expected[0] else "shadow"), python.stderr
    assert shell.stdout == "".join(f"{int(value)}\n" for value in expected), shell.stderr
    return python.stderr, shell.stderr


def set_overrides(env, overrides, expected):
    for index, value in overrides.items():
        env["VNX_OVERRIDE_" + KEYS[index][4:]] = value
        expected[index] = value == "1"


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("global_set", [False, True])
@pytest.mark.parametrize("store", ["ok", "unreadable", "missing"])
@pytest.mark.parametrize("overrides", OVERRIDES)
def test_reader_matrix(checkout, project, global_set, store, overrides):
    _, env = checkout
    env.update(zip(KEYS, ("1", "0")))
    if project:
        env["VNX_PROJECT_ID"] = "oi3095"
    db = Path(env["VNX_STATE_DIR"]) / "runtime_coordination.db"
    if store == "ok":
        with sqlite3.connect(db) as conn:
            for key, value in zip(KEYS, ("1", "0")):
                config_store_db.set_config(conn, "oi3095", key, value, actor="test", approval_id="oi3095")
    elif store == "unreadable":
        db.write_bytes(b"not sqlite")
    if global_set:
        Path(env["VNX_GLOBAL_CONFIG_FILE"]).write_text(json.dumps(
            {"version": 1, "values": {key: {"value": val} for key, val in zip(KEYS, ("0", "1"))}}))
    expected = [True, False] if project and store != "missing" else [not global_set, global_set]
    set_overrides(env, overrides, expected)
    cause = "no_project" if not project else ("store_missing" if store == "missing" else None)
    errors = read_pair(checkout, expected, cause)
    if project and store == "unreadable" and len(overrides) < 2:
        assert all("project_config_error" in error for error in errors)


@pytest.mark.parametrize("overrides", OVERRIDES)
@pytest.mark.parametrize("failure", ["resolver_failed", "global_corrupt", "global_unreadable"])
def test_read_errors_preserve_valid_overrides(checkout, overrides, failure):
    _, env = checkout
    env.update(zip(KEYS, ("0", "1")))
    cause, source = "no_project", "global_config_error"
    if failure == "resolver_failed":
        env.update(VNX_PROJECT_ID="oi3095", VNX_STATE_DIR="x" * 1024)
        cause, source = "resolver_failed", "project_config_error"
    elif failure == "global_corrupt":
        Path(env["VNX_GLOBAL_CONFIG_FILE"]).write_text("invalid json")
    else:
        Path(env["VNX_GLOBAL_CONFIG_FILE"]).mkdir()
    expected = [True, False]
    set_overrides(env, overrides, expected)
    errors = read_pair(checkout, expected, cause)
    if len(overrides) < 2:
        assert all(source in error for error in errors)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("value", ["", "null", "0\n1", "unknown"])
def test_invalid_override_stays_restrictive(checkout, index, value):
    env = checkout[1]
    env.update(zip(KEYS, ("0", "1")))
    env["VNX_OVERRIDE_" + KEYS[index][4:]] = value
    assert all("override" in error for error in read_pair(checkout, [True, False], "no_project"))


def test_no_project_defaults(checkout):
    read_pair(checkout, [False, True], "no_project")


@pytest.mark.parametrize("failure", ["reader_missing", "python_missing", "nonzero"])
def test_shell_reader_failure_stays_restrictive(checkout, failure):
    repo, env = checkout
    prefix = ""
    if failure == "python_missing":
        env["PATH"] = str(repo)
    if failure == "nonzero":
        # Run the real reader, then fail after its otherwise usable answer.
        prefix = 'PYTHON="$3"; python3() { "$PYTHON" "$@"; return 23; }; '
    result = subprocess.run(["/bin/bash", "-euc", prefix + 'source "$1/scripts/lib/provenance_config.sh"; '
                             'vnx_read_provenance_config "$2"; '
                             'printf "%s %s" "$ENFORCEMENT" "$LEGACY_ACCEPTED"',
                             "oi3095", str(ROOT), str(repo if failure == "reader_missing" else ROOT),
                             sys.executable], cwd=repo, env=env,
                            capture_output=True, text=True, check=True)
    assert result.stdout == "1 0"
    assert "Config read failed" in result.stderr
