"""Ledger PR 2: the receipt processor writes receipt_processor_heartbeat.json
twice per poll cycle, atomically, with last_cycle_seconds.

Sources scripts/receipt_processor.sh in lib mode (like
test_receipt_processor_poll_converter_rejection_visible.py) and runs real
cycles against a temporary state dir with a stub converter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

RP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

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
    data_dir = tmp / "data"
    dirs = {
        "unified": tmp / "unified", "headless": tmp / "headless",
        "state": data_dir / "state", "pids": tmp / "pids",
        "locks": tmp / "locks", "data_dir": data_dir,
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _make_stub_scripts(tmp: Path) -> Path:
    """The real scripts/lib tree with only the converter replaced by a quiet stub."""
    stub = tmp / "stub_scripts"
    lib = stub / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    conv = lib / "report_to_receipt_converter.py"
    conv.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        sys.exit(0)
        """), encoding="utf-8")
    real_lib = RP_SCRIPT.parent / "lib"
    for entry in real_lib.iterdir():
        if entry.name in (conv.name, "__pycache__"):
            continue
        (lib / entry.name).symlink_to(entry)
    return stub


def _run_bash(dirs: dict, body: str) -> subprocess.CompletedProcess:
    cmd = ENV_PREAMBLE.format(rp_script=RP_SCRIPT, **dirs) + body
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def _hb(dirs: dict) -> dict:
    return json.loads((dirs["state"] / "receipt_processor_heartbeat.json").read_text(encoding="utf-8"))


