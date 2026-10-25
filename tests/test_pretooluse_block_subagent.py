"""Tests for pretooluse_block_subagent.sh, pretooluse_subagent_guard.py and
scripts/subagents_allow.py.

OI-1643: the hook used to emit the deprecated flat ``{"decision":"block",...}``
form. Claude Code's PreToolUse event reads
``hookSpecificOutput.permissionDecision`` (``allow``/``deny``/``ask``) instead,
so the old form was silently ignored and subagents were never actually
blocked. This suite locks in the corrected contract plus the operator
override mechanism (marker file + audit trail + CLI).

Covers:
  - No marker: deny, valid hookSpecificOutput JSON.
  - Valid marker: allow, exactly one new line appended to the audit NDJSON.
  - Expired marker: deny, reason mentions the marker is expired.
  - Marker with an empty/missing reason: deny (never a bare "allow").
  - Non-Task tool calls: allowed silently (empty stdout) — unaffected by the
    marker/guard logic (defense-in-depth fast path).
  - CLI (scripts/subagents_allow.py): --status without a marker (exit 1,
    "niet toegestaan"); --reason + --hours grants (exit 0); --status after
    grant (exit 0); --revoke removes the marker; no --reason refuses.
  - Static: bash -n / py_compile on every touched file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "pretooluse_block_subagent.sh"
GUARD_CORE = REPO_ROOT / "scripts" / "hooks" / "pretooluse_subagent_guard.py"
CLI_SCRIPT = REPO_ROOT / "scripts" / "subagents_allow.py"

MARKER_FILENAME = "subagents_allowed.json"
AUDIT_FILENAME = "subagent_use.ndjson"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_now() -> str:
    return _iso(datetime.now(timezone.utc))


def iso_in(hours: float) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def make_task_payload(session_id: str = "test-session", prompt: str = "Do a subtask") -> str:
    payload = {
        "tool_name": "Task",
        "tool_input": {
            "description": "test subagent",
            "prompt": prompt,
            "subagent_type": "general-purpose",
        },
        "session_id": session_id,
        "cwd": "/tmp/test-project",
        "transcript_path": "/tmp/test.jsonl",
    }
    return json.dumps(payload)


def run_hook(payload: str, state_dir: Path) -> tuple[int, str]:
    env = dict(os.environ, VNX_STATE_DIR=str(state_dir))
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return result.returncode, result.stdout


def run_cli(args: list[str], state_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, VNX_STATE_DIR=str(state_dir))
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def write_marker(state_dir: Path, **fields) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "reason": "operator toegestaan (test)",
        "granted_at": iso_now(),
        "expires_at": iso_in(1),
        "granted_by": "test-user",
    }
    marker.update(fields)
    (state_dir / MARKER_FILENAME).write_text(json.dumps(marker), encoding="utf-8")


# ── 1. No marker: deny ─────────────────────────────────────────────────────────

class TestNoMarker:
    def test_no_marker_denies(self, tmp_path):
        rc, out = run_hook(make_task_payload(), tmp_path)
        assert rc == 0, "hook must always exit 0; decision travels via stdout JSON"
        assert out.strip(), "a deny decision must produce JSON stdout"
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"]

    def test_no_marker_produces_no_audit_file(self, tmp_path):
        run_hook(make_task_payload(), tmp_path)
        assert not (tmp_path / AUDIT_FILENAME).exists()


# ── 2. Valid marker: allow + audit line ────────────────────────────────────────

class TestValidMarker:
    def test_valid_marker_allows(self, tmp_path):
        write_marker(tmp_path)
        rc, out = run_hook(make_task_payload(), tmp_path)
        assert rc == 0
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert hso["hookEventName"] == "PreToolUse"

    def test_valid_marker_writes_exactly_one_audit_line(self, tmp_path):
        write_marker(tmp_path, reason="ronde-2 handtest")
        run_hook(make_task_payload(session_id="sess-abc", prompt="X" * 300), tmp_path)
        audit_path = tmp_path / AUDIT_FILENAME
        assert audit_path.is_file(), "an allowed subagent call must leave an audit trail"
        lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["reason"] == "ronde-2 handtest"
        assert entry["session_id"] == "sess-abc"
        assert len(entry["prompt_excerpt"]) <= 200

    def test_two_allowed_calls_append_two_lines(self, tmp_path):
        write_marker(tmp_path)
        run_hook(make_task_payload(), tmp_path)
        run_hook(make_task_payload(), tmp_path)
        lines = [
            ln for ln in (tmp_path / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 2


# ── 3. Expired marker: deny, reason mentions expiry ────────────────────────────

class TestExpiredMarker:
    def test_expired_marker_denies(self, tmp_path):
        write_marker(tmp_path, granted_at=iso_in(-3), expires_at=iso_in(-1))
        rc, out = run_hook(make_task_payload(), tmp_path)
        assert rc == 0
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "verlopen" in hso["permissionDecisionReason"].lower()

    def test_expired_marker_produces_no_audit_file(self, tmp_path):
        write_marker(tmp_path, granted_at=iso_in(-3), expires_at=iso_in(-1))
        run_hook(make_task_payload(), tmp_path)
        assert not (tmp_path / AUDIT_FILENAME).exists()


# ── 4. Marker without a reason: deny ───────────────────────────────────────────

class TestMarkerWithoutReason:
    def test_empty_reason_denies(self, tmp_path):
        write_marker(tmp_path, reason="")
        rc, out = run_hook(make_task_payload(), tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_missing_reason_key_denies(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        marker = {"granted_at": iso_now(), "expires_at": iso_in(1), "granted_by": "x"}
        (tmp_path / MARKER_FILENAME).write_text(json.dumps(marker), encoding="utf-8")
        rc, out = run_hook(make_task_payload(), tmp_path)
        data = json.loads(out)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── Defense-in-depth: non-Task tools are unaffected ────────────────────────────

class TestNonTaskTool:
    def test_non_task_tool_allows_silently(self, tmp_path):
        env = dict(os.environ, VNX_STATE_DIR=str(tmp_path))
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "x"})
        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)], input=payload, capture_output=True, text=True, env=env, timeout=15
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""


# ── 5. CLI: scripts/subagents_allow.py ─────────────────────────────────────────

class TestCLI:
    def test_status_without_marker_exits_1(self, tmp_path):
        result = run_cli(["--status"], tmp_path)
        assert result.returncode == 1
        assert "niet toegestaan" in (result.stdout + result.stderr).lower()

    def test_grant_requires_reason(self, tmp_path):
        result = run_cli(["--hours", "1"], tmp_path)
        assert result.returncode != 0

    def test_grant_requires_nonempty_reason(self, tmp_path):
        result = run_cli(["--reason", "   ", "--hours", "1"], tmp_path)
        assert result.returncode != 0
        assert not (tmp_path / MARKER_FILENAME).exists()

    def test_grant_writes_marker(self, tmp_path):
        result = run_cli(["--reason", "sessie-test", "--hours", "2"], tmp_path)
        assert result.returncode == 0
        marker_path = tmp_path / MARKER_FILENAME
        assert marker_path.is_file()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert marker["reason"] == "sessie-test"
        assert marker["granted_at"]
        assert marker["expires_at"]
        assert marker["granted_by"]

    def test_status_after_grant_exits_0(self, tmp_path):
        run_cli(["--reason", "sessie-test", "--hours", "1"], tmp_path)
        result = run_cli(["--status"], tmp_path)
        assert result.returncode == 0

    def test_revoke_removes_marker(self, tmp_path):
        run_cli(["--reason", "sessie-test", "--hours", "1"], tmp_path)
        assert (tmp_path / MARKER_FILENAME).exists()
        result = run_cli(["--revoke"], tmp_path)
        assert result.returncode == 0
        assert not (tmp_path / MARKER_FILENAME).exists()

    def test_revoke_is_idempotent_without_marker(self, tmp_path):
        result = run_cli(["--revoke"], tmp_path)
        assert result.returncode == 0

    def test_cli_grant_then_hook_allows(self, tmp_path):
        """End-to-end: CLI grant is readable by the hook (same state dir, same schema)."""
        grant = run_cli(["--reason", "end-to-end", "--hours", "1"], tmp_path)
        assert grant.returncode == 0
        rc, out = run_hook(make_task_payload(), tmp_path)
        data = json.loads(out)
        assert data["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── Static checks ───────────────────────────────────────────────────────────────

class TestStatic:
    def test_hook_script_exists(self):
        assert HOOK_SCRIPT.exists()

    def test_guard_core_exists(self):
        assert GUARD_CORE.exists()

    def test_cli_script_exists(self):
        assert CLI_SCRIPT.exists()

    def test_hook_script_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(HOOK_SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_guard_core_python_syntax(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(GUARD_CORE)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_cli_python_syntax(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(CLI_SCRIPT)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
