"""Tests for receipt_processor_v4.sh bootstrap protection.

Exercises _rp_apply_bootstrap_protection() by sourcing the real script in
_RP_LIB_MODE=1 so tests always exercise the production function, not a copy.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

RP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor_v4.sh"


def _run_bootstrap(
    watermark_age_secs: int,
    bootstrap_max_age: int,
    report_mtimes: list[int] | None = None,
    make_unreadable: bool = False,
    make_events_dir_unwritable: bool = False,
    make_events_file_a_dir: bool = False,
) -> tuple[str, int, str, str, str, bool]:
    """
    Invoke the real _rp_apply_bootstrap_protection from receipt_processor_v4.sh
    by sourcing the script in _RP_LIB_MODE=1.

    Returns (stderr, returncode, final_watermark_value, old_watermark_raw,
             events_ndjson_content, events_dir_exists).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        headless = tmp / "headless"
        state = tmp / "state"
        pids = tmp / "pids"
        locks = tmp / "locks"
        data_dir = tmp / "data"
        for d in (unified, headless, state, pids, locks, data_dir):
            d.mkdir(parents=True)

        watermark_file = state / "receipt_processor_watermark"
        now = int(time.time())
        old_ts = now - watermark_age_secs
        watermark_file.write_text(str(old_ts))

        # Drop dummy report files with specific mtimes
        for i, mtime in enumerate(report_mtimes or []):
            report = unified / f"report_{i}.md"
            report.write_text("# dummy")
            os.utime(str(report), (mtime, mtime))

        # Optionally add a broken symlink to trigger OSError on stat()
        if make_unreadable:
            broken = unified / "broken_report.md"
            broken.symlink_to("/nonexistent_vnx_test_target/report.md")

        events_dir = data_dir / "events"
        events_file = events_dir / "receipt_processor.ndjson"

        if make_events_dir_unwritable:
            events_dir.mkdir(parents=True)
            events_dir.chmod(0o500)

        if make_events_file_a_dir:
            # Preflight (mkdir -p + touch) will succeed because touch on a directory returns 0.
            # The subsequent `printf >>` will fail with EISDIR, triggering the new abort path.
            events_dir.mkdir(parents=True)
            (events_dir / "receipt_processor.ndjson").mkdir()

        bash_cmd = f"""
set -e
export _RP_LIB_MODE=1
export VNX_DATA_DIR="{data_dir}"
export VNX_STATE_DIR="{state}"
export VNX_PIDS_DIR="{pids}"
export VNX_LOCKS_DIR="{locks}"
export VNX_REPORTS_DIR="{unified}"
export VNX_HEADLESS_REPORTS_DIR="{headless}"
source "{RP_SCRIPT}" || {{ echo "FATAL: source {RP_SCRIPT} failed" >&2; exit 1; }}

WATERMARK_FILE="{watermark_file}"
UNIFIED_REPORTS="{unified}"
HEADLESS_REPORTS="{headless}"
BOOTSTRAP_MAX_AGE="{bootstrap_max_age}"
VNX_DATA_DIR="{data_dir}"

_rp_apply_bootstrap_protection
_rc=$?
cat "{watermark_file}"
exit $_rc
"""
        result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
        final_watermark = result.stdout.strip()

        # Restore permissions so TemporaryDirectory cleanup can remove the dir.
        if make_events_dir_unwritable and events_dir.exists():
            events_dir.chmod(0o700)
        if make_events_file_a_dir and events_dir.exists():
            events_dir.chmod(0o700)

        events_dir_exists = events_dir.is_dir()
        events_content = events_file.read_text() if events_file.is_file() else ""
        return result.stderr, result.returncode, final_watermark, str(old_ts), events_content, events_dir_exists


def test_bootstrap_skips_old_watermark():
    """Watermark 48h old → bootstrap mode entered, watermark advanced to newest report mtime."""
    now = int(time.time())
    report_mtime = now - 3600  # report from 1h ago

    stderr, rc, final_wm, old_wm, *_ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[report_mtime],
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "BOOTSTRAP mode" in stderr, f"Expected BOOTSTRAP mode in stderr:\n{stderr}"
    assert final_wm.isdigit(), f"Expected integer watermark, got: {final_wm!r}"
    assert int(final_wm) > int(old_wm), (
        f"Watermark should have been advanced: old={old_wm} new={final_wm}"
    )
    # Watermark should be close to the report's mtime
    assert abs(int(final_wm) - report_mtime) < 5, (
        f"Watermark ({final_wm}) should match report mtime ({report_mtime})"
    )


