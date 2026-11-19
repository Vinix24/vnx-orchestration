"""``vnx config list/get/explain/get --raw`` (config-een-oppervlak PR-10a): read-only chain view."""

from __future__ import annotations

import json
import os
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


def test_raw_unset_key_is_empty_line():
    code, out, _ = _run(["get", "--raw", "VNX_SMTP_HOST", KEY_A])
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
