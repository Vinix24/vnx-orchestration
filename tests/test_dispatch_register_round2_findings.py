"""Regression tests for round-2 codex findings against PR #302.

Three findings were flagged after the round-1 fix:

1. ``finalize_dispatch_delivery`` unconditionally treated delivery as
   successful even when ``rc_delivery_success`` failed — leaving local fs
   saying "active" while the broker still said "delivering" or had
   rejected the dispatch.
2. ``finalize_dispatch_delivery`` moved ``pending/`` → ``active/`` without a
   reliable matching ``dispatch_promoted`` emit (best-effort emit happened
   AFTER the mv with stderr suppressed; transient register failures left
   register-driven views misclassifying the dispatch).
3. ``queue_auto_accept.sh`` recorded ``dispatch_created`` AFTER the mv with
   stderr suppressed — same class of problem: a transient register failure
   leaves the dispatch invisible to register-backed reporting forever.

The fixes implement:
  - ``rc_delivery_success`` returns non-zero on broker rejection or CLI
    failure (idempotent no-op still returns zero).
  - ``finalize_dispatch_delivery`` checks the return code: if the broker did
    not confirm acceptance, it appends ``dispatch_failed`` to the register
    and refuses to mv to active/ — local state stays consistent with the
    broker.
  - ``dispatch_promoted`` is emitted BEFORE the mv, with captured stderr and
    structured failure logging on emit failure.
  - ``dispatch_created`` in ``queue_auto_accept.sh`` is emitted BEFORE the
    mv, with the same captured-stderr / surfaced-failure pattern.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIFECYCLE_SH = PROJECT_ROOT / "scripts" / "lib" / "dispatch_lifecycle.sh"
QUEUE_SH = PROJECT_ROOT / "scripts" / "queue_auto_accept.sh"


# ---------------------------------------------------------------------------
# Helpers — extract a single bash function body for isolated sourcing
# ---------------------------------------------------------------------------

def _extract_function(source_path: Path, fn_name: str) -> str:
    """Return the text of a single bash function, top-level brace-balanced."""
    text = source_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(fn_name)}\s*\(\s*\)\s*\{{", re.MULTILINE)
    m = pattern.search(text)
    assert m, f"Function {fn_name} not found in {source_path}"
    start = m.start()
    depth = 0
    i = m.end() - 1  # position of the opening {
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise AssertionError(f"Unbalanced braces extracting {fn_name}")


# ---------------------------------------------------------------------------
# Finding 1: rc_delivery_success return-code contract
# ---------------------------------------------------------------------------

def _run_rc_delivery_success(tmp_path: Path, mock_stdout: str, mock_rc: int) -> dict:
    """Source rc_delivery_success with a stubbed _rc_python and capture behavior.

    Returns a dict with keys: rc (int), failures (list of structured-failure codes),
    log_lines (list of plain log lines).
    """
    fn_body = _extract_function(LIFECYCLE_SH, "rc_delivery_success")
    failures_log = tmp_path / "failures.log"
    log_log = tmp_path / "log.log"

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        log() {{ echo "$1" >> "{log_log}"; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
        _rc_enabled() {{ return 0; }}
        _rc_python() {{
            cat <<'__EOF__'
{mock_stdout}
__EOF__
            return {mock_rc}
        }}

{fn_body}

        rc_delivery_success "test-d-001" "attempt-xyz"
        echo "RC=$?"
        """
    )

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    rc_match = re.search(r"RC=(\d+)", proc.stdout)
    rc_val = int(rc_match.group(1)) if rc_match else -1
    failures = (
        failures_log.read_text(encoding="utf-8").splitlines()
        if failures_log.exists()
        else []
    )
    log_lines = (
        log_log.read_text(encoding="utf-8").splitlines() if log_log.exists() else []
    )
    return {"rc": rc_val, "failures": failures, "log_lines": log_lines}


