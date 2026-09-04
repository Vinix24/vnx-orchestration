"""Tests for OI D3: a report_to_receipt_converter REJECTED must be visible
in the receipt_processor.sh log.

Bug: _ppr_process_rate_limited() ran the converter with ``2>/dev/null``,
throwing away its stderr — and that is exactly where the converter's
fail-closed REJECTED warning is logged (Python's ``logging`` default handler
writes to stderr). The converter's own ``main()`` always returns 0 even when
it rejected reports this scan (a rejection is not a crash), so the
``|| log ERROR ... (exit $?)`` fallback never fired for this case either —
the two defects compounded into total silence: a refused report vanished
from the audit trail with zero trace anywhere in the log.

Exercises the REAL receipt_processor.sh function (_ppr_process_rate_limited)
by sourcing the production script in _RP_LIB_MODE=1, with SCRIPTS_DIR
redirected to a stub converter that reproduces the real converter's
behavior: prints a WARNING to stderr, exits 0.
"""

from __future__ import annotations

import subprocess
import tempfile
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
    "REJECTED (fail-closed) dispatch=DISP-STUB file=stub.md reason=missing model"
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
    return stub_scripts


def _run_bash(dirs: dict, body: str) -> subprocess.CompletedProcess:
    cmd = ENV_PREAMBLE.format(rp_script=RP_SCRIPT, **dirs) + body
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_rejected_warning_is_not_silently_lost_first_confirm_it_would_show(tmp_path: Path) -> None:
    """Nul-is-eerst-een-meetfout sanity check: before trusting an assertion
    that REJECTED is absent from the log, prove the search mechanism itself
    would find a known-present string. Run the stub converter directly
    (no receipt_processor.sh redirection at all) and confirm its stderr
    really does carry the REJECTED text."""
    stub_scripts = _make_stub_converter(tmp_path)
    converter = stub_scripts / "lib" / "report_to_receipt_converter.py"
    result = subprocess.run(
        ["python3", str(converter), "--state-dir", str(tmp_path), str(tmp_path), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert "REJECTED (fail-closed)" in result.stderr, (
        f"sanity check failed: stub converter did not write REJECTED to stderr: {result.stderr!r}"
    )
    assert result.returncode == 0


def test_rejected_warning_reaches_the_processing_log(tmp_path: Path) -> None:
    """The real defect: with the current receipt_processor.sh, does the
    converter's REJECTED warning land in $PROCESSING_LOG (receipt_processing.log)?"""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_ppr_process_rate_limited
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"

    processing_log = dirs["state"] / "receipt_processing.log"
    assert processing_log.is_file(), "receipt_processing.log was never created"
    log_contents = processing_log.read_text(encoding="utf-8")

    assert "REJECTED (fail-closed)" in log_contents, (
        "converter's REJECTED warning never reached the processing log "
        f"(stderr was thrown away). log contents:\n{log_contents}"
    )
    assert "1 rejected, 0 malformed, 0 error(s) this scan" in log_contents


def test_rejected_warning_also_reaches_stderr_stream(tmp_path: Path) -> None:
    """log() tees to both the log file AND stderr — confirm the live
    stream (what an operator watching the daemon sees) carries it too."""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_ppr_process_rate_limited
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    assert "REJECTED (fail-closed)" in result.stderr


def test_non_fatal_design_preserved_on_a_real_crash(tmp_path: Path) -> None:
    """Sanity: a genuine converter crash (nonzero exit) must still be
    logged as a non-fatal ERROR and must NOT abort the processor — the
    non-fatal contract is explicit-by-design and this fix must not change it."""
    dirs = _make_dirs(tmp_path)
    stub_scripts = _make_stub_converter(tmp_path, exit_code=1)

    body = f"""
SCRIPTS_DIR="{stub_scripts}"
_ppr_process_rate_limited
echo "REACHED_END=yes"
"""
    result = _run_bash(dirs, body)
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    assert "REACHED_END=yes" in result.stdout, "processor must continue past a converter crash"

    processing_log = (dirs["state"] / "receipt_processing.log").read_text(encoding="utf-8")
    assert "FAILED non-fatal" in processing_log
    assert "exit 1" in processing_log
