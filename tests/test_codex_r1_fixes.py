#!/usr/bin/env python3
"""
Regression tests for Codex round-1 blocking findings on PR #316.

Finding 1: _cleanup_stuck_dispatches bypassed receipt path (release_terminal_claim
           and runtime_core_cli release-on-receipt) when moving old active files.
Finding 2: prepare_dispatch_payload ran terminal mode setup (configure_terminal_mode)
           before acquire_dispatch_lease, so lease/validation failures could wipe
           a worker terminal without delivering a dispatch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPATCHER_SH = REPO_ROOT / "scripts" / "dispatcher_v8_minimal.sh"
DISPATCH_CREATE_SH = REPO_ROOT / "scripts" / "lib" / "dispatch_create.sh"


# ---------------------------------------------------------------------------
# Finding 1 — _cleanup_stuck_dispatches
# ---------------------------------------------------------------------------

class TestCleanupStuckDispatches:
    """_cleanup_stuck_dispatches must release lease/claim before moving files."""

    def test_bash_syntax_dispatcher(self):
        """dispatcher_v8_minimal.sh passes bash -n after fix."""
        result = subprocess.run(
            ["bash", "-n", str(DISPATCHER_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"

    def test_cleanup_calls_release_terminal_claim(self):
        """_cleanup_stuck_dispatches body calls release_terminal_claim before mv."""
        text = DISPATCHER_SH.read_text()
        idx = text.find("_cleanup_stuck_dispatches()")
        assert idx != -1, "_cleanup_stuck_dispatches function not found"

        # Find the closing brace of the function
        brace_depth = 0
        func_body_start = text.find("{", idx)
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        assert "release_terminal_claim" in func_body, (
            "_cleanup_stuck_dispatches must call release_terminal_claim "
            "before moving a stuck dispatch to completed"
        )

    def test_cleanup_calls_release_on_receipt(self):
        """_cleanup_stuck_dispatches body calls release-on-receipt before mv."""
        text = DISPATCHER_SH.read_text()
        idx = text.find("_cleanup_stuck_dispatches()")
        assert idx != -1

        func_body_start = text.find("{", idx)
        brace_depth = 0
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        assert "release-on-receipt" in func_body, (
            "_cleanup_stuck_dispatches must call runtime_core_cli release-on-receipt "
            "to clear the canonical lease before moving a stuck dispatch to completed"
        )

    def test_cleanup_release_before_mv(self):
        """release_terminal_claim and release-on-receipt appear before mv in function body."""
        text = DISPATCHER_SH.read_text()
        idx = text.find("_cleanup_stuck_dispatches()")
        func_body_start = text.find("{", idx)
        brace_depth = 0
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        release_pos = func_body.find("release_terminal_claim")
        receipt_pos = func_body.find("release-on-receipt")
        mv_pos = func_body.rfind('mv "$stuck_file"')

        assert release_pos != -1 and release_pos < mv_pos, (
            "release_terminal_claim must appear before the mv command"
        )
        assert receipt_pos != -1 and receipt_pos < mv_pos, (
            "release-on-receipt must appear before the mv command"
        )


# ---------------------------------------------------------------------------
# Finding 2 — prepare_dispatch_payload before acquire_dispatch_lease
# ---------------------------------------------------------------------------

class TestPayloadLeaseOrdering:
    """Terminal mode I/O must be deferred until after the lease is acquired."""

    def test_bash_syntax_dispatch_create(self):
        """dispatch_create.sh passes bash -n after fix."""
        result = subprocess.run(
            ["bash", "-n", str(DISPATCH_CREATE_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"

    def test_pdp_resolve_target_does_not_call_configure_terminal_mode(self):
        """_pdp_resolve_target must not call configure_terminal_mode directly."""
        text = DISPATCH_CREATE_SH.read_text()
        idx = text.find("_pdp_resolve_target()")
        assert idx != -1, "_pdp_resolve_target function not found"

        func_body_start = text.find("{", idx)
        brace_depth = 0
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        assert "configure_terminal_mode" not in func_body, (
            "_pdp_resolve_target must NOT call configure_terminal_mode — "
            "terminal I/O must be deferred to _pdp_apply_terminal_mode_setup "
            "which is called post-lease by dispatch_with_skill_activation"
        )

    def test_pdp_apply_terminal_mode_setup_exists(self):
        """_pdp_apply_terminal_mode_setup function must exist in dispatch_create.sh."""
        text = DISPATCH_CREATE_SH.read_text()
        assert "_pdp_apply_terminal_mode_setup()" in text, (
            "_pdp_apply_terminal_mode_setup must be defined in dispatch_create.sh "
            "to hold the deferred post-lease terminal I/O steps"
        )

    def test_pdp_apply_terminal_mode_setup_contains_io(self):
        """_pdp_apply_terminal_mode_setup must contain the deferred I/O calls."""
        text = DISPATCH_CREATE_SH.read_text()
        idx = text.find("_pdp_apply_terminal_mode_setup()")
        assert idx != -1

        func_body_start = text.find("{", idx)
        brace_depth = 0
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        for expected in ("reset_terminal_context", "switch_terminal_model", "activate_terminal_mode"):
            assert expected in func_body, (
                f"_pdp_apply_terminal_mode_setup must call {expected}"
            )

    def test_dispatch_with_skill_activation_calls_post_lease_setup(self):
        """dispatch_with_skill_activation must call _pdp_apply_terminal_mode_setup after acquire_dispatch_lease."""
        text = DISPATCHER_SH.read_text()
        idx = text.find("dispatch_with_skill_activation()")
        assert idx != -1

        func_body_start = text.find("{", idx)
        brace_depth = 0
        end = func_body_start
        for i, ch in enumerate(text[func_body_start:], func_body_start):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end = i
                    break
        func_body = text[func_body_start:end]

        assert "_pdp_apply_terminal_mode_setup" in func_body, (
            "dispatch_with_skill_activation must call _pdp_apply_terminal_mode_setup "
            "after acquire_dispatch_lease so that terminal wipe cannot occur on lease failure"
        )

        # The post-lease setup call must appear after acquire_dispatch_lease
        acquire_pos = func_body.find("acquire_dispatch_lease")
        setup_pos = func_body.find("_pdp_apply_terminal_mode_setup")
        assert acquire_pos != -1 and setup_pos != -1
        assert setup_pos > acquire_pos, (
            "_pdp_apply_terminal_mode_setup must be called AFTER acquire_dispatch_lease"
        )

    def test_needs_mode_setup_flag_in_dispatch_create(self):
        """_PDP_NEEDS_MODE_SETUP global must be set in dispatch_create.sh."""
        text = DISPATCH_CREATE_SH.read_text()
        assert "_PDP_NEEDS_MODE_SETUP" in text, (
            "_PDP_NEEDS_MODE_SETUP flag must be defined so dispatch_with_skill_activation "
            "can gate the post-lease terminal setup step"
        )
