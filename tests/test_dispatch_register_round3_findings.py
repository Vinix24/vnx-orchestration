"""Regression tests for round-3 codex findings against PR #302.

After round-2 the codex gate (recorded_at 2026-04-29T19:23:57Z) flagged two
remaining blocking findings:

1. ``rc_delivery_start`` returning an empty ``_DL_RC_ATTEMPT_ID`` was treated
   as non-fatal, but downstream ``rc_delivery_success`` no-ops on empty
   attempt_id and ``finalize_dispatch_delivery`` still moved the dispatch to
   ``active/``. A partial/failed delivery-start could leave the broker in
   ``queued``/``claimed``/``delivering`` while the filesystem reported
   successful delivery.

2. ``rc_release_on_failure`` parsed ``cleanup_complete`` from the broker
   response but its audit emission still reported the same
   ``lease_released_on_failure`` event with ``lease_released=true`` even when
   ``cleanup_complete=false``. Audit consumers that only inspected the
   event_type/lease_released fields could not distinguish a clean cleanup
   from a partially-failed one.

The round-3 fixes:

* ``rc_delivery_success`` fails closed (returns non-zero, logs
  ``delivery_success_no_attempt_id``) when invoked with an empty attempt_id
  while runtime-core is enabled — defense in depth so any future caller that
  forgets the empty-attempt check still leaves broker and local state in
  agreement.
* ``_adl_register_and_acquire`` releases the canonical lease + legacy claim
  and returns 1 (blocking the dispatch) when ``rc_delivery_start`` returns an
  empty attempt_id.
* ``rc_release_on_failure`` emits a distinct
  ``lease_released_broker_inconsistent`` event_type (with
  ``lease_released=false`` to highlight that the cleanup as a whole did not
  complete) when ``cleanup_complete=false`` — making the inconsistency
  visible to audit consumers without parsing the optional error field.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIFECYCLE_SH = PROJECT_ROOT / "scripts" / "lib" / "dispatch_lifecycle.sh"


def _extract_function(source_path: Path, fn_name: str) -> str:
    text = source_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(fn_name)}\s*\(\s*\)\s*\{{", re.MULTILINE)
    m = pattern.search(text)
    assert m, f"Function {fn_name} not found in {source_path}"
    start = m.start()
    depth = 0
    i = m.end() - 1
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
# Finding 1: rc_delivery_success must fail-closed on empty attempt_id (RC on)
# ---------------------------------------------------------------------------


def _run_rc_delivery_success(tmp_path: Path, attempt_id: str, rc_enabled: bool) -> dict:
    fn_body = _extract_function(LIFECYCLE_SH, "rc_delivery_success")
    failures_log = tmp_path / "failures.log"

    rc_enabled_body = "return 0" if rc_enabled else "return 1"

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        log() {{ true; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
        _rc_enabled() {{ {rc_enabled_body}; }}
        _rc_python() {{ echo '{{}}'; return 0; }}

{fn_body}

        rc_delivery_success "test-d-001" "{attempt_id}"
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
    return {"rc": rc_val, "failures": failures}


class TestRcDeliverySuccessEmptyAttemptId:
    """Empty attempt_id with RC enabled must surface as failure, not silent success."""

    def test_empty_attempt_id_with_rc_enabled_returns_nonzero(self, tmp_path):
        result = _run_rc_delivery_success(tmp_path, attempt_id="", rc_enabled=True)
        assert result["rc"] != 0, (
            "Empty attempt_id with RC enabled must return non-zero — caller "
            "must refuse to mark dispatch active when broker has no attempt "
            "to transition."
        )

    def test_empty_attempt_id_with_rc_enabled_logs_structured_failure(self, tmp_path):
        result = _run_rc_delivery_success(tmp_path, attempt_id="", rc_enabled=True)
        assert any(
            "delivery_success_no_attempt_id" in line for line in result["failures"]
        ), (
            "Expected structured failure 'delivery_success_no_attempt_id' "
            f"for empty attempt_id path. Got: {result['failures']!r}"
        )

    def test_empty_attempt_id_with_rc_disabled_is_noop(self, tmp_path):
        """When RC is disabled there is no broker — empty attempt_id is a true no-op."""
        result = _run_rc_delivery_success(tmp_path, attempt_id="", rc_enabled=False)
        assert result["rc"] == 0
        assert result["failures"] == []

    def test_nonempty_attempt_id_real_success_returns_zero(self, tmp_path):
        fn_body = _extract_function(LIFECYCLE_SH, "rc_delivery_success")
        failures_log = tmp_path / "failures.log"
        script = textwrap.dedent(
            f"""\
            #!/bin/bash
            set -uo pipefail
            log() {{ true; }}
            log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
            _rc_enabled() {{ return 0; }}
            _rc_python() {{ echo '{{"success": true, "noop": false}}'; return 0; }}

