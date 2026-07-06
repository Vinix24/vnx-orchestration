#!/usr/bin/env python3
"""Tests for process_cleanup — the global-process-cleanup loop's classifier."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import process_cleanup as pc  # noqa: E402


def _scan(lines, **kw):
    return pc.scan_processes("\n".join(lines), **kw)


def _by_pid(findings):
    return {f.pid: f for f in findings}


def test_forbidden_headless_claude_is_violation():
    f = _by_pid(_scan(["1000 1 500 0.0 /Users/x/.local/bin/claude -p do the thing"]))
    assert f[1000].klass == pc.VIOLATION
    assert "claude-headless" in f[1000].reason


def test_print_flag_is_also_violation():
    f = _by_pid(_scan(["1000 1 500 0.0 claude --print summarize"]))
    assert f[1000].klass == pc.VIOLATION


def test_interactive_claude_is_protected():
    f = _by_pid(_scan(["1001 1 30000 0.1 /usr/local/bin/claude"]))
    assert f[1001].klass == pc.PROTECTED


def test_shell_containing_claude_p_text_is_not_a_violation():
    # A shell whose script merely CONTAINS 'claude -p' must NOT be flagged — this is
    # the exact false positive a naive grep hits (argv[0] is the shell, not claude).
    f = _by_pid(_scan(["1002 1 100 3.0 /bin/zsh -c cd repo && claude -p foo"]))
    assert f[1002].klass != pc.VIOLATION


def test_idle_work_process_is_surfaced():
    f = _by_pid(_scan(["2000 1 20000 0.0 python3 scripts/lib/provider_dispatch.py --provider kimi"]))
    assert f[2000].klass == pc.IDLE


def test_recent_work_process_is_ok():
    f = _by_pid(_scan(["2002 1 100 0.0 python3 scripts/lib/provider_dispatch.py"]))
    assert f[2002].klass == pc.OK


def test_busy_work_process_is_ok():
    f = _by_pid(_scan(["2001 1 20000 55.0 python3 scripts/lib/provider_dispatch.py"]))
    assert f[2001].klass == pc.OK


def test_idle_non_work_daemon_is_ok():
    f = _by_pid(_scan(["2003 1 90000 0.0 /Applications/Ollama.app/Contents/MacOS/Ollama"]))
    assert f[2003].klass == pc.OK


def test_explicit_protected_pid_never_actionable():
    f = _by_pid(_scan(
        ["2000 1 20000 0.0 python3 scripts/lib/provider_dispatch.py"],
        protected_pids={2000},
    ))
    assert f[2000].klass == pc.PROTECTED


def test_emit_proposals_marks_gating(tmp_path):
    findings = _scan([
        "1000 1 500 0.0 claude -p x",                                   # violation
        "2000 1 20000 0.0 python3 scripts/lib/provider_dispatch.py",    # idle
        "1001 1 30000 0.1 claude",                                      # protected
        "3000 1 100 0.0 /usr/sbin/cron",                                # ok
    ])
    import json
    path = pc.emit_proposals(findings, tmp_path)
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    by_class = {r["class"]: r for r in rows}
    assert set(by_class) == {"violation", "idle"}  # only actionable classes emitted
    assert by_class["violation"]["proposed_action"] == "kill"
    assert by_class["violation"]["operator_gated"] is False
    assert by_class["idle"]["proposed_action"] == "operator_confirm_close"
    assert by_class["idle"]["operator_gated"] is True  # idle is human-decided, never auto-killed


def test_kill_violations_targets_only_violations(monkeypatch):
    findings = _scan([
        "1000 1 500 0.0 claude -p x",                                   # violation
        "2000 1 20000 0.0 python3 scripts/lib/provider_dispatch.py",    # idle
        "1001 1 30000 0.1 claude",                                      # protected
    ])
    killed = []
    monkeypatch.setattr(pc.os, "kill", lambda pid, sig: killed.append(pid))
    result = pc._kill_violations(findings)
    assert killed == [1000]  # idle + protected are never killed
    assert result == [1000]
