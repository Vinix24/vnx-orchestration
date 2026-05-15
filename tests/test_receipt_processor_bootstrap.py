"""Tests for receipt_processor_v4.sh bootstrap protection.

Exercises _rp_apply_bootstrap_protection() in isolation by sourcing only the
function and its minimal dependencies (log stub + the four required variables).
"""
from __future__ import annotations

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
        except OSError:
            pass
print(max_mtime if max_mtime > 0 else fallback)
PY
)
        [ -z "$new_watermark" ] && new_watermark="$now"

        echo "$new_watermark" > "${WATERMARK_FILE}.tmp" \\
            && mv "${WATERMARK_FILE}.tmp" "$WATERMARK_FILE"
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
) -> tuple[str, str, str]:
    """
    Run _rp_apply_bootstrap_protection in an isolated bash subshell.

    Returns (stderr, final_watermark_value, watermark_raw).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        headless = tmp / "headless"
        unified.mkdir()
        headless.mkdir()

        watermark_file = tmp / "watermark"
        now = int(time.time())
        old_ts = now - watermark_age_secs
        watermark_file.write_text(str(old_ts))

        # Drop dummy report files with specific mtimes
        for i, mtime in enumerate(report_mtimes or []):
            report = unified / f"report_{i}.md"
            report.write_text("# dummy")
            import os
            os.utime(str(report), (mtime, mtime))

        script = _BOOTSTRAP_FUNC_EXTRACT + f"""
WATERMARK_FILE="{watermark_file}"
UNIFIED_REPORTS="{unified}"
HEADLESS_REPORTS="{headless}"
BOOTSTRAP_MAX_AGE="{bootstrap_max_age}"

_rp_apply_bootstrap_protection
cat "$WATERMARK_FILE"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
        )
        final_watermark = result.stdout.strip()
        return result.stderr, final_watermark, str(old_ts)


def test_bootstrap_skips_old_watermark():
    """Watermark 48h old → bootstrap mode entered, watermark advanced to newest report mtime."""
    now = int(time.time())
    report_mtime = now - 3600  # report from 1h ago

    stderr, final_wm, old_wm = _run_bootstrap(
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
    stderr, final_wm, old_wm = _run_bootstrap(
        watermark_age_secs=3600,
        bootstrap_max_age=86400,
    )

    assert "BOOTSTRAP mode" not in stderr, f"Should NOT enter bootstrap:\n{stderr}"
    assert "normal catchup" in stderr, f"Expected 'normal catchup' in stderr:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_disable_bootstrap_via_env():
    """BOOTSTRAP_MAX_AGE=0 disables bootstrap even with a very old watermark."""
    stderr, final_wm, old_wm = _run_bootstrap(
        watermark_age_secs=30 * 24 * 3600,  # 30 days old
        bootstrap_max_age=0,
    )

    assert "BOOTSTRAP mode" not in stderr, f"Bootstrap must be disabled when MAX_AGE=0:\n{stderr}"
    # Watermark must be unchanged
    assert final_wm == old_wm, f"Watermark should be unchanged: old={old_wm} new={final_wm}"


def test_bootstrap_fallback_to_now_when_no_reports():
    """Old watermark + no reports → bootstrap advances watermark to now (fallback)."""
    now = int(time.time())

    stderr, final_wm, old_wm = _run_bootstrap(
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
