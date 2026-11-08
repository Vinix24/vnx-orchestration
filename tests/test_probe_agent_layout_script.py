"""Tests for scripts/probe_agent_layout.sh (agents-als-map-fase-1, PR-6).

No test starts a real claude: --self-check validates the table without a CLI, and
the no-CLI test runs with a minimal PATH. A new script has no old-code red run
(the plan names this exception); the red run is the missing PROBE file itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "probe_agent_layout.sh"
BASH = "/bin/bash"
EXPECTED = {"A": (3, True), "C": (3, False), "D": (3, True), "H1": (4, True), "H2": (4, True), "L": (4, True), "B": (5, True)}
KEYS = {"cell", "point", "decisive", "outcome", "reason", "command", "claude_version", "sha256"}


def _run(tmp_path, *args, path=None):
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
    if path is not None:
        env["PATH"] = path
    return subprocess.run([BASH, str(PROBE), *args], capture_output=True, text=True, env=env, check=False)


def _rows(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_script_exists_and_parses():
    assert PROBE.exists(), "scripts/probe_agent_layout.sh missing"
    assert subprocess.run([BASH, "-n", str(PROBE)], check=False).returncode == 0


def test_self_check_covers_every_cell_with_the_right_flags(tmp_path):
    proc = _run(tmp_path, "--self-check")
    assert proc.returncode == 0, proc.stderr
    rows = {row["cell"]: row for row in _rows(proc.stdout)}
    assert set(rows) == set(EXPECTED)
    for cell, flags in EXPECTED.items():
        assert (rows[cell]["point"], rows[cell]["decisive"]) == flags, cell
        assert KEYS <= set(rows[cell]) and rows[cell]["outcome"] in {"ja", "nee", "niet_gemeten"}
        if rows[cell]["outcome"] == "niet_gemeten":
            assert rows[cell]["reason"], cell
    assert all(rows[c]["outcome"] == "niet_gemeten" for c in EXPECTED if c != "L")


def test_cell_l_names_a_lane_with_file_and_line(tmp_path):
    row = _rows(_run(tmp_path, "--cell", "L").stdout)[0]
    assert (row["cell"], row["outcome"]) == ("L", "ja")
    assert re.search(r"[A-Za-z0-9_./-]+\.py:\d+", row["reason"]), row["reason"]
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])


def test_cli_cell_without_claude_is_niet_gemeten_with_a_reason(tmp_path):
    row = _rows(_run(tmp_path, "--cell", "A", path="/usr/bin:/bin").stdout)[0]
    assert (row["cell"], row["outcome"]) == ("A", "niet_gemeten")
    assert "claude" in row["reason"] and row["sha256"] == "-"


# --- round 2: H1/H2 and B judge the recorded run, not only the files ---------------------

HOOK_TEXT = "worker-scope: write target 'x' is outside file_write_scope for role 'code-worker'"
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
if "--version" in argv:
    print("2.1.319 (Claude Code)"); raise SystemExit
prompt = argv[argv.index("-p") + 1]
def emit(ev): print(json.dumps(ev))
if not prompt.startswith("Use the Write tool"):
    mode = os.environ.get("FAKE_B", "env")
    if mode == "env":
        mode = "key" if os.environ.get("ANTHROPIC_API_KEY") else "oauth"
    if mode in ("oauth", "key"):
        emit({"type": "system", "subtype": "init", "apiKeySource": "none" if mode == "oauth" else "ANTHROPIC_API_KEY"})
    elif mode == "nullsrc":
        emit({"type": "system", "subtype": "init", "apiKeySource": None})
    elif mode == "emptysrc":
        emit({"type": "system", "subtype": "init", "apiKeySource": ""})
    elif mode == "intsrc":
        emit({"type": "system", "subtype": "init", "apiKeySource": 7})
    elif mode == "nosrc":
        emit({"type": "system", "subtype": "init"})
    emit({"type": "result", "is_error": False, "result": "OK"})
    raise SystemExit(int(os.environ.get("FAKE_B_EXIT", "0")))
target = prompt.rsplit(" to ", 1)[1]
denied = target.endswith("H1-outside.txt") or target.endswith("scripts/x.py")
mode = os.environ.get("FAKE_DENIED", "deny") if denied else os.environ.get("FAKE_ALLOWED", "write")
def write_call(result):
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": target}}]}})
    emit({"type": "user", "message": {"content": [dict(type="tool_result", tool_use_id="t1", **result)]}})
if mode == "write":
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "w").write("probe\n")
    write_call({"is_error": False, "content": "File created"})
elif mode == "deny":
    write_call({"is_error": True, "content": FAKE_HOOK})
elif mode == "denylist":
    write_call({"is_error": True, "content": [{"type": "text", "text": FAKE_HOOK}]})
elif mode == "nohook":
    write_call({"is_error": True, "content": "permission denied by user"})
elif mode == "noerrorflag":
    write_call({"content": FAKE_HOOK})
elif mode == "wrongpath":
    target = target + ".other"; write_call({"is_error": True, "content": FAKE_HOOK})
elif mode == "authfail":
    emit({"type": "result", "is_error": True, "result": "Invalid API key - Please run /login"})
    raise SystemExit(1)
elif mode == "exit1":
    raise SystemExit(3)
elif mode == "timeout":
    time.sleep(10)
emit({"type": "result", "is_error": False, "result": "done"})
'''.replace("FAKE_HOOK", repr(HOOK_TEXT))


def _fake_cell(tmp_path, cell, **env):
    fake = tmp_path / "fakeclaude"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    fake.chmod(0o755)
    run_env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path), PROBE_CLAUDE_BIN=str(fake), PROBE_TIMEOUT_SECONDS="2", **env)
    proc = subprocess.run([BASH, str(PROBE), "--cell", cell], capture_output=True, text=True, env=run_env, check=False)
    return _rows(proc.stdout)[0]


def test_write_cell_ja_needs_write_attempt_and_hook_denial(tmp_path):
    for cell in ("H1", "H2"):
        row = _fake_cell(tmp_path, cell)
        assert row["outcome"] == "ja", row
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
    assert _fake_cell(tmp_path, "H1", FAKE_DENIED="denylist")["outcome"] == "ja"


def test_write_cell_nee_when_denied_file_exists(tmp_path):
    for cell in ("H1", "H2"):
        assert _fake_cell(tmp_path, cell, FAKE_DENIED="write")["outcome"] == "nee"


def test_write_cell_without_recorded_denial_stays_niet_gemeten(tmp_path):
    cases = {
        "exit1": "cli_exit_3",
        "timeout": "timeout",
        "authfail": "auth_failed",
        "none": "no_write_attempt",
        "nohook": "no_hook_denial",
        "noerrorflag": "no_hook_denial",
        "wrongpath": "no_write_attempt",
        "bogus": "no_write_attempt",
    }
    for mode, reason in cases.items():
        row = _fake_cell(tmp_path, "H1", FAKE_DENIED=mode)
        assert row["outcome"] == "niet_gemeten", (mode, row)
        assert reason in row["reason"], (mode, row)


def test_write_cell_positive_control_without_proof_is_niet_gemeten(tmp_path):
    for mode in ("exit1", "none", "bogus", "authfail"):
        row = _fake_cell(tmp_path, "H2", FAKE_ALLOWED=mode)
        assert row["outcome"] == "niet_gemeten", (mode, row)
        assert "positive control" in row["reason"], row


def test_cell_b_ja_only_when_init_names_no_key_and_env_is_scrubbed(tmp_path):
    row = _fake_cell(tmp_path, "B", FAKE_B="oauth")
    assert row["outcome"] == "ja", row
    assert "env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX" in row["command"]
    assert "--output-format stream-json --verbose" in row["command"]
    leaked = _fake_cell(tmp_path, "B", FAKE_B="env", ANTHROPIC_API_KEY="sk-test", ANTHROPIC_BASE_URL="http://x")
    assert leaked["outcome"] == "ja", leaked


def test_cell_b_without_proof_of_the_credential_path_is_niet_gemeten(tmp_path):
    row = _fake_cell(tmp_path, "B", FAKE_B="key")
    assert (row["outcome"], "auth_source_api_key" in row["reason"]) == ("niet_gemeten", True), row
    for mode in ("noinit", "nosrc", "nullsrc", "emptysrc", "intsrc"):
        row = _fake_cell(tmp_path, "B", FAKE_B=mode)
        assert row["outcome"] == "niet_gemeten" and "auth_source_unproven" in row["reason"], (mode, row)
    row = _fake_cell(tmp_path, "B", FAKE_B="oauth", FAKE_B_EXIT="4")
    assert row["outcome"] == "niet_gemeten" and "cli_exit_4" in row["reason"], row
