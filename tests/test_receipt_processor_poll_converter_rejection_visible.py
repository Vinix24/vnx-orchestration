"""golf3b / F1-2: the MONITOR-mode polling cycle must not silently discard
the converter's REJECTED reason either.

tests/test_receipt_processor_converter_rejection_visible.py (PR #1753 /
Cluster D) already fixed and covers _ppr_process_rate_limited — the
catchup/manual-mode call site. But scripts/receipt_processor.sh's actual
24/7 production path is _poll_new_reports (MODE=monitor, the default), which
had its OWN separate `2>/dev/null` on the converter invocation
(receipt_processor.sh:444-447 as measured 2026-09-05) that PR #1753 never
touched — confirmed live: health/report_to_receipt_converter.json shows
rejected_count=27 with last_run_iso 2026-09-03T05:35:06Z, two days stale at
the time of measurement, while the processing log carries zero trace of any
REJECTED line.

Both call sites now share one helper, _run_receipt_converter_scan (see
receipt_processor.sh) — this test exercises that helper via the polling
loop's own per-cycle function, _poll_new_reports_cycle (extracted from the
`while true` loop specifically so a single cycle is callable in isolation
without needing to break out of an infinite loop).
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

RP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"

ENV_PREAMBLE = """
set -u
export _RP_LIB_MODE=1
export VNX_DATA_DIR="{data_dir}"
export VNX_STATE_DIR="{state}"
export VNX_PIDS_DIR="{pids}"
export VNX_LOCKS_DIR="{locks}"
export VNX_REPORTS_DIR="{unified}"
export VNX_HEADLESS_REPORTS_DIR="{headless}"
source "{rp_script}" || {{ echo "FATAL: source failed" >&2; exit 1; }}
"""

_REJECTED_LINE = (
    "WARNING report_to_receipt_converter: report_to_receipt_converter: "
    "REJECTED (fail-closed) dispatch=DISP-POLL-STUB file=poll-stub.md reason=missing model"
)
_SUMMARY_LINE = (
    "WARNING report_to_receipt_converter: report_to_receipt_converter: "
    "1 rejected, 0 malformed, 0 error(s) this scan"
)


def _make_dirs(tmp: Path) -> dict:
    data_dir = tmp / "data"
    dirs = {
        "unified": tmp / "unified",
        "headless": tmp / "headless",
        "state": data_dir / "state",
        "pids": tmp / "pids",
        "locks": tmp / "locks",
        "data_dir": data_dir,
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _make_stub_converter(tmp: Path, *, exit_code: int = 0) -> Path:
    """A stub lib/report_to_receipt_converter.py reproducing the real
    converter's fail-closed REJECTED warning-to-stderr, exit-0 behavior."""
    stub_scripts = tmp / "stub_scripts"
    stub_lib = stub_scripts / "lib"
    stub_lib.mkdir(parents=True, exist_ok=True)
    converter = stub_lib / "report_to_receipt_converter.py"
    converter.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            print({_REJECTED_LINE!r}, file=sys.stderr)
            print({_SUMMARY_LINE!r}, file=sys.stderr)
            sys.exit({exit_code})
            """),
        encoding="utf-8",
    )
    converter.chmod(0o755)
    # The real beacon writer lives alongside the converter under lib/ — copy
    # it into the stub tree so _run_receipt_converter_scan's second stage
    # (piping stderr into receipt_conversion_rejection_beacon.py) resolves
    # against the SAME "$SCRIPTS_DIR/lib/..." path the production script
    # uses, exactly like the real deployment layout.
    real_beacon_writer = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "lib" / "receipt_conversion_rejection_beacon.py"
    )
    (stub_lib / "receipt_conversion_rejection_beacon.py").write_text(
        real_beacon_writer.read_text(encoding="utf-8"), encoding="utf-8",
    )
    real_health_beacon = (
        Path(__file__).resolve().parent.parent / "scripts" / "lib" / "health_beacon.py"
    )
    (stub_lib / "health_beacon.py").write_text(
        real_health_beacon.read_text(encoding="utf-8"), encoding="utf-8",
    )
    return stub_scripts


def _run_bash(dirs: dict, body: str) -> subprocess.CompletedProcess:
    cmd = ENV_PREAMBLE.format(rp_script=RP_SCRIPT, **dirs) + body
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_poll_cycle_rejected_warning_reaches_the_processing_log(tmp_path: Path) -> None:
    """The real defect: does a REJECTED warning surfaced during the MONITOR
    polling loop's periodic converter sweep (every 6th cycle) land in
    $PROCESSING_LOG, or does it vanish behind the loop's own `2>/dev/null`?"""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