def test_one_cycle_writes_the_heartbeat_with_the_full_schema(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    result = _run_bash(dirs, f'SCRIPTS_DIR="{stub}"\n_poll_new_reports_cycle 1 100\n')
    assert result.returncode == 0, result.stderr
    hb_path = dirs["state"] / "receipt_processor_heartbeat.json"
    assert hb_path.is_file(), "no heartbeat file after one cycle"
    hb = _hb(dirs)
    assert set(hb) == {
        "schema", "pid", "cycle", "started_at", "updated_at", "last_progress_at",
        "last_cycle_seconds", "watermark_hash", "converter",
    }
    assert hb["schema"] == 1
    assert isinstance(hb["pid"], int) and hb["pid"] > 0
    assert hb["cycle"] == 1
    for key in ("started_at", "updated_at", "last_progress_at"):
        assert ISO_Z.match(hb[key]), (key, hb[key])
    assert hb["last_cycle_seconds"] == 0.0
    assert hb["converter"] == {"rejected": 0, "malformed": 0, "errors": 0}
    leftovers = [p.name for p in dirs["state"].iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"tmp file left behind: {leftovers}"


def test_heartbeat_is_written_twice_per_cycle(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    body = f"""
SCRIPTS_DIR="{stub}"
_hb_calls=0
_write_processor_heartbeat() {{ _hb_calls=$(( _hb_calls + 1 )); }}
_poll_new_reports_cycle 6 100
echo "CALLS=$_hb_calls"
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    assert "CALLS=2" in result.stdout


def test_second_write_comes_after_the_converter_scan(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    body = f"""
SCRIPTS_DIR="{stub}"
_trace=""
_write_processor_heartbeat() {{ _trace="$_trace W"; }}
_run_receipt_converter_scan() {{ _trace="$_trace S"; }}
_poll_new_reports_cycle 6 100
echo "TRACE=$_trace"
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    assert "TRACE= W S W" in result.stdout


def test_last_cycle_seconds_is_the_duration_of_the_previous_cycle(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    body = f"""
SCRIPTS_DIR="{stub}"
_retry_pending_receipts() {{ sleep 1.2; }}
_poll_new_reports_cycle 1 1
_poll_new_reports_cycle 2 1
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    hb = _hb(dirs)
    assert hb["cycle"] == 2
    assert 1.2 <= hb["last_cycle_seconds"] < 10.0, hb["last_cycle_seconds"]


def test_last_progress_at_moves_only_when_a_report_is_ingested(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    (dirs["unified"] / "report-a.md").write_text("x", encoding="utf-8")
    body = f"""
SCRIPTS_DIR="{stub}"
UNIFIED_REPORTS="{dirs['unified']}"
HEADLESS_REPORTS="{dirs['headless']}"
should_process_report() {{ return 0; }}
process_single_report() {{ return 1; }}
_poll_new_reports_cycle 1 100
sleep 1.1
_poll_new_reports_cycle 2 100
cp "{dirs['state']}/receipt_processor_heartbeat.json" "{dirs['state']}/snap_idle.json"
process_single_report() {{ return 0; }}
sleep 1.1
_poll_new_reports_cycle 3 100
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    idle = json.loads((dirs["state"] / "snap_idle.json").read_text(encoding="utf-8"))
    assert idle["last_progress_at"] == idle["started_at"], "failed ingest must not move progress"
    assert idle["updated_at"] != idle["started_at"]
    final = _hb(dirs)
    assert final["last_progress_at"] != final["started_at"], "ingest must move progress"
    assert final["last_progress_at"] > idle["last_progress_at"]


def test_write_failure_is_logged_and_the_cycle_continues(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    body = f"""
SCRIPTS_DIR="{stub}"
STATE_DIR="{tmp_path / 'does-not-exist'}"
_poll_new_reports_cycle 1 100
echo "REACHED_END=yes"
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    assert "REACHED_END=yes" in result.stdout
    log = dirs["state"] / "receipt_processing.log"
    assert not (tmp_path / "does-not-exist" / "receipt_processor_heartbeat.json").exists()
    assert log.is_file(), "the failed write must be logged"
    assert "Failed to write receipt processor heartbeat" in log.read_text(encoding="utf-8")


def test_converter_counts_come_from_the_converter_beacon(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    health = dirs["data_dir"] / "health"
    health.mkdir(parents=True, exist_ok=True)
    (health / "report_to_receipt_converter.json").write_text(json.dumps({
        "details": {"rejected_count": 3, "malformed_count": 2, "error_count": 5},
    }), encoding="utf-8")
    result = _run_bash(dirs, f'SCRIPTS_DIR="{stub}"\n_poll_new_reports_cycle 1 100\n')
    assert result.returncode == 0, result.stderr
    assert _hb(dirs)["converter"] == {"rejected": 3, "malformed": 2, "errors": 5}


def test_malformed_converter_beacon_yields_zero_counts_not_a_crash(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    health = dirs["data_dir"] / "health"
    health.mkdir(parents=True, exist_ok=True)
    (health / "report_to_receipt_converter.json").write_text("{not json", encoding="utf-8")
    result = _run_bash(dirs, f'SCRIPTS_DIR="{stub}"\n_poll_new_reports_cycle 1 100\n')
    assert result.returncode == 0, result.stderr
    assert _hb(dirs)["converter"] == {"rejected": 0, "malformed": 0, "errors": 0}


def test_watermark_hash_follows_the_watermark_file(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    (dirs["state"] / "receipt_processor_watermark").write_text("1234\n", encoding="utf-8")
    result = _run_bash(dirs, f'SCRIPTS_DIR="{stub}"\n_poll_new_reports_cycle 1 100\n')
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", _hb(dirs)["watermark_hash"])


def test_pid_is_the_processor_shell(tmp_path: Path) -> None:
    dirs = _make_dirs(tmp_path)
    stub = _make_stub_scripts(tmp_path)
    body = f'SCRIPTS_DIR="{stub}"\n_poll_new_reports_cycle 1 100\necho "PID=$$"\n'
    result = _run_bash(dirs, body)
    assert result.returncode == 0, result.stderr
    pid = int(re.search(r"PID=(\d+)", result.stdout).group(1))
    assert _hb(dirs)["pid"] == pid
    assert os.path.basename(str(RP_SCRIPT)) == "receipt_processor.sh"
