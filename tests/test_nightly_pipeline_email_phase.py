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
import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIPELINE_SCRIPT = _REPO_ROOT / "scripts" / "nightly_intelligence_pipeline.sh"

_START_MARKER = "# ── Phase 11: Email digest"
_END_MARKER = "# ── Write pipeline health summary"


def _extract_phase_11_block() -> str:
    text = _PIPELINE_SCRIPT.read_text(encoding="utf-8")
    start = text.index(_START_MARKER)
    end = text.index(_END_MARKER, start)
    block = text[start:end]
    # The startup snapshot now precedes phases; execute it before the email gate.
    snapshot = text[text.index("# Config snapshot."):text.index("# End config snapshot.")]
    return snapshot + block


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
VNX_ROOT={str(_REPO_ROOT)!r}
log_msg() {{ echo "LOG: $*"; }}
log_phase_result() {{ echo "PHASE_RESULT:$*"; }}
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


@pytest.mark.parametrize("cli_status,recipient", [
    (3, ""), (3, "ops@example.com"), (None, ""), (0, ""), (0, "ops@example.com"),
])
def test_snapshot_failure_only_skips_email(tmp_path, cli_status, recipient):
    """Run the whole shell script; isolate only CLI/phase process boundaries."""
    engine = tmp_path / "consumer/.vnx"
    scripts = engine / "scripts"
    (scripts / "lib").mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(engine)], check=True)
    shutil.copy2(_PIPELINE_SCRIPT, scripts / _PIPELINE_SCRIPT.name)
    for name in ("vnx_paths.sh", "vnx_resolve_root.sh"):
        shutil.copy2(_REPO_ROOT / "scripts/lib" / name, scripts / "lib" / name)
    binaries = engine / "bin"
    binaries.mkdir()
    if cli_status is not None:
        cli = binaries / "vnx"
        cli.write_text(f'#!/bin/bash\nprintf "%s\\n" "{recipient}"\nexit {cli_status}\n')
        cli.chmod(0o755)
    python = binaries / "python3"
    python.write_text(f'''#!/bin/bash
case "$1" in
    "$TEST_SCRIPTS/"*)
        [ "$VNX_PROJECT_ROOT" = "$TEST_CONSUMER" ] || exit 9
        printf 'EXECUTED:%s\\n' "${{1##*/}}" ;;
    *) exec {sys.executable!r} "$@" ;;
esac
''')
    python.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "data/state"
    state.mkdir(parents=True)
    result = subprocess.run(["bash", str(scripts / _PIPELINE_SCRIPT.name)],
                            cwd=engine.parent, capture_output=True, text=True, timeout=30,
                            env={"PATH": f"{binaries}:{os.environ['PATH']}", "HOME": str(home),
                                 "VNX_HOME": str(engine), "VNX_PROJECT_ID": "consumer-test",
                                 "VNX_DATA_DIR": str(state.parent), "VNX_STATE_DIR": str(state),
                                 "VNX_PROJECT_ROOT": str(engine.parent), "TEST_CONSUMER": str(engine.parent),
                                 "TEST_SCRIPTS": str(scripts)})
    # This completion marker must exist even when the CLI failed or was missing.
    assert "=== Pipeline complete:" in result.stdout, result.stdout + result.stderr
    phases = [json.loads(line) for line in (state / "nightly_pipeline_phases.ndjson").read_text().splitlines()]
    assert [p["phase"] for p in phases[:-1]] == [
        "0-schema-init", "1a-quality-scan", "1b-snippet-extract", "1c-doc-extract",
        "2-conversation-analyze", "3-session-dispatch-link", "2b-event-analyze",
        "2c-pattern-extract", "4-learning-cycle", "4a-memory-consolidation",
        "4b-dream-consolidation", "4c-weekly-digest", "5-stale-edits", "6-session-brief",
        "7-governance", "8-suggested-edits", "8b-edit-remeasure", "9-quality-digest", "10-recommendations",
    ]
    assert all(p["status"] == "ok" for p in phases[:-1])
    failed = cli_status != 0
    skipped = failed or not recipient
    assert phases[-1]["phase"] == "11-email-digest"
    assert phases[-1]["status"] == ("skipped" if skipped else "ok")
    assert phases[-1]["detail"] == ("config_snapshot_unavailable" if failed else "no_recipient_configured" if skipped else "")
    assert ("EXECUTED:send_digest_email.py" in result.stdout) == (not skipped)
    assert ("WARNING: VNX_DIGEST_EMAIL snapshot unavailable" in result.stderr) == failed
    assert result.returncode == (1 if failed else 0)
    health = json.loads((state / "nightly_pipeline_health.json").read_text())
    assert health["overall_status"] == ("partial" if failed else "ok")
