"""OI-3100: exercise the real poll cycle and age gate in an isolated store."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
CUTOFF = 1700000000


def reports(tmp_path, old=0, fresh=3):
    for directory in ("unified", "headless", "state", "bin"):
        (tmp_path / directory).mkdir(exist_ok=True)
    for i in range(old + fresh):
        path = tmp_path / ("unified" if i % 2 else "headless") / f"report-{i}.md"
        path.write_text(f"report {i}\n")
        stamp = CUTOFF - 1 if i < old else CUTOFF + i - old
        os.utime(path, (stamp, stamp))
    (tmp_path / "state/receipt_processor_watermark").write_text(str(CUTOFF))
    (tmp_path / "state/processed_receipts.txt").write_text("")


def run_cycle(tmp_path, body="_poll_new_reports_cycle 1 100", mode="monitor", script=None):
    for name in ("basename", "stat"):
        shim = tmp_path / "bin" / name
        shim.write_text(f'#!/bin/sh\nprintf "{name}\\n" >> "$FORK_LOG"\n'
                        f'exec {shlex.quote(shutil.which(name))} "$@"\n')
        shim.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(VNX_HOME=str(REPO), VNX_DATA_DIR=str(tmp_path), VNX_DATA_DIR_EXPLICIT="1",
               VNX_STATE_DIR=str(tmp_path / "state"), VNX_REPORTS_DIR=str(tmp_path / "unified"),
               VNX_HEADLESS_REPORTS_DIR=str(tmp_path / "headless"), _RP_LIB_MODE="1",
               FORK_LOG=str(tmp_path / "forks"), VNX_MODE=mode)
    preamble = f'''
set -u
source {shlex.quote(str(script or REPO / "scripts/receipt_processor.sh"))} || exit 99
RP_PYTHON={shlex.quote(sys.executable)}
PATH={shlex.quote(str(tmp_path / "bin"))}:$PATH
original=$(declare -f _spr_get_cutoff_seconds)
eval "${{original/_spr_get_cutoff_seconds/_original_cutoff}}"
_spr_get_cutoff_seconds() {{ printf 'cutoff\\n' >> "$STATE_DIR/cutoff_calls"; _original_cutoff; }}
# Observe the processing boundary; the real age/hash gate runs before it.
process_single_report() {{ printf '%s\\0' "$1" >> "$STATE_DIR/selected"; }}
'''
    return subprocess.run(["/bin/bash", "-c", preamble + body], env=env,
                          text=True, capture_output=True, timeout=1800)


def selected(tmp_path):
    path = tmp_path / "state/selected"
    return path.read_bytes().split(b"\0")[:-1] if path.exists() else []


def test_poll_prefilters_history_before_per_file_work(tmp_path):
    reports(tmp_path, old=3000)
    result = run_cycle(tmp_path)
    assert result.returncode == 0, result.stderr
    assert {Path(os.fsdecode(p)).name for p in selected(tmp_path)} == {
        "report-3000.md", "report-3001.md", "report-3002.md"}
    forks = (tmp_path / "forks").read_text().splitlines()
    assert len(forks) < 50, f"basename/stat forks: {len(forks)}"
    assert (tmp_path / "state/cutoff_calls").read_text().splitlines() == ["cutoff"]
    log = (tmp_path / "state/receipt_processing.log").read_text()
    assert log.count("Skipped 3000 too-old report(s) this cycle") == 1
    assert "Report too old:" not in log


@pytest.mark.parametrize("mode", ["monitor", "catchup", "manual"])
@pytest.mark.parametrize("watermark", [str(CUTOFF), "invalid", None])
def test_candidates_match_age_gate_including_equal_cutoff(tmp_path, mode, watermark):
    reports(tmp_path, old=1)
    wm = tmp_path / "state/receipt_processor_watermark"
    if watermark is None:
        wm.rename(tmp_path / "prior-watermark")
    else:
        wm.write_text(watermark)
    unusual = tmp_path / "unified/space ' and\nnewline.md"
    unusual.write_text("unusual name")
    os.utime(unusual, (CUTOFF, CUTOFF))
    (tmp_path / "unified/directory.md").mkdir()
    (tmp_path / "unified/.hidden.md").write_text("hidden")
    result = run_cycle(tmp_path, f'''
MAX_AGE_HOURS=24
date() {{ if [ "$1" = +%s ]; then echo {CUTOFF + 86400}; else command date "$@"; fi; }}
for report in "$UNIFIED_REPORTS"/*.md "$HEADLESS_REPORTS"/*.md; do
    [ -f "$report" ] || continue
    if should_process_report "$report"; then printf '%s\\0' "$report" >> "$STATE_DIR/expected"; fi
done
_poll_new_reports_cycle 1 100
''', mode)
    assert result.returncode == 0, result.stderr
    assert selected(tmp_path) == (tmp_path / "state/expected").read_bytes().split(b"\0")[:-1]
    assert len(selected(tmp_path)) == 4  # Includes two files exactly at cutoff.


@pytest.mark.parametrize("missing", [("headless",), ("unified",), ("unified", "headless")])
def test_missing_directories_complete_cycle_with_both_heartbeats(tmp_path, missing):
    reports(tmp_path, fresh=2)
    for directory in missing:
        (tmp_path / directory).rename(tmp_path / f"prior-{directory}")
    expected = {os.fsencode(p) for directory in ("unified", "headless")
                for p in (tmp_path / directory).glob("*.md")}
    result = run_cycle(tmp_path, "PS4='+ '; set -x\n_poll_new_reports_cycle 6 100")
    assert result.returncode == 0, result.stderr
    assert "[ERROR]" not in result.stderr
    assert set(selected(tmp_path)) == expected
    heartbeat, scan = "+ _write_processor_heartbeat 6", "+ _run_receipt_converter_scan"
    trace = [line for line in result.stderr.splitlines() if line in (heartbeat, scan)]
    assert trace == [heartbeat, scan, heartbeat]
    assert (tmp_path / "state/receipt_processor_heartbeat.json").is_file()


@pytest.mark.parametrize("failure", ["not_directory", "unreadable", "dangling", "loop"])
def test_scan_problem_continues_cycle_logs_once_and_recovers(tmp_path, failure):
    if failure == "unreadable" and os.geteuid() == 0:
        pytest.skip("root can scan chmod 000 directories")
    reports(tmp_path, fresh=2)
    setup, repair = "", ""
    if failure == "not_directory":
        setup, repair = 'HEADLESS_REPORTS="$WATERMARK_FILE"', f'HEADLESS_REPORTS="{tmp_path / "headless"}"'
    elif failure == "unreadable":
        (tmp_path / "headless").chmod(0)
        repair = 'chmod 755 "$HEADLESS_REPORTS"'
    else:
        broken = tmp_path / "unified/broken ' name.md"
        broken.symlink_to(broken if failure == "loop" else tmp_path / "missing")
    try:
        result = run_cycle(tmp_path, f'''{setup}
_run_receipt_converter_scan() {{ :; }}
PS4='+ '; set -x
for cycle in 6 12; do
    _poll_new_reports_cycle "$cycle" 6 || exit 1
    echo "ERRORS=$_SPR_POLL_SCAN_ERRORS WATERMARK=$(cat "$WATERMARK_FILE")"
done
{repair}
_poll_new_reports_cycle 18 6
echo "FINAL_ERRORS=$_SPR_POLL_SCAN_ERRORS"
''')
    finally:
        (tmp_path / "headless").chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert os.fsencode(tmp_path / "unified/report-1.md") in selected(tmp_path)
    error = {"not_directory": "NotADirectoryError", "unreadable": "PermissionError",
             "dangling": "FileNotFoundError", "loop": "OSError"}[failure]
    log = (tmp_path / "state/receipt_processing.log").read_text()
    assert sum("[ERROR]" in line and error in line for line in log.splitlines()) == 1
    held = failure in ("not_directory", "unreadable")
    assert f"FINAL_ERRORS={0 if held else 1}" in result.stdout
    assert result.stdout.count(f"ERRORS=1 WATERMARK={CUTOFF if held else CUTOFF + 1}") == 2
    if held:
        assert selected(tmp_path)[-1] == os.fsencode(tmp_path / "headless/report-0.md")
    assert (tmp_path / "state/receipt_processor_watermark").read_text().strip() == str(CUTOFF + 1)
    trace = [line for line in result.stderr.splitlines() if line.startswith((
        "+ _write_processor_heartbeat ", "+ _run_receipt_converter_scan", "+ _retry_pending_receipts"))]
    assert trace == [line for cycle in (6, 12, 18) for line in (
        f"+ _write_processor_heartbeat {cycle}", "+ _run_receipt_converter_scan",
        f"+ _write_processor_heartbeat {cycle}", "+ _retry_pending_receipts")]


@pytest.mark.parametrize("failure", ["python", "cutoff", "invalid", "empty", "truncated"])
def test_prefilter_failure_is_visible_without_processing_or_watermark_advance(tmp_path, failure):
    reports(tmp_path)
    if failure == "python":
        body = 'RP_PYTHON="$STATE_DIR/missing-python"\n'
    elif failure == "cutoff":
        body = '_spr_get_cutoff_seconds() { return 1; }\n'
    elif failure == "invalid":
        body = '_spr_get_cutoff_seconds() { echo unknown; }\n'
    elif failure == "truncated":
        body = 'RP_PYTHON=partial; partial() { printf "F%s\\0" "$UNIFIED_REPORTS/report-1.md"; }\n'
    else:
        body = '_spr_get_cutoff_seconds() { printf ""; }\n'
    result = run_cycle(tmp_path, body + "_poll_new_reports_cycle 1 100")
    assert result.returncode == 1, result.stderr
    assert "prefilter failed" in result.stderr
    assert selected(tmp_path) == []
    assert (tmp_path / "state/receipt_processor_watermark").read_text() == str(CUTOFF)


def test_empty_cycle_and_hash_dedup_are_preserved(tmp_path):
    reports(tmp_path, fresh=0)
    result = run_cycle(tmp_path)
    assert result.returncode == 0, result.stderr
    assert selected(tmp_path) == []
    reports(tmp_path, fresh=1)
    result = run_cycle(tmp_path, '''
_sha256 "$HEADLESS_REPORTS/report-0.md" > "$PROCESSED_HASHES"
_poll_new_reports_cycle 1 100
''')
    assert result.returncode == 0, result.stderr
    assert selected(tmp_path) == []
    assert "Already processed: report-0.md" in result.stderr
