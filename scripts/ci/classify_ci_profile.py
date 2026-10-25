#!/usr/bin/env python3
"""Classify a set of changed paths into a CI profile: ``light`` or ``heavy``.

The Profile A prelude (``.github/actions/profile-a-prelude/action.yml``, used by
Profile A and its xdist shadow in ``.github/workflows/vnx-ci.yml``) calls this
to decide how much of the test sweep a PR must run.  The rule is an ALLOWLIST, not a blocklist: only
paths that are unambiguously documentation get the light profile, and
everything else — code, unclassifiable paths, empty input, malformed paths —
falls through to the heavy profile.  An unknown path must never produce a
lighter run.  This is the lesson of #1513, where a blocklist that called itself
an allowlist let through everything it did not name.

Light-profile allowlist (the complete set of things that may be light):

* ``docs/**``       — anything under ``docs/``
* ``claudedocs/**`` — anything under ``claudedocs/``
* ``*.md`` at the repository root — a single path component ending in ``.md``
* a CHANGELOG fragment, ``changelog.d/<name>.md`` exactly as
  ``changelog_fragments.is_fragment_path`` defines it, and
  ``changelog.d/README.md`` — any other path under ``changelog.d/`` is heavy

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

Test selection (``--tests-only``, track deterministische-testverificatie)
-------------------------------------------------------------------------

The same paths file answers a second question: which tests this PR adds or
changes.  ``select_test_paths`` returns ``{"selected", "excluded_touched",
"deleted", "conftest_changed"}``: the added or changed pytest files under
``tests/`` (``test_*.py``/``*_test.py``, sorted and unique); the subset of them
that ``scripts/ci/test_exclusions.txt`` names, kept selected on purpose because
Profile A ignores them in the full sweep and a touched excluded file must still
run red-on-base; the test files the PR removes, read from the head checkout — the
prelude writes ``--name-only``, so an added file is there and a deleted one is
not; and every ``tests/**/conftest.py`` the PR touches, because a conftest holds
no tests and the reader decides.

Fail closed: an entry that cannot be read (not text, ``.``/``..``/absolute, git's
C-quoted form, a ``<status>\\t<path>`` row) raises ``MalformedPathsError`` and the
CLI exits 2 *without* a selection — the entry might be a test file, so a partial
selection would report "nothing to run".  A missing paths file, exclusions file or
root is refused too.  An empty paths file is not an error, but an empty
``selected`` is not evidence that anything passed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release"))

from changelog_fragments import README_PATH, is_fragment_path

PROFILE_LIGHT = "light"
PROFILE_HEAVY = "heavy"

TESTS_DIR = "tests"
CONFTEST_NAME = "conftest.py"
EXCLUSIONS_PATH = Path("scripts/ci/test_exclusions.txt")
REPO_ROOT = Path(__file__).resolve().parents[2]

_INLINE_COMMENT_RE = re.compile(r"[ \t]*#.*")  # the consumer's inline-comment sed


class MalformedPathsError(ValueError):
    """An entry or option cannot be read cleanly, so no selection is emitted."""


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
    # A CHANGELOG fragment or the fragment README; nothing else in changelog.d/.
    return is_fragment_path(path) or path == README_PATH


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


def _is_plain_repo_path(path: str) -> bool:
    """True for a plain repo-relative path: no '.', '..', '//', absolute form or quote."""
    if not path or path.startswith(("/", '"')) or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def tests_path_kind(path: str) -> str | None:
    """Return ``"test"``, ``"conftest"`` or ``None`` for a path under ``tests/``."""
    if not _is_plain_repo_path(path):
        return None
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != TESTS_DIR:
        return None
    name = parts[-1]
    if name == CONFTEST_NAME:
        return "conftest"
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return "test"
    return None


def parse_exclusion_entry(raw: str) -> str:
    """Return the file path one exclusions-file line names, or ``""``.

    The rules of ``build_ignore`` in ``profile_a_pytest_args.sh``: a blank line or
    whole-line ``#`` comment names nothing, an inline ``#`` starts the reason and
    whitespace is removed; a nodeid entry yields its file part.
    """
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return "".join(_INLINE_COMMENT_RE.sub("", raw, count=1).split()).split("::", 1)[0]


def read_exclusion_entries(text: str) -> list[str]:
    """Return the excluded file paths named by an exclusions file's *text*."""
    entries: list[str] = []
    for entry in map(parse_exclusion_entry, text.splitlines()):
        if entry and entry not in entries:
            entries.append(entry)
    return entries


