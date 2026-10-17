"""CHANGELOG fragments: one file per PR, folded into CHANGELOG.md by the release PR.

Every PR used to add its bullet directly under ``## [Unreleased]`` in
``CHANGELOG.md``, so two open PRs that inserted at the same line conflicted
(OI-2118).  A PR now adds ``changelog.d/<name>.md`` instead; a new file never
conflicts with another PR's new file.  The release PR folds the fragments into
``CHANGELOG.md`` with ``scripts/release/assemble_changelog.py``, before the tag.

This module owns the rule.  The assembler, the CI check
(``scripts/ci/check_changelog_fragments.py``) and the CI profile classifier
(``scripts/ci/classify_ci_profile.py``) all read it from here, and ``assemble``
is the only implementation of the fold: the assembler runs it on the working
tree, the CI check runs it on the merge base's blobs to recompute a release PR.

A fragment is ``changelog.d/<name>.md``, one level deep, where ``<name>``
matches ``[A-Za-z0-9][A-Za-z0-9._-]*`` and the file is not ``README.md``
(reserved for the rule itself).  Its content is a bullet block: UTF-8, ends
with a newline, its first non-blank line starts with ``- ``, every other
non-blank line starts with ``- `` or with at least two spaces, and no line
starts with ``#``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FRAGMENT_DIR = "changelog.d"
README_NAME = "README.md"
README_PATH = f"{FRAGMENT_DIR}/{README_NAME}"
CHANGELOG_PATH = "CHANGELOG.md"
UNRELEASED_HEADING = "## [Unreleased]"
ROLLBACK_COMMAND = f"git checkout -- {CHANGELOG_PATH} {FRAGMENT_DIR}"

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEADING_RE = re.compile(r"^## \[(?P<label>[^\]]+)\](?P<rest>.*)$")


class ChangelogError(Exception):
    """A refusal: the input cannot be assembled or checked as it stands."""


# ---------------------------------------------------------------------------
# The fragment rule (pure)
# ---------------------------------------------------------------------------


def is_fragment_path(path: str) -> bool:
    """True when the repo-relative *path* names a fragment.

    Pure: no file system access, so the classifier and the CI check can apply
    it to a list of changed paths.  ``changelog.d/README.md`` is not a fragment.
    """
    if not path:
        return False
    parts = path.split("/")
    if len(parts) != 2 or parts[0] != FRAGMENT_DIR:
        return False
    filename = parts[1]
    if filename == README_NAME or not filename.endswith(".md"):
        return False
    return _NAME_RE.fullmatch(filename[: -len(".md")]) is not None


def fragment_path(name: str) -> str:
    """The repo-relative path of the fragment file *name*."""
    return f"{FRAGMENT_DIR}/{name}"


def validate_fragment(name: str, data: bytes) -> Optional[str]:
    """Return why the fragment *name* with content *data* is refused, or None.

    Pure.  Every reason names the file and, where one exists, the first
    offending line with its number.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        line_no = data[: exc.start].count(b"\n") + 1
        return f"{name}:{line_no}: not valid UTF-8 (byte {exc.start})"
    if not text:
        return f"{name}: is empty; a fragment is at least one '- ' line"
    if not text.endswith("\n"):
        last_line = text.split("\n")[-1]
        return f"{name}:{text.count(chr(10)) + 1}: does not end with a newline: {last_line!r}"
    seen_entry = False
    for line_no, line in enumerate(text.split("\n")[:-1], start=1):
        reason = _line_refusal(line, seen_entry)
        if reason:
            return f"{name}:{line_no}: {reason}: {line!r}"
        if line.strip():
            seen_entry = True
    if not seen_entry:
        return f"{name}: holds no bullet; a fragment is at least one '- ' line"
    return None


def _line_refusal(line: str, seen_entry: bool) -> Optional[str]:
    """Why one fragment line is refused, or None; *seen_entry* is whether a
    non-blank line came before it."""
    if "\r" in line:
        return "carriage return (use LF line endings)"
    if line.startswith("#"):
        return "a heading line is not allowed in a bullet block"
    if not line.strip():
        return None
    if line.startswith("- "):
        return None
    if not seen_entry:
        return "the first non-blank line must start with '- '"
    if line.startswith("  "):
        return None
    return "a line must start with '- ' or with at least two spaces"


def select_fragments(root: Path) -> List[str]:
    """The fragment file names in ``<root>/changelog.d/``, sorted.

    Only regular files whose path passes ``is_fragment_path`` count; a symlink
    or a directory is never a fragment.  No directory means no fragments.
    """
    directory = Path(root) / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    names = []
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if is_fragment_path(fragment_path(entry.name)):
            names.append(entry.name)
    return sorted(names)


