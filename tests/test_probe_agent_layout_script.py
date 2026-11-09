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
    if os.environ.get("FAKE_STDERR"):
        print(os.environ["FAKE_STDERR"], file=sys.stderr)
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
elif mode in ("writenoflag", "writenone"):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "w").write("probe\n")
    write_call({"content": "File created"} if mode == "writenoflag" else {"is_error": None, "content": "File created"})
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


# --- round 4: the probe reads a successful Write and an ancestor CLAUDE.md correctly ---------

def _sh(tmp_path, snippet, tmpdir=None):
    """Run a snippet with the probe's functions loaded; the CLI binary does not exist, so nothing can start.

    `--self-check` keeps the old layout (which ran main on source) harmless; the new layout ignores it.
    """
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=tmpdir or str(tmp_path), PROBE_CLAUDE_BIN="/nonexistent/claude")
    script = 'source "%s" --self-check >/dev/null 2>&1 || true\n%s' % (PROBE, snippet)
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, check=False)


def _stream(path, target, result, tool_id="t1"):
    events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Write", "input": {"file_path": str(target)}}]}}]
    if result is not None:
        events.append({"type": "user", "message": {"content": [dict(type="tool_result", tool_use_id=tool_id, **result)]}})
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _facts(tmp_path, result, target=None):
    target = target or tmp_path / "t" / "allowed.txt"
    raw = _stream(tmp_path / "raw.ndjson", target, result)
    return _sh(tmp_path, 'stream_facts "%s" write "%s"' % (raw, target)).stdout.strip()


