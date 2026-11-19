"""``vnx config list/get/explain/get --raw`` (config-een-oppervlak PR-10a): read-only chain view."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_cli  # noqa: E402
import config_registry as cr  # noqa: E402
from vnx_mode import VNXMode, check_command_allowed  # noqa: E402

KEY_A, KEY_B = "VNX_ANALYZER_LLM", "VNX_ENFORCEMENT_PROFILE"


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "empty-global" / "config.json"))
    for k in (KEY_A, KEY_B):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{k[4:]}", raising=False)


def _run(argv, resolver=None):
    out, err = [], []
    code = config_cli.main(argv, resolver=resolver or (lambda pid, key: None), project_id="p1",
                           out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def _boom(pid, key):
    raise RuntimeError("db locked")


def test_raw_two_keys_in_order(monkeypatch):
    monkeypatch.setenv(KEY_A, "alpha")
    monkeypatch.setenv(KEY_B, "beta")
    assert _run(["get", "--raw", KEY_B, KEY_A])[:2] == (0, "beta\nalpha")


def test_raw_unset_key_is_empty_line(monkeypatch):
    monkeypatch.delenv("VNX_DIGEST_EMAIL", raising=False)
    code, out, _ = _run(["get", "--raw", "VNX_DIGEST_EMAIL", KEY_A])
    assert code == 0 and out.split("\n")[0] == ""


def test_raw_unknown_key_exits_2():
    code, out, err = _run(["get", "--raw", KEY_A, "VNX_NO_SUCH_KEY"])
    assert code == 2 and out == "" and "VNX_NO_SUCH_KEY" in err


def test_raw_store_raises_exits_3():
    code, out, err = _run(["get", "--raw", KEY_B], resolver=_boom)
    assert code == 3 and out == "" and KEY_B in err


def test_raw_override_beats_store_error(monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_ENFORCEMENT_PROFILE", "forced")
    code, out, _ = _run(["get", "--raw", KEY_B], resolver=_boom)
    assert (code, out) == (0, "forced")


def test_get_non_raw_and_unknown():
    code, out, _ = _run(["get", KEY_A, "--json"])
    row = json.loads(out)
    assert code == 0 and row["key"] == KEY_A and row["source"] in cr.CHAIN_LAYERS
    assert _run(["get", "NOPE"])[0] == 2 and _run(["explain", "NOPE"])[0] == 2
    assert _run(["get", KEY_B], resolver=_boom)[0] == 3


def test_explain_all_five_layers_in_order_default_wins():
    code, out, _ = _run(["explain", KEY_A])
    layers = [ln.split(":")[0] for ln in out.split("\n")[1:6]]
    assert code == 0 and layers == list(cr.CHAIN_LAYERS) and out.endswith("winner: default")


def test_explain_override_wins(monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_ENFORCEMENT_PROFILE", "forced")
    monkeypatch.setenv(KEY_B, "fromenv")
    code, out, _ = _run(["explain", KEY_B], resolver=lambda p, k: "fromdb")
    assert code == 0 and out.endswith("winner: override")
    assert "project_config: fromdb" in out and "env: fromenv" in out


def test_explain_unreadable_store_shows_error_not_not_set():
    code, out, _ = _run(["explain", KEY_B], resolver=_boom)
    lines = out.split("\n")
    assert code == 3
    assert "project_config: ERROR" in out and "project_config: not set" not in out
    assert lines[-1].startswith("winner: unknown")


def test_explain_global_key_skips_project_layer():
    assert "project_config: not applicable" in _run(["explain", KEY_A], resolver=_boom)[1]


def test_list_json_has_scope_and_allowed_values():
    code, out, _ = _run(["list", "--json"])
    rows = {r["key"]: r for r in json.loads(out)}
    assert code == 0 and len(rows) == len(cr.CONFIG_REGISTRY)
    assert rows[KEY_A]["allowed_values"] == list(cr.CONFIG_REGISTRY[KEY_A].allowed_values)
    assert rows[KEY_A]["scope"] == cr.CONFIG_REGISTRY[KEY_A].scope
    assert "allowed=" in _run(["list"])[1] and _run(["list"], resolver=_boom)[0] == 3


def test_config_allowed_in_every_mode():
    for mode in VNXMode:
        check_command_allowed("config", mode)


def test_subprocess_raw_stdout_only_values(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "VNX_GLOBAL_CONFIG_FILE": str(tmp_path / "g.json"),
           "VNX_ANALYZER_LLM": "alpha"}
    env.pop("VNX_PROJECT_ID", None)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "config_cli.py"), "get", "--raw",
                           KEY_A, "--project-id", "tproj"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0 and proc.stdout == "alpha\n"


# --- set / unset (PR-10b) -----------------------------------------------------------------------
PROJ, GLOB, APPROVAL = "VNX_REVIEW_ROUND_CAP", "VNX_OLLAMA_HOST", "VNX_ENFORCEMENT_PROFILE"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A temp data dir: ``mode.json`` beside ``state/runtime_coordination.db``; a temp global file."""
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    sqlite3.connect(str(state / "runtime_coordination.db")).close()
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "home" / "config.json"))
    for k in (PROJ, GLOB, APPROVAL):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{k[4:]}", raising=False)
    _mode(state, "operator")
    return state


