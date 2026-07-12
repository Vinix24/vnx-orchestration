"""Tests for docs/DOCS_INDEX.md reachability (PR-11, docs bloat cleanup).

Enforces the framework-status-audit-and-cockpit PRD's PR-11 rule: a doc under
docs/**/*.md may stay outside docs/_archive/ only if it is reachable (directly,
or transitively through an intermediate doc such as docs/operations/README.md)
from docs/DOCS_INDEX.md or root README.md, OR it was touched in git within the
last 12 months. A doc that is BOTH unreachable AND untouched for more than 12
months is docs bloat and belongs in docs/_archive/.
"""
from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_INDEX = REPO_ROOT / "docs" / "DOCS_INDEX.md"
STALENESS_DAYS = 365


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def _all_repo_md_files() -> set:
    out = _git("ls-files", "*.md")
    return {line for line in out.splitlines() if line}


def _non_archive_docs(all_md: set) -> list:
    return sorted(f for f in all_md if f.startswith("docs/") and not f.startswith("docs/_archive/"))


def _extract_link_targets(text: str) -> list:
    targets = list(re.findall(r"\]\(([^)\s]+)\)", text))
    targets += re.findall(r"`([^`]+\.md)`", text)
    return targets


def _resolve(base_file: str, target: str, all_md: set):
    if target.startswith("http") or target.startswith("#") or not target:
        return None
    target = target.split("#")[0]
    if not target:
        return None
    candidates = []
    if target.startswith("/"):
        candidates.append(target.lstrip("/"))
    else:
        base_dir = os.path.dirname(base_file)
        candidates.append(os.path.normpath(os.path.join(base_dir, target)))
        candidates.append(os.path.normpath(target))
    for cand in candidates:
        if cand in all_md:
            return cand
    return None


def _reachable_docs(all_md: set) -> set:
    """BFS over markdown links, starting from DOCS_INDEX.md and root README.md."""
    adjacency = {}
    for f in all_md:
        path = REPO_ROOT / f
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        resolved = set()
        for target in _extract_link_targets(text):
            r = _resolve(f, target, all_md)
            if r:
                resolved.add(r)
        adjacency[f] = resolved

    roots = ["docs/DOCS_INDEX.md", "README.md"]
    visited = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for nxt in adjacency.get(cur, ()):
            if nxt not in visited:
                visited.add(nxt)
                stack.append(nxt)
    return visited


def _last_touched(path: str) -> datetime.date | None:
    out = _git("log", "-1", "--format=%ad", "--date=short", "--", path)
    if not out:
        return None
    return datetime.date.fromisoformat(out)


def _index_table_paths() -> list:
    """Extract every backtick-wrapped `Path` cell from DOCS_INDEX.md's tables."""
    text = DOCS_INDEX.read_text(encoding="utf-8")
    paths = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = re.fullmatch(r"`([^`]+)`", cells[1])
        if m:
            paths.append(m.group(1))
    return paths


class TestDocsIndexLinksResolve:
    def test_every_index_path_exists(self):
        # docs/internal/ is gitignored by design (private research docs) — its
        # listed paths are expected to be absent from a plain checkout and are
        # not part of the public docs bloat this PR addresses.
        missing = []
        for rel_path in _index_table_paths():
            if rel_path.startswith("docs/internal/"):
                continue
            target = rel_path.rstrip("/")
            if not (REPO_ROOT / target).exists():
                missing.append(rel_path)
        assert missing == [], f"DOCS_INDEX.md references paths that do not exist: {missing}"

    def test_no_row_points_into_archive_except_the_archive_row(self):
        for rel_path in _index_table_paths():
            if rel_path.startswith("docs/_archive"):
                continue
            assert "/_archive/" not in rel_path, (
                f"DOCS_INDEX.md row points into docs/_archive/: {rel_path}"
            )

    def test_comparisons_no_longer_indexed(self):
        text = DOCS_INDEX.read_text(encoding="utf-8")
        assert "docs/comparisons/" not in text, (
            "docs/comparisons/ was archived by PR-11 but DOCS_INDEX.md still references it"
        )


class TestNoStaleUnreachableDocsRemain:
    def test_unreachable_and_stale_set_is_empty(self):
        """The PR-11 archival rule, executed live: any doc that is both unreachable
        from DOCS_INDEX.md/README.md and untouched for >12 months must already be
        under docs/_archive/. If this fails, new docs bloat has accumulated and
        should be archived (or linked, if still active)."""
        all_md = _all_repo_md_files()
        non_archive = _non_archive_docs(all_md)
        reachable = _reachable_docs(all_md)
        threshold = datetime.date.today() - datetime.timedelta(days=STALENESS_DAYS)

        offenders = []
        for f in non_archive:
            if f in reachable:
                continue
            touched = _last_touched(f)
            if touched is not None and touched < threshold:
                offenders.append((f, touched.isoformat()))

        assert offenders == [], (
            f"Docs bloat detected — unreachable AND untouched for >{STALENESS_DAYS} days, "
            f"should move to docs/_archive/: {offenders}"
        )
