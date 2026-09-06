"""test_nightly_pipeline_email_phase.py — Phase 11 (email digest) gate contract.

Covers the 2026-09-06 fix: `send_digest_email.py` (since #1751, 2026-09-04)
resolves VNX_SMTP_PASS itself, falling back to the macOS keychain and failing
loudly when both the env var and the keychain are empty. But phase 11 of
`nightly_intelligence_pipeline.sh` gated the *call* on `VNX_SMTP_PASS` being
non-empty too — with the env var removed from the operator's shell profile
(the whole point of the keychain fallback), the sender is never invoked and
the keychain path is unreachable from the pipeline.

These tests extract the ACTUAL phase-11 shell block verbatim from the
pipeline script (never a hand-copied duplicate) and execute it in a minimal
bash harness, with `send_digest_email.py` replaced by a stub that records
whether it was invoked. This proves the real production conditional, not a
description of it.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIPELINE_SCRIPT = _REPO_ROOT / "scripts" / "nightly_intelligence_pipeline.sh"

_START_MARKER = "# ── Phase 11: Email digest"
_END_MARKER = "# ── Write pipeline health summary"


def _extract_phase_11_block() -> str:
    text = _PIPELINE_SCRIPT.read_text(encoding="utf-8")
    start = text.index(_START_MARKER)
    end = text.index(_END_MARKER, start)
    return text[start:end]


def _run_phase_11_block(env_overrides: dict) -> tuple[str, bool]:
    """Execute the real phase-11 block in an isolated bash harness.

    Returns (combined stdout+stderr, stub_was_called).
    """
    block = _extract_phase_11_block()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "stub_called.marker"
        stub = tmp_path / "send_digest_email.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('called')\n",
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        harness = f"""
set -uo pipefail
SCRIPT_DIR={str(tmp_path)!r}
log_msg() {{ echo "LOG: $*"; }}
run_phase() {{
    local phase_name="$1"; shift
    "$@"
    echo "RAN_PHASE:$phase_name"
}}
{block}
"""
        env = os.environ.copy()
        for var in ("VNX_DIGEST_EMAIL", "VNX_SMTP_PASS"):
            env.pop(var, None)
        env.update(env_overrides)

        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        return result.stdout + result.stderr, marker.exists()


def test_email_configured_smtp_pass_empty_sender_is_still_invoked():
    """VNX_DIGEST_EMAIL set, VNX_SMTP_PASS empty (the keychain-only operator
    setup) — phase 11 must still call the sender so it can fall back to the
    keychain. Fails on the pre-fix conditional (skips phase 11 outright)."""
    output, called = _run_phase_11_block({"VNX_DIGEST_EMAIL": "ops@example.com"})

    assert called, f"send_digest_email.py stub was NOT invoked.\noutput:\n{output}"
    assert "RAN_PHASE:11-email-digest" in output


def test_email_configured_smtp_pass_set_sender_is_invoked():
    """Unaffected case: both set — sender must still run."""
    output, called = _run_phase_11_block(
        {"VNX_DIGEST_EMAIL": "ops@example.com", "VNX_SMTP_PASS": "env-secret"}
    )

    assert called, f"send_digest_email.py stub was NOT invoked.\noutput:\n{output}"
    assert "RAN_PHASE:11-email-digest" in output


def test_email_not_configured_phase_is_skipped_and_message_names_only_digest_email():
    """VNX_DIGEST_EMAIL unset — phase must be skipped (no recipient to send
    to), and the skip message must name only VNX_DIGEST_EMAIL, since
    VNX_SMTP_PASS is no longer this phase's concern."""
    output, called = _run_phase_11_block({})

    assert not called, f"send_digest_email.py stub was invoked unexpectedly.\noutput:\n{output}"
    assert "RAN_PHASE" not in output
    assert "VNX_DIGEST_EMAIL not set" in output
    assert "VNX_SMTP_PASS" not in output