def _mode(state, mode):
    (state.parent / "mode.json").write_text(json.dumps({"mode": mode, "schema_version": 1}))


def _w(state, *argv):
    out, err = [], []
    code = config_cli.main(list(argv), project_id="p1", state_dir=state, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def _audit(state):
    """``(project audit rows, global audit lines)``: what a refused write must leave unchanged."""
    conn = sqlite3.connect(str(state / "runtime_coordination.db"))
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_config_audit'").fetchone()
        rows = conn.execute("SELECT operation, reason, changed_by FROM project_config_audit"
                            " ORDER BY id").fetchall() if has else []
    finally:
        conn.close()
    log = Path(os.environ["VNX_GLOBAL_CONFIG_FILE"]).parent / "config_audit.ndjson"
    return rows, (log.read_text().splitlines() if log.exists() else [])


def test_set_then_get_then_unset_brings_back_default(store):
    code, out, _ = _w(store, "set", PROJ, "5", "--reason", "fewer rounds")
    assert code == 0 and out.startswith(f"{PROJ}=5  [project_config]")
    assert _w(store, "get", "--raw", PROJ)[1] == "5"
    code, out, _ = _w(store, "unset", PROJ, "--reason", "back to default")
    assert code == 0 and out.startswith(f"{PROJ}=3  [default]")
    assert _audit(store)[0] == [("set", "fewer rounds", "operator"), ("unset", "back to default", "operator")]


def test_global_set_and_unset(store):
    code, out, _ = _w(store, "set", GLOB, "http://h:1", "--global", "--reason", "remote ollama")
    assert code == 0 and out.startswith(f"{GLOB}=http://h:1  [global_config]")
    code, out, _ = _w(store, "unset", GLOB, "--global", "--reason", "local again")
    assert code == 0 and "[global_config]" not in out
    lines = [json.loads(line) for line in _audit(store)[1]]
    assert [(r["operation"], r["reason"], r["actor"]) for r in lines] == [
        ("set", "remote ollama", "operator"), ("unset", "local again", "operator")]


@pytest.mark.parametrize("mode", ["starter", None])
def test_not_operator_refuses_and_writes_nothing(store, mode):
    if mode:
        _mode(store, mode)
    else:
        (store.parent / "mode.json").unlink()
    for argv in (["set", PROJ, "5"], ["unset", GLOB, "--global"]):
        code, out, err = _w(store, *argv, "--reason", "r")
        assert code == 4 and out == "" and "vnx init --operator" in err
    assert _audit(store) == ([], []) and not Path(os.environ["VNX_GLOBAL_CONFIG_FILE"]).exists()


@pytest.mark.parametrize("argv, code, needle", [
    (["set", PROJ, "5", "--reason", "  "], 4, "blank"),
    (["set", APPROVAL, "enforced", "--reason", "r"], 4, "approval_id"),
    (["set", APPROVAL, "bogus", "--reason", "r", "--approval-id", "a1"], 4, "not one of"),
    (["set", GLOB, "x", "--reason", "r"], 4, "global key"),
    (["set", PROJ, "5", "--global", "--reason", "r"], 4, "drop --global"),
    (["set", "VNX_ANALYZER_LLM", "bogus", "--global", "--reason", "r"], 4, "not one of"),
    (["unset", "VNX_NO_SUCH_KEY", "--reason", "r"], 2, "unknown key"),
])
def test_refusals_leave_audit_unchanged(store, argv, code, needle):
    got, out, err = _w(store, *argv)
    assert got == code and out == "" and needle in err
    assert _audit(store) == ([], [])


def test_unreadable_global_file_refuses(store):
    gfile = Path(os.environ["VNX_GLOBAL_CONFIG_FILE"])
    gfile.parent.mkdir(parents=True)
    gfile.write_text("{not json")
    code, out, err = _w(store, "set", GLOB, "h", "--global", "--reason", "r")
    assert code == 3 and out == "" and "unreadable" in err and gfile.read_text() == "{not json"


def test_missing_project_db_refuses(store):
    (store / "runtime_coordination.db").unlink()
    code, out, err = _w(store, "set", PROJ, "5", "--reason", "r")
    assert code == 3 and "does not exist" in err and not (store / "runtime_coordination.db").exists()
