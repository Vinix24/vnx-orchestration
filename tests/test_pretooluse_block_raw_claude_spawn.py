"""Tests for pretooluse_block_raw_claude_spawn.sh and pretooluse_spawn_detector.py.

Covers:
  - BLOCK: claude -p, claude --print, claude --dangerously-skip-permissions
  - BLOCK: background/nohup/bash-c/pipe variants of the above
  - BLOCK: combined flags in any order
  - ALLOW: python3 scripts/lib/subprocess_dispatch.py (governed wrapper)
  - ALLOW: python3 scripts/lib/provider_dispatch.py   (governed wrapper)
  - ALLOW: claude --version, claude --help (benign)
  - ALLOW: interactive claude (no blocked flags)
  - ALLOW: non-Bash tool calls (Write, Read, etc.)
  - ALLOW: grep -p (grep's -p should not trigger claude guard)
  - JSON output contract: decision + reason on block, empty on allow
  - Exit code always 0
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "pretooluse_block_raw_claude_spawn.sh"
DETECTOR_MODULE = REPO_ROOT / "scripts" / "hooks" / "pretooluse_spawn_detector.py"

sys.path.insert(0, str(DETECTOR_MODULE.parent))
from pretooluse_spawn_detector import classify  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_payload(tool_name: str, command: str | None = None) -> str:
    """Build a minimal Claude Code PreToolUse JSON payload."""
    payload: dict = {
        "tool_name": tool_name,
        "session_id": "test-session-govhook",
        "cwd": "/tmp/test-project",
        "transcript_path": "/tmp/test.jsonl",
    }
    if command is not None:
        payload["tool_input"] = {"command": command}
    return json.dumps(payload)


def run_hook(tool_name: str, command: str | None = None) -> tuple[int, str]:
    """Invoke the bash hook script with a mock payload.

    Returns (returncode, stdout).  stdout is empty for allow; JSON for block.
    """
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=make_payload(tool_name, command),
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode, result.stdout


def assert_blocked(tool_name: str, command: str) -> dict:
    """Run hook; assert block decision. Returns parsed JSON."""
    rc, out = run_hook(tool_name, command)
    assert rc == 0, f"Hook must exit 0 even on block; got {rc}"
    assert out.strip(), f"Blocked command produced no output: {command!r}"
    data = json.loads(out)
    assert data.get("decision") == "block", (
        f"Expected block for {command!r}, got {data.get('decision')!r}"
    )
    return data


def assert_allowed(tool_name: str, command: str | None = None) -> None:
    """Run hook; assert allow (empty stdout)."""
    rc, out = run_hook(tool_name, command)
    assert rc == 0, f"Hook must exit 0; got {rc}"
    assert out.strip() == "", (
        f"Expected empty output (allow) for {command!r}, got {out!r}"
    )


# ── Python-layer unit tests (fast, no subprocess) ─────────────────────────────

class TestClassifyDirect:
    """Test the classify() function from pretooluse_spawn_detector.py directly."""

    # Blocked cases
    def test_classify_blocks_claude_dash_p(self):
        assert classify('claude -p "do the thing"') == "block"

    def test_classify_blocks_claude_print(self):
        assert classify("claude --print 'hello'") == "block"

    def test_classify_blocks_dangerously_skip(self):
        assert classify("claude --dangerously-skip-permissions") == "block"

    def test_classify_blocks_combined_flags(self):
        assert classify('claude --dangerously-skip-permissions -p "hi"') == "block"

    def test_classify_blocks_reversed_flags(self):
        assert classify('claude -p "hi" --dangerously-skip-permissions') == "block"

    def test_classify_blocks_with_model_flag(self):
        assert classify('claude --model opus -p "task"') == "block"

    def test_classify_blocks_nohup(self):
        assert classify('nohup claude -p "task" &') == "block"

    def test_classify_blocks_pipe(self):
        assert classify('claude -p "task" | grep result') == "block"

    def test_classify_blocks_background(self):
        assert classify('claude -p "task" &') == "block"

    def test_classify_blocks_bash_c(self):
        assert classify('bash -c \'claude -p "task"\'') == "block"

    def test_classify_blocks_after_and_operator(self):
        assert classify('setup && claude -p "go"') == "block"

    def test_classify_blocks_after_semicolon(self):
        assert classify('setup; claude -p "go"') == "block"

    # Allowed cases
    def test_classify_allows_subprocess_dispatch(self):
        assert classify("python3 scripts/lib/subprocess_dispatch.py dispatch-123") == "allow"

    def test_classify_allows_provider_dispatch(self):
        assert classify("python3 scripts/lib/provider_dispatch.py dispatch-123") == "allow"

    def test_classify_allows_subprocess_dispatch_full_path(self):
        assert classify("/home/user/proj/scripts/lib/subprocess_dispatch.py run") == "allow"

    def test_classify_allows_claude_version(self):
        assert classify("claude --version") == "allow"

    def test_classify_allows_claude_help(self):
        assert classify("claude --help") == "allow"

    def test_classify_allows_interactive_claude(self):
        assert classify("claude") == "allow"

    def test_classify_allows_empty_command(self):
        assert classify("") == "allow"

    def test_classify_allows_git_command(self):
        assert classify("git status") == "allow"

    def test_classify_allows_grep_p(self):
        """grep -p must NOT trigger the claude guard."""
        assert classify("grep -p pattern file.txt") == "allow"

    def test_classify_allows_mkdir_p(self):
        """mkdir -p must NOT trigger."""
        assert classify("mkdir -p /some/path") == "allow"

    def test_classify_allows_python_script_unrelated(self):
        assert classify("python3 scripts/some_other_script.py") == "allow"

    def test_classify_allows_claude_in_path_string(self):
        """If 'claude' appears in a file path but not as a command token, allow."""
        assert classify("cat /home/user/.config/claude/settings.json") == "allow"


# ── Integration tests (full bash hook via subprocess) ─────────────────────────

class TestHookBlockedCases:
    """Full hook execution — blocked commands."""

    def test_blocks_claude_dash_p(self):
        assert_blocked("Bash", 'claude -p "do the thing"')

    def test_blocks_claude_print(self):
        assert_blocked("Bash", "claude --print 'task'")

    def test_blocks_claude_dangerously_skip_permissions(self):
        assert_blocked("Bash", "claude --dangerously-skip-permissions")

    def test_blocks_combined_flags(self):
        assert_blocked("Bash", 'claude --dangerously-skip-permissions -p "task"')

    def test_blocks_reversed_flag_order(self):
        assert_blocked("Bash", 'claude -p "task" --dangerously-skip-permissions')

    def test_blocks_background_spawn(self):
        assert_blocked("Bash", 'claude -p "task" &')

    def test_blocks_nohup_spawn(self):
        assert_blocked("Bash", 'nohup claude -p "task" &')

    def test_blocks_bash_c_spawn(self):
        assert_blocked("Bash", 'bash -c \'claude -p "task"\'')

    def test_blocks_with_model_flag(self):
        assert_blocked("Bash", 'claude --model claude-opus-4-7 -p "task"')

    def test_blocks_with_pipe(self):
        assert_blocked("Bash", 'claude -p "task" | grep done')

    def test_blocks_after_and_operator(self):
        assert_blocked("Bash", 'cd /tmp && claude -p "task"')


class TestHookAllowedCases:
    """Full hook execution — allowed commands."""

    def test_allows_subprocess_dispatch(self):
        assert_allowed("Bash", "python3 scripts/lib/subprocess_dispatch.py dispatch-123")

    def test_allows_provider_dispatch(self):
        assert_allowed("Bash", "python3 scripts/lib/provider_dispatch.py dispatch-123")

    def test_allows_claude_version(self):
        assert_allowed("Bash", "claude --version")

    def test_allows_claude_help(self):
        assert_allowed("Bash", "claude --help")

    def test_allows_interactive_claude(self):
        assert_allowed("Bash", "claude")

    def test_allows_git_command(self):
        assert_allowed("Bash", "git status")

    def test_allows_grep_p(self):
        """grep -p must NOT be caught as a claude -p pattern."""
        assert_allowed("Bash", "grep -p pattern file.txt")

    def test_allows_python_script(self):
        assert_allowed("Bash", "python3 scripts/build_t0_state.py")

    def test_allows_non_bash_write_tool(self):
        assert_allowed("Write")

    def test_allows_non_bash_read_tool(self):
        assert_allowed("Read")

    def test_allows_non_bash_grep_tool(self):
        assert_allowed("Grep")


class TestHookOutputContract:
    """Block output must be valid JSON with the required fields."""

    def test_block_is_valid_json(self):
        _, out = run_hook("Bash", 'claude -p "test"')
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_block_has_decision_block(self):
        data = assert_blocked("Bash", 'claude -p "test"')
        assert data["decision"] == "block"

    def test_block_has_reason_field(self):
        data = assert_blocked("Bash", 'claude -p "test"')
        assert "reason" in data
        assert len(data["reason"]) > 10

    def test_reason_mentions_subprocess_dispatch(self):
        data = assert_blocked("Bash", 'claude -p "test"')
        assert "subprocess_dispatch" in data["reason"]

    def test_reason_mentions_receipt(self):
        data = assert_blocked("Bash", 'claude --dangerously-skip-permissions')
        assert "receipt" in data["reason"].lower()

    def test_allow_produces_no_stdout(self):
        _, out = run_hook("Bash", "claude --version")
        assert out.strip() == ""

    def test_exit_code_always_zero_on_block(self):
        rc, _ = run_hook("Bash", 'claude -p "task"')
        assert rc == 0

    def test_exit_code_always_zero_on_allow(self):
        rc, _ = run_hook("Bash", "claude --version")
        assert rc == 0


class TestHookSyntaxAndExistence:
    """Static checks on the hook files."""

    def test_hook_script_exists(self):
        assert HOOK_SCRIPT.exists(), f"Hook script missing: {HOOK_SCRIPT}"

    def test_detector_module_exists(self):
        assert DETECTOR_MODULE.exists(), f"Detector module missing: {DETECTOR_MODULE}"

    def test_hook_script_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(HOOK_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed:\n{result.stderr}"
        )

    def test_detector_python_syntax(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(DETECTOR_MODULE)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Python syntax check failed:\n{result.stderr}"
        )