def test_normal_catchup_under_threshold():
    """Watermark 1h old with 24h threshold → normal catchup, no bootstrap."""
    stderr, rc, final_wm, old_wm, *_ = _run_bootstrap(
        watermark_age_secs=3600,
        bootstrap_max_age=86400,
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "BOOTSTRAP mode" not in stderr, f"Should NOT enter bootstrap:\n{stderr}"
    assert "normal catchup" in stderr, f"Expected 'normal catchup' in stderr:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_disable_bootstrap_via_env():
    """BOOTSTRAP_MAX_AGE=0 disables bootstrap even with a very old watermark."""
    stderr, rc, final_wm, old_wm, *_ = _run_bootstrap(
        watermark_age_secs=30 * 24 * 3600,  # 30 days old
        bootstrap_max_age=0,
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "BOOTSTRAP mode" not in stderr, f"Bootstrap must be disabled when MAX_AGE=0:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_bootstrap_fallback_to_now_when_no_reports():
    """Old watermark + no reports → bootstrap advances watermark to now (fallback)."""
    now = int(time.time())

    stderr, rc, final_wm, old_wm, *_ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[],  # no reports
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "BOOTSTRAP mode" in stderr
    assert final_wm.isdigit()
    # Should be close to 'now' (within a few seconds of test execution)
    assert abs(int(final_wm) - now) < 10, (
        f"No-report fallback watermark ({final_wm}) should be close to now ({now})"
    )


def test_bootstrap_logs_stat_failure_to_stderr():
    """Unreadable report file → warning logged to stderr, watermark still advances."""
    now = int(time.time())
    report_mtime = now - 3600

    stderr, rc, final_wm, old_wm, *_ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[report_mtime],
        make_unreadable=True,
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "warning: stat failed" in stderr, (
        f"Expected 'warning: stat failed' in stderr:\n{stderr}"
    )
    # Loop must continue despite the failure — watermark still advanced
    assert "BOOTSTRAP mode" in stderr, f"Expected BOOTSTRAP mode:\n{stderr}"
    assert final_wm.isdigit(), f"Expected integer watermark, got: {final_wm!r}"
    assert int(final_wm) > int(old_wm), (
        f"Watermark should still advance despite stat failure: old={old_wm} new={final_wm}"
    )


def test_bootstrap_emits_ndjson_audit_event():
    """Bootstrap skip → exactly one bootstrap_skip event in VNX_DATA_DIR/events/receipt_processor.ndjson."""
    stderr, rc, final_wm, old_wm, events_content, events_dir_exists = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
    )

    assert rc == 0, f"Bootstrap function returned non-zero rc={rc}:\n{stderr}"
    assert "BOOTSTRAP mode" in stderr, f"Expected BOOTSTRAP mode:\n{stderr}"
    assert events_dir_exists, "VNX_DATA_DIR/events/ directory must be created by bootstrap"
    assert events_content, "events/receipt_processor.ndjson must not be empty after bootstrap skip"

    lines = [ln for ln in events_content.strip().splitlines() if ln.strip()]
    bootstrap_lines = [ln for ln in lines if '"bootstrap_skip"' in ln]
    assert len(bootstrap_lines) == 1, (
        f"Expected exactly 1 bootstrap_skip event, got {len(bootstrap_lines)}:\n{events_content}"
    )

    event = json.loads(bootstrap_lines[0])
    assert event.get("event_type") == "bootstrap_skip"
    assert event.get("trigger") == "stale_watermark_bootstrap"
    assert "old_watermark" in event, f"Missing old_watermark in event: {event}"
    assert "new_watermark" in event, f"Missing new_watermark in event: {event}"
    assert "watermark_age_seconds" in event, f"Missing watermark_age_seconds in event: {event}"
    assert event.get("new_watermark") == final_wm, (
        f"Event new_watermark {event['new_watermark']!r} != watermark file value {final_wm!r}"
    )
    assert event.get("old_watermark") == old_wm, (
        f"Event old_watermark {event['old_watermark']!r} != expected {old_wm!r}"
    )


def test_bootstrap_aborts_if_events_file_unwritable():
    """If events dir is unwritable, bootstrap must abort without advancing the watermark."""
    stderr, rc, final_wm, old_wm, events_content, _ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        make_events_dir_unwritable=True,
    )

    # Bootstrap must return non-zero (preflight failure)
    assert rc != 0, (
        f"Bootstrap should return non-zero when events dir is unwritable, got rc={rc}:\n{stderr}"
    )
    # Watermark must NOT have been advanced
    assert final_wm == old_wm, (
        f"Watermark must not advance when audit write fails: old={old_wm} final={final_wm}"
    )
    # ERROR must appear in the log
    assert "ERROR" in stderr, f"Expected ERROR log when events dir unwritable:\n{stderr}"
    # No audit event should have been written
    assert not events_content, (
        f"No audit event should be written when abort: {events_content!r}"
    )


