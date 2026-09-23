"""A generated MCP config never carries a credential and never freezes a server table.

Defect (measured 2026-09-23): 21 terminal ``.mcp.json`` files over five projects held
Perplexity keys, 6 literally and 15 as ``${PERPLEXITY_API_KEY}`` from a forgotten
export. Both generators (``bin/vnx bootstrap-terminals`` and
``scripts/vnx_init.py:_generate_mcp_configs``) copied every ``mcpServers`` entry out of
``~/.claude.json`` with ``dict(cfg)``, ``env`` included, and only added ``disabled: true``.
Claude Code does not read that flag, so the copy started with its frozen credentials and,
because a project ``.mcp.json`` outranks ``~/.claude.json``, a key rotation never reached it.

The worker path has the same shape: ``resolve_role_mcp_config`` handed a role-allowlisted
server, ``env`` and all, to ``claude --mcp-config <json>``. That JSON is an argv element, so
the value shows up in the process list.

Every credential below is a made-up literal. The real ``~/.claude.json`` is never read: the
generators are pointed at a tmp HOME, or at ``VNX_GLOBAL_MCP_CONFIG_PATH``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_VNX = REPO_ROOT / "bin" / "vnx"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from vnx_init import _generate_mcp_configs
from worker_permissions import (
    PermissionProfile,
    build_claude_scope_args,
    resolve_role_mcp_config,
)

LITERAL_ENV = "FAKE-literal-env-value-0001"
LITERAL_HEADER = "FAKE-literal-header-value-0002"
LITERAL_ARG = "FAKE-literal-arg-value-0003"
LITERAL_URL = "FAKE-literal-url-value-0004"
LITERAL_ENV_2 = "FAKE-literal-env-value-0005"
ALL_LITERALS = (LITERAL_ENV, LITERAL_HEADER, LITERAL_ARG, LITERAL_URL, LITERAL_ENV_2)

GENERATORS = ["vnx_init", "bin_vnx"]


def _global_table() -> dict:
    return {
        "perplexity": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "perplexity-mcp", "--api-key", LITERAL_ARG],
            "env": {
                "PERPLEXITY_API_KEY": LITERAL_ENV,
                "PERPLEXITY_ALIAS": "${PERPLEXITY_API_KEY}",
            },
        },
        "brave-search": {
            "command": "npx",
            "args": ["-y", "brave-mcp"],
            "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}", "BRAVE_OLD": LITERAL_ENV_2},
        },
        "remote": {
            "type": "http",
            "url": f"https://mcp.example.test/v1?key={LITERAL_URL}",
            "headers": {
                "Authorization": f"Bearer {LITERAL_HEADER}",
                "X-Token": "Bearer ${REMOTE_TOKEN}",
            },
        },
    }


def _write_global(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers, "numStartups": 3}))


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A tmp HOME with a fake ``~/.claude.json`` and an empty tmp project."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.delenv("VNX_GLOBAL_MCP_CONFIG_PATH", raising=False)
    _write_global(home / ".claude.json", _global_table())
    return {"home": home, "project": project, "global": home / ".claude.json"}


def _generate(
    kind: str,
    sandbox: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    force: bool = False,
    terminals: tuple[str, ...] = ("T0", "T1"),
    override: Path | None = None,
) -> Path:
    """Run one generator against the sandbox. Returns the terminals dir."""
    terminals_dir = sandbox["project"] / ".claude" / "terminals"
    if kind == "vnx_init":
        monkeypatch.setenv("HOME", str(sandbox["home"]))
        if override is not None:
            monkeypatch.setenv("VNX_GLOBAL_MCP_CONFIG_PATH", str(override))
        _generate_mcp_configs(terminals_dir, list(terminals), force)
        return terminals_dir

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(sandbox["home"]),
        "VNX_HOME": str(REPO_ROOT),
        "PROJECT_ROOT": str(sandbox["project"]),
        "VNX_PROJECT_ROOT": str(sandbox["project"]),
    }
    if override is not None:
        env["VNX_GLOBAL_MCP_CONFIG_PATH"] = str(override)
    cmd = [str(BIN_VNX), "bootstrap-terminals", "--terminals", " ".join(terminals)]
    if force:
        cmd.append("--force")
    subprocess.run(
        cmd, cwd=sandbox["project"], env=env, check=True, capture_output=True, text=True
    )
    return terminals_dir


def _servers(terminals_dir: Path, tid: str = "T0") -> dict:
    return json.loads((terminals_dir / tid / ".mcp.json").read_text())["mcpServers"]


# ---------------------------------------------------------------------------
# (a) a generated terminal .mcp.json carries neither a literal key nor a reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", GENERATORS)
def test_terminal_mcp_json_carries_no_key_and_no_reference(kind, sandbox, monkeypatch):
    terminals_dir = _generate(kind, sandbox, monkeypatch)

    for tid in ("T0", "T1"):
        text = (terminals_dir / tid / ".mcp.json").read_text()
        for literal in ALL_LITERALS:
            assert literal not in text, f"{kind}/{tid}: literal {literal!r} was written"
        assert "${" not in text, f"{kind}/{tid}: an env reference was written"
        assert "API_KEY" not in text and "REMOTE_TOKEN" not in text

        servers = _servers(terminals_dir, tid)
        assert set(servers) == {"perplexity", "brave-search", "remote"}
        for name, entry in servers.items():
            assert entry.get("command"), (
                f"{kind}/{tid}/{name}: Claude Code skips an entry without a command"
            )
            leaked = set(entry) - {"type", "command"}
            assert not leaked, f"{kind}/{tid}/{name}: copied fields {sorted(leaked)}"


@pytest.mark.parametrize("kind", GENERATORS)
def test_no_global_config_writes_an_empty_server_table(kind, sandbox, monkeypatch):
    sandbox["global"].unlink()

    terminals_dir = _generate(kind, sandbox, monkeypatch)

    assert _servers(terminals_dir) == {}


@pytest.mark.parametrize("kind", GENERATORS)
def test_global_config_path_override_is_honoured(kind, sandbox, monkeypatch, tmp_path):
    sandbox["global"].unlink()
    override = tmp_path / "elsewhere.json"
    _write_global(override, {"only-here": {"command": "npx", "env": {"K": LITERAL_ENV}}})

    terminals_dir = _generate(kind, sandbox, monkeypatch, override=override)

    servers = _servers(terminals_dir)
    assert set(servers) == {"only-here"}
    assert LITERAL_ENV not in (terminals_dir / "T0" / ".mcp.json").read_text()


# ---------------------------------------------------------------------------
# (b) the file follows the global table: a removed server disappears on regenerate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", GENERATORS)
def test_removed_global_server_disappears_when_regenerated(kind, sandbox, monkeypatch):
    terminals_dir = _generate(kind, sandbox, monkeypatch)
    assert "brave-search" in _servers(terminals_dir)

    table = _global_table()
    del table["brave-search"]
    table["added-later"] = {"command": "npx"}
    _write_global(sandbox["global"], table)
    _generate(kind, sandbox, monkeypatch)

    for tid in ("T0", "T1"):
        assert set(_servers(terminals_dir, tid)) == {"perplexity", "remote", "added-later"}


@pytest.mark.parametrize("kind", GENERATORS)
def test_regeneration_scrubs_a_legacy_frozen_entry_and_keeps_the_operators_own(
    kind, sandbox, monkeypatch
):
    """The pre-fix generator wrote every entry with ``disabled: true`` and its full env."""
    target = sandbox["project"] / ".claude" / "terminals" / "T0" / ".mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {
        "perplexity": {
            "type": "stdio", "command": "npx",
            "env": {"PERPLEXITY_API_KEY": LITERAL_ENV}, "disabled": True,
        },
        "no-longer-global": {"command": "npx", "env": {"K": LITERAL_ENV_2}, "disabled": True},
        "project-tool": {"command": "my-project-tool", "args": ["--serve"]},
    }}))

    terminals_dir = _generate(kind, sandbox, monkeypatch, terminals=("T0",))

    text = (terminals_dir / "T0" / ".mcp.json").read_text()
    assert LITERAL_ENV not in text and LITERAL_ENV_2 not in text
    servers = _servers(terminals_dir)
    assert "no-longer-global" not in servers
    assert servers["project-tool"] == {"command": "my-project-tool", "args": ["--serve"]}
    assert set(servers["perplexity"]) <= {"type", "command"}


@pytest.mark.parametrize("kind", GENERATORS)
def test_force_starts_from_a_clean_file(kind, sandbox, monkeypatch):
    target = sandbox["project"] / ".claude" / "terminals" / "T0" / ".mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"project-tool": {"command": "x"}}}))

    terminals_dir = _generate(kind, sandbox, monkeypatch, force=True, terminals=("T0",))

    assert "project-tool" not in _servers(terminals_dir)
    assert set(_servers(terminals_dir)) == {"perplexity", "brave-search", "remote"}


def test_dead_profile_manager_script_is_gone():
    """``scripts/mcp_profile_manager.sh`` called ``scripts/lib/mcp_profiles.py``, removed in #205."""
    assert not (REPO_ROOT / "scripts" / "mcp_profile_manager.sh").exists()


