"""OI-2587: the kimi worker lane refuses `git stash` (OI-1447).

kimi-cli 1.46.0 has no --disallowedTools and --yolo auto-approves every tool
call, so the deny is a PreToolUse hook carried in the per-run ``--config``.
Nothing here starts kimi itself: (a) the argv, (b)+(c) the hook script run as a
real subprocess with a kimi hook payload on stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_LIB = str(_REPO / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from provider_spawns.kimi_spawn import _build_kimi_cmd  # noqa: E402

_DENY_SCRIPT = _REPO / "scripts" / "kimi_deny_git_stash.py"


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
    def test_config_flag_carries_user_config_and_hook(self):
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
                cmd = _build_kimi_cmd("prompt", None, Path("/tmp/worktree"))
                self.assertEqual(sorted(p.name for p in share.iterdir()), ["config.toml"])
            finally:
                if old is None:
                    os.environ.pop("KIMI_SHARE_DIR", None)
                else:
                    os.environ["KIMI_SHARE_DIR"] = old
        self.assertIn("--config", cmd)
        config = json.loads(cmd[cmd.index("--config") + 1])
        self.assertEqual(config["default_model"], "test/model")
        self.assertIn("test/model", config["models"])
        hook = config["hooks"][-1]
        self.assertEqual(hook["event"], "PreToolUse")
        self.assertEqual(hook["matcher"], "^Shell$")
        self.assertIn(str(_DENY_SCRIPT), hook["command"])
        self.assertIn("--yolo", cmd)

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
                cmd = _build_kimi_cmd("prompt", None, None)
            finally:
                if old is None:
                    os.environ.pop("KIMI_SHARE_DIR", None)
                else:
                    os.environ["KIMI_SHARE_DIR"] = old
        hooks = json.loads(cmd[cmd.index("--config") + 1])["hooks"]
        self.assertEqual([h["event"] for h in hooks], ["Stop", "PreToolUse"])


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


if __name__ == "__main__":
    unittest.main()
