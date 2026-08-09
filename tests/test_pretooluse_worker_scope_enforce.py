#!/usr/bin/env python3
"""Tests for scripts/hooks/pretooluse_worker_scope_enforce.{py,sh}.

Dispatch 20260724-worker-scope-enforce-hook. Covers the required 5-case matrix:
  (a) a bash_deny command is blocked
  (b) an out-of-scope file write is blocked
  (c) an in-scope write + an allowed command pass
  (d) flag OFF (unset or != "1") => guaranteed no-op for all of the above
  (e) a block emits the worker_scope_block audit receipt

Plus unit coverage of evaluate() semantics (role resolution, path relativizing,
malformed payloads) and the JSON/exit-code hook contract (exit 0 always,
{"decision":"block","reason":...} on stdout only for blocks).

Audit side effects are redirected to a tmp dir via VNX_DATA_DIR +
VNX_DATA_DIR_EXPLICIT=1 so no test ever writes into real .vnx-data state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_DIR = REPO_ROOT / "scripts" / "hooks"
HOOK_SH = HOOK_DIR / "pretooluse_worker_scope_enforce.sh"
HOOK_PY = HOOK_DIR / "pretooluse_worker_scope_enforce.py"

sys.path.insert(0, str(HOOK_DIR))
import pretooluse_worker_scope_enforce as hook  # noqa: E402

ROLE = "quality-engineer"  # bash_deny: git push*; file_write_scope: tests/**, scripts/check_*


def make_payload(
    tool_name: str,
    tool_input: dict,
    cwd: str = "/wt",
) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": "test-session-worker-scope",
            "cwd": cwd,
            "transcript_path": "/tmp/test.jsonl",
        }
    )


def run_hook(
    payload: str,
    *,
    enforce: bool,
    role: "str | None" = ROLE,
    data_dir: "Path | None" = None,
    extra_env: "dict | None" = None,
) -> subprocess.CompletedProcess:
    """Run the .sh launcher end-to-end with a controlled environment."""
    env = dict(os.environ)
    # Deterministic baseline: the gate flag must never leak in from the shell.
    env.pop("VNX_ENFORCE_WORKER_PERMISSIONS", None)
    env.pop("VNX_WORKER_ROLE", None)
    # Prevent repo-level env vars from steering YAML resolution to the main
    # repo instead of the worktree-local .vnx/worker_permissions.yaml.
    for _steer in ("PROJECT_ROOT", "VNX_PROJECT_ROOT", "VNX_HOME"):
        env.pop(_steer, None)
    if enforce:
        env["VNX_ENFORCE_WORKER_PERMISSIONS"] = "1"
    if role is not None:
        env["VNX_WORKER_ROLE"] = role
    if data_dir is not None:
        env["VNX_DATA_DIR"] = str(data_dir)
        env["VNX_DATA_DIR_EXPLICIT"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def parse_block(stdout: str) -> dict:
    """Assert stdout is exactly one block decision and return it."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {stdout!r}"
    decision = json.loads(lines[0])
    assert decision["decision"] == "block"
    assert decision.get("reason")
    return decision


class HookTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "vnx-data"
        self.wt = self.tmp / "wt"
        self.wt.mkdir()


