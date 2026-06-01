"""Unit tests for scripts/lib/rp_find_newest_report_mtime.py

Tests exercise the Python module directly (find_newest_report_mtime) and as a
CLI subprocess to verify the interface used by receipt_processor.sh.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Allow importing the module directly.
_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "rp_find_newest_report_mtime.py"
sys.path.insert(0, str(_LIB.parent))

from rp_find_newest_report_mtime import find_newest_report_mtime  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the script as a CLI subprocess."""
    return subprocess.run(
        [sys.executable, str(_LIB), *args],
        capture_output=True,
        text=True,
    )


def _make_report(directory: Path, name: str, mtime: int) -> Path:
    f = directory / name
    f.write_text("# dummy report")
    os.utime(str(f), (mtime, mtime))
    return f


# ---------------------------------------------------------------------------
# Unit tests — find_newest_report_mtime()
# ---------------------------------------------------------------------------

def test_returns_max_mtime_across_both_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        headless = tmp / "headless"
        unified.mkdir()
        headless.mkdir()

        t1 = 1_700_000_000
        t2 = 1_700_001_000  # newer

        _make_report(unified, "a.md", t1)
        _make_report(headless, "b.md", t2)

        result = find_newest_report_mtime(str(unified), str(headless), fallback=0)
        assert result == t2, f"Expected {t2}, got {result}"


def test_uses_unified_when_headless_absent():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        unified.mkdir()

        t1 = 1_700_000_000
        _make_report(unified, "r.md", t1)

        result = find_newest_report_mtime(str(unified), str(tmp / "nonexistent"), fallback=0)
        assert result == t1


def test_fallback_when_no_reports():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        headless = tmp / "headless"
        unified.mkdir()
        headless.mkdir()

        fallback = 1_600_000_000
        result = find_newest_report_mtime(str(unified), str(headless), fallback=fallback)
        assert result == fallback, f"Expected fallback {fallback}, got {result}"


def test_fallback_when_both_dirs_absent():
    fallback = 1_600_000_000
    result = find_newest_report_mtime("/nonexistent_a", "/nonexistent_b", fallback=fallback)
    assert result == fallback


def test_ignores_non_md_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        unified = tmp / "unified"
        unified.mkdir()

        t_md = 1_700_000_000
        t_other = 1_700_999_999  # would be newer if counted

        _make_report(unified, "report.md", t_md)
        txt = unified / "notes.txt"
        txt.write_text("ignored")
        os.utime(str(txt), (t_other, t_other))

        result = find_newest_report_mtime(str(unified), "/nonexistent", fallback=0)
        assert result == t_md, f"Non-.md file must be ignored; expected {t_md}, got {result}"


def test_stat_failure_skipped_others_still_counted(capsys, tmp_path):
    """Broken symlink triggers OSError; remaining files are still considered."""
    unified = tmp_path / "unified"
    unified.mkdir()

    t_good = 1_700_000_000
    _make_report(unified, "good.md", t_good)

    broken = unified / "broken.md"
    broken.symlink_to("/nonexistent_vnx_test_target/report.md")

    result = find_newest_report_mtime(str(unified), "/nonexistent", fallback=0)
    assert result == t_good, f"Expected {t_good} despite broken symlink, got {result}"

    captured = capsys.readouterr()
    assert "warning: stat failed" in captured.err


def test_single_report_equals_its_mtime():
    with tempfile.TemporaryDirectory() as tmpdir:
        unified = Path(tmpdir) / "unified"
        unified.mkdir()
        t = 1_710_000_000
        _make_report(unified, "only.md", t)

        result = find_newest_report_mtime(str(unified), "/nonexistent", fallback=0)
        assert result == t


# ---------------------------------------------------------------------------
# CLI tests — subprocess interface used by Bash
# ---------------------------------------------------------------------------

def test_cli_outputs_integer_to_stdout():
    with tempfile.TemporaryDirectory() as tmpdir:
        unified = Path(tmpdir) / "u"
        unified.mkdir()
        t = 1_720_000_000
        _make_report(unified, "r.md", t)

        proc = _run_cli(str(unified), "/nonexistent", "0")
        assert proc.returncode == 0, f"CLI failed: {proc.stderr}"
        assert proc.stdout.strip().isdigit(), f"Expected integer stdout, got: {proc.stdout!r}"
        assert int(proc.stdout.strip()) == t


def test_cli_fallback_when_no_reports():
    with tempfile.TemporaryDirectory() as tmpdir:
        unified = Path(tmpdir) / "u"
        unified.mkdir()
        fallback = 1_600_000_000

        proc = _run_cli(str(unified), "/nonexistent", str(fallback))
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == fallback


def test_cli_missing_args_exits_nonzero():
    proc = _run_cli("only_one_arg")
    assert proc.returncode != 0
    assert "Usage" in proc.stderr


def test_cli_invalid_fallback_exits_nonzero():
    proc = _run_cli("/tmp", "/tmp", "not_a_number")
    assert proc.returncode != 0
    assert "error" in proc.stderr.lower()


def test_cli_warns_on_stat_failure_stderr():
    """Broken symlink → warning on stderr, still exits 0, mtime from good files used."""
    with tempfile.TemporaryDirectory() as tmpdir:
        unified = Path(tmpdir) / "u"
        unified.mkdir()
        t = 1_700_000_000
        _make_report(unified, "good.md", t)

        broken = unified / "broken.md"
        broken.symlink_to("/nonexistent_vnx_test_target/report.md")

        proc = _run_cli(str(unified), "/nonexistent", "0")
        assert proc.returncode == 0, f"CLI must exit 0 despite stat failure: {proc.stderr}"
        assert "warning: stat failed" in proc.stderr
        assert int(proc.stdout.strip()) == t
