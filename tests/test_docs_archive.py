"""Tests for docs/_archive/ integrity (PR-11, docs bloat cleanup).

Guards two invariants:
  1. Every file this sweep archived lives under docs/_archive/ only — its
     pre-archive path must no longer exist.
  2. docs/_archive/ARCHIVED_MANIFEST.md's "this sweep" table matches disk
     exactly and every row satisfies its declared rule.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "docs" / "_archive"
MANIFEST = ARCHIVE_DIR / "ARCHIVED_MANIFEST.md"
SWEEP_HEADING = "## This sweep (2026-07-12) — 3 files archived, rule 1 (explicit-comparisons)"


def _manifest_table_rows(section_heading: str) -> list:
    text = MANIFEST.read_text(encoding="utf-8")
    idx = text.index(section_heading)
    rest = text[idx + len(section_heading):]
    next_heading = re.search(r"\n##\s", rest)
    section = rest[: next_heading.start()] if next_heading else rest

    rows = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= {"-"} for c in cells if c):
            continue  # header separator row
        rows.append(cells)
    return rows[1:]  # drop the header row itself


class TestComparisonsArchived:
    def test_comparisons_dir_removed_from_docs(self):
        assert not (REPO_ROOT / "docs" / "comparisons").exists(), (
            "docs/comparisons/ should have been fully archived and removed"
        )

    def test_comparisons_files_live_under_archive(self):
        for name in ("headless_vs_interactive.md", "vnx_vs_claude_code.md", "vnx_vs_frameworks.md"):
            assert (ARCHIVE_DIR / "comparisons" / name).is_file(), f"missing archived file: {name}"
            assert not (REPO_ROOT / "docs" / "comparisons" / name).exists()


class TestManifestMatchesSweep:
    def test_this_sweep_rows_match_disk_and_rule(self):
        rows = _manifest_table_rows(SWEEP_HEADING)
        assert len(rows) == 3
        for now_path, before_path, _touched, rule in rows:
            now_path = now_path.strip("`")
            before_path = before_path.strip("`")
            assert (ARCHIVE_DIR / now_path).is_file(), f"manifest row not on disk: {now_path}"
            assert not (REPO_ROOT / before_path).exists(), (
                f"manifest claims {before_path} was archived but it still exists"
            )
            assert rule.strip() == "explicit-comparisons"
            assert now_path.startswith("comparisons/")

    def test_no_extra_files_under_archived_comparisons(self):
        on_disk = {p.name for p in (ARCHIVE_DIR / "comparisons").glob("*.md")}
        manifest_names = {
            row[0].strip("`").split("/")[-1] for row in _manifest_table_rows(SWEEP_HEADING)
        }
        assert on_disk == manifest_names
