"""Tests for scripts/probe_agent_layout.sh (agents-als-map-fase-1, PR-6).

No test starts a real claude: --self-check validates the table without a CLI, and
the no-CLI test runs with a minimal PATH. A new script has no old-code red run
(the plan names this exception); the red run is the missing PROBE file itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "scripts" / "probe_agent_layout.sh"
BASH = "/bin/bash"
EXPECTED = {"A": (3, True), "C": (3, False), "D": (3, True), "H1": (4, True), "H2": (4, True), "L": (4, True), "B": (5, True)}
KEYS = {"cell", "point", "decisive", "outcome", "reason", "command", "claude_version", "sha256"}


def _run(tmp_path, *args, path=None):
    env = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))
    if path is not None:
        env["PATH"] = path
    return subprocess.run([BASH, str(PROBE), *args], capture_output=True, text=True, env=env, check=False)


def _rows(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_script_exists_and_parses():
    assert PROBE.exists(), "scripts/probe_agent_layout.sh missing"
    assert subprocess.run([BASH, "-n", str(PROBE)], check=False).returncode == 0


def test_self_check_covers_every_cell_with_the_right_flags(tmp_path):
    proc = _run(tmp_path, "--self-check")
    assert proc.returncode == 0, proc.stderr
    rows = {row["cell"]: row for row in _rows(proc.stdout)}
    assert set(rows) == set(EXPECTED)
    for cell, flags in EXPECTED.items():
        assert (rows[cell]["point"], rows[cell]["decisive"]) == flags, cell
        assert KEYS <= set(rows[cell]) and rows[cell]["outcome"] in {"ja", "nee", "niet_gemeten"}
        if rows[cell]["outcome"] == "niet_gemeten":
            assert rows[cell]["reason"], cell
    assert all(rows[c]["outcome"] == "niet_gemeten" for c in EXPECTED if c != "L")


def test_cell_l_names_a_lane_with_file_and_line(tmp_path):
    row = _rows(_run(tmp_path, "--cell", "L").stdout)[0]
    assert (row["cell"], row["outcome"]) == ("L", "ja")
    assert re.search(r"[A-Za-z0-9_./-]+\.py:\d+", row["reason"]), row["reason"]
    assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])


def test_cli_cell_without_claude_is_niet_gemeten_with_a_reason(tmp_path):
    row = _rows(_run(tmp_path, "--cell", "A", path="/usr/bin:/bin").stdout)[0]
    assert (row["cell"], row["outcome"]) == ("A", "niet_gemeten")
    assert "claude" in row["reason"] and row["sha256"] == "-"