def test_bootstrap_aborts_if_audit_emission_fails():
    """Preflight passes but printf >> events_file fails (EISDIR) → watermark NOT advanced."""
    # Pre-create events_file as a directory: touch on a dir returns 0 (preflight passes),
    # but printf >> dir fails with EISDIR — this exercises the new audit-before-mutation path.
    stderr, rc, final_wm, old_wm, events_content, _ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        make_events_file_a_dir=True,
    )

    assert rc != 0, (
        f"Bootstrap should return non-zero when audit emission fails, got rc={rc}:\n{stderr}"
    )
    assert final_wm == old_wm, (
        f"Watermark must not advance when audit fails: old={old_wm} final={final_wm}"
    )
    assert "audit emission failed" in stderr, (
        f"Expected 'audit emission failed' in stderr:\n{stderr}"
    )
    assert "ERROR" in stderr, f"Expected ERROR log on audit failure:\n{stderr}"


def test_audit_emit_precedes_state_mutation():
    """Static order check: audit printf line must appear before mv watermark line in the function."""
    script_text = RP_SCRIPT.read_text()
    lines = script_text.splitlines()

    # Find the function body of _rp_apply_bootstrap_protection
    func_start = next(
        (i for i, ln in enumerate(lines) if "_rp_apply_bootstrap_protection()" in ln), None
    )
    assert func_start is not None, "_rp_apply_bootstrap_protection() not found in script"

    # Locate closing brace of the function (first line with only "}" after func_start)
    func_end = next(
        (i for i, ln in enumerate(lines[func_start:], start=func_start) if ln.strip() == "}"),
        len(lines),
    )

    func_body = lines[func_start:func_end]

    audit_line = next(
        (i for i, ln in enumerate(func_body) if '"bootstrap_skip"' in ln and "printf" in ln),
        None,
    )
    mv_line = next(
        (i for i, ln in enumerate(func_body) if "mv" in ln and "WATERMARK_FILE" in ln and ".tmp" in ln),
        None,
    )

    assert audit_line is not None, "Could not find audit printf line in _rp_apply_bootstrap_protection"
    assert mv_line is not None, "Could not find mv watermark line in _rp_apply_bootstrap_protection"
    assert audit_line < mv_line, (
        f"ADR-005 violated: audit emit (body line {audit_line}) must come before "
        f"mv watermark (body line {mv_line})"
    )


def test_lib_mode_does_not_install_trap():
    """_RP_LIB_MODE=1 source must not override caller's EXIT trap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        data_dir = tmp / "data"
        state = tmp / "state"
        pids = tmp / "pids"
        locks = tmp / "locks"
        unified = tmp / "unified"
        headless = tmp / "headless"
        for d in (data_dir, state, pids, locks, unified, headless):
            d.mkdir(parents=True)

        sentinel = tmp / "caller_trap_fired"
        bash_cmd = f"""
trap "touch '{sentinel}'" EXIT
export _RP_LIB_MODE=1
export VNX_DATA_DIR="{data_dir}"
export VNX_STATE_DIR="{state}"
export VNX_PIDS_DIR="{pids}"
export VNX_LOCKS_DIR="{locks}"
export VNX_REPORTS_DIR="{unified}"
export VNX_HEADLESS_REPORTS_DIR="{headless}"
source "{RP_SCRIPT}"
"""
        result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
        assert result.returncode == 0, (
            f"Source with _RP_LIB_MODE=1 failed unexpectedly:\n{result.stderr}"
        )
        assert sentinel.exists(), (
            "Caller's EXIT trap was not fired after lib-mode source — "
            "cleanup trap may have replaced it.\n"
            f"stderr: {result.stderr}"
        )
