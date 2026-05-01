#!/usr/bin/env python3
"""CI gate: validate Dispatch-ID present and slug-match in squash merge commit bodies.

Checks that commits in a PR branch satisfy two invariants:
  1. At least one commit body contains a ``Dispatch-ID: <id>`` line.
  2. The slug portion of each Dispatch-ID matches the branch name slug.

Branch slug derivation:
  Strip common type prefixes (fix/, feat/, feature/, test/, docs/, chore/,
  refactor/) then normalise (lowercase, underscores → hyphens).

Dispatch-ID slug derivation:
  Format: ``YYYYMMDD-HHMMSS-<slug>-<track>``
  Slug is the middle segment after removing the date/time prefix and the
  single-letter track suffix (A/B/C).

Exit codes
----------
0  passed (or shadow mode with non-blocking warnings)
1  validation failed
2  bad arguments / environment / git error

Usage
-----
  python3 scripts/check_ci_slug_match.py [--base-ref BRANCH] [--branch-name BRANCH] [--enforce]

Environment (GitHub Actions compatible)
---------------------------------------
  GITHUB_EVENT_NAME    event type (e.g. ``pull_request`` or ``push``)
  GITHUB_HEAD_REF      PR source branch (set only on ``pull_request`` events)
  GITHUB_REF_NAME      ref name for ``push`` events (e.g. ``main``)
  GITHUB_BASE_REF      target/base branch name
  VNX_SLUG_ENFORCEMENT "1" = block on failure (default shadow/warn)
  VNX_DEFAULT_BRANCHES comma-separated list of default branches to skip on
                       push events (default: ``main,master``)
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISPATCH_ID_RE = re.compile(
    r"^\d{8}-\d{6}-.+-[A-C]$"
)
DISPATCH_ID_EXTRACT_RE = re.compile(
    r"^(\d{8})-(\d{6})-(.+)-([A-C])$"
)
DISPATCH_ID_LINE_RE = re.compile(
    r"^Dispatch-ID:\s*(\S+)", re.MULTILINE
)
BRANCH_PREFIX_RE = re.compile(
    r"^(?:fix|feat|feature|test|docs|chore|refactor|hotfix|perf|ci|build|revert)/"
)

# ---------------------------------------------------------------------------
# Slug utilities
# ---------------------------------------------------------------------------


def branch_slug(branch_name: str) -> str:
    """Derive the canonical slug from a branch name.

    Strips any ``type/`` prefix, lowercases, and converts underscores to hyphens.
    """
    name = branch_name.strip()
    # Remove origin/ prefix if present (e.g. from GITHUB_BASE_REF)
    if name.startswith("origin/"):
        name = name[len("origin/"):]
    name = BRANCH_PREFIX_RE.sub("", name)
    return name.lower().replace("_", "-")


def dispatch_id_slug(dispatch_id: str) -> str | None:
    """Extract the slug from a dispatch ID string.

    Returns None if the format is invalid.
    """
    m = DISPATCH_ID_EXTRACT_RE.match(dispatch_id.strip())
    if not m:
        return None
    return m.group(3).lower().replace("_", "-")


def slugs_match(a: str, b: str) -> bool:
    """Return True if two slugs are equivalent after normalisation."""
    def _norm(s: str) -> str:
        return s.lower().replace("_", "-").strip("-")
    return _norm(a) == _norm(b)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command and return stdout. Raises RuntimeError on failure."""
    cmd = ["git"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)!r} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout
    except FileNotFoundError:
        raise RuntimeError("git not found in PATH")


def resolve_base_ref(base_ref: str) -> str:
    """Resolve base_ref to a valid git ref, trying origin/ fallback."""
    try:
        _git("rev-parse", "--verify", base_ref)
        return base_ref
    except RuntimeError:
        pass
    origin = f"origin/{base_ref}"
    try:
        _git("rev-parse", "--verify", origin)
        return origin
    except RuntimeError:
        pass
    raise RuntimeError(
        f"Base ref '{base_ref}' not found locally or as 'origin/{base_ref}'"
    )


