"""Runs a job script's real ``__main__`` with argparse forced to raise (OI-3079 crash tests)."""
import subprocess
import sys
from pathlib import Path

CRASH_WRAPPER = (
    "import argparse, runpy, sys\n"
    "script = sys.argv[1]\n"
    "sys.argv = [script] + sys.argv[2:]\n"
    "def _boom(self, *a, **k):\n"
    "    raise RuntimeError('forced crash')\n"
    "argparse.ArgumentParser.parse_args = _boom\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def run_script_crashing(script: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-c", CRASH_WRAPPER, str(script), *args],
        capture_output=True, text=True, timeout=60,
    )
