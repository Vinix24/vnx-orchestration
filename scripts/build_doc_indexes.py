#!/usr/bin/env python3
"""build_doc_indexes.py — Catalog PRD and ADR artifacts into JSON indexes.

Walks docs/prds/ and docs/adrs/ in the project root, parses YAML frontmatter
from each .md file, and writes:
  .vnx-data/strategy/prd_index.json
  .vnx-data/strategy/adr_index.json

Idempotent. Missing source dirs → empty index, no error.
Malformed frontmatter → entry written with status='draft', no crash.
Stable output: sorted by id.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from project_root import resolve_data_dir, resolve_project_root  # noqa: E402

VALID_STATUSES = frozenset({"draft", "active", "superseded", "retired"})
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        import yaml
        parsed = yaml.safe_load(m.group(1))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_entry(md_file: Path, project_root: Path) -> dict:
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError:
        text = ""

    fm = _parse_frontmatter(text)

    doc_id = fm.get("id") or md_file.stem
    title = fm.get("title") or md_file.stem
    version = str(fm.get("version") or "")
    status = fm.get("status") or "draft"
    if status not in VALID_STATUSES:
        status = "draft"

    supersedes = fm.get("supersedes")
    if supersedes is not None:
        supersedes = str(supersedes)

    try:
        rel_path = str(md_file.relative_to(project_root))
    except ValueError:
        rel_path = str(md_file)

    return {
        "id": str(doc_id),
        "path": rel_path,
        "version": version,
        "status": status,
        "supersedes": supersedes,
        "title": str(title),
    }


def _build_index(source_dir: Path, project_root: Path) -> list[dict]:
    if not source_dir.exists() or not source_dir.is_dir():
        return []
    entries = [_extract_entry(f, project_root) for f in source_dir.glob("*.md")]
    return sorted(entries, key=lambda e: e["id"])


def build(
    data_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> tuple[list[dict], list[dict]]:
    """Return (prd_index, adr_index) without writing anything."""
    if project_root is None:
        project_root = resolve_project_root(__file__)
    if data_dir is None:
        data_dir = resolve_data_dir(__file__)

    prd_index = _build_index(project_root / "docs" / "prds", project_root)
    adr_index = _build_index(project_root / "docs" / "adrs", project_root)
    return prd_index, adr_index


def main() -> None:
    project_root = resolve_project_root(__file__)
    data_dir = resolve_data_dir(__file__)
    strategy_dir = data_dir / "strategy"
    strategy_dir.mkdir(parents=True, exist_ok=True)

    prd_index, adr_index = build(data_dir, project_root)

    prd_out = strategy_dir / "prd_index.json"
    adr_out = strategy_dir / "adr_index.json"

    prd_out.write_text(json.dumps(prd_index, indent=2))
    adr_out.write_text(json.dumps(adr_index, indent=2))

    print(f"[ok] prd_index.json written ({len(prd_index)} entries) → {prd_out}")
    print(f"[ok] adr_index.json written ({len(adr_index)} entries) → {adr_out}")


if __name__ == "__main__":
    main()