def commits_since(base_ref: str) -> list[tuple[str, str]]:
    """Return list of (sha, full_body) for commits between base_ref and HEAD."""
    try:
        merge_base = _git("merge-base", base_ref, "HEAD").strip()
    except RuntimeError:
        merge_base = base_ref

    log_output = _git(
        "log", f"{merge_base}..HEAD", "--format=%H%x00%B%x00", "--no-walk=unsorted"
    )
    if not log_output.strip():
        log_output = _git("log", f"{merge_base}..HEAD", "--format=%H%x00%B%x00")

    results: list[tuple[str, str]] = []
    # Split on null-byte pairs emitted by the format string
    parts = log_output.split("\x00")
    i = 0
    while i + 1 < len(parts):
        sha = parts[i].strip()
        body = parts[i + 1]
        if sha:
            results.append((sha, body))
        i += 2
    return results


def current_branch() -> str:
    """Return the current git branch name."""
    try:
        out = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if out and out != "HEAD":
            return out
    except RuntimeError:
        pass
    raise RuntimeError("Unable to determine current branch")


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


@dataclass
class CommitResult:
    sha: str
    subject: str
    dispatch_ids: list[str]
    slug_matches: list[bool]

    @property
    def has_dispatch_id(self) -> bool:
        return bool(self.dispatch_ids)

    @property
    def all_slugs_match(self) -> bool:
        # Empty slug_matches means Dispatch-ID was malformed (couldn't extract
        # slug). Treat that as failure, not vacuous True.
        if not self.slug_matches:
            return False
        return all(self.slug_matches)