# cycle=6 is divisible by 6 -> triggers the converter sweep.
# retry_cycles=7 -> 6 % 7 != 0 -> _retry_pending_receipts is skipped.
_poll_new_reports_cycle 6 7
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"

    processing_log = dirs["state"] / "receipt_processing.log"
    assert processing_log.is_file(), "receipt_processing.log was never created"
    log_contents = processing_log.read_text(encoding="utf-8")

    assert "REJECTED (fail-closed)" in log_contents, (
        "converter's REJECTED warning never reached the processing log during "
        f"the polling cycle (stderr was thrown away). log contents:\n{log_contents}"
    )
    assert "1 rejected, 0 malformed, 0 error(s) this scan" in log_contents


def test_poll_cycle_skips_converter_on_non_sixth_cycle(tmp_path: Path) -> None:
    """Unchanged cadence: the converter must only run every 6th cycle, exactly
    as before this fix — a cycle count that is NOT a multiple of 6 must not
    invoke it at all."""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_poll_new_reports_cycle 5 100
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"

    processing_log = dirs["state"] / "receipt_processing.log"
    log_contents = processing_log.read_text(encoding="utf-8") if processing_log.is_file() else ""
    assert "REJECTED (fail-closed)" not in log_contents


def test_poll_cycle_rejection_lands_in_its_own_health_beacon(tmp_path: Path) -> None:
    """golf3b requirement #2: the reason must be traceable to WHICH report
    and WHY, not just a bare count. Confirms the per-cycle converter sweep
    writes dispatch_id/file/reason into
    health/receipt_conversion_rejections.json — the same already-wired
    reading path (hooks/sessionstart.sh's digest, health_check.py, vnx
    doctor, the dashboard) every other component's beacon uses."""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_poll_new_reports_cycle 6 7
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"

    beacon_path = dirs["data_dir"] / "health" / "receipt_conversion_rejections.json"
    assert beacon_path.is_file(), "receipt_conversion_rejections.json beacon was never written"
    payload = json.loads(beacon_path.read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert payload["details"]["rejections"] == [{
        "dispatch_id": "DISP-POLL-STUB",
        "file": "poll-stub.md",
        "reason": "missing model",
    }]


def test_poll_cycle_non_fatal_design_preserved_on_a_real_crash(tmp_path: Path) -> None:
    """Sanity: a genuine converter crash (nonzero exit) during the polling
    cycle must still be logged as a non-fatal ERROR and must NOT abort the
    processor's per-cycle function."""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path, exit_code=1)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_poll_new_reports_cycle 6 7
echo "REACHED_END=yes"
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    assert "REACHED_END=yes" in result.stdout, "the cycle function must return, not abort, on a converter crash"

    processing_log = (dirs["state"] / "receipt_processing.log").read_text(encoding="utf-8")
    assert "FAILED non-fatal" in processing_log
    assert "exit 1" in processing_log


def test_no_raw_devnull_remains_on_the_converter_invocation() -> None:
    """Regression guard: the exact class of bug this dispatch closes (a
    converter call whose stderr is piped straight to /dev/null) must not
    reappear on either call site. Both now route through
    _run_receipt_converter_scan, which captures stderr with `2>&1 >/dev/null`
    (redirect-then-log) rather than discarding it — this asserts no sibling
    line still throws it away directly."""
    text = RP_SCRIPT.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "report_to_receipt_converter.py" in line and "2>/dev/null" in line:
            raise AssertionError(
                f"found a converter invocation still discarding stderr: {line!r}"
            )