class TestRcDeliverySuccessReturnContract:
    """rc_delivery_success must signal failure to its caller."""

    def test_real_success_returns_zero(self, tmp_path):
        result = _run_rc_delivery_success(
            tmp_path, json.dumps({"success": True, "noop": False}), 0
        )
        assert result["rc"] == 0
        assert result["failures"] == []

    def test_idempotent_noop_returns_zero(self, tmp_path):
        """Duplicate acceptance (broker already accepted) is success from caller's perspective."""
        result = _run_rc_delivery_success(
            tmp_path, json.dumps({"success": True, "noop": True}), 0
        )
        assert result["rc"] == 0
        assert result["failures"] == []
        assert any("idempotent no-op" in line for line in result["log_lines"])

    def test_terminal_state_rejection_returns_nonzero(self, tmp_path):
        """noop_rejected (broker says terminal state forbids the transition) is NOT success."""
        result = _run_rc_delivery_success(
            tmp_path, json.dumps({"noop_rejected": True}), 1
        )
        assert result["rc"] != 0, (
            "Broker rejection must propagate — caller must refuse to mark dispatch active"
        )
        assert any("delivery_success_rejected" in line for line in result["failures"])

    def test_hard_cli_failure_returns_nonzero(self, tmp_path):
        """A real CLI/runtime-core error must propagate as non-zero."""
        result = _run_rc_delivery_success(tmp_path, "{}", 2)
        assert result["rc"] != 0
        assert any(
            "delivery_success_record_failed" in line for line in result["failures"]
        )


# ---------------------------------------------------------------------------
# Finding 1+2: finalize_dispatch_delivery flow contract
# ---------------------------------------------------------------------------