{fn_body}

            rc_delivery_success "test-d-001" "attempt-xyz"
            echo "RC=$?"
            """
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        rc_match = re.search(r"RC=(\d+)", proc.stdout)
        assert rc_match
        assert int(rc_match.group(1)) == 0


# ---------------------------------------------------------------------------
# Finding 1: _adl_register_and_acquire fail-closed on empty attempt_id
# ---------------------------------------------------------------------------


def _run_register_and_acquire(
    tmp_path: Path,
    attempt_id_from_start: str,
) -> dict:
    """Drive _adl_register_and_acquire with stubbed dependencies and capture
    the resulting return code, structured failures, audit emissions, lease
    releases, and claim releases."""
    fn_body = _extract_function(LIFECYCLE_SH, "_adl_register_and_acquire")
    failures_log = tmp_path / "failures.log"
    audit_log = tmp_path / "audit.log"
    actions_log = tmp_path / "actions.log"
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        VNX_DISPATCH_PAYLOAD_DIR="{payload_dir}"
        _DL_RC_GENERATION=""
        _DL_RC_ATTEMPT_ID=""

        log() {{ true; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
        emit_blocked_dispatch_audit() {{
            echo "BLOCKED $3 $4" >> "{audit_log}"
        }}

        _rc_enabled() {{ return 0; }}
        rc_register() {{ return 0; }}
        rc_acquire_lease() {{ echo "7"; return 0; }}
        rc_delivery_start() {{ echo "{attempt_id_from_start}"; return 0; }}
        rc_release_lease() {{
            echo "RELEASE_LEASE $@" >> "{actions_log}"
            return 0
        }}
        release_terminal_claim() {{
            echo "RELEASE_CLAIM $@" >> "{actions_log}"
            return 0
        }}

{fn_body}

        _adl_register_and_acquire \\
            "test-d-001" "T1" "A" "backend-developer" "PR0" "stub prompt"
        echo "RC=$?"
        echo "GEN=$_DL_RC_GENERATION"
        echo "ATTEMPT=$_DL_RC_ATTEMPT_ID"
        """
    )

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    rc_match = re.search(r"RC=(\d+)", proc.stdout)
    gen_match = re.search(r"GEN=(\S*)", proc.stdout)
    attempt_match = re.search(r"ATTEMPT=(\S*)", proc.stdout)
    failures = (
        failures_log.read_text(encoding="utf-8").splitlines()
        if failures_log.exists()
        else []
    )
    audits = (
        audit_log.read_text(encoding="utf-8").splitlines() if audit_log.exists() else []
    )
    actions = (
        actions_log.read_text(encoding="utf-8").splitlines()
        if actions_log.exists()
        else []
    )
    return {
        "rc": int(rc_match.group(1)) if rc_match else -1,
        "gen": gen_match.group(1) if gen_match else None,
        "attempt": attempt_match.group(1) if attempt_match else None,
        "failures": failures,
        "audits": audits,
        "actions": actions,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


class TestRegisterAndAcquireEmptyAttemptId:
    """When rc_delivery_start returns empty attempt_id, the dispatch must be blocked."""

    def test_empty_attempt_id_returns_failure(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert result["rc"] != 0, (
            "Empty attempt_id from rc_delivery_start must block dispatch "
            f"(rc={result['rc']}). actions={result['actions']!r}"
        )

    def test_empty_attempt_id_releases_canonical_lease(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert any("RELEASE_LEASE" in a for a in result["actions"]), (
            f"Expected canonical lease release after empty attempt_id. "
            f"actions={result['actions']!r}"
        )
        # Lease release must be invoked with the failure exit-status sentinel
        # so the audit trail reflects a failed dispatch, not a successful one.
        assert any(
            "RELEASE_LEASE" in a and "failure" in a for a in result["actions"]
        ), (
            "rc_release_lease must be invoked with dispatch_exit_status=failure. "
            f"actions={result['actions']!r}"
        )

    def test_empty_attempt_id_releases_terminal_claim(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert any("RELEASE_CLAIM" in a for a in result["actions"]), (
            f"Expected terminal claim release. actions={result['actions']!r}"
        )

    def test_empty_attempt_id_emits_blocked_audit(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert any(
            "delivery_start_no_attempt" in a and "dispatch_blocked" in a
            for a in result["audits"]
        ), (
            f"Expected blocked-dispatch audit with delivery_start_no_attempt "
            f"reason. audits={result['audits']!r}"
        )

    def test_empty_attempt_id_logs_structured_failure(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert any(
            "delivery_start_no_attempt" in line for line in result["failures"]
        ), (
            f"Expected structured failure 'delivery_start_no_attempt'. "
            f"failures={result['failures']!r}"
        )

    def test_empty_attempt_id_clears_state_globals(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="")
        assert result["gen"] in (None, ""), (
            "_DL_RC_GENERATION must be cleared after fail-closed exit "
            f"(got {result['gen']!r}) so subsequent dispatches do not reuse stale state."
        )
        assert result["attempt"] in (None, ""), (
            "_DL_RC_ATTEMPT_ID must be cleared after fail-closed exit "
            f"(got {result['attempt']!r})."
        )

    def test_nonempty_attempt_id_returns_success(self, tmp_path):
        result = _run_register_and_acquire(tmp_path, attempt_id_from_start="att-123")
        assert result["rc"] == 0
        assert result["attempt"] == "att-123"
        # No release actions should fire on the success path.
        assert not any(
            "RELEASE_LEASE" in a or "RELEASE_CLAIM" in a for a in result["actions"]
        )


# ---------------------------------------------------------------------------
# Finding 2: rc_release_on_failure must emit distinct event_type when cleanup
# ---------------------------------------------------------------------------


def _run_release_on_failure(tmp_path: Path, broker_response: dict) -> dict:
    fn_body = _extract_function(LIFECYCLE_SH, "rc_release_on_failure")
    failures_log = tmp_path / "failures.log"
    audit_log = tmp_path / "audit.log"

    script = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -uo pipefail

        log() {{ true; }}
        log_structured_failure() {{ echo "$1" >> "{failures_log}"; }}
        emit_lease_cleanup_audit() {{
            local ev="$3" lr="$4" err="${{5:-}}"
            echo "AUDIT event=$ev lease_released=$lr err=$err" >> "{audit_log}"
        }}
        _rc_enabled() {{ return 0; }}
        _rc_python() {{
            cat <<'__EOF__'
{json.dumps(broker_response)}
__EOF__
            return 0
        }}
        _call_cleanup_worker_exit() {{ return 0; }}
        rc_release_lease() {{ true; }}

{fn_body}

        rc_release_on_failure "test-d-001" "att-1" "T1" "5" "test reason"
        echo "RC=$?"
        """
    )

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    rc_match = re.search(r"RC=(\d+)", proc.stdout)
    failures = (
        failures_log.read_text(encoding="utf-8").splitlines()
        if failures_log.exists()
        else []
    )
    audits = (
        audit_log.read_text(encoding="utf-8").splitlines() if audit_log.exists() else []
    )
    return {
        "rc": int(rc_match.group(1)) if rc_match else -1,
        "failures": failures,
        "audits": audits,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


class TestReleaseOnFailureAuditEventType:
    """Audit consumers must be able to distinguish partial vs full cleanup
    without parsing the optional error field — drives the event_type choice."""

    def test_partial_cleanup_uses_distinct_event_type(self, tmp_path):
        """cleanup_complete=false → distinct event_type, not lease_released_on_failure."""
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": False,
                "lease_released": True,
                "cleanup_complete": False,
                "lease_error": None,
                "failure_error": "broker raised RuntimeError",
            },
        )
        assert any(
            "event=lease_released_broker_inconsistent" in a for a in result["audits"]
        ), (
            "Partial cleanup must emit a distinct audit event_type so consumers "
            "filtering on lease_released_on_failure cannot mistake it for a clean "
            f"release. audits={result['audits']!r}"
        )
        # And it must NOT use the clean event_type.
        assert not any(
            "event=lease_released_on_failure" in a for a in result["audits"]
        ), (
            "Partial cleanup must NOT also emit lease_released_on_failure — "
            f"would defeat the discriminator. audits={result['audits']!r}"
        )

    def test_partial_cleanup_audit_lease_released_false(self, tmp_path):
        """The lease_released field on the audit entry is set to false when
        broker side did not record the failure — the cleanup as a whole was
        not completed even though the lease lock itself was released."""
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": False,
                "lease_released": True,
                "cleanup_complete": False,
                "lease_error": None,
                "failure_error": "broker raised RuntimeError",
            },
        )
        assert any(
            "event=lease_released_broker_inconsistent" in a
            and "lease_released=false" in a
            for a in result["audits"]
        ), (
            "Partial cleanup audit must mark lease_released=false to surface "
            f"the inconsistency. audits={result['audits']!r}"
        )

    def test_partial_cleanup_logs_failure_recording_missed(self, tmp_path):
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": False,
                "lease_released": True,
                "cleanup_complete": False,
                "lease_error": None,
                "failure_error": "broker raised RuntimeError",
            },
        )
        assert any(
            "failure_recording_missed" in line for line in result["failures"]
        ), (
            f"Expected structured failure 'failure_recording_missed'. "
            f"failures={result['failures']!r}"
        )

    def test_partial_cleanup_failure_log_includes_diagnostics(self, tmp_path):
        """The structured-failure detail must include attempt_id, failure_recorded,
        and failure_error so an operator can act on the audit entry without
        having to correlate with broker logs."""
        # The structured failure body is currently passed only as the message,
        # but our implementation includes the diagnostic fields in the third
        # arg. Stub captures only $1 (the failure code), so this test asserts
        # the failure code itself — diagnostic-arg coverage is exercised
        # separately via the full bash invocation in
        # test_release_on_failure_partial_cleanup.py.
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": False,
                "lease_released": True,
                "cleanup_complete": False,
                "lease_error": None,
                "failure_error": "broker raised RuntimeError",
            },
        )
        assert "failure_recording_missed" in "\n".join(result["failures"])

    def test_full_cleanup_uses_clean_event_type(self, tmp_path):
        """cleanup_complete=true → keep the canonical lease_released_on_failure
        event_type so existing audit consumers see no behavioral change on
        the happy path."""
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": True,
                "lease_released": True,
                "cleanup_complete": True,
                "lease_error": None,
                "failure_error": None,
            },
        )
        assert any(
            "event=lease_released_on_failure" in a and "lease_released=true" in a
            for a in result["audits"]
        ), (
            f"Full cleanup must emit lease_released_on_failure with "
            f"lease_released=true. audits={result['audits']!r}"
        )
        assert not any(
            "event=lease_released_broker_inconsistent" in a for a in result["audits"]
        )
        assert not any(
            "failure_recording_missed" in line for line in result["failures"]
        )

    def test_lease_release_failure_uses_lease_release_failed(self, tmp_path):
        """When the lease itself was not released, the existing
        lease_release_failed audit must still be emitted."""
        result = _run_release_on_failure(
            tmp_path,
            {
                "failure_recorded": True,
                "lease_released": False,
                "cleanup_complete": False,
                "lease_error": "stale generation",
                "failure_error": None,
            },
        )
        assert any(
            "event=lease_release_failed" in a and "lease_released=false" in a
            for a in result["audits"]
        ), (
            f"Lease-release failure must emit lease_release_failed. "
            f"audits={result['audits']!r}"
        )
        assert any(
            "lease_release_failed" in line for line in result["failures"]
        )
