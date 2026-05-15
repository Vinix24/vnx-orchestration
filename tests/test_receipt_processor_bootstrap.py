"""Tests for receipt_processor_v4.sh bootstrap protection.

Exercises _rp_apply_bootstrap_protection() in isolation by sourcing only the
function and its minimal dependencies (log stub + the four required variables).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

RP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor_v4.sh"

_BOOTSTRAP_FUNC_EXTRACT = """
# Minimal stubs so the function can be sourced without the full RP context.
log() { echo "[$1] $2" >&2; }

_rp_apply_bootstrap_protection() {
    if [ ! -f "$WATERMARK_FILE" ]; then
        log "INFO" "No watermark file; skipping bootstrap check"
        return 0
    fi

    local watermark_ts
    watermark_ts=$(cat "$WATERMARK_FILE" 2>/dev/null || echo "")
    if ! [[ "$watermark_ts" =~ ^[0-9]+$ ]]; then
        log "WARN" "Watermark unreadable (not an integer); skipping bootstrap check"
        return 0
    fi

    local now
    now=$(date +%s)
    local watermark_age=$(( now - watermark_ts ))

    if [ "$BOOTSTRAP_MAX_AGE" -gt 0 ] && [ "$watermark_age" -gt "$BOOTSTRAP_MAX_AGE" ]; then
        log "WARN" "Watermark is ${watermark_age}s old (>${BOOTSTRAP_MAX_AGE}s). Entering BOOTSTRAP mode."
        log "WARN" "Marking current report state as baseline. Historical reports skipped."

        local new_watermark
        new_watermark=$(python3 - "$UNIFIED_REPORTS" "$HEADLESS_REPORTS" "$now" <<'PY'
import sys
from pathlib import Path

unified, headless, fallback = sys.argv[1], sys.argv[2], int(sys.argv[3])
max_mtime = 0
for d in (unified, headless):
    p = Path(d)
    if not p.is_dir():
        continue
    for f in p.glob("*.md"):
        try:
            mtime = int(f.stat().st_mtime)
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError as e:
            print(f"warning: stat failed for {f}: {e}", file=sys.stderr)
print(max_mtime if max_mtime > 0 else fallback)
PY
)
        [ -z "$new_watermark" ] && new_watermark="$now"

        local _old_watermark
        _old_watermark=$(cat "$WATERMARK_FILE" 2>/dev/null || echo "0")

        echo "$new_watermark" > "${WATERMARK_FILE}.tmp" \\
            && mv "${WATERMARK_FILE}.tmp" "$WATERMARK_FILE"

        local _bootstrap_event_file="$STATE_DIR/events.ndjson"
        local _now_iso
        _now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        printf '{"timestamp":"%s","event_type":"bootstrap_skip","source":"receipt_processor","file":"receipt_processor_watermark","trigger":"stale_watermark_bootstrap","watermark_age_seconds":%s,"max_age_seconds":%s,"old_watermark":"%s","new_watermark":"%s"}\\n' \\
            "$_now_iso" "$watermark_age" "$BOOTSTRAP_MAX_AGE" "$_old_watermark" "$new_watermark" \\
            >> "$_bootstrap_event_file"
        log "INFO" "Bootstrap skip audited to $_bootstrap_event_file"
        log "INFO" "Bootstrap watermark set to $new_watermark"
        log "INFO" "If you need historical reports replayed, manually rewind watermark and restart."
    else
        log "INFO" "Watermark age ${watermark_age}s (<= ${BOOTSTRAP_MAX_AGE}s). Running normal catchup."
    fi
}
"""


def _run_bootstrap(
    watermark_age_secs: int,
    bootstrap_max_age: int,
    report_mtimes: list[int] | None = None,
    make_unreadable: bool = False,
) -> tuple[str, str, str, str]:
    """
    Run _rp_apply_bootstrap_protection in an isolated bash subshell.

    Returns (stderr, final_watermark_value, old_watermark_raw, events_ndjson_content).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        headless = tmp / "headless"
        state = tmp / "state"
        unified.mkdir()
        headless.mkdir()
        state.mkdir()

        watermark_file = tmp / "watermark"
        now = int(time.time())
        old_ts = now - watermark_age_secs
        watermark_file.write_text(str(old_ts))

        # Drop dummy report files with specific mtimes
        for i, mtime in enumerate(report_mtimes or []):
            report = unified / f"report_{i}.md"
            report.write_text("# dummy")
            os.utime(str(report), (mtime, mtime))

        # Optionally add a broken symlink to trigger OSError on stat()
        # chmod 000 alone does not fail stat() on macOS (stat only needs dir-execute);
        # a broken symlink causes FileNotFoundError (subclass of OSError) on f.stat().
        if make_unreadable:
            broken = unified / "broken_report.md"
            broken.symlink_to("/nonexistent_vnx_test_target/report.md")

        events_file = state / "events.ndjson"

        script = _BOOTSTRAP_FUNC_EXTRACT + f"""
WATERMARK_FILE="{watermark_file}"
UNIFIED_REPORTS="{unified}"
HEADLESS_REPORTS="{headless}"
BOOTSTRAP_MAX_AGE="{bootstrap_max_age}"
STATE_DIR="{state}"

_rp_apply_bootstrap_protection
cat "$WATERMARK_FILE"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        final_watermark = result.stdout.strip()

        events_content = events_file.read_text() if events_file.exists() else ""
        return result.stderr, final_watermark, str(old_ts), events_content


def test_bootstrap_skips_old_watermark():
    """Watermark 48h old → bootstrap mode entered, watermark advanced to newest report mtime."""
    now = int(time.time())
    report_mtime = now - 3600  # report from 1h ago

    stderr, final_wm, old_wm, _ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[report_mtime],
    )

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
    stderr, final_wm, old_wm, _ = _run_bootstrap(
        watermark_age_secs=3600,
        bootstrap_max_age=86400,
    )

    assert "BOOTSTRAP mode" not in stderr, f"Should NOT enter bootstrap:\n{stderr}"
    assert "normal catchup" in stderr, f"Expected 'normal catchup' in stderr:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_disable_bootstrap_via_env():
    """BOOTSTRAP_MAX_AGE=0 disables bootstrap even with a very old watermark."""
    stderr, final_wm, old_wm, _ = _run_bootstrap(
        watermark_age_secs=30 * 24 * 3600,  # 30 days old
        bootstrap_max_age=0,
    )

    assert "BOOTSTRAP mode" not in stderr, f"Bootstrap must be disabled when MAX_AGE=0:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_bootstrap_fallback_to_now_when_no_reports():
    """Old watermark + no reports → bootstrap advances watermark to now (fallback)."""
    now = int(time.time())

    stderr, final_wm, old_wm, _ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[],  # no reports
    )

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

    stderr, final_wm, old_wm, _ = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
        report_mtimes=[report_mtime],
        make_unreadable=True,
    )

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
    """Bootstrap skip → exactly one bootstrap_skip event in STATE_DIR/events.ndjson."""
    stderr, final_wm, old_wm, events_content = _run_bootstrap(
        watermark_age_secs=48 * 3600,
        bootstrap_max_age=86400,
    )

    assert "BOOTSTRAP mode" in stderr, f"Expected BOOTSTRAP mode:\n{stderr}"
    assert events_content, "events.ndjson must not be empty after bootstrap skip"

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