def _run_finalize(
    tmp_path: Path,
    rc_delivery_success_rc: int,
    register_rc: int = 0,
) -> dict:
    """Run finalize_dispatch_delivery with stubbed dependencies.

    Records: register CLI calls (events + ordering), mv calls (with timestamp),
    structured failures, and the function's return code. Allows inspecting that
    dispatch_promoted is emitted BEFORE the mv, and dispatch_failed instead of
    promoted/mv on failure.
    """
    fn_body = _extract_function(LIFECYCLE_SH, "finalize_dispatch_delivery")
    actions_log = tmp_path / "actions.log"
    failures_log = tmp_path / "failures.log"

    pending_dir = tmp_path / "pending"
    active_dir = tmp_path / "active"
    pending_dir.mkdir()
    active_dir.mkdir()

    dispatch_file = pending_dir / "test-d-001.md"
    dispatch_file.write_text("# stub dispatch\n", encoding="utf-8")

    # Fake VNX_DIR points at a directory containing scripts/lib/dispatch_register.py
    # We use a stub register CLI that just logs the call, so it must live under
    # scripts/lib/dispatch_register.py from the fake root.
    fake_vnx = tmp_path / "vnx"
    (fake_vnx / "scripts" / "lib").mkdir(parents=True)
    (fake_vnx / "scripts").mkdir(exist_ok=True)
    register_stub = fake_vnx / "scripts" / "lib" / "dispatch_register.py"
    register_stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys, time
            with open("{actions_log}", "a", encoding="utf-8") as fh:
                fh.write(f"REGISTER\\t{{time.time_ns()}}\\t{{' '.join(sys.argv[1:])}}\\n")
            sys.exit({register_rc})
            """
        ),
        encoding="utf-8",
    )
    register_stub.chmod(0o755)

    notify_stub = fake_vnx / "scripts" / "notify_dispatch.py"
    notify_stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    notify_stub.chmod(0o755)

    progress_stub = fake_vnx / "scripts" / "update_progress_state.py"
    progress_stub.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n", encoding="utf-8")
    progress_stub.chmod(0o755)

    metadata_stub = fake_vnx / "scripts" / "log_dispatch_metadata.py"
    metadata_stub.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(0)\n", encoding="utf-8")
    metadata_stub.chmod(0o755)

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        VNX_DIR="{fake_vnx}"
        ACTIVE_DIR="{active_dir}"
        _DL_RC_ATTEMPT_ID="attempt-xyz"

        log() {{ true; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}

        # Wrap mv to record exact timestamp/order vs register calls
        real_mv() {{ command mv "$@"; }}
        mv() {{
            local ts
            ts=$(python3 -c 'import time; print(time.time_ns())')
            echo "MV"$'\\t'"$ts"$'\\t'"$*" >> "{actions_log}"
            real_mv "$@"
        }}

        # Stub helpers that finalize_dispatch_delivery calls
        _fdd_update_progress_state() {{ true; }}
        _fdd_log_dispatch_metadata() {{ true; }}

        # Stub rc_delivery_success with caller-controlled return code
        rc_delivery_success() {{ return {rc_delivery_success_rc}; }}

{fn_body}

        finalize_dispatch_delivery \\
            "{dispatch_file}" "A" "T1" "test-d-001" \\
            "" "PR0" "backend-developer" "stub instruction" ""
        echo "RC=$?"
        """
    )

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    rc_match = re.search(r"RC=(\d+)", proc.stdout)
    rc_val = int(rc_match.group(1)) if rc_match else -1
    actions = (
        actions_log.read_text(encoding="utf-8").splitlines()
        if actions_log.exists()
        else []
    )
    failures = (
        failures_log.read_text(encoding="utf-8").splitlines()
        if failures_log.exists()
        else []
    )
    return {
        "rc": rc_val,
        "actions": actions,
        "failures": failures,
        "active_dir": active_dir,
        "pending_dir": pending_dir,
        "dispatch_file": dispatch_file,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


class TestFinalizeDeliveryAcceptedPath:
    """When the broker confirms acceptance, finalize must promote AND move."""

    def test_returns_zero_and_moves_to_active(self, tmp_path):
        r = _run_finalize(tmp_path, rc_delivery_success_rc=0)
        assert r["rc"] == 0, f"Expected success rc=0, got {r['rc']}\n{r['stdout']}\n{r['stderr']}"
        assert (r["active_dir"] / "test-d-001.md").exists()
        assert not r["dispatch_file"].exists()

    def test_dispatch_promoted_emitted_before_mv(self, tmp_path):
        """Order-of-operations: dispatch_promoted register entry must precede the mv.

        If emit happens after mv and the emit fails, register-driven views
        will see a dispatch in active/ with no canonical promotion event —
        exactly the misclassification codex flagged.
        """
        r = _run_finalize(tmp_path, rc_delivery_success_rc=0)
        promote_ts = None
        mv_ts = None
        for line in r["actions"]:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            kind, ts, payload = parts[0], parts[1], parts[2]
            if kind == "REGISTER" and "dispatch_promoted" in payload and promote_ts is None:
                promote_ts = int(ts)
            if kind == "MV" and mv_ts is None:
                mv_ts = int(ts)
        assert promote_ts is not None, (
            f"dispatch_promoted register call not found. Actions: {r['actions']}"
        )
        assert mv_ts is not None, f"mv not found. Actions: {r['actions']}"
        assert promote_ts < mv_ts, (
            f"dispatch_promoted ({promote_ts}) must be emitted BEFORE mv ({mv_ts}). "
            f"Actions: {r['actions']}"
        )

    def test_no_dispatch_failed_on_success(self, tmp_path):
        r = _run_finalize(tmp_path, rc_delivery_success_rc=0)
        register_calls = [line for line in r["actions"] if line.startswith("REGISTER\t")]
        for line in register_calls:
            assert "dispatch_failed" not in line, (
                f"dispatch_failed must not be emitted on success path: {line}"
            )

    def test_register_emit_failure_surfaces_structured_failure(self, tmp_path):
        """If dispatch_promoted emit fails, log_structured_failure must fire so the
        broker/register inconsistency is visible in audit (not silent)."""
        r = _run_finalize(tmp_path, rc_delivery_success_rc=0, register_rc=1)
        assert any("register_emit_failed" in line for line in r["failures"]), (
            f"Expected register_emit_failed in structured failures. Got: {r['failures']}"
        )


class TestFinalizeDeliveryRejectedPath:
    """When the broker did NOT confirm acceptance, finalize must fail closed."""

    def test_returns_nonzero(self, tmp_path):
        r = _run_finalize(tmp_path, rc_delivery_success_rc=1)
        assert r["rc"] != 0, "finalize must propagate rc_delivery_success failure"

    def test_does_not_move_to_active(self, tmp_path):
        r = _run_finalize(tmp_path, rc_delivery_success_rc=1)
        assert r["dispatch_file"].exists(), (
            "Dispatch must remain in pending/ when broker did not confirm acceptance"
        )
        assert not (r["active_dir"] / "test-d-001.md").exists(), (
            "Dispatch must NOT be moved to active/ when delivery-success failed"
        )

    def test_no_dispatch_promoted_on_failure(self, tmp_path):
        """If we never confirmed acceptance, we must not falsely emit dispatch_promoted."""
        r = _run_finalize(tmp_path, rc_delivery_success_rc=1)
        register_calls = [line for line in r["actions"] if line.startswith("REGISTER\t")]
        for line in register_calls:
            assert "dispatch_promoted" not in line, (
                f"dispatch_promoted must not be emitted on failed delivery: {line}"
            )

    def test_emits_dispatch_failed_event(self, tmp_path):
        """register-driven views should see dispatch_failed when the broker rejected."""
        r = _run_finalize(tmp_path, rc_delivery_success_rc=1)
        assert any(
            "dispatch_failed" in line and "delivery_success_unconfirmed" in line
            for line in r["actions"]
        ), (
            f"Expected dispatch_failed register emit with delivery_success_unconfirmed reason. "
            f"Actions: {r['actions']}"
        )


# ---------------------------------------------------------------------------
# Finding 3: queue_auto_accept emits dispatch_created BEFORE the mv
# ---------------------------------------------------------------------------

class TestQueueAutoAcceptOrdering:
    """The dispatch_created emit must precede the mv so the register can never
    miss a dispatch that successfully landed in pending/."""

    def test_dispatch_created_emit_precedes_mv(self):
        """Source-level check: the python register call appears BEFORE the mv
        within the for-loop body that promotes queue/ → pending/."""
        source = QUEUE_SH.read_text(encoding="utf-8")

        # Locate the inner for loop body
        for_match = re.search(r"for f in \"\$QUEUE_DIR\"/\*\.md", source)
        assert for_match, "Queue iteration loop not found"
        body = source[for_match.start() :]
        # Find the dispatch_created emit (python invocation) and the mv call
        emit_match = re.search(
            r"python3.*dispatch_register\.py.*append\s+dispatch_created", body
        )
        # The mv that promotes the file (after the dedup early-return) — the FIRST
        # mv in the body is the queue→pending mv
        mv_match = re.search(r"^\s*mv\s+\"\$f\"\s+\"\$target\"", body, re.MULTILINE)
        assert emit_match, "dispatch_created emit not found in queue_auto_accept loop"
        assert mv_match, "queue→pending mv not found in queue_auto_accept loop"
        assert emit_match.start() < mv_match.start(), (
            "dispatch_created emit must appear BEFORE the queue→pending mv. "
            "Otherwise a transient register-write failure leaves the dispatch "
            "in pending/ but invisible to register-backed reporting forever."
        )

    def test_emit_captures_stderr_for_diagnostics(self):
        """The emit must capture stderr (not silently >/dev/null) so diagnostic
        output is preserved when the python invocation fails."""
        source = QUEUE_SH.read_text(encoding="utf-8")
        # Find the dispatch_created emit block
        block_match = re.search(
            r"python3 \"\$VNX_HOME/scripts/lib/dispatch_register\.py\" append dispatch_created[^\n]*\n[^\n]*\n",
            source,
        )
        assert block_match, "dispatch_created emit block not found"
        block = block_match.group(0)
        assert "2>&1" in block or "_reg_stderr" in block, (
            "dispatch_created emit must capture stderr (e.g. via 2>&1) so that "
            "register-write diagnostics are preserved on failure. Block:\n" + block
        )

    def test_emit_failure_logs_warning_with_diagnostic_info(self):
        """Failure-handling block must echo the captured stderr / rc for diagnosis."""
        source = QUEUE_SH.read_text(encoding="utf-8")
        # The warning echo on _reg_rc != 0
        warning_match = re.search(
            r'echo "\[auto-accept\] WARNING: dispatch_created emit failed[^"]+"',
            source,
        )
        assert warning_match, "Warning echo for failed dispatch_created emit not found"
        warning = warning_match.group(0)
        assert "$_reg_rc" in warning or "rc=" in warning, (
            "Warning must include the rc / stderr from the failed emit so the "
            "operator has actionable diagnostic info: " + warning
        )


# ---------------------------------------------------------------------------
# End-to-end: run queue_auto_accept's loop body once and verify ordering
# ---------------------------------------------------------------------------

class TestQueueAutoAcceptRuntimeOrdering:
    """Execute the queue_auto_accept loop body with stubs and verify that
    register emit happens before the mv at runtime (not just textually)."""

    def test_runtime_emit_before_mv(self, tmp_path):
        actions_log = tmp_path / "actions.log"
        queue_dir = tmp_path / "queue"
        pending_dir = tmp_path / "pending"
        queue_dir.mkdir()
        pending_dir.mkdir()
        dispatch_id = "runtime-order-001"
        (queue_dir / f"{dispatch_id}.md").write_text("# stub\n", encoding="utf-8")

        fake_vnx_home = tmp_path / "vnx_home"
        (fake_vnx_home / "scripts" / "lib").mkdir(parents=True)
        register_stub = fake_vnx_home / "scripts" / "lib" / "dispatch_register.py"
        register_stub.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import sys, time
                with open("{actions_log}", "a", encoding="utf-8") as fh:
                    fh.write(f"REGISTER\\t{{time.time_ns()}}\\t{{' '.join(sys.argv[1:])}}\\n")
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        register_stub.chmod(0o755)

        # Extract just the loop body (the for ... done block) and run it once
        source = QUEUE_SH.read_text(encoding="utf-8")
        loop_match = re.search(
            r"(for f in \"\$QUEUE_DIR\"/\*\.md; do.*?^\s*done)",
            source,
            re.DOTALL | re.MULTILINE,
        )
        assert loop_match, "Could not extract for-loop body"
        loop_body = loop_match.group(1)

        script = textwrap.dedent(
            f"""\
            #!/bin/bash
            set -uo pipefail

            QUEUE_DIR="{queue_dir}"
            PENDING_DIR="{pending_dir}"
            VNX_HOME="{fake_vnx_home}"
            moved=0

            real_mv() {{ command mv "$@"; }}
            mv() {{
                local ts
                ts=$(python3 -c 'import time; print(time.time_ns())')
                echo "MV"$'\\t'"$ts"$'\\t'"$*" >> "{actions_log}"
                real_mv "$@"
            }}

{loop_body}
            """
        )

        subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

        actions = actions_log.read_text(encoding="utf-8").splitlines()
        register_ts = None
        mv_ts = None
        for line in actions:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            kind, ts, payload = parts[0], parts[1], parts[2]
            if kind == "REGISTER" and "dispatch_created" in payload and register_ts is None:
                register_ts = int(ts)
            if kind == "MV" and mv_ts is None:
                mv_ts = int(ts)
        assert register_ts is not None, f"dispatch_created register call not made. Actions: {actions}"
        assert mv_ts is not None, f"mv not made. Actions: {actions}"
        assert register_ts < mv_ts, (
            f"At runtime, dispatch_created ({register_ts}) must be emitted "
            f"BEFORE the mv ({mv_ts}). Actions: {actions}"
        )
        # And the mv actually moved the file
        assert (pending_dir / f"{dispatch_id}.md").exists()