# ---------------------------------------------------------------------------
# (c) the worker --mcp-config leaks no literal key, in the config or in argv
# ---------------------------------------------------------------------------


def _worker_global(tmp_path: Path) -> Path:
    path = tmp_path / "worker.claude.json"
    _write_global(path, {
        "notion": {
            "command": "npx",
            "args": ["-y", "notion-mcp"],
            "env": {
                "NOTION_KEY": LITERAL_ENV,
                "NOTION_ALIAS": "${NOTION_TOKEN_ELSEWHERE}",
                "NOTION_MIXED": "Bearer ${NOTION_TOKEN_ELSEWHERE}",
                "NOTION_DEFAULTED": "${NOTION_TOKEN_ELSEWHERE:-" + LITERAL_ENV_2 + "}",
            },
        },
        "remote": {
            "type": "http",
            "url": "https://mcp.example.test/v1",
            "headers": {
                "Authorization": f"Bearer {LITERAL_HEADER}",
                "X-Token": "Bearer ${REMOTE_TOKEN}",
            },
        },
    })
    return path


def _mcp_config_arg(argv: list[str]) -> str:
    return argv[argv.index("--mcp-config") + 1]


def test_worker_config_keeps_references_and_drops_literals(tmp_path):
    profile = PermissionProfile(role="r", mcp_servers=["notion", "remote"])

    config = resolve_role_mcp_config(profile, _worker_global(tmp_path))

    text = json.dumps(config)
    for literal in (LITERAL_ENV, LITERAL_ENV_2, LITERAL_HEADER):
        assert literal not in text
    notion = config["mcpServers"]["notion"]
    assert notion["command"] == "npx" and notion["args"] == ["-y", "notion-mcp"]
    assert notion["env"] == {
        "NOTION_ALIAS": "${NOTION_TOKEN_ELSEWHERE}",
        "NOTION_MIXED": "Bearer ${NOTION_TOKEN_ELSEWHERE}",
    }
    remote = config["mcpServers"]["remote"]
    assert remote["url"] == "https://mcp.example.test/v1"
    assert remote["headers"] == {"X-Token": "Bearer ${REMOTE_TOKEN}"}