def test_ancestor_claude_md_is_found_under_a_tmpdir_that_ends_in_a_slash(tmp_path):
    base = tmp_path / "base"
    (base / "A").mkdir(parents=True)
    (base / "A" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    cap = tmp_path / "cap.json"
    cap.write_text(json.dumps({"status": "ok", "instructions": {"files": [{"path": str(base / "A" / "CLAUDE.md"), "type": "Project"}]}}), encoding="utf-8")
    out = _sh(tmp_path, 'inspect "%s" instructions "%s//A/CLAUDE.md"' % (cap, base)).stdout
    assert out.startswith("ja\t"), out


def test_ancestor_claude_md_of_another_path_is_still_nee(tmp_path):
    cap = tmp_path / "cap.json"
    cap.write_text(json.dumps({"status": "ok", "instructions": {"files": [{"path": "/elsewhere/A/CLAUDE.md", "type": "Project"}]}}), encoding="utf-8")
    assert _sh(tmp_path, 'inspect "%s" instructions "%s//A/CLAUDE.md"' % (cap, tmp_path)).stdout.startswith("nee\t")
    cap.write_text(json.dumps({"status": "ok", "instructions": {"files": [{"path": str(tmp_path / "A" / "CLAUDE.md"), "type": "User"}]}}), encoding="utf-8")
    assert _sh(tmp_path, 'inspect "%s" instructions "%s//A/CLAUDE.md"' % (cap, tmp_path)).stdout.startswith("nee\t")


def test_probe_dir_has_no_double_slash_when_tmpdir_ends_in_a_slash(tmp_path):
    proc = _sh(tmp_path, 'PROBE_DIR=""; ensure_probe; printf "%s" "$PROBE_DIR"', tmpdir=str(tmp_path) + "/")
    assert proc.stdout.startswith(str(tmp_path) + "/probe-agent-layout."), proc.stdout
    assert "//" not in proc.stdout, proc.stdout


def test_tmp_base_returns_a_physical_path_when_tmpdir_is_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    proc = _sh(tmp_path, 'printf "%s" "$(tmp_base)"', tmpdir=str(link) + "/")
    assert proc.stdout == os.path.realpath(str(real)), proc.stdout
    proc = _sh(tmp_path, 'PROBE_DIR=""; ensure_probe; printf "%s" "$PROBE_DIR"', tmpdir=str(link))
    assert proc.stdout.startswith(os.path.realpath(str(real)) + "/probe-agent-layout."), proc.stdout
    assert str(link) not in proc.stdout


def test_successful_write_without_is_error_is_written(tmp_path):
    for result in ({"content": "File created"}, {"is_error": None, "content": "File created"}, {"is_error": False, "content": "File created"}):
        assert _facts(tmp_path, result) == "written", result


def test_write_result_that_is_not_a_clean_success_is_not_written(tmp_path):
    hook = HOOK_TEXT
    cases = [
        ({"is_error": "false", "content": "ok"}, "unclear_result"),
        ({"is_error": 0, "content": "ok"}, "unclear_result"),
        ({"is_error": 1, "content": "ok"}, "unclear_result"),
        ({"content": hook}, "unclear_result"),
        ({"is_error": None, "content": [{"type": "text", "text": hook}]}, "unclear_result"),
        ({"is_error": True, "content": "permission denied by user"}, "tool_error"),
        (None, "no_tool_result"),
    ]
    for result, token in cases:
        assert _facts(tmp_path, result) == token, result


def test_hook_refusal_is_denied_and_needs_the_exact_shape(tmp_path):
    assert _facts(tmp_path, {"is_error": True, "content": HOOK_TEXT}) == "denied"
    assert _facts(tmp_path, {"is_error": True, "content": [{"type": "text", "text": HOOK_TEXT}]}) == "denied"
    assert _facts(tmp_path, {"is_error": True, "content": "worker-scope: write target 'x' is allowed"}) == "tool_error"


def test_result_of_another_tool_use_id_does_not_count_and_a_missing_id_never_matches(tmp_path):
    target = tmp_path / "t" / "allowed.txt"
    raw = tmp_path / "raw.ndjson"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(target)}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "File created"}]}},
    ]
    raw.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    assert _sh(tmp_path, 'stream_facts "%s" write "%s"' % (raw, target)).stdout.strip() == "no_attempt"
    _stream(raw, target, {"content": "File created"}, tool_id="other")
    other = json.loads(raw.read_text().splitlines()[1])
    other["message"]["content"][0]["tool_use_id"] = "not-t1"
    raw.write_text(raw.read_text().splitlines()[0] + "\n" + json.dumps(other) + "\n", encoding="utf-8")
    assert _sh(tmp_path, 'stream_facts "%s" write "%s"' % (raw, target)).stdout.strip() == "no_tool_result"


def test_why_not_names_what_happened(tmp_path):
    def why(status, token, which):
        return _sh(tmp_path, 'why_not "%s" "%s" "%s"' % (status, token, which)).stdout
    assert why(0, "no_attempt", "allowed") == "no_write_attempt"
    assert why(0, "no_tool_result", "allowed") == "no_tool_result"
    assert why(0, "no_tool_result", "denied") == "no_tool_result"
    assert why(0, "tool_error", "allowed") == "write_error_without_hook_text"
    assert why(0, "unclear_result", "allowed") == "write_result_unclear"
    assert why(0, "denied", "allowed") == "hook_refused_the_allowed_write"
    assert why(0, "written", "allowed") == "allowed_file_not_as_expected"
    assert why(0, "tool_error", "denied").startswith("no_hook_denial")
    assert why(0, "unclear_result", "denied").startswith("no_hook_denial")
    assert why(0, "bogus", "allowed") == "unknown_write_state"
    assert "no_hook_denial" not in why(0, "tool_error", "allowed") + why(0, "no_attempt", "denied")
    assert why(142, "written", "allowed") == "timeout" and why(1, "auth_failed", "denied") == "auth_failed"
    assert why(7, "written", "denied") == "cli_exit_7" and why("", "written", "denied") == "cli_exit_unknown"


