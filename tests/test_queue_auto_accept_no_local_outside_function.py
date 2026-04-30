"""Regression test for Finding 2: queue_auto_accept.sh must not use `local` outside functions.

Using `local` at the top level (outside a bash function) is a runtime error in bash.
With `set -euo pipefail`, this terminates the script before the dispatch_created event
is ever emitted, silently leaving dispatch state stale in the register.

This test reads the shell source, parses function boundaries, and asserts that no
`local` declarations appear in the top-level (non-function) scope.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = PROJECT_ROOT / "scripts" / "queue_auto_accept.sh"


def _extract_top_level_lines(source: str) -> list[tuple[int, str]]:
    """Return (lineno, content) pairs for lines that are NOT inside a function body.

    Heuristic: a function body starts at a line matching `<name>() {` or `function <name> {`
    and ends at the matching closing `}` at column 0. Top-level code is everything else.
    """
    lines = source.splitlines()
    top_level: list[tuple[int, str]] = []
    depth = 0
    in_function = False

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()

        # Function opener — either `name() {` or `function name {`
        if re.match(r'^(\w+)\s*\(\s*\)\s*\{', stripped) or re.match(r'^function\s+\w+', stripped):
            in_function = True
            depth = 1
            continue

        if in_function:
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                in_function = False
                depth = 0
            continue

        # Top-level line
        top_level.append((lineno, raw))

    return top_level


def test_no_local_in_top_level_scope():
    """No `local` keyword may appear outside a function body in queue_auto_accept.sh."""
    source = _SCRIPT.read_text(encoding="utf-8")
    top_level_lines = _extract_top_level_lines(source)

    violations: list[str] = []
    for lineno, line in top_level_lines:
        # Match `local ` or `local\t` — skip comment lines
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r'\blocal\b', stripped):
            violations.append(f"  Line {lineno}: {line.rstrip()!r}")

    assert not violations, (
        "queue_auto_accept.sh uses `local` outside a function body — "
        "this is a bash runtime error.\n"
        "Violations:\n" + "\n".join(violations)
    )


def test_dispatch_created_emit_present_in_accept_loop():
    """The accept loop must call dispatch_register.py append dispatch_created."""
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "dispatch_created" in source, (
        "queue_auto_accept.sh must emit dispatch_created in the accept loop"
    )
    assert "dispatch_register.py" in source, (
        "dispatch_created emit must call dispatch_register.py"
    )


def test_emit_is_best_effort_non_fatal():
    """The dispatch_created emit must be wrapped in set +e / set -e (best-effort contract)."""
    source = _SCRIPT.read_text(encoding="utf-8")
    # After the `mv` that lands the file in pending/, we must have set +e before the register call
    # and set -e after, so a register failure does not abort the watcher loop.
    assert "set +e" in source, "set +e must guard the dispatch_created emit"
    assert "set -e" in source, "set -e must be restored after the best-effort emit"
