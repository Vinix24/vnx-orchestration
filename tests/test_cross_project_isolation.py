#!/usr/bin/env python3
"""Tests for OI-1067 cross-project dispatch contamination guard.

Coverage
========
A. ``vnx_dispatch_extract_project_id`` extracts the Project-ID stamp.
B. Legacy dispatch (no Project-ID line) → status='legacy', rc=0, file untouched.
C. Matching Project-ID → status='match', rc=0, file untouched.
D. Mismatching Project-ID → status='reject', rc=1, file moved to rejected/
   with a [REJECTED: project_id mismatch] marker appended.
E. Re-running the guard on an already-rejected dispatch is idempotent
   (no duplicate marker, no error).
F. Malformed expected project_id (allowlist violation) → status='fatal', rc=2.
G. ``vnx_dispatch_resolve_project_id`` honors VNX_PROJECT_ID env override
   and rejects malformed values; default falls back to 'vnx-dev'.
H. ``vnx_dispatch_assert_dir_under`` returns 0 when child is under parent
   and 1 otherwise — pinning the path-scoping invariant used at startup.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SH = REPO_ROOT / "scripts" / "lib" / "dispatch_project_guard.sh"
META_SH = REPO_ROOT / "scripts" / "lib" / "dispatch_metadata.sh"


def _run_bash(snippet: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/")}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", snippet],
        env=full_env,
        capture_output=True,
        text=True,
    )


def _write_dispatch(path: Path, project_id: str | None) -> None:
    body = ["[[TARGET:A]]", "Track: A", "Role: backend-developer", "Gate: test-gate"]
    if project_id is not None:
        body.append(f"Project-ID: {project_id}")
    body.append("")
    body.append("Instruction:")
    body.append("noop")
    body.append("[[DONE]]")
    path.write_text("\n".join(body) + "\n")


# ── A. extractor ────────────────────────────────────────────────────────────

def test_extract_project_id_returns_stamped_value(tmp_path):
    d = tmp_path / "d.md"
    _write_dispatch(d, "tenant-a")
    out = _run_bash(
        f'set -euo pipefail; source "{META_SH}"; vnx_dispatch_extract_project_id "{d}"',
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "tenant-a"


def test_extract_project_id_returns_empty_for_legacy(tmp_path):
    d = tmp_path / "d.md"
    _write_dispatch(d, project_id=None)
    out = _run_bash(
        f'set -euo pipefail; source "{META_SH}"; vnx_dispatch_extract_project_id "{d}"',
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# ── B/C/D. validate guard cases ─────────────────────────────────────────────

_GUARD_HARNESS = """
set -uo pipefail
source "{meta}"
source "{guard}"
status=$(vnx_dispatch_validate_project_id "$1" "$2" "$3")
rc=$?
printf 'STATUS=%s\\nRC=%s\\n' "$status" "$rc"
"""


def _run_guard(dispatch: Path, expected: str, rejected_dir: Path) -> tuple[str, int]:
    snippet = _GUARD_HARNESS.format(meta=META_SH, guard=GUARD_SH)
    proc = subprocess.run(
        ["bash", "-c", snippet, "harness", str(dispatch), expected, str(rejected_dir)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/")},
    )
    lines = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)
    return lines.get("STATUS", ""), int(lines.get("RC", "-1"))


def test_legacy_dispatch_accepted(tmp_path):
    d = tmp_path / "legacy.md"
    rej = tmp_path / "rejected"
    rej.mkdir()
    _write_dispatch(d, project_id=None)
    status, rc = _run_guard(d, "vnx-dev", rej)
    assert status == "legacy"
    assert rc == 0
    assert d.exists(), "legacy dispatch must not be moved"
    assert list(rej.iterdir()) == []


def test_matching_project_id_accepted(tmp_path):
    d = tmp_path / "match.md"
    rej = tmp_path / "rejected"
    rej.mkdir()
    _write_dispatch(d, project_id="tenant-a")
    status, rc = _run_guard(d, "tenant-a", rej)
    assert status == "match"
    assert rc == 0
    assert d.exists()
    assert "[REJECTED:" not in d.read_text()


def test_mismatching_project_id_rejected_and_moved(tmp_path):
    d = tmp_path / "foreign.md"
    rej = tmp_path / "rejected"
    rej.mkdir()
    _write_dispatch(d, project_id="tenant-b")
    status, rc = _run_guard(d, "tenant-a", rej)
    assert status == "reject"
    assert rc == 1
    assert not d.exists(), "rejected dispatch must be moved out of source dir"
    moved = rej / "foreign.md"
    assert moved.exists()
    text = moved.read_text()
    assert "[REJECTED: project_id mismatch]" in text
    assert "tenant-b" in text and "tenant-a" in text


def test_repeat_reject_is_idempotent(tmp_path):
    d = tmp_path / "again.md"
    rej = tmp_path / "rejected"
    rej.mkdir()
    _write_dispatch(d, project_id="tenant-b")
    _run_guard(d, "tenant-a", rej)
    moved = rej / "again.md"
    # Re-run the guard on the already-rejected file.
    status, rc = _run_guard(moved, "tenant-a", rej)
    assert status == "reject"
    assert rc == 1
    assert moved.read_text().count("[REJECTED: project_id mismatch]") == 1


# ── E. malformed expected pid ───────────────────────────────────────────────

def test_malformed_expected_project_id_returns_fatal(tmp_path):
    d = tmp_path / "ok.md"
    rej = tmp_path / "rejected"
    rej.mkdir()
    _write_dispatch(d, project_id="tenant-a")
    status, rc = _run_guard(d, "INVALID UPPER", rej)
    assert status == "fatal"
    assert rc == 2
    # Dispatch must not be moved on fatal errors.
    assert d.exists()


# ── F. resolve_project_id ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "env_pid,expected_stdout,expected_rc",
    [
        (None, "vnx-dev", 0),
        ("", "vnx-dev", 0),
        ("tenant-a", "tenant-a", 0),
        ("Bad", "", 1),
        ("0bad", "", 1),
        ("a" * 33, "", 1),
    ],
)
def test_resolve_project_id(env_pid, expected_stdout, expected_rc):
    snippet = (
        f'set -uo pipefail; source "{GUARD_SH}"; '
        'vnx_dispatch_resolve_project_id; printf "RC=%s\\n" "$?"'
    )
    env: dict = {}
    if env_pid is not None:
        env["VNX_PROJECT_ID"] = env_pid
    out = _run_bash(snippet, env=env)
    lines = out.stdout.strip().splitlines()
    rc_line = next(l for l in lines if l.startswith("RC="))
    assert int(rc_line.split("=", 1)[1]) == expected_rc
    if expected_stdout:
        assert expected_stdout in [l for l in lines if not l.startswith("RC=")]


# ── G. assert_dir_under (path scoping invariant) ────────────────────────────

def test_assert_dir_under_accepts_descendant(tmp_path):
    parent = tmp_path / "data"
    child = parent / "dispatches"
    child.mkdir(parents=True)
    snippet = (
        f'set -uo pipefail; source "{GUARD_SH}"; '
        f'vnx_dispatch_assert_dir_under "{child}" "{parent}"; printf "RC=%s\\n" "$?"'
    )
    out = _run_bash(snippet)
    assert "RC=0" in out.stdout


def test_assert_dir_under_rejects_sibling(tmp_path):
    proj_a = tmp_path / "projectA" / ".vnx-data"
    proj_b = tmp_path / "projectB" / ".vnx-data"
    proj_a.mkdir(parents=True)
    (proj_b / "dispatches").mkdir(parents=True)
    snippet = (
        f'set -uo pipefail; source "{GUARD_SH}"; '
        f'vnx_dispatch_assert_dir_under "{proj_b / "dispatches"}" "{proj_a}"; '
        'printf "RC=%s\\n" "$?"'
    )
    out = _run_bash(snippet)
    assert "RC=1" in out.stdout


def test_assert_dir_under_accepts_self(tmp_path):
    parent = tmp_path / "data"
    parent.mkdir()
    snippet = (
        f'set -uo pipefail; source "{GUARD_SH}"; '
        f'vnx_dispatch_assert_dir_under "{parent}" "{parent}"; printf "RC=%s\\n" "$?"'
    )
    out = _run_bash(snippet)
    assert "RC=0" in out.stdout