def _judge(tmp_path, s1, s2, allowed_text="probe\n", deny_exists=False, a_result=None, d_result=None):
    allowed, denied = tmp_path / "w" / "allowed.txt", tmp_path / "w" / "denied.txt"
    allowed.parent.mkdir(exist_ok=True)
    if allowed_text is not None:
        allowed.write_text(allowed_text, encoding="utf-8")
    if deny_exists:
        denied.write_text("x", encoding="utf-8")
    r1 = _stream(tmp_path / "r1.ndjson", allowed, a_result if a_result is not None else {"content": "File created"})
    r2 = _stream(tmp_path / "r2.ndjson", denied, d_result if d_result is not None else {"is_error": True, "content": HOOK_TEXT})
    return _sh(tmp_path, 'judge_write "%s" "%s" "%s" "%s" "%s" "%s"' % (r1, s1, allowed, r2, s2, denied)).stdout.strip()


def test_judge_write_ja_on_a_clean_pair_of_calls(tmp_path):
    assert _judge(tmp_path, 0, 0).startswith("ja\t")


def test_judge_write_never_ja_when_a_call_exited_non_zero(tmp_path):
    cases = ((3, 0, "positive control", "(exit 3)"), (0, 3, "hook denial", "(exit 3)"), (142, 0, "timeout", "(exit 142)"),
             ("", 0, "cli_exit_unknown", "(exit unknown)"), (0, "", "cli_exit_unknown", "(exit unknown)"))
    for s1, s2, word, exit_text in cases:
        out = _judge(tmp_path, s1, s2)
        assert out.startswith("niet_gemeten\t"), (s1, s2, out)
        assert word in out and exit_text in out, (s1, s2, out)


def test_judge_write_stays_blocking_on_missing_or_odd_evidence(tmp_path):
    assert _judge(tmp_path, 0, 0, allowed_text=None).startswith("niet_gemeten\t")
    assert _judge(tmp_path, 0, 0, allowed_text="other\n").startswith("niet_gemeten\t")
    assert _judge(tmp_path, 0, 0, a_result={"is_error": "false", "content": "ok"}).startswith("niet_gemeten\t")
    assert _judge(tmp_path, 0, 0, d_result={"content": HOOK_TEXT}).startswith("niet_gemeten\t")
    assert _judge(tmp_path, 0, 0, deny_exists=True).startswith("nee\t")


def test_write_cell_is_ja_when_the_successful_write_has_no_is_error(tmp_path):
    for cell in ("H1", "H2"):
        for mode in ("writenoflag", "writenone"):
            row = _fake_cell(tmp_path, cell, FAKE_ALLOWED=mode)
            assert row["outcome"] == "ja", (cell, mode, row)


def test_write_cell_reason_names_the_allowed_write_failure(tmp_path):
    row = _fake_cell(tmp_path, "H1", FAKE_ALLOWED="deny")
    assert row["outcome"] == "niet_gemeten" and "hook_refused_the_allowed_write" in row["reason"], row
    assert "no_hook_denial" not in row["reason"], row
    row = _fake_cell(tmp_path, "H2", FAKE_ALLOWED="nohook")
    assert "write_error_without_hook_text" in row["reason"] and "no_hook_denial" not in row["reason"], row


def test_cell_c_records_the_class_of_a_failed_bare_call(tmp_path):
    cases = {
        "error: ANTHROPIC_API_KEY is required with --bare": "requires_api_key",
        "error: unknown option '--bare'": "unknown_flag",
        "something else entirely sk-secret-123": "other",
    }
    for text, cls in cases.items():
        row = _fake_cell(tmp_path, "C", FAKE_B_EXIT="1", FAKE_STDERR=text)
        assert row["outcome"] == "niet_gemeten", row
        assert "stderr class: %s" % cls in row["reason"], row
        assert "sk-secret" not in row["reason"] and "ANTHROPIC_API_KEY" not in row["reason"], row


def test_script_sourced_does_not_run_main(tmp_path):
    proc = _sh(tmp_path, "type -t main inspect stream_facts judge_write why_not | tr '\\n' ' '")
    assert proc.stdout.strip() == "function function function function function", proc.stdout
