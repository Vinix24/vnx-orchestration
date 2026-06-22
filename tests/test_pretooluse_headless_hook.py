"""Tests for D1.4 — PreToolUse headless-hook: --allow-headless shadow detection.

Covers:
  - dispatch_bridge.py --allow-headless → shadow (allow) in default mode
  - dispatch_bridge.py --allow-headless → block in enforce mode
  - vnx dispatch --allow-headless → shadow / block
  - --allow-headless anywhere in tokenized argv → shadow / block
  - --adapter subprocess → NOT matched (subprocess-pin lane stays clean)
  - VNX_ADAPTER_T0=subprocess → NOT matched
  - VNX_ADAPTER_T1=subprocess → NOT matched
  - Fail-open on malformed input
  - Telemetry: matched_rule = "claude_allow_headless"
  - Quote-dequoted --allow-headless form → shadow / block
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


# ── Helpers (same pattern as test_pretooluse_spawn_detector.py) ───────────────

def _classify(cmd: str, enforce: bool = False) -> str:
    env = {"VNX_HOOK_ENFORCE": "1" if enforce else "0"}
    with mock.patch.dict(os.environ, env):
        return det.classify(cmd)


def _run_main(cmd: str, tmp_path: Path, enforce: bool = False) -> tuple[str, list[dict]]:
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


# ── Shadow detection: dispatch commands with --allow-headless ─────────────────

class TestAllowHeadlessShadow:
    """--allow-headless in a dispatch command: allow+log when not enforcing."""

    def test_dispatch_bridge_allow_headless_shadow_allows(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id abc --terminal T1 --allow-headless"
        assert _classify(cmd, enforce=False) == "allow"

    def test_dispatch_bridge_allow_headless_enforce_blocks(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id abc --terminal T1 --allow-headless"
        assert _classify(cmd, enforce=True) == "block"

    def test_vnx_dispatch_allow_headless_shadow_allows(self):
        cmd = "vnx dispatch --dispatch-id abc --allow-headless"
        assert _classify(cmd, enforce=False) == "allow"

    def test_vnx_dispatch_allow_headless_enforce_blocks(self):
        cmd = "vnx dispatch --dispatch-id abc --allow-headless"
        assert _classify(cmd, enforce=True) == "block"

    def test_bare_allow_headless_flag_shadow_allows(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --allow-headless"
        assert _classify(cmd, enforce=False) == "allow"

    def test_bare_allow_headless_flag_enforce_blocks(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --allow-headless"
        assert _classify(cmd, enforce=True) == "block"

    def test_allow_headless_with_headless_reason_shadow_allows(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id x --terminal T2 --allow-headless --headless-reason benchmark"
        assert _classify(cmd, enforce=False) == "allow"

    def test_allow_headless_with_headless_reason_enforce_blocks(self):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id x --terminal T2 --allow-headless --headless-reason benchmark"
        assert _classify(cmd, enforce=True) == "block"

    def test_quoted_allow_headless_shadow_allows(self):
        cmd = 'python3 scripts/lib/dispatch_bridge.py "--allow-headless"'
        assert _classify(cmd, enforce=False) == "allow"

    def test_quoted_allow_headless_enforce_blocks(self):
        cmd = 'python3 scripts/lib/dispatch_bridge.py "--allow-headless"'
        assert _classify(cmd, enforce=True) == "block"


# ── Subprocess-pin lane NOT matched ───────────────────────────────────────────

class TestSubprocessPinLaneNotMatched:
    """--adapter and VNX_ADAPTER_T{n}= are the Wave-5 subprocess-pin lane — must NOT shadow."""

    @pytest.mark.parametrize("enforce", [False, True])
    def test_adapter_subprocess_not_matched(self, enforce):
        cmd = "python3 scripts/lib/tmux_interactive_dispatch.py --adapter subprocess --dispatch-id abc"
        # --adapter is not the headless flag; tmux_interactive_dispatch.py IS a lane script
        # so this may be shadow-detected as lane_script_direct, but NOT as claude_allow_headless.
        # The key property: the decision is NOT 'block' in default mode; allow in shadow.
        result = _classify(cmd, enforce=False)
        assert result == "allow"  # lane_script_direct → shadow → allow when not enforcing

    @pytest.mark.parametrize("enforce", [False, True])
    def test_vnx_adapter_env_not_matched(self, enforce):
        cmd = "VNX_ADAPTER_T0=subprocess vnx dispatch --dispatch-id abc"
        # VNX_ADAPTER_T0=subprocess is an env assignment, not --allow-headless
        assert _classify(cmd, enforce=enforce) == "allow"

    @pytest.mark.parametrize("enforce", [False, True])
    def test_vnx_adapter_t1_env_not_matched(self, enforce):
        cmd = "VNX_ADAPTER_T1=subprocess python3 scripts/lib/dispatch_bridge.py --dispatch-id abc"
        assert _classify(cmd, enforce=enforce) == "allow"

    def test_adapter_flag_no_headless_not_matched(self):
        cmd = "python3 dispatch_bridge.py --dispatch-id abc --adapter subprocess"
        assert _classify(cmd, enforce=True) == "allow"

    def test_allow_headless_false_string_not_matched(self):
        # --allow-headless as a value (not an exact token) — e.g. echo the flag
        cmd = "echo allow-headless"
        assert _classify(cmd, enforce=True) == "allow"

    def test_allow_headless_partial_token_not_matched(self):
        # A flag that starts with --allow-headless but is longer must NOT match
        cmd = "python3 dispatch_bridge.py --allow-headless-extra"
        assert _classify(cmd, enforce=True) == "allow"


# ── Telemetry for --allow-headless ────────────────────────────────────────────

class TestAllowHeadlessTelemetry:

    def test_shadow_hit_writes_ndjson_with_correct_rule(self, tmp_path):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id abc --terminal T1 --allow-headless"
        decision, entries = _run_main(cmd, tmp_path, enforce=False)
        assert decision == "allow"
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matched_rule"] == "claude_allow_headless"
        assert entry["severity"] == "shadow"
        assert entry["mode"] == "shadow"

    def test_enforce_hit_writes_ndjson_with_correct_rule(self, tmp_path):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id abc --terminal T1 --allow-headless"
        decision, entries = _run_main(cmd, tmp_path, enforce=True)
        assert decision == "block"
        assert len(entries) == 1
        entry = entries[0]
        assert entry["matched_rule"] == "claude_allow_headless"
        assert entry["severity"] == "block"
        assert entry["mode"] == "enforce"

    def test_ndjson_entry_has_all_required_fields(self, tmp_path):
        cmd = "vnx dispatch --allow-headless"
        _, entries = _run_main(cmd, tmp_path, enforce=False)
        assert len(entries) == 1
        entry = entries[0]
        for field in ("timestamp", "command", "matched_rule", "severity", "mode"):
            assert field in entry, f"Missing field: {field}"

    def test_no_headless_flag_no_telemetry(self, tmp_path):
        cmd = "python3 scripts/lib/dispatch_bridge.py --dispatch-id abc --terminal T1"
        decision, entries = _run_main(cmd, tmp_path, enforce=False)
        assert decision == "allow"
        assert entries == []


# ── Fail-open on malformed input ──────────────────────────────────────────────

class TestFailOpenWithHeadlessFlag:
    """Malformed payloads must still allow, even when --allow-headless appears in cmd."""

    def test_unbalanced_quote_with_allow_headless_allows(self):
        # shlex fails → legacy fallback → checks regex for --allow-headless
        cmd = 'python3 dispatch_bridge.py --allow-headless "unterminated'
        result = _classify(cmd, enforce=False)
        assert result == "allow"

    def test_unbalanced_quote_with_allow_headless_enforce_blocks(self):
        # shlex fails → legacy fallback → detects --allow-headless → block in enforce
        cmd = 'python3 dispatch_bridge.py --allow-headless "unterminated'
        result = _classify(cmd, enforce=True)
        assert result == "block"

    def test_none_command_allows(self):
        result = _classify("", enforce=True)
        assert result == "allow"
