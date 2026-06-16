"""Tests for pretooluse_spawn_detector.py PR-9 / PR-9b changes.

Covers:
  - Evasion #1 closed: ALLOWLIST_PATTERN removed; provider_dispatch.py + claude -p → block
  - Clean governed wrapper invocations still allow in shadow mode
  - Shadow evasion: lane-script direct / python -m / python -c → allow+log in default mode
  - Enforce mode (VNX_HOOK_ENFORCE=1): shadow evasions → block
  - Hard-block rules unchanged in both modes
  - Benign commands unchanged
  - Telemetry: shadow/block → one ndjson line; failure → fail-open
  - Malformed stdin JSON → allow
  - P0-A: fail-open invariant for all malformed payloads
  - P0-B: raw-CLI bypass closures (path-form, de-quoting, redirect/here-string)
  - P1-A: enforce-mode shadow over-match (mentions vs invocations)
  - P1-A: -m/-c no-space forms; importlib.import_module detection
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "hooks"))

import pretooluse_spawn_detector as det  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify(cmd: str, enforce: bool = False) -> str:
    env = {"VNX_HOOK_ENFORCE": "1" if enforce else "0"}
    with mock.patch.dict(os.environ, env):
        return det.classify(cmd)


def _run_main(cmd: str, tmp_path: Path, enforce: bool = False) -> tuple[str, list[dict]]:
    """Run main() with mock stdin; return (decision, ndjson_entries)."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    data_dir = tmp_path / "_vnx_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "VNX_DATA_DIR": str(data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_HOOK_ENFORCE": "1" if enforce else "0",
    }
    captured = io.StringIO()
    with mock.patch.dict(os.environ, env):
        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch("sys.stdout", captured):
                det.main()
    decision = captured.getvalue().strip()
    ndjson = data_dir / "events" / "hook_blocks.ndjson"
    entries: list[dict] = []
    if ndjson.exists():
        for line in ndjson.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return decision, entries


def _run_main_raw(raw_stdin: str, tmp_path: Path, enforce: bool = False) -> str:
    """Run main() with raw stdin string; return decision only."""
    data_dir = tmp_path / "_vnx_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "VNX_DATA_DIR": str(data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_HOOK_ENFORCE": "1" if enforce else "0",
    }
    captured = io.StringIO()
    with mock.patch.dict(os.environ, env):
        with mock.patch("sys.stdin", io.StringIO(raw_stdin)):
            with mock.patch("sys.stdout", captured):
                det.main()
    return captured.getvalue().strip()


# ── P0-A: Fail-open invariant ─────────────────────────────────────────────────

class TestFailOpen:
    """main() must emit exactly 'allow' for every malformed payload — no traceback."""

    def test_list_payload_allows(self, tmp_path):
        # [] is valid JSON but not a dict → allow
        result = _run_main_raw("[]", tmp_path)
        assert result == "allow"

    def test_list_command_allows(self, tmp_path):
        # command is a list instead of str → allow
        payload = json.dumps({"tool_input": {"command": ["claude", "-p"]}})
        result = _run_main_raw(payload, tmp_path)
        assert result == "allow"

    def test_tool_input_int_allows(self, tmp_path):
        # tool_input is not a dict → allow
        payload = json.dumps({"tool_input": 42})
        result = _run_main_raw(payload, tmp_path)
        assert result == "allow"

    def test_empty_stdin_allows(self, tmp_path):
        result = _run_main_raw("", tmp_path)
        assert result == "allow"

    def test_non_json_allows(self, tmp_path):
        result = _run_main_raw("not valid json {", tmp_path)
        assert result == "allow"

    def test_null_json_allows(self, tmp_path):
        # null is valid JSON but not a dict
        result = _run_main_raw("null", tmp_path)
        assert result == "allow"

    def test_number_json_allows(self, tmp_path):
        result = _run_main_raw("42", tmp_path)
        assert result == "allow"


# ── P0-B: Raw-CLI bypass closures ─────────────────────────────────────────────

