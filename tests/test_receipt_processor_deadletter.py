"""Tests for OI-1085/OI-1086: receipt-processor dead-letter + log damping.

Exercises the REAL receipt_processor.sh functions by sourcing the production
script in _RP_LIB_MODE=1 against a fully sandboxed state dir — no
reimplemented logic. Covers:

- A report refused N times on the same deterministic code (missing_model) is
  moved to the dead-letter directory and never retried again.
- Refusals below the threshold leave the report in place (the mid-write race
  a transient invalid_json can self-heal from).
- Transient codes (unexpected_error) are never counted toward dead-letter.
- A restored copy of a dead-lettered report is skipped via the processed-hash
  record.
- The 'Report too old' DEBUG flood is damped: one line per report per
  process plus one aggregate line per scan cycle, instead of one line per
  report per cycle (93% of the 4.3 GB log that triggered OI-1085).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
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


def _make_dirs(tmp: Path) -> dict:
    # vnx_paths.sh derives VNX_STATE_DIR as $VNX_DATA_DIR/state — both must
    # agree and exist, so state lives under data/state here.
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


def _run_bash(dirs: dict, body: str) -> subprocess.CompletedProcess:
    cmd = ENV_PREAMBLE.format(rp_script=RP_SCRIPT, **dirs) + body
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_deadletter_after_n_identical_rejections():
    """3 identical missing_model refusals -> report moved to dead-letter dir,
    indexed, and its hash recorded as processed. Attempts 1 and 2 leave the
    report in place."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        report = dirs["unified"] / "20260808-120000-A-broken-report.md"
        report.write_text("# report with no model anywhere\n")

        body = f"""
rc1=0; record_rejection_and_maybe_deadletter "{report}" "$(basename "{report}")" missing_model || rc1=$?
[ -f "{report}" ] && in_place_1=yes || in_place_1=no
rc2=0; record_rejection_and_maybe_deadletter "{report}" "$(basename "{report}")" missing_model || rc2=$?
[ -f "{report}" ] && in_place_2=yes || in_place_2=no
rc3=0; record_rejection_and_maybe_deadletter "{report}" "$(basename "{report}")" missing_model || rc3=$?
[ -f "{report}" ] && in_place_3=yes || in_place_3=no
echo "rc=$rc1,$rc2,$rc3 in_place=$in_place_1,$in_place_2,$in_place_3"
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        assert "rc=1,1,0 in_place=yes,yes,no" in result.stdout, result.stdout

        deadletter = dirs["state"] / "receipt_deadletter"
        moved = deadletter / report.name
        assert moved.is_file(), f"report not quarantined: {list(deadletter.iterdir()) if deadletter.exists() else 'no dir'}"
        assert moved.read_text() == "# report with no model anywhere\n"

        index = (deadletter / "INDEX.txt").read_text()
        assert report.name in index and "missing_model" in index

        processed = (dirs["state"] / "processed_receipts.txt").read_text().splitlines()
        assert len(processed) == 1, "dead-lettered hash must be recorded as processed exactly once"

        rejections = (dirs["state"] / "receipt_rejections.txt").read_text().strip()
        assert rejections.endswith(" 3"), f"rejection count must reach threshold: {rejections}"


def test_transient_code_is_never_counted():
    """unexpected_error (crash/IO) is transient: not counted, never dead-lettered."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        report = dirs["unified"] / "20260808-120001-A-flaky.md"
        report.write_text("# flaky\n")

        body = f"""
for i in 1 2 3 4 5; do
  record_rejection_and_maybe_deadletter "{report}" "$(basename "{report}")" unexpected_error || :
done
[ -f "{report}" ] && echo "in_place=yes" || echo "in_place=no"
[ -f "{dirs['state']}/receipt_rejections.txt" ] && echo "rejections_file=yes" || echo "rejections_file=no"
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        assert "in_place=yes" in result.stdout
        assert "rejections_file=no" in result.stdout


def test_distinct_codes_count_independently():
    """A report refused on two different codes needs N of the SAME code."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        report = dirs["unified"] / "20260808-120002-A-two-codes.md"
        report.write_text("# two codes\n")
        name = report.name

        body = f"""
record_rejection_and_maybe_deadletter "{report}" "{name}" missing_model || :
record_rejection_and_maybe_deadletter "{report}" "{name}" invalid_json || :
record_rejection_and_maybe_deadletter "{report}" "{name}" missing_model || :
[ -f "{report}" ] && echo "in_place=yes" || echo "in_place=no"
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        # 2x missing_model + 1x invalid_json — neither code reached 3.
        assert "in_place=yes" in result.stdout


def test_restored_copy_is_skipped_via_processed_hash():
    """If a dead-lettered report is restored into the scan directory, the
    processed-hash record makes should_process_report skip it (fresh content
    hash would start a new count — same bytes stay quarantined)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        report = dirs["unified"] / "20260808-120003-A-restored.md"
        report.write_text("# restored\n")

        body = f"""
for i in 1 2 3; do
  record_rejection_and_maybe_deadletter "{report}" "{report.name}" missing_model || :
done
cp "{dirs['state']}/receipt_deadletter/{report.name}" "{report}"
if should_process_report "{report}"; then echo "verdict=process" ; else echo "verdict=skip"; fi
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        assert "verdict=skip" in result.stdout


def test_too_old_log_damped_to_first_occurrence_plus_cycle_summary():
    """The OI-1085 flood fix: repeated too-old skips of the same report emit
    ONE per-report line (first occurrence) and the counter feeds ONE
    aggregate line per cycle via log_too_old_cycle_summary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        old_report = dirs["unified"] / "20260801-000000-A-ancient.md"
        old_report.write_text("# ancient\n")
        old_ts = int(time.time()) - 48 * 3600
        os.utime(str(old_report), (old_ts, old_ts))

        # Two full simulated scan cycles over the same old report.
        body = f"""
should_process_report "{old_report}" || :
should_process_report "{old_report}" || :
should_process_report "{old_report}" || :
log_too_old_cycle_summary
should_process_report "{old_report}" || :
log_too_old_cycle_summary
log_too_old_cycle_summary
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        out = result.stderr + result.stdout  # log() writes via tee to stderr

        per_report = [ln for ln in out.splitlines() if "Report too old:" in ln]
        assert len(per_report) == 1, f"expected exactly one per-report line, got {len(per_report)}: {per_report}"
        assert old_report.name in per_report[0]
        assert "age:" in per_report[0]

        summaries = [ln for ln in out.splitlines() if "too-old report(s) this cycle" in ln]
        assert len(summaries) == 2, f"expected one summary per non-empty cycle, got {len(summaries)}: {summaries}"
        assert "Skipped 2 too-old report(s)" in summaries[0]
        assert "Skipped 1 too-old report(s)" in summaries[1]


def test_threshold_is_configurable_via_env():
    """VNX_RECEIPT_DEADLETTER_THRESHOLD=1 quarantines on the first refusal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dirs = _make_dirs(tmp)
        report = dirs["unified"] / "20260808-120004-A-threshold.md"
        report.write_text("# threshold\n")

        body = f"""
export VNX_RECEIPT_DEADLETTER_THRESHOLD=1
DEADLETTER_THRESHOLD=1
rc=0; record_rejection_and_maybe_deadletter "{report}" "{report.name}" missing_model || rc=$?
echo "rc=$rc"
"""
        result = _run_bash(dirs, body)
        assert result.returncode == 0, f"harness failed: {result.stderr}"
        assert "rc=0" in result.stdout
        assert not report.exists()
