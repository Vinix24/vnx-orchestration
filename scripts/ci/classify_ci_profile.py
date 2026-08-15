#!/usr/bin/env python3
"""Classify a set of changed paths into a CI profile: ``light`` or ``heavy``.

Profile A in ``.github/workflows/vnx-ci.yml`` calls this to decide how much of
the test sweep a PR must run.  The rule is an ALLOWLIST, not a blocklist: only
paths that are unambiguously documentation get the light profile, and
everything else — code, unclassifiable paths, empty input, malformed paths —
falls through to the heavy profile.  An unknown path must never produce a
lighter run.  This is the lesson of #1513, where a blocklist that called itself
an allowlist let through everything it did not name.

Light-profile allowlist (the complete set of things that may be light):

* ``docs/**``       — anything under ``docs/``
* ``claudedocs/**`` — anything under ``claudedocs/``
* ``*.md`` at the repository root — a single path component ending in ``.md``

Everything else is heavy.  ``skills/**`` is deliberately heavy: a skill's
markdown drives agent behaviour, so a skill change is a code change for CI
purposes.  ``.github/**`` (including this workflow's own YAML) is heavy.

Fail-closed guards, all of which yield ``heavy``:

* an empty list of changed paths (no diff is not "nothing to check", it is
  "we could not tell what changed"),
* a path containing whitespace or a non-ASCII character (a filename that
  shape is abnormal enough that it must not be allowed to slip through a
  prefix/glob match into the light profile).

The classifier never crashes on malformed input and always exits 0 — it
*reports* a profile; the caller decides what to do with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROFILE_LIGHT = "light"
PROFILE_HEAVY = "heavy"


def _has_whitespace_or_non_ascii(path: str) -> bool:
    """True when *path* contains whitespace or a character outside ASCII."""
    return any(ch.isspace() or ord(ch) > 127 for ch in path)


def is_docs_only_path(path: str) -> bool:
    """Return True when *path* is unambiguously documentation-only.

    Matches the allowlist in the module docstring.  A path that is empty,
    contains whitespace, or contains a non-ASCII character is never
    documentation-only — it falls through to the heavy profile rather than
    risking a prefix/glob match on an abnormal filename.
    """
    if not path:
        return False
    if _has_whitespace_or_non_ascii(path):
        return False
    if path == "docs" or path.startswith("docs/"):
        return True
    if path == "claudedocs" or path.startswith("claudedocs/"):
        return True
    # Root-level markdown: no directory component, ends in ".md".
    if "/" not in path and path.endswith(".md"):
        return True
    return False


def classify_paths(paths) -> str:
    """Return ``PROFILE_LIGHT`` or ``PROFILE_HEAVY`` for *paths*.

    *paths* is any iterable of raw path strings (typically from
    ``git diff --name-only``).  Whitespace-only entries are treated as blank
    lines and dropped; a path with leading/trailing whitespace is kept and
    rejected by the whitespace guard.  The result is ``light`` only when every
    non-blank path matches the docs-only allowlist; anything else is ``heavy``.
    """
    meaningful = [p for p in paths if p.strip() != ""]
    if not meaningful:
        return PROFILE_HEAVY
    for path in meaningful:
        if not is_docs_only_path(path):
            return PROFILE_HEAVY
    return PROFILE_LIGHT


def _read_paths(argv: list[str]) -> list[str]:
    """Resolve the changed-paths input from argv (``--paths-file``, positional
    args, or stdin) into a list of raw path strings."""
    if "--paths-file" in argv:
        idx = argv.index("--paths-file")
        fp = argv[idx + 1]
        return Path(fp).read_text(encoding="utf-8").splitlines()
    if argv:
        return argv
    if not sys.stdin.isatty():
        return sys.stdin.read().splitlines()
    return []


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    print(classify_paths(_read_paths(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
