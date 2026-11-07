"""OI-2587: the kimi worker lane refuses `git stash` (OI-1447), and the guard
config never rides on argv (OI-2587 round 2, T0 review of #2446).

kimi-cli 1.46.0 has no --disallowedTools and --yolo auto-approves every tool
call, so the deny is a PreToolUse hook carried in a private per-run config file
passed with ``--config-file`` (a ``--config`` JSON string would expose the
user's API keys to every local process through ``ps``). Nothing here starts
kimi itself: a fake ``subprocess.Popen`` runs a real short-lived
``sys.executable -c`` process so the process lifetime is exercised for real.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[1]
_LIB = str(_REPO / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from provider_spawns import kimi_spawn  # noqa: E402
from provider_spawns.kimi_spawn import _build_kimi_cmd, spawn_kimi  # noqa: E402

_DENY_SCRIPT = _REPO / "scripts" / "kimi_deny_git_stash.py"
_REAL_POPEN = subprocess.Popen
_PREFIX = "vnx-kimi-config-"


def _payload(command: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s-1",
            "cwd": "/tmp",
            "tool_name": "Shell",
            "tool_input": {"command": command},
            "tool_call_id": "c-1",
        }
    )


class TestBuildKimiCmdCarriesTheGuard(unittest.TestCase):
    def test_config_file_flag_names_the_path_and_no_config_string(self):
        cmd = _build_kimi_cmd("prompt", None, Path("/tmp/worktree"), Path("/tmp/cfg.json"))
        self.assertIn("--config-file", cmd)
        self.assertEqual(cmd[cmd.index("--config-file") + 1], "/tmp/cfg.json")
        self.assertNotIn("--config", cmd)
        self.assertIn("--yolo", cmd)

    def test_private_config_file_carries_user_config_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp) / "kimi"
            share.mkdir()
            (share / "config.toml").write_text(
                'default_model = "test/model"\n\n[models."test/model"]\n'
                'provider = "p"\nmodel = "m"\nmax_context_size = 1000\n',
                encoding="utf-8",
            )
            old = os.environ.get("KIMI_SHARE_DIR")
            os.environ["KIMI_SHARE_DIR"] = str(share)
            try:
                path = kimi_spawn._write_kimi_config_file()
            finally:
                if old is None:
                    os.environ.pop("KIMI_SHARE_DIR", None)
                else:
                    os.environ["KIMI_SHARE_DIR"] = old
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(sorted(p.name for p in share.iterdir()), ["config.toml"])
            finally:
                kimi_spawn._remove_kimi_config_file(path)
        self.assertEqual(config["default_model"], "test/model")
        self.assertIn("test/model", config["models"])
        hook = config["hooks"][-1]
        self.assertEqual(hook["event"], "PreToolUse")
        self.assertEqual(hook["matcher"], "^Shell$")
        self.assertIn(str(_DENY_SCRIPT), hook["command"])

    def test_hooks_already_in_the_user_config_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            share = Path(tmp) / "kimi"
            share.mkdir()
            (share / "config.toml").write_text(
                '[[hooks]]\nevent = "Stop"\ncommand = "true"\n', encoding="utf-8"
            )
            old = os.environ.get("KIMI_SHARE_DIR")
            os.environ["KIMI_SHARE_DIR"] = str(share)
            try:
                path = kimi_spawn._write_kimi_config_file()
            finally:
                if old is None:
                    os.environ.pop("KIMI_SHARE_DIR", None)
                else:
                    os.environ["KIMI_SHARE_DIR"] = old
            try:
                hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            finally:
                kimi_spawn._remove_kimi_config_file(path)
        self.assertEqual([h["event"] for h in hooks], ["Stop", "PreToolUse"])


def _fake_popen_recording(record: dict):
    """Stand-in for subprocess.Popen: records argv + config file, then runs a
    real short-lived ``sys.executable -c pass`` process (never kimi)."""

    def fake(cmd, **kwargs):
        if not cmd or cmd[0] != "kimi":
            # Non-kimi spawns in spawn_kimi's path (e.g. the git maintenance
            # guard) run for real; only the kimi argv carries the config file.
            return _REAL_POPEN(cmd, **kwargs)
        record["argv"] = list(cmd)
        path = Path(cmd[cmd.index("--config-file") + 1])
        record["path"] = path
        record["file_mode"] = stat.S_IMODE(path.stat().st_mode)
        record["dir_mode"] = stat.S_IMODE(path.parent.stat().st_mode)
        record["content"] = path.read_text(encoding="utf-8")
        return _REAL_POPEN(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    return fake


def test_argv_carries_config_file_path_and_no_secret_values():
    record: dict = {}
    with patch("subprocess.Popen", side_effect=_fake_popen_recording(record)):
        spawn_kimi("do the work", dispatch_id="d1", terminal_id="T1")
    argv = record["argv"]
    assert "--config-file" in argv
    assert argv[argv.index("--config-file") + 1] == str(record["path"])
    assert "--config" not in argv
    assert not any("providers" in token or "api_key" in token for token in argv)


def test_config_file_is_private_and_holds_user_config_plus_hook(tmp_path, monkeypatch):
    share = tmp_path / "kimi"
    share.mkdir()
    (share / "config.toml").write_text('default_model = "test/model"\n', encoding="utf-8")
    monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
    record: dict = {}
    with patch("subprocess.Popen", side_effect=_fake_popen_recording(record)):
        spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
    assert record["file_mode"] == 0o600
    assert record["dir_mode"] == 0o700
    config = json.loads(record["content"])
    assert config["default_model"] == "test/model"
    hook = config["hooks"][-1]
    assert hook["event"] == "PreToolUse"
    assert str(_DENY_SCRIPT) in hook["command"]


def test_config_file_and_dir_are_gone_after_a_normal_return():
    record: dict = {}
    with patch("subprocess.Popen", side_effect=_fake_popen_recording(record)):
        spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
    assert not record["path"].exists()
    assert not record["path"].parent.exists()


def test_config_file_and_dir_are_gone_when_the_wait_raises():
    record: dict = {}

    def boom(*args, **kwargs):
        raise RuntimeError("drainer exploded")

    with patch("subprocess.Popen", side_effect=_fake_popen_recording(record)), \
            patch("provider_spawns.kimi_spawn._consume_kimi_stream", side_effect=boom):
        with pytest.raises(RuntimeError, match="drainer exploded"):
            spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
    assert not record["path"].exists()
    assert not record["path"].parent.exists()


def test_config_file_and_dir_are_gone_when_popen_raises():
    record: dict = {}

    def fake(cmd, **kwargs):
        record["path"] = Path(cmd[cmd.index("--config-file") + 1])
        raise OSError("cannot fork")

    with patch("subprocess.Popen", side_effect=fake):
        result = spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
    assert result.returncode == 126
    assert not record["path"].exists()
    assert not record["path"].parent.exists()


def test_dead_supervisor_config_dir_is_swept_and_live_one_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = tmp_path / f"{_PREFIX}{dead.pid}-deadbeef"
    dead_dir.mkdir(mode=0o700)
    (dead_dir / "config.json").write_text("{}", encoding="utf-8")
    live_dir = tmp_path / f"{_PREFIX}{os.getpid()}-livecafe"
    live_dir.mkdir(mode=0o700)
    record: dict = {}
    with patch("subprocess.Popen", side_effect=_fake_popen_recording(record)):
        spawn_kimi("prompt", dispatch_id="d1", terminal_id="T1")
    assert not dead_dir.exists()
    assert live_dir.exists()


class TestDenyHookScript(unittest.TestCase):
    def _run(self, stdin_text: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_DENY_SCRIPT)],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _assert_denied(self, command: str) -> None:
        result = self._run(_payload(command))
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("OI-1447", decision["permissionDecisionReason"])

    def test_denies_git_stash_forms(self):
        for command in (
            "git stash list",
            "git stash pop",
            "git -C /tmp/x stash drop",
            "cd /tmp && git --git-dir=/tmp/x/.git stash clear",
            "GIT_DIR=/tmp/x/.git git stash",
        ):
            with self.subTest(command=command):
                self._assert_denied(command)

    def test_allows_commands_that_do_not_run_git_stash(self):
        for command in (
            "git status",
            "ls",
            "git log --grep stash",
            "git -C /tmp/x status",
            "echo 'git stash list'",
        ):
            with self.subTest(command=command):
                result = self._run(_payload(command))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_malformed_payload_is_reported_not_a_deny(self):
        for stdin_text in (
            "",
            "{not json",
            json.dumps({"hook_event_name": "PreToolUse"}),
            json.dumps({"tool_input": {"command": 42}}),
            json.dumps([1, 2, 3]),
        ):
            with self.subTest(stdin_text=stdin_text):
                result = self._run(stdin_text)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout.strip(), "")
                self.assertIn("malformed", result.stderr)


_HIDDEN_STASH = [
    "git stash; true",
    "true;git stash pop",
    "true; git stash pop",
    "bash -c 'git stash clear'",
    'bash -lc "cd x && git stash"',
    "sh -c 'git -C /tmp stash list'",
    "git stash&&true",
    "(git stash)",
    "{ git stash; }",
    "echo a\ngit stash",
    "$(git stash)",
    "`git stash`",
    "eval git stash",
    'eval "git stash pop"',
    "env FOO=1 git stash",
    "/usr/bin/git stash",
    "git stash 'x",
]

_STASH_FREE = [
    "git status",
    "git log --grep stash",
    'git commit -m "do not use git stash"',
    "echo stash",
    "git checkout stash-fix",
]


def _hook(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_DENY_SCRIPT)],
        input=_payload(command),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("command", _HIDDEN_STASH)
def test_the_hook_sees_a_stash_behind_a_shell_boundary_or_a_nested_shell(command):
    """A `;`, a `&&`, a subshell, a nested `sh -c`/`eval` or a substitution around
    `git stash` is still the stash subcommand: the guard is not bypassable."""
    result = _hook(command)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "OI-1447" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("command", _STASH_FREE)
def test_the_hook_allows_a_compound_command_without_the_stash_subcommand(command):
    result = _hook(command)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


if __name__ == "__main__":
    unittest.main()