class TestEnforcementMatrix(HookTestCase):
    """The required 5-case test matrix, end-to-end through the .sh launcher."""

    def test_a_bash_deny_command_is_blocked(self):
        # git push --force is denied by quality-engineer's bash_deny_patterns
        # (W2 resolution: git push* is NOT denied for this build role — only
        # git push --force* and git push -f*).
        res = run_hook(
            make_payload("Bash", {"command": "git push --force origin main"}),
            enforce=True,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        decision = parse_block(res.stdout)
        self.assertIn("git push --force*", decision["reason"])
        self.assertIn(ROLE, decision["reason"])

    def test_b_out_of_scope_file_write_is_blocked(self):
        target = self.wt / "docs" / "x.md"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        decision = parse_block(res.stdout)
        self.assertIn("docs/x.md", decision["reason"])
        self.assertIn("file_write_scope", decision["reason"])

    def test_c_in_scope_write_and_allowed_command_pass(self):
        # In-scope write (tests/** for test-engineer) -> allow (empty stdout).
        target = self.wt / "tests" / "test_x.py"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)

        # Allowed command (no bash_deny_patterns match) -> allow.
        res = run_hook(
            make_payload("Bash", {"command": "pytest -q"}),
            enforce=True,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)

    def test_d_flag_off_is_noop(self):
        """Flag unset AND explicitly '0': every case above passes through untouched."""
        payloads = [
            make_payload("Bash", {"command": "git push origin main"}),
            make_payload(
                "Write",
                {"file_path": str(self.wt / "docs" / "x.md")},
                cwd=str(self.wt),
            ),
            make_payload("Bash", {"command": "pytest -q"}),
        ]
        for flag_value in (None, "0"):
            extra = (
                {} if flag_value is None else {"VNX_ENFORCE_WORKER_PERMISSIONS": flag_value}
            )
            for payload in payloads:
                res = run_hook(
                    payload,
                    enforce=False,
                    data_dir=self.data_dir,
                    extra_env=extra,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(
                    res.stdout.strip(),
                    "",
                    f"flag OFF ({flag_value!r}) must be a no-op, got stdout: {res.stdout!r}",
                )
        # No audit events may be emitted when the flag is off.
        self.assertFalse((self.data_dir / "events").exists())

    def test_e_block_emits_audit_receipt(self):
        res = run_hook(
            make_payload("Bash", {"command": "git push --force origin main"}),
            enforce=True,
            data_dir=self.data_dir,
            extra_env={"VNX_CURRENT_DISPATCH_ID": "20260724-test-dispatch"},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        parse_block(res.stdout)

        events_file = self.data_dir / "events" / "worker_scope_block.ndjson"
        self.assertTrue(events_file.exists(), "block must emit a worker_scope_block audit event")
        records = [
            json.loads(ln)
            for ln in events_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["event_type"], "worker_scope_block")
        self.assertEqual(record["decision"], "block")
        self.assertEqual(record["tool_name"], "Bash")
        self.assertEqual(record["role"], ROLE)
        self.assertEqual(record["dispatch_id"], "20260724-test-dispatch")
        self.assertIn("git push --force*", record["reason"])


class TestEvaluateUnit(HookTestCase):
    """Unit coverage of evaluate() — in-process, no subprocess."""

    def _env(self, **overrides):
        env = {
            "VNX_ENFORCE_WORKER_PERMISSIONS": "1",
            "VNX_WORKER_ROLE": ROLE,
            # Prevent repo-level env vars from steering YAML resolution to the
            # main repo instead of the worktree-local .vnx/worker_permissions.yaml.
            "PROJECT_ROOT": "",
            "VNX_PROJECT_ROOT": "",
            "VNX_HOME": "",
        }
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    def test_flag_off_allows_deny_matching_command(self):
        payload = json.loads(make_payload("Bash", {"command": "git push origin main"}))
        with patch.dict(os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": "0"}, clear=False):
            self.assertEqual(hook.evaluate(payload), ("allow", None))

    def test_bash_deny_block(self):
        payload = json.loads(make_payload("Bash", {"command": "git push -f origin x"}))
        with self._env():
            decision, reason = hook.evaluate(payload)
        self.assertEqual(decision, "block")
        self.assertIn("bash_deny_patterns", reason)

    def test_multiedit_out_of_scope_block(self):
        payload = json.loads(
            make_payload("MultiEdit", {"file_path": "/wt/dashboard/app.py"}, cwd="/wt")
        )
        with self._env():
            decision, reason = hook.evaluate(payload)
        self.assertEqual(decision, "block")
        self.assertIn("dashboard/app.py", reason)

    def test_edit_in_scope_allow(self):
        payload = json.loads(
            make_payload("Edit", {"file_path": "/wt/tests/test_y.py"}, cwd="/wt")
        )
        with self._env():
            self.assertEqual(hook.evaluate(payload), ("allow", None))

    def test_write_outside_cwd_tree_is_blocked(self):
        """An absolute path outside the worktree can never match a project-relative scope glob."""
        payload = json.loads(
            make_payload("Write", {"file_path": "/etc/passwd"}, cwd="/wt")
        )
        with self._env():
            decision, _ = hook.evaluate(payload)
        self.assertEqual(decision, "block")

    def test_role_unset_falls_back_to_default_profile(self):
        """Without VNX_WORKER_ROLE the role-agnostic default profile carries no
        fine-grained deny/scope lists — the hook never invents restrictions."""
        payload = json.loads(make_payload("Bash", {"command": "git push origin main"}))
        with patch.dict(os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": "1"}, clear=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("VNX_WORKER_ROLE", None)
                self.assertEqual(hook.evaluate(payload), ("allow", None))

    def test_malformed_payloads_allow(self):
        with self._env():
            self.assertEqual(hook.evaluate({}), ("allow", None))
            self.assertEqual(hook.evaluate({"tool_name": "Bash"}), ("allow", None))
            self.assertEqual(
                hook.evaluate({"tool_name": "Bash", "tool_input": "nope"}),
                ("allow", None),
            )
            self.assertEqual(
                hook.evaluate({"tool_name": "Bash", "tool_input": {}}),
                ("allow", None),
            )

    def test_unrelated_tool_allow(self):
        payload = json.loads(make_payload("Read", {"file_path": "/wt/docs/x.md"}, cwd="/wt"))
        with self._env():
            self.assertEqual(hook.evaluate(payload), ("allow", None))

    def test_relative_to_cwd(self):
        self.assertEqual(
            hook._relative_to_cwd("/wt/scripts/a.py", "/wt"), "scripts/a.py"
        )
        # Outside cwd stays absolute so no project-relative glob can match it.
        self.assertEqual(
            hook._relative_to_cwd("/other/a.py", "/wt"), "/other/a.py"
        )
        # Relative paths and empty inputs pass through untouched.
        self.assertEqual(hook._relative_to_cwd("scripts/a.py", "/wt"), "scripts/a.py")
        self.assertEqual(hook._relative_to_cwd("/wt/a.py", ""), "/wt/a.py")


class TestHookContract(HookTestCase):
    """Hook contract: exit 0 always; stdout empty unless blocking."""

    def test_malformed_json_stdin_allow(self):
        res = run_hook("not json at all", enforce=True, data_dir=self.data_dir)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_empty_stdin_allow(self):
        res = run_hook("", enforce=True, data_dir=self.data_dir)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_core_missing_fails_open(self):
        """If the .py core is absent the launcher must exit 0 with no output."""
        import shutil
        import stat
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            shutil.copy(HOOK_SH, td_path / HOOK_SH.name)
            (td_path / HOOK_SH.name).chmod(
                (td_path / HOOK_SH.name).stat().st_mode | stat.S_IXUSR
            )
            res = subprocess.run(
                ["bash", str(td_path / HOOK_SH.name)],
                input=make_payload("Bash", {"command": "git push origin main"}),
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "VNX_ENFORCE_WORKER_PERMISSIONS": "1",
                    "VNX_WORKER_ROLE": ROLE,
                },
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.strip(), "")


class TestUnknownRoleFallback(HookTestCase):
    """Unknown role → fallback code-worker profile with restrictive file_write_scope.

    Proves the fallback is genuinely restrictive on the file-write dimension
    (depth-limited patterns, no blank-check empty list) while remaining
    functional for in-scope worktree writes and report-path writes.
    """

    UNKNOWN = "nonexistent-role-fallback-test"

    def test_out_of_scope_file_write_is_blocked_for_unknown_role(self):
        """An unknown role must NOT silently allow writes outside the depth-limited scope."""
        # 7 segments — the deepest pattern is */*/*/*/*/* (6 segments).
        target = self.wt / "a" / "b" / "c" / "d" / "e" / "f" / "out.md"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        decision = parse_block(res.stdout)
        self.assertIn("file_write_scope", decision["reason"])
        self.assertIn("code-worker", decision["reason"])

    def test_in_scope_worktree_write_allowed_for_unknown_role(self):
        """An unknown role can still write within the depth-limited scope."""
        target = self.wt / "src" / "app.py"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)

    def test_absolute_path_write_blocked_for_unknown_role(self):
        """An absolute path outside the worktree cannot match depth-limited patterns."""
        res = run_hook(
            make_payload("Write", {"file_path": "/etc/passwd"}, cwd=str(self.wt)),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        decision = parse_block(res.stdout)
        self.assertIn("file_write_scope", decision["reason"])

    def test_report_path_write_exempt_for_unknown_role(self):
        """A write to VNX_DATA_DIR/unified_reports/ is exempt from scope (report obligation)."""
        reports_dir = self.data_dir / "unified_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        target = reports_dir / "dispatch-X.md"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)

    def test_fallback_warning_fires_for_unknown_role(self):
        """The WARNING fires when falling back to code-worker for an unknown role."""
        res = run_hook(
            make_payload("Bash", {"command": "pytest -q"}),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("code-worker", res.stderr)

    def test_git_push_not_blocked_by_fallback_no_bash_deny(self):
        """The fallback profile has no bash_deny_patterns — git push is allowed.

        This is by design: the fallback code-worker needs to push. The restriction
        is on file_write_scope, not on bash commands.
        """
        res = run_hook(
            make_payload("Bash", {"command": "git push origin main"}),
            enforce=True,
            role=self.UNKNOWN,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)


class TestReportPathExemption(HookTestCase):
    """Report obligation: writes to $VNX_DATA_DIR/unified_reports/ are exempt from scope."""

    def test_known_role_report_write_exempt(self):
        """Even with a restrictive file_write_scope, report writes are allowed."""
        reports_dir = self.data_dir / "unified_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        target = reports_dir / "dispatch-X.md"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            role=ROLE,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", res.stdout)

    def test_known_role_non_report_outside_scope_still_blocked(self):
        """The exemption is narrow: non-report writes outside scope are still blocked."""
        target = self.wt / "docs" / "x.md"
        res = run_hook(
            make_payload("Write", {"file_path": str(target)}, cwd=str(self.wt)),
            enforce=True,
            role=ROLE,
            data_dir=self.data_dir,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        decision = parse_block(res.stdout)
        self.assertIn("docs/x.md", decision["reason"])


class TestSubprocessLaneMarker(HookTestCase):
    """E4 marker — subprocess lane (``claude -p``) hook discoverability.

    What this test proves (statically, in-PR):
      1. The subprocess lane (scripts/lib/subprocess_adapter.py) never passes a
         ``--settings`` flag, so a headless ``claude -p`` spawn falls back to the
         same cwd-based settings discovery that the spike proved live for the
         tmux lane (E1/E2) — the discovery mechanism is identical by construction.
      2. The registration written at worktree allocation anchors the hook at the
         FABRIC install root (OI-1089 finding 1): the worker-scope script ships
         only with the fabric, so the command bakes in the fabric-absolute path
         (vnx_paths._resolve_vnx_home) and never resolves against a
         consumer/worktree git top-level that lacks ``scripts/hooks/``.
      3. The hook assets the registration points at exist and are executable.

    What remains DEFERRED (explicit, per spike E4 recommendation): a live-fire
    marker test proving a PreToolUse hook actually fires inside a real
    ``claude -p`` process. That cannot be proven in this PR: it requires spawning
    a live provider subprocess, which (a) is blocked by design inside this repo by
    scripts/hooks/pretooluse_block_raw_claude_spawn.sh (raw claude -p spawns are
    governance-bypass blocks), and (b) consumes provider quota for a question the
    spike already scoped to a dedicated subprocess-lane dispatch. Until that
    dispatch runs, treat subprocess-lane enforcement as assume-until-proven: the
    coarse launch-time posture (--allowedTools/--permission-mode, ADR-012) remains
    the enforcement layer of record for that lane.
    """

    def test_subprocess_lane_has_no_settings_flag(self):
        src = (REPO_ROOT / "scripts" / "lib" / "subprocess_adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "--settings",
            src,
            "subprocess lane must not override settings discovery with --settings; "
            "cwd-based discovery is the mechanism the worktree hook wiring relies on",
        )

    def test_hook_registration_anchors_at_fabric_root(self):
        from tmux_interactive_dispatch import _worker_scope_hook_entry

        sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
        entry = _worker_scope_hook_entry()
        command = entry["hooks"][0]["command"]
        # OI-1089 finding 1: the hook ships only with the fabric. The command
        # bakes in the fabric-absolute path at registration and must never fall
        # back to a consumer/worktree git top-level that lacks scripts/hooks/.
        self.assertNotIn(
            "git rev-parse",
            command,
            "hook command must not resolve against the firing cwd's git top-level",
        )
        self.assertIn(
            "scripts/hooks/pretooluse_worker_scope_enforce.sh",
            command,
            "hook command must point at the fabric hook artifact",
        )
        self.assertIn(
            "MISSING",
            command,
            "a missing fabric artifact must fail loud instead of silently no-oping",
        )
        self.assertEqual(entry["matcher"], "Bash|Write|Edit|MultiEdit")

    def test_hook_assets_exist_and_launcher_is_executable(self):
        self.assertTrue(HOOK_PY.exists())
        self.assertTrue(HOOK_SH.exists())
        self.assertTrue(
            os.access(HOOK_SH, os.X_OK),
            "launcher must be executable for the settings registration to work",
        )


if __name__ == "__main__":
    unittest.main()
