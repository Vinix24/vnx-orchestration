#!/usr/bin/env python3
"""Static guard: production code never builds a VNX store path from XDG.

The VNX runtime data root is the central per-project store
(``$HOME/.vnx-data/<project-id>``) — see ``_resolve_state_root`` in
``scripts/lib/vnx_paths.py`` and ``_vnx_resolve_state_root`` in
``scripts/lib/vnx_paths.sh``. PR #1437 moved both resolvers there, but a third,
inline copy in ``bin/vnx`` kept ending in an XDG data dir
(``$XDG_DATA_HOME/vnx/<id>``, falling back to ``~/.local/share/vnx/<id>``), so a
fresh install landed on a path the data-dir guard then rejected (OI-1055).

This guard scans the production source roots listed in ``SCAN_ROOTS`` for a
store path built from XDG and fails, naming every line. It reads tracked files
only (``git ls-files``): a gitignored local tree, a worktree's runtime state or
a build artifact is not shipped code and must never decide this check.

Fail-closed: an undecodable tracked file that is not binary (a NUL byte marks
a binary blob, which cannot construct a path in source) is a violation, not a
skip — the guard cannot prove it clean. An empty file list is also a failure:
a guard that scans nothing passes everything.

They allowlist below is deliberately EMPTY. PR-2 adds the detection module
that must read XDG by design; it gets an entry here, with its reason.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Production roots the guard sweeps. Runtime state (``.vnx-data/``), tests and
#: docs are out of scope by construction: they never ship as the resolver.
SCAN_ROOTS = ("bin", "scripts", "vnx_cli", "hooks")

#: Repo-relative path -> why that file may still mention an XDG store default.
#: Empty on purpose; see the module docstring.
XDG_STORE_ALLOWLIST: dict = {}

#: ``${XDG_DATA_HOME...}`` or ``$XDG_DATA_HOME`` used on a line that also holds
#: a path separator — i.e. the variable feeds a path, not an ``env -u`` list.
_XDG_VAR_PATH_RE = re.compile(r"\$\{?XDG_DATA_HOME\}?[^\n]*/")

#: The concrete XDG location, written out instead of going through the variable.
_XDG_LITERAL_PATH_RE = re.compile(r"\.local/share/vnx")


def _tracked_files(roots=SCAN_ROOTS) -> list:
    """Return repo-relative tracked paths under ``roots`` (``git ls-files``)."""
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", *roots],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return [path for path in proc.stdout.split("\0") if path]


def _read_source(path: Path) -> str:
    """Decode a tracked file as UTF-8; binary blobs read as empty text.

    A NUL byte marks a binary blob, which cannot construct a path in source.
    Anything else that refuses UTF-8 is a violation: this guard will not
    silently skip a file it cannot inspect.
    """
    data = path.read_bytes()
    if b"\x00" in data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"{path}: not UTF-8 and not binary ({exc}); the XDG store guard "
            "cannot prove this file clean"
        ) from exc


def _line_builds_xdg_store(line: str) -> bool:
    """True when ``line`` builds a store path from XDG.

    Comment-only lines are skipped: prose cannot construct a path. An
    ``env -u XDG_DATA_HOME`` / ``unset XDG_DATA_HOME`` mention carries no path
    separator after the (unexpanded) name and is therefore not flagged.
    """
    if line.lstrip().startswith("#"):
        return False
    return bool(
        _XDG_VAR_PATH_RE.search(line) or _XDG_LITERAL_PATH_RE.search(line)
    )


def find_xdg_store_defaults(files=None) -> list:
    """Return ``path:line: text`` for every XDG-built store path in ``files``."""
    violations = []
    for rel in (files if files is not None else _tracked_files()):
        if rel in XDG_STORE_ALLOWLIST:
            continue
        text = _read_source(REPO_ROOT / rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            if _line_builds_xdg_store(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    return violations


class TestNoXdgStoreDefaults:
    def test_production_code_builds_no_store_path_from_xdg(self):
        files = _tracked_files()
        assert files, (
            "git ls-files reported no tracked files under "
            f"{SCAN_ROOTS!r} — the guard scanned nothing, so it proved nothing"
        )
        violations = find_xdg_store_defaults(files)
        assert not violations, (
            "production code builds a VNX store path from XDG; the runtime data "
            "root must be the central per-project store ($HOME/.vnx-data/<id>):\n"
            + "\n".join(violations)
        )

    def test_allowlist_entries_are_tracked_and_carry_a_reason(self):
        tracked = set(_tracked_files())
        for rel, reason in XDG_STORE_ALLOWLIST.items():
            assert reason and str(reason).strip(), (
                f"allowlist entry {rel!r} has no reason; an unexplained "
                "exception is how the XDG default survived in the first place"
            )
            assert rel in tracked, f"allowlist names an untracked file: {rel!r}"
            assert rel.startswith(tuple(root + "/" for root in SCAN_ROOTS)), (
                f"allowlist names a file outside the scanned roots: {rel!r}"
            )


class TestDetector:
    """The detector itself must flag XDG store paths and only those."""

    def test_flags_xdg_variable_feeding_a_path(self):
        line = '      VNX_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/vnx/$_vnx_pid"'
        assert _line_builds_xdg_store(line)

    def test_flags_literal_local_share_vnx(self):
        assert _line_builds_xdg_store('STORE = Path.home() / ".local/share/vnx"')

    def test_ignores_env_unset_mention(self):
        assert not _line_builds_xdg_store('-u VNX_DATA_HOME -u XDG_DATA_HOME \\')

    def test_ignores_comment_lines(self):
        assert not _line_builds_xdg_store("# never build from $XDG_DATA_HOME/vnx")

    def test_ignores_central_store_line(self):
        assert not _line_builds_xdg_store('    VNX_DATA_DIR="$HOME/.vnx-data/$_vnx_pid"')