def _read_changed_path(text: str) -> str:
    """Return *text* as a plain repo-relative path, or raise ``MalformedPathsError``.

    The list is the prelude's ``git diff --name-only`` output: a C-quoted path
    (non-ASCII, control characters), a ``<status>\\t<path>`` row and any other
    unplain path are refused rather than guessed at, because dropping one could
    drop a test file from the selection.
    """
    if text.startswith('"'):
        raise MalformedPathsError(
            f"cannot read C-quoted path {text!r}; list paths with 'git diff -c core.quotePath=false'"
        )
    if "\t" in text or not _is_plain_repo_path(text):
        raise MalformedPathsError(f"cannot read changed-path entry {text!r}")
    return text


def _changed_test_paths(paths):
    """Yield every readable changed path; raise ``MalformedPathsError`` if one is not.

    Refusing the whole list is what keeps the selection from silently shrinking.
    """
    if paths is None:
        raise MalformedPathsError("no changed-path list was given (None)")
    if isinstance(paths, (str, bytes)):
        raise MalformedPathsError("the changed-path list is one string, not a list of paths")
    for raw in paths:
        if not isinstance(raw, str):
            raise MalformedPathsError(f"changed-path entry is not text: {raw!r}")
        if raw.strip():
            yield _read_changed_path(raw.rstrip("\r"))


def _deleted_by_tree(root: Path, path: str) -> bool:
    """True when *path* is absent from *root*, the PR's head checkout.

    ``lexists``, so a broken symlink counts as present and runs; a *root* that is
    not a directory is refused, since every listed test would read as deleted.
    """
    if not root.is_dir():
        raise MalformedPathsError(f"root {str(root)!r} is not a directory, so an addition cannot be told from a deletion")
    return not os.path.lexists(root / path)


def select_test_paths(paths, exclusions, root: str | Path | None = None) -> dict:
    """Return the test-selection payload for a PR's changed *paths*.

    *exclusions* is one entry or an iterable of them (raw lines or plain paths;
    ``parse_exclusion_entry`` is idempotent on either).  *root* is the head
    checkout the paths are relative to.
    """
    base = Path(root) if root is not None else REPO_ROOT
    if exclusions is None:
        # "nothing is excluded" would hide red-on-base for a quarantined file.
        raise MalformedPathsError("no exclusions were given (None)")
    if isinstance(exclusions, str):
        exclusions = [exclusions]
    excluded = {entry for entry in map(parse_exclusion_entry, exclusions) if entry}
    selected: set[str] = set()
    deleted: set[str] = set()
    conftest: set[str] = set()
    for path in _changed_test_paths(paths):
        kind = tests_path_kind(path)
        if kind == "conftest":
            conftest.add(path)  # touching one is a change, deleted or not
        elif kind == "test":
            (deleted if _deleted_by_tree(base, path) else selected).add(path)
    return {
        "selected": sorted(selected),
        "excluded_touched": sorted(selected & excluded),
        "deleted": sorted(deleted),
        "conftest_changed": sorted(conftest),
    }


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


def _pop_option(args: list[str], name: str) -> str | None:
    """Remove *name* and its value from *args*; ``None`` when it is absent."""
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        raise MalformedPathsError(f"{name} needs a value")
    value = args[index + 1]
    del args[index : index + 2]
    if name in args:
        raise MalformedPathsError(f"{name} is given more than once")
    return value


def _run_tests_only(argv: list[str]) -> int:
    """Print the test selection as JSON; exit 2 when the input cannot be read."""
    args = [arg for arg in argv if arg != "--tests-only"]
    try:
        root_arg = _pop_option(args, "--root")
        exclusions_arg = _pop_option(args, "--exclusions")
        base = Path(root_arg) if root_arg else REPO_ROOT
        exclusions_file = Path(exclusions_arg) if exclusions_arg else base / EXCLUSIONS_PATH
        # A missing exclusions file is refused, not read as "nothing is excluded".
        exclusions = read_exclusion_entries(exclusions_file.read_text(encoding="utf-8"))
        payload = select_test_paths(_read_paths(args), exclusions, root=base)
    except (MalformedPathsError, OSError, UnicodeDecodeError) as exc:
        print(f"classify_ci_profile --tests-only: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--tests-only" in args:
        return _run_tests_only(args)
    print(classify_paths(_read_paths(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