# ---------------------------------------------------------------------------
# The fold (pure)
# ---------------------------------------------------------------------------


def is_valid_version(version: str) -> bool:
    return _VERSION_RE.fullmatch(version or "") is not None


def is_valid_date(date: str) -> bool:
    return _DATE_RE.fullmatch(date or "") is not None


def version_heading(version: str, date: str) -> str:
    return f"## [{version}] - {date}"


def parse_heading(line: str) -> Optional[Tuple[str, str]]:
    """``(label, rest)`` for a ``## [label]rest`` line, else None."""
    match = _HEADING_RE.match(line.rstrip("\r"))
    if not match:
        return None
    return match.group("label"), match.group("rest")


def unreleased_bounds(lines: Sequence[str]) -> Optional[Tuple[int, int]]:
    """``(heading_index, end_index)`` of the ``## [Unreleased]`` section.

    *end_index* is the index of the next ``## `` line, or ``len(lines)``.
    None when the file has no ``## [Unreleased]`` heading.
    """
    for index, line in enumerate(lines):
        if line.rstrip() == UNRELEASED_HEADING:
            end = index + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            return index, end
    return None


def has_unreleased(changelog_text: str) -> bool:
    """True when the changelog has a ``## [Unreleased]`` heading."""
    return unreleased_bounds(_split_lines(changelog_text)) is not None


def unreleased_lines(changelog_text: str) -> List[str]:
    """The lines of the ``## [Unreleased]`` section body (empty without one)."""
    lines = _split_lines(changelog_text)
    bounds = unreleased_bounds(lines)
    if bounds is None:
        return []
    start, end = bounds
    return lines[start + 1 : end]


def assemble(
    changelog_text: str,
    fragments: Sequence[Tuple[str, str]],
    version: str,
    date: str,
) -> Tuple[str, List[str]]:
    """Fold *fragments* into *changelog_text* as release *version* of *date*.

    *fragments* is an ordered list of ``(name, text)``.  The result has a new
    ``## [version] - date`` heading directly under ``## [Unreleased]``, which
    stays and is left empty.  Under the new heading come first the lines that
    were under ``## [Unreleased]`` (unchanged, in their order), then each
    fragment's bullet block, in the given order, one blank line between blocks.
    Returns ``(new_text, consumed_names)``.  Raises ``ChangelogError`` when the
    changelog has no ``## [Unreleased]``, already has ``## [version]``, or holds
    a line under ``## [Unreleased]`` that breaks the fragment's line rule
    (``unreleased_body_refusal``).
    """
    lines = _split_lines(changelog_text)
    bounds = unreleased_bounds(lines)
    if bounds is None:
        raise ChangelogError(f"{CHANGELOG_PATH} has no '{UNRELEASED_HEADING}' heading")
    for line in lines:
        parsed = parse_heading(line)
        if parsed and parsed[0] == version:
            raise ChangelogError(f"{CHANGELOG_PATH} already has a '## [{version}]' heading")
    start, end = bounds
    refusal = unreleased_body_refusal(lines[start + 1 : end], first_line_no=start + 2)
    if refusal:
        raise ChangelogError(refusal)
    blocks = []
    legacy = _trim_blank(lines[start + 1 : end])
    if legacy:
        blocks.append(legacy)
    for _name, text in fragments:
        block = _trim_blank(_split_lines(text))
        if block:
            blocks.append(block)
    out = lines[: start + 1] + ["", version_heading(version, date), ""]
    for block in blocks:
        out.extend(block)
        out.append("")
    rest = lines[end:]
    if rest:
        out.extend(rest)
    else:
        out = _trim_trailing_blank(out)
    return "\n".join(out) + "\n", [name for name, _text in fragments]


def unreleased_body_refusal(body: Sequence[str], first_line_no: int) -> Optional[str]:
    """Why the ``## [Unreleased]`` *body* cannot be folded, or None.

    The body follows a fragment's line rule: blank, a ``- `` bullet, or after
    the first bullet a continuation of at least two spaces.  Prose there would
    land between the new heading and its first bullet, where the CI check
    reads the hand-written release intro and compares nothing.  *first_line_no*
    is the ``CHANGELOG.md`` line number of ``body[0]``.
    """
    seen_entry = False
    for offset, line in enumerate(body):
        reason = _line_refusal(line, seen_entry)
        if reason:
            return (f"{CHANGELOG_PATH}:{first_line_no + offset}: under '{UNRELEASED_HEADING}' "
                    f"{reason}: {line!r}; only bullets fold into a release")
        if line.strip():
            seen_entry = True
    return None