def test_worker_config_without_credentials_drops_the_empty_maps(tmp_path):
    path = tmp_path / "plain.claude.json"
    _write_global(path, {"plain": {"command": "npx", "env": {"MODE": "fast"}, "headers": {"A": "b"}}})

    config = resolve_role_mcp_config(PermissionProfile(role="r", mcp_servers=["plain"]), path)

    assert config == {"mcpServers": {"plain": {"command": "npx"}}}


def test_worker_config_argv_carries_no_literal_value(tmp_path):
    profile = PermissionProfile(role="r", allowed_tools=["Read"], mcp_servers=["notion"])

    argv = build_claude_scope_args(profile, global_mcp_config_path=_worker_global(tmp_path))

    joined = "\n".join(argv)
    for literal in (LITERAL_ENV, LITERAL_ENV_2):
        assert literal not in joined
    assert "--strict-mcp-config" in argv


def test_worker_config_literal_is_absent_from_the_live_process_list(tmp_path):
    """The measurement: a running process is launched with this argv and `ps` is read back."""
    profile = PermissionProfile(role="r", allowed_tools=["Read"], mcp_servers=["notion"])
    argv = build_claude_scope_args(profile, global_mcp_config_path=_worker_global(tmp_path))
    # Stand-in for `claude`: subprocess_adapter hands its argv list to subprocess.Popen(cmd).
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *argv])
    try:
        time.sleep(0.5)
        seen = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(proc.pid)],
            capture_output=True, text=True, check=True,
        ).stdout
    finally:
        proc.kill()
        proc.wait()

    assert "--mcp-config" in seen, "the stand-in did not receive the scoped argv"
    for literal in (LITERAL_ENV, LITERAL_ENV_2):
        assert literal not in seen


def test_worker_config_warning_names_server_and_key_but_never_the_value(tmp_path, caplog):
    profile = PermissionProfile(role="r", mcp_servers=["notion", "remote"])

    with caplog.at_level(logging.WARNING):
        resolve_role_mcp_config(profile, _worker_global(tmp_path))

    for name in ("notion", "NOTION_KEY", "remote", "Authorization"):
        assert name in caplog.text
    for literal in ALL_LITERALS:
        assert literal not in caplog.text
