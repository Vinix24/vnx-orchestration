"""tests/test_worker_scope_hook_registration.py — agents-als-map-fase-1 PR-3.

The worker scope hook (``scripts/hooks/pretooluse_worker_scope_enforce.sh``) is
registered in this repo's ``.claude/settings.json`` for the four tools it
enforces. Registration must be inert while ``VNX_ENFORCE_WORKER_PERMISSIONS`` is
off: ``evaluate()`` returns allow without reading a file, also in a session
with no dispatch environment (a T0 session).
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETTINGS_PATH = REPO / ".claude" / "settings.json"
WRAPPER = "pretooluse_worker_scope_enforce.sh"
ENFORCED_TOOLS = ("Write", "Edit", "MultiEdit", "Bash")

# Everything the hook reads from the environment to resolve a dispatch.
DISPATCH_ENV = (
    "VNX_ENFORCE_WORKER_PERMISSIONS",
    "VNX_WORKER_ENFORCEMENT_SKIP",
    "VNX_WORKER_ROLE",
    "VNX_DISPATCH_PATHS",
    "VNX_CURRENT_DISPATCH_ID",
    "VNX_DISPATCH_ID",
    "VNX_DATA_DIR",
)


def _entries_naming_wrapper() -> list[tuple[str, dict, dict]]:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    found = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                if WRAPPER in hook.get("command", ""):
                    found.append((event, group, hook))
    return found


def _load_hook_module():
    path = REPO / "scripts" / "hooks" / "pretooluse_worker_scope_enforce.py"
    spec = importlib.util.spec_from_file_location("worker_scope_hook_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exactly_one_entry_names_the_wrapper_under_pretooluse():
    entries = _entries_naming_wrapper()
    assert len(entries) == 1, f"expected one entry naming {WRAPPER}, got {len(entries)}"
    assert entries[0][0] == "PreToolUse"


def test_the_matcher_covers_every_enforced_tool():
    (_, group, _hook), = _entries_naming_wrapper()
    alternatives = group.get("matcher", "").split("|")
    for tool in ENFORCED_TOOLS:
        assert tool in alternatives, f"matcher {group.get('matcher')!r} misses {tool}"


def test_the_timeout_is_five_seconds():
    (_, _group, hook), = _entries_naming_wrapper()
    assert hook.get("timeout") == 5
    assert isinstance(hook["timeout"], int) and not isinstance(hook["timeout"], bool)


def test_the_command_uses_the_portable_repo_root_form():
    (_, _group, hook), = _entries_naming_wrapper()
    assert hook.get("type") == "command"
    command = hook["command"]
    assert "git rev-parse --show-toplevel" in command
    assert f"scripts/hooks/{WRAPPER}" in command
    assert "/Users/" not in command and "/home/" not in command


def test_the_registered_wrapper_exists_and_is_executable():
    wrapper = REPO / "scripts" / "hooks" / WRAPPER
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)


@pytest.mark.parametrize("flag", [None, "0", "false", "no", "off", "", "maybe"])
@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}, "cwd": "/"},
        {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}},
        {"tool_name": "MultiEdit", "tool_input": {"file_path": "x.py"}},
        {},
    ],
)
def test_flag_off_allows_without_touching_the_filesystem(monkeypatch, flag, payload):
    hook = _load_hook_module()
    for name in DISPATCH_ENV:
        monkeypatch.delenv(name, raising=False)
    if flag is not None:
        monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", flag)

    def _refuse(*args, **kwargs):
        raise AssertionError("evaluate() touched the filesystem with the flag off")

    monkeypatch.setattr(builtins, "open", _refuse)
    monkeypatch.setattr(Path, "read_text", _refuse)
    monkeypatch.setattr(Path, "open", _refuse)
    monkeypatch.setattr(Path, "exists", _refuse)
    monkeypatch.setattr(Path, "resolve", _refuse)
    monkeypatch.setattr(os, "stat", _refuse)

    assert hook.evaluate(payload) == ("allow", None)


def test_explicit_skip_beats_an_enabled_flag(monkeypatch):
    hook = _load_hook_module()
    for name in DISPATCH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    monkeypatch.setenv("VNX_WORKER_ENFORCEMENT_SKIP", "1")
    payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    assert hook.evaluate(payload) == ("allow", None)


def _expected_matcher_tools() -> set[str]:
    module = _load_hook_module()
    return set(module._WRITE_LIKE_TOOLS) | {"Bash"}


def test_the_settings_matcher_equals_the_hooks_write_like_tools_plus_bash():
    (_, group, _hook), = _entries_naming_wrapper()
    assert set(group.get("matcher", "").split("|")) == _expected_matcher_tools()


def test_the_probe_settings_matcher_equals_the_hooks_write_like_tools_plus_bash():
    import re

    text = (REPO / "scripts" / "probe_agent_layout.sh").read_text(encoding="utf-8")
    matchers = re.findall(r'"matcher":"([^"]*)"[^\n]*' + re.escape(WRAPPER), text)
    assert len(matchers) == 1, f"expected one probe registration, got {matchers}"
    assert set(matchers[0].split("|")) == _expected_matcher_tools()