class TestHardBlockBypasses:
    """Bypass vectors that previously evaded hard-block must now BLOCK."""

    # Path-form
    def test_path_claude_p_blocks(self):
        assert _classify("/usr/local/bin/claude -p 'x'") == "block"

    def test_path_kimi_print_blocks(self):
        assert _classify("/usr/bin/kimi --print x") == "block"

    def test_path_codex_exec_blocks(self):
        assert _classify("/usr/bin/codex exec") == "block"

    def test_dotslash_claude_p_blocks(self):
        assert _classify("./claude -p 'x'") == "block"

    # De-quoting (empty quote pair collapse)
    def test_dequote_claude_p_blocks(self):
        assert _classify("cla\"\"ude -p 'x'") == "block"

    def test_dequote_kimi_print_blocks(self):
        assert _classify("ki\"\"mi --print x") == "block"

    def test_dequote_codex_exec_blocks(self):
        assert _classify("co\"\"dex exec") == "block"

    def test_dequote_codex_exec2_blocks(self):
        assert _classify("codex e\"\"xec --json") == "block"

    # Here-string / redirect suffix
    def test_redirect_claude_p_blocks(self):
        assert _classify("claude -p<<<'x'") == "block"

    def test_redirect_kimi_print_blocks(self):
        assert _classify("kimi --print<<<x") == "block"

    def test_redirect_codex_exec_blocks(self):
        assert _classify("codex exec<<<x") == "block"

    # Wrapped in bash -c
    def test_bash_c_claude_p_blocks(self):
        assert _classify('bash -c "claude -p<<<x"') == "block"

    # Both modes block for hard-block rules
    @pytest.mark.parametrize("enforce", [False, True])
    def test_path_claude_p_both_modes(self, enforce):
        assert _classify("/usr/local/bin/claude -p 'x'", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_dequote_kimi_both_modes(self, enforce):
        assert _classify("ki\"\"mi --print x", enforce=enforce) == "block"


# ── P1-A: Enforce-mode over-match (mentions must NOT block) ───────────────────

class TestEnforceModeOverMatch:
    """Commands that mention a lane .py as an argument must NOT be blocked."""

    def test_echo_provider_dispatch_allows(self):
        assert _classify("echo provider_dispatch.py", enforce=True) == "allow"

    def test_cat_provider_dispatch_allows(self):
        assert _classify("cat docs/provider_dispatch.py", enforce=True) == "allow"

    def test_git_grep_provider_dispatch_allows(self):
        assert _classify("git grep provider_dispatch.py", enforce=True) == "allow"

    def test_python_c_string_mention_allows(self):
        # Mention in a string literal — best-effort: not a statement-position import
        # Residual known: some nested-quote forms may still match; documented.
        assert _classify("python -c \"print('import provider_dispatch')\"", enforce=True) == "allow"


# ── P1-A: Shadow invocation forms must still DETECT ───────────────────────────

class TestShadowInvocationDetected:
    """These forms must be shadow-detected (block in enforce mode)."""

    def test_python_m_nospace_provider_dispatch_enforce(self):
        assert _classify("python -mprovider_dispatch", enforce=True) == "block"

    def test_python_m_nospace_provider_dispatch_shadow(self):
        assert _classify("python -mprovider_dispatch", enforce=False) == "allow"

    def test_python3_m_nospace_subprocess_dispatch_enforce(self):
        assert _classify("python3 -msubprocess_dispatch", enforce=True) == "block"

    def test_python_c_nospace_import_enforce(self):
        assert _classify("python -c'import provider_dispatch'", enforce=True) == "block"

    def test_python3_c_nospace_import_shadow(self):
        assert _classify("python3 -c'import provider_dispatch'", enforce=False) == "allow"

    def test_importlib_import_module_enforce(self):
        cmd = "python3 -c 'import importlib; importlib.import_module(\"provider_dispatch\")'"
        assert _classify(cmd, enforce=True) == "block"

    def test_importlib_import_module_shadow(self):
        cmd = "python3 -c 'import importlib; importlib.import_module(\"provider_dispatch\")'"
        assert _classify(cmd, enforce=False) == "allow"


# ── Evasion #1: old allowlist bypass is closed ───────────────────────────────

class TestEvasion1Closed:
    """The ALLOWLIST_PATTERN early-return is gone. claude -p after provider_dispatch.py blocks."""

    def test_provider_dispatch_then_claude_p_blocks(self):
        cmd = "python3 scripts/lib/provider_dispatch.py --provider codex ; claude -p 'x'"
        assert _classify(cmd) == "block"

    def test_subprocess_dispatch_then_claude_p_blocks(self):
        cmd = "subprocess_dispatch.py --provider claude ; claude -p 'prompt'"
        assert _classify(cmd) == "block"

    def test_provider_dispatch_then_kimi_print_blocks(self):
        cmd = "python3 scripts/lib/provider_dispatch.py --provider kimi ; kimi --print 'x'"
        assert _classify(cmd) == "block"

    def test_provider_dispatch_then_codex_exec_blocks(self):
        cmd = "python3 scripts/lib/provider_dispatch.py --provider codex ; codex exec --json"
        assert _classify(cmd) == "block"

    def test_clean_provider_dispatch_allows_shadow_mode(self):
        # shadow rule matches but VNX_HOOK_ENFORCE=0 → decision is still "allow"
        cmd = "python3 scripts/lib/provider_dispatch.py --provider codex --dispatch-id test-123"
        assert _classify(cmd, enforce=False) == "allow"

    def test_clean_subprocess_dispatch_allows_shadow_mode(self):
        cmd = "python3 scripts/lib/subprocess_dispatch.py dispatch-id-456"
        assert _classify(cmd, enforce=False) == "allow"


# ── Hard-block rules: unchanged in both modes ─────────────────────────────────

class TestHardBlocksUnchanged:
    """Existing blocking rules must block regardless of VNX_HOOK_ENFORCE."""

    @pytest.mark.parametrize("enforce", [False, True])
    def test_claude_p_blocks(self, enforce):
        assert _classify("claude -p 'x'", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_claude_print_blocks(self, enforce):
        assert _classify("claude --print 'task'", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_claude_dangerously_skip_blocks(self, enforce):
        assert _classify("claude --dangerously-skip-permissions", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_kimi_print_blocks(self, enforce):
        assert _classify("kimi --print 'x'", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_kimi_p_blocks(self, enforce):
        assert _classify("kimi -p 'x'", enforce=enforce) == "block"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_codex_exec_blocks(self, enforce):
        assert _classify("codex exec --json", enforce=enforce) == "block"


# ── Benign commands: always allow ─────────────────────────────────────────────

class TestBenignAllow:

    def test_claude_version(self):
        assert _classify("claude --version") == "allow"

    def test_claude_help(self):
        assert _classify("claude --help") == "allow"

    def test_bare_claude(self):
        assert _classify("claude") == "allow"

    def test_kimi_login(self):
        assert _classify("kimi login") == "allow"

    def test_kimi_version(self):
        assert _classify("kimi --version") == "allow"

    def test_kimi_help(self):
        assert _classify("kimi --help") == "allow"

    def test_bare_kimi(self):
        assert _classify("kimi") == "allow"

    def test_codex_help(self):
        assert _classify("codex --help") == "allow"

    def test_codex_version(self):
        assert _classify("codex --version") == "allow"

    def test_bare_codex(self):
        assert _classify("codex") == "allow"

    def test_empty_command(self):
        assert _classify("") == "allow"

    def test_git_status(self):
        assert _classify("git status") == "allow"

    def test_grep_p(self):
        assert _classify("grep -p pattern file.txt") == "allow"

    def test_mkdir_p(self):
        assert _classify("mkdir -p /some/path") == "allow"

    def test_unrelated_python_script(self):
        assert _classify("python3 scripts/build_t0_state.py") == "allow"


# ── Shadow evasion: direct lane script ────────────────────────────────────────

class TestShadowLaneScriptDirect:

    def test_provider_dispatch_direct_shadow_allows(self):
        assert _classify("python3 scripts/lib/provider_dispatch.py --provider codex", enforce=False) == "allow"

    def test_provider_dispatch_direct_enforce_blocks(self):
        assert _classify("python3 scripts/lib/provider_dispatch.py --provider codex", enforce=True) == "block"

    def test_subprocess_dispatch_direct_shadow_allows(self):
        assert _classify("python3 scripts/lib/subprocess_dispatch.py prompt", enforce=False) == "allow"

    def test_subprocess_dispatch_direct_enforce_blocks(self):
        assert _classify("python3 scripts/lib/subprocess_dispatch.py prompt", enforce=True) == "block"

    def test_tmux_interactive_dispatch_shadow_allows(self):
        assert _classify("python3 scripts/lib/tmux_interactive_dispatch.py --provider claude", enforce=False) == "allow"

    def test_tmux_interactive_dispatch_enforce_blocks(self):
        assert _classify("python3 scripts/lib/tmux_interactive_dispatch.py --provider claude", enforce=True) == "block"

    def test_dispatch_cli_shadow_allows(self):
        assert _classify("scripts/lib/dispatch_cli.py --provider claude", enforce=False) == "allow"

    def test_dispatch_cli_enforce_blocks(self):
        assert _classify("scripts/lib/dispatch_cli.py --provider claude", enforce=True) == "block"

    def test_absolute_path_lane_script_shadow_allows(self):
        assert _classify("/home/user/proj/scripts/lib/provider_dispatch.py --provider kimi", enforce=False) == "allow"

    def test_absolute_path_lane_script_enforce_blocks(self):
        assert _classify("/home/user/proj/scripts/lib/provider_dispatch.py --provider kimi", enforce=True) == "block"


# ── Shadow evasion: python -m <lane_module> ───────────────────────────────────

class TestShadowPythonMLane:

    def test_python_m_provider_dispatch_shadow_allows(self):
        assert _classify("python -m provider_dispatch", enforce=False) == "allow"

    def test_python_m_provider_dispatch_enforce_blocks(self):
        assert _classify("python -m provider_dispatch", enforce=True) == "block"

    def test_python3_m_subprocess_dispatch_shadow_allows(self):
        assert _classify("python3 -m subprocess_dispatch", enforce=False) == "allow"

    def test_python3_m_subprocess_dispatch_enforce_blocks(self):
        assert _classify("python3 -m subprocess_dispatch", enforce=True) == "block"

    def test_python_m_tmux_interactive_shadow_allows(self):
        assert _classify("python -m tmux_interactive_dispatch", enforce=False) == "allow"

    def test_python_m_tmux_interactive_enforce_blocks(self):
        assert _classify("python -m tmux_interactive_dispatch", enforce=True) == "block"

    def test_python_m_dispatch_cli_shadow_allows(self):
        assert _classify("python -m dispatch_cli --provider claude", enforce=False) == "allow"

    def test_python_m_dispatch_cli_enforce_blocks(self):
        assert _classify("python -m dispatch_cli --provider claude", enforce=True) == "block"

    def test_python3_m_with_flags_shadow_allows(self):
        assert _classify("python3 -W ignore -m provider_dispatch", enforce=False) == "allow"

    def test_python3_m_with_flags_enforce_blocks(self):
        assert _classify("python3 -W ignore -m provider_dispatch", enforce=True) == "block"


# ── Shadow evasion: python -c "import <lane_module>" ─────────────────────────

class TestShadowPythonCImport:

    def test_python_c_import_subprocess_dispatch_shadow_allows(self):
        assert _classify('python -c "import subprocess_dispatch"', enforce=False) == "allow"

    def test_python_c_import_subprocess_dispatch_enforce_blocks(self):
        assert _classify('python -c "import subprocess_dispatch"', enforce=True) == "block"

    def test_python3_c_from_import_shadow_allows(self):
        assert _classify("python3 -c 'from provider_dispatch import main; main()'", enforce=False) == "allow"

    def test_python3_c_from_import_enforce_blocks(self):
        assert _classify("python3 -c 'from provider_dispatch import main; main()'", enforce=True) == "block"

    def test_python_c_import_tmux_shadow_allows(self):
        assert _classify('python3 -c "import tmux_interactive_dispatch"', enforce=False) == "allow"

    def test_python_c_import_tmux_enforce_blocks(self):
        assert _classify('python3 -c "import tmux_interactive_dispatch"', enforce=True) == "block"

    def test_python_c_import_dispatch_cli_shadow_allows(self):
        assert _classify('python -c "import dispatch_cli; dispatch_cli.run()"', enforce=False) == "allow"

    def test_python_c_import_dispatch_cli_enforce_blocks(self):
        assert _classify('python -c "import dispatch_cli; dispatch_cli.run()"', enforce=True) == "block"

    def test_python_c_no_lane_import_allows(self):
        # -c with unrelated import must not trigger
        assert _classify('python -c "import os; os.listdir(\'.\')"', enforce=False) == "allow"

    def test_python_without_c_flag_allows(self):
        # bare python with import in a .py file: not a -c pattern
        assert _classify("python import_provider_dispatch.py", enforce=False) == "allow"


# ── Telemetry ─────────────────────────────────────────────────────────────────

class TestTelemetry:

    def test_shadow_hit_writes_one_ndjson_line(self, tmp_path):
        decision, entries = _run_main("python -m provider_dispatch", tmp_path, enforce=False)
        assert decision == "allow"
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matched_rule"] == "python_m_lane_module"
        assert entry["severity"] == "shadow"
        assert entry["mode"] == "shadow"

    def test_hard_block_writes_one_ndjson_line(self, tmp_path):
        decision, entries = _run_main("claude -p 'x'", tmp_path, enforce=False)
        assert decision == "block"
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matched_rule"] == "claude_raw_cli"
        assert entry["severity"] == "block"

    def test_shadow_enforce_writes_block_severity(self, tmp_path):
        decision, entries = _run_main("python -m subprocess_dispatch", tmp_path, enforce=True)
        assert decision == "block"
        assert len(entries) == 1
        assert entries[0]["severity"] == "block"
        assert entries[0]["mode"] == "enforce"

    def test_ndjson_entry_has_all_required_fields(self, tmp_path):
        _, entries = _run_main('python -c "import subprocess_dispatch"', tmp_path)
        assert len(entries) == 1
        entry = entries[0]
        for field in ("timestamp", "command", "matched_rule", "severity", "mode"):
            assert field in entry, f"Missing field: {field}"

    def test_ndjson_command_truncated_to_2000(self, tmp_path):
        long_cmd = "python -m provider_dispatch " + "x" * 3000
        _, entries = _run_main(long_cmd, tmp_path)
        assert len(entries) == 1
        assert len(entries[0]["command"]) <= 2000

    def test_benign_command_writes_no_telemetry(self, tmp_path):
        decision, entries = _run_main("git status", tmp_path)
        assert decision == "allow"
        assert entries == []

    def test_telemetry_failure_still_allows(self, tmp_path):
        """Telemetry write error must never change the hook decision."""
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "python -m provider_dispatch"}})
        captured = io.StringIO()
        with mock.patch("pretooluse_spawn_detector.project_root") as mock_pr:
            mock_pr.resolve_data_dir.side_effect = OSError("disk full")
            with mock.patch.dict(os.environ, {"VNX_HOOK_ENFORCE": "0"}):
                with mock.patch("sys.stdin", io.StringIO(payload)):
                    with mock.patch("sys.stdout", captured):
                        det.main()
        assert captured.getvalue().strip() == "allow"

    def test_lane_script_direct_telemetry(self, tmp_path):
        cmd = "scripts/lib/dispatch_cli.py --provider claude"
        _, entries = _run_main(cmd, tmp_path, enforce=False)
        assert len(entries) == 1
        assert entries[0]["matched_rule"] == "lane_script_direct"
        assert entries[0]["severity"] == "shadow"

    def test_python_c_import_telemetry(self, tmp_path):
        cmd = 'python3 -c "import subprocess_dispatch"'
        _, entries = _run_main(cmd, tmp_path, enforce=False)
        assert len(entries) == 1
        assert entries[0]["matched_rule"] == "python_c_lane_import"


# ── Malformed stdin ───────────────────────────────────────────────────────────

class TestMalformedInput:

    def test_malformed_json_allows(self):
        captured = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("not valid json {")):
            with mock.patch("sys.stdout", captured):
                det.main()
        assert captured.getvalue().strip() == "allow"

    def test_empty_stdin_allows(self):
        captured = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")):
            with mock.patch("sys.stdout", captured):
                det.main()
        assert captured.getvalue().strip() == "allow"

    def test_missing_tool_input_allows(self):
        payload = json.dumps({"tool_name": "Bash"})
        captured = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch("sys.stdout", captured):
                det.main()
        assert captured.getvalue().strip() == "allow"