def release_section(changelog_text: str, version: str) -> str:
    """The ``## [version]`` heading and its body, up to the next ``## `` line."""
    lines = _split_lines(changelog_text)
    for index, line in enumerate(lines):
        parsed = parse_heading(line)
        if parsed and parsed[0] == version:
            end = index + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            return "\n".join(_trim_trailing_blank(lines[index:end])) + "\n"
    return ""


def _split_lines(text: str) -> List[str]:
    """Split on LF only, so every other byte survives a round trip."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _trim_blank(lines: Sequence[str]) -> List[str]:
    out = _trim_trailing_blank(lines)
    while out and not out[0].strip():
        out.pop(0)
    return out


def _trim_trailing_blank(lines: Sequence[str]) -> List[str]:
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    return out


# ---------------------------------------------------------------------------
# Introduction order (git)
# ---------------------------------------------------------------------------


def run_git_bytes(repo: Path, *args: str) -> bytes:
    """Run git in *repo* and return raw stdout; ``ChangelogError`` on failure."""
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    except OSError as exc:
        raise ChangelogError(f"cannot run git: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise ChangelogError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def run_git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return stdout as text; ``ChangelogError`` on failure."""
    return run_git_bytes(repo, *args).decode("utf-8", "replace")


def tree_entries(repo: Path, rev: str, directory: str) -> Dict[str, Tuple[str, str, str]]:
    """``{path: (mode, type, object)}`` for the entries directly in *directory* at *rev*.

    Read from git objects, never from a working tree.  A directory that does
    not exist at *rev* gives an empty mapping.
    """
    raw = run_git_bytes(repo, "ls-tree", "-z", rev, "--", f"{directory}/")
    entries = {}
    for record in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, kind, obj = meta.split(" ")
        entries[path] = (mode, kind, obj)
    return entries


def is_regular_blob(entry: Tuple[str, str, str]) -> bool:
    """True for a regular file (mode 100644 or 100755); a symlink is 120000."""
    mode, kind, _obj = entry
    return kind == "blob" and mode in ("100644", "100755")


def fragment_names_at(repo: Path, rev: str) -> List[str]:
    """The fragment file names in ``changelog.d/`` at *rev*, regular files only, sorted."""
    entries = tree_entries(repo, rev, FRAGMENT_DIR)
    return sorted(
        path.split("/", 1)[1]
        for path, entry in entries.items()
        if is_fragment_path(path) and is_regular_blob(entry)
    )


def read_blob(repo: Path, rev: str, path: str) -> Optional[bytes]:
    """The bytes of *path* at *rev*, or None when *rev* has no regular file there."""
    raw = run_git_bytes(repo, "ls-tree", "-z", rev, "--", path)
    for record in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not record:
            continue
        meta, found = record.split("\t", 1)
        mode, kind, obj = meta.split(" ")
        if found == path and is_regular_blob((mode, kind, obj)):
            return run_git_bytes(repo, "cat-file", "blob", obj)
    return None


def introduction_order(repo: Path, rev: str, names: Iterable[str]) -> List[str]:
    """Order fragment *names* by when they entered the first-parent history of *rev*.

    A fragment's position is the newest first-parent commit of *rev* that added
    its path: the file's current lifetime, so a name consumed by an earlier
    release and added again sorts by its new addition.  Commit dates are never
    read.  Two fragments added by the same commit sort by name.  A fragment
    that no first-parent commit of *rev* added (branch-only or uncommitted) is
    refused by name.
    """
    names = sorted(set(names))
    if not names:
        return []
    history = run_git(repo, "rev-list", "--first-parent", "--reverse", rev).split()
    position = {sha: index for index, sha in enumerate(history)}
    keyed, missing = [], []
    for name in names:
        sha = run_git(
            repo, "log", "--first-parent", "--diff-merges=first-parent", "--no-patch",
            "--no-renames", "--diff-filter=A", "-1", "--format=%H", rev, "--", fragment_path(name),
        ).strip()
        if sha not in position:
            missing.append(name)
            continue
        keyed.append((position[sha], name))
    if missing:
        listed = ", ".join(fragment_path(name) for name in missing)
        raise ChangelogError(
            f"not added by any first-parent commit of {rev}: {listed} "
            f"(a fragment must be merged into {rev} before a release assembles it)"
        )
    return [name for _position, name in sorted(keyed)]