def scan_commits(
    commits: list[tuple[str, str]],
    target_slug: str,
) -> list[CommitResult]:
    """Analyse each commit for Dispatch-ID presence and slug match."""
    results: list[CommitResult] = []
    for sha, body in commits:
        found_ids = DISPATCH_ID_LINE_RE.findall(body)
        slug_matches = []
        for did in found_ids:
            dslug = dispatch_id_slug(did)
            if dslug is not None:
                slug_matches.append(slugs_match(dslug, target_slug))
        # Extract subject from body (first non-empty line)
        subject = next((ln for ln in body.splitlines() if ln.strip()), sha[:12])
        results.append(
            CommitResult(
                sha=sha,
                subject=subject.strip(),
                dispatch_ids=found_ids,
                slug_matches=slug_matches,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _bar() -> None:
    print("══════════════════════════════════════════════════════════")


def _line() -> None:
    print("──────────────────────────────────────────────────────────")


def run_gate(
    base_ref: str,
    branch_name: str,
    enforce: bool,
) -> int:
    """Execute the slug-match gate. Returns exit code (0/1/2)."""
    _bar()
    print(" VNX CI — Dispatch-ID Slug-Match Gate")
    print(f" Branch : {branch_name}")
    print(f" Base   : {base_ref}")
    print(f" Mode   : {'ENFORCED' if enforce else 'shadow/warn'}")
    _bar()
    print()

    # Resolve branch slug
    bslug = branch_slug(branch_name)
    print(f" Branch slug: {bslug!r}")
    print()

    # Resolve base ref
    try:
        resolved_base = resolve_base_ref(base_ref)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    # Collect commits
    try:
        commits = commits_since(resolved_base)
    except RuntimeError as exc:
        print(f"[ERROR] Failed to enumerate commits: {exc}", file=sys.stderr)
        return 2

    if not commits:
        print(" No new commits to check.")
        print()
        print("RESULT: PASS (no commits)")
        return 0

    # Scan commits
    scan = scan_commits(commits, bslug)

    missing_ids: list[CommitResult] = []
    slug_mismatches: list[CommitResult] = []

    for cr in scan:
        short = cr.sha[:8]
        if not cr.has_dispatch_id:
            icon = "✗"
            note = "missing Dispatch-ID"
            missing_ids.append(cr)
        elif not cr.all_slugs_match:
            icon = "!"
            note = f"slug mismatch: {cr.dispatch_ids}"
            slug_mismatches.append(cr)
        else:
            icon = "✓"
            note = f"Dispatch-ID: {cr.dispatch_ids[0]}"

        print(f"  {icon} {short} {cr.subject[:60]}  [{note}]")

    print()
    _line()
    total = len(scan)
    ok = total - len(missing_ids) - len(slug_mismatches)
    print(
        f"  Total: {total} | OK: {ok} | "
        f"Missing: {len(missing_ids)} | Mismatch: {len(slug_mismatches)}"
    )
    _line()

    failed = bool(missing_ids or slug_mismatches)

    if missing_ids:
        print()
        print("Commits missing Dispatch-ID:")
        for cr in missing_ids:
            print(f"  - {cr.sha[:8]} {cr.subject[:60]}")
        print()
        print("Fix: Add 'Dispatch-ID: <dispatch-id>' to the commit body.")

    if slug_mismatches:
        print()
        print(f"Commits with slug mismatch (expected branch slug: {bslug!r}):")
        for cr in slug_mismatches:
            print(f"  - {cr.sha[:8]} {cr.subject[:60]}  ids={cr.dispatch_ids}")

    print()
    if failed:
        if enforce:
            print("RESULT: FAIL (enforcement mode)")
            return 1
        else:
            print("RESULT: WARN (shadow mode — not blocking)")
            return 0

    print("RESULT: PASS")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    import os

    parser = argparse.ArgumentParser(
        description="CI gate: Dispatch-ID present + slug-match in branch commits"
    )
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("GITHUB_BASE_REF", "main"),
        help="Base branch to compare against (default: main or GITHUB_BASE_REF)",
    )
    parser.add_argument(
        "--branch-name",
        default=None,
        help="Current branch name (default: auto-detect from GITHUB_HEAD_REF or git)",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        default=os.environ.get("VNX_SLUG_ENFORCEMENT", "0") == "1",
        help="Exit 1 on failure instead of warning (also: VNX_SLUG_ENFORCEMENT=1)",
    )
    return parser


DEFAULT_BRANCH_NAMES = ("main", "master")


def _default_branches() -> tuple[str, ...]:
    import os
    raw = os.environ.get("VNX_DEFAULT_BRANCHES", "")
    if not raw.strip():
        return DEFAULT_BRANCH_NAMES
    return tuple(b.strip() for b in raw.split(",") if b.strip())


def resolve_branch_name(args_branch_name: str | None) -> tuple[str, bool]:
    """Resolve the branch name and detect push-to-default-branch events.

    Returns (branch_name, skip_push_default).

    Resolution order:
      1. Explicit ``--branch-name`` argument (overrides everything)
      2. ``GITHUB_HEAD_REF`` (set on ``pull_request`` events)
      3. ``GITHUB_REF_NAME`` (set on ``push`` events; previously the gate
         left this empty and silently passed)
      4. ``git rev-parse --abbrev-ref HEAD``

    When the resolved branch is a default branch (e.g. ``main``) and the
    event is a ``push`` (or no PR head ref is available), the gate is
    skipped: the dispatch-id-vs-branch invariant only meaningfully
    applies on PR/topic branches.
    """
    import os

    if args_branch_name:
        return args_branch_name, False

    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    head_ref = os.environ.get("GITHUB_HEAD_REF", "").strip()
    ref_name = os.environ.get("GITHUB_REF_NAME", "").strip()

    if head_ref:
        return head_ref, False

    if ref_name:
        is_push = event_name == "push" or event_name == ""
        if is_push and ref_name in _default_branches():
            return ref_name, True
        return ref_name, False

    branch = current_branch()
    if event_name == "push" and branch in _default_branches():
        return branch, True
    return branch, False


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        branch_name, skip_push_default = resolve_branch_name(args.branch_name)
    except RuntimeError as exc:
        print(f"[ERROR] Cannot determine branch: {exc}", file=sys.stderr)
        return 2

    if skip_push_default:
        _bar()
        print(" VNX CI — Dispatch-ID Slug-Match Gate")
        print(f" Branch : {branch_name}")
        print(" Mode   : SKIPPED (push event on default branch)")
        _bar()
        print()
        print("RESULT: SKIP (gate only meaningful on PR/topic branches)")
        return 0

    return run_gate(
        base_ref=args.base_ref,
        branch_name=branch_name,
        enforce=args.enforce,
    )


if __name__ == "__main__":
    sys.exit(main())
