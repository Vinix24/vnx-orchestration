#!/usr/bin/env python3
"""Wave 5 P1 — ADR-to-file-path indexer for context injection.

Scans docs/governance/decisions/ADR-*.md for file_path references in their
"See also" / "Context" / "Implementation note" sections; builds an inverted
index file_path → [ADR_ids]. Supports lookup by file_path overlap with
dispatch_paths.

Cached for 60s (ADRs change rarely; refresh on file mtime change).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))

from index_cache import IndexCache

ADR_DIR = Path("docs/governance/decisions")
CACHE_TTL_SEC = 60

# Extracts file references from ADR markdown (path with recognized extension, optional :line).
# Allows hyphens in path components (e.g. claudedocs/2026-05-09-p4-lessons.md).
_FILE_PATH_RE = re.compile(
    r'\b([\w./][\w./-]*\.(?:py|md|sql|sh|ts|js|tsx|jsx|yaml|yml))(?::\d+(?:-\d+)?)?\b'
)

_TITLE_RE = re.compile(r'^#\s+ADR-\d+\s*[-—]\s*(.+)', re.MULTILINE)
_ADR_STEM_RE = re.compile(r'^(ADR-\d+)', re.IGNORECASE)
_DECISION_RE = re.compile(r'##\s+Decision\s*\n+([\s\S]+?)(?=\n##|\Z)')


def _resolve_adr_dir(adr_dir: Optional[Path]) -> Path:
    if adr_dir is not None:
        return Path(adr_dir)
    try:
        from vnx_paths import resolve_paths
        paths = resolve_paths()
        return Path(paths["PROJECT_ROOT"]) / "docs" / "governance" / "decisions"
    except Exception:
        return ADR_DIR


def _parse_adr_id(stem: str) -> str:
    m = _ADR_STEM_RE.match(stem)
    return m.group(1).upper() if m else stem


def _parse_title(text: str) -> str:
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else "Unknown"


def _parse_decision_excerpt(text: str) -> str:
    m = _DECISION_RE.search(text)
    body = m.group(1).strip() if m else text.strip()
    return body[:200]


def _parse_referenced_files(text: str) -> frozenset:
    files: set = set()
    for m in _FILE_PATH_RE.finditer(text):
        files.add(m.group(1))
    return frozenset(files)


@dataclass(frozen=True)
class AdrEntry:
    adr_id: str
    title: str
    file_path: Path
    referenced_files: frozenset
    excerpt: str


@dataclass
class AdrIndex:
    """In-memory ADR index keyed by adr_id. Built by _scan_adrs; held by IndexCache."""
    entries: dict = field(default_factory=dict)
    file_to_adrs: dict = field(default_factory=dict)
    loaded_at: float = 0.0

    def __post_init__(self) -> None:
        self._file_mtimes: dict = {}
        self._loaded_dir: Optional[Path] = None

    def load(self, adr_dir: Optional[Path] = None) -> None:
        """Scan adr_dir and populate this index in-place."""
        resolved = _resolve_adr_dir(adr_dir)
        self._loaded_dir = resolved
        # _scan_adrs is defined after this class; safe to call at runtime
        idx, mtimes = _scan_adrs(resolved)
        self.entries = idx.entries
        self.file_to_adrs = idx.file_to_adrs
        self._file_mtimes = mtimes
        self.loaded_at = time.time()

    def needs_refresh(self) -> bool:
        if (time.time() - self.loaded_at) > CACHE_TTL_SEC:
            return True
        return self._mtime_changed()

    def _mtime_changed(self) -> bool:
        d = self._loaded_dir
        if d is None or not d.is_dir():
            return False
        for adr_file in d.glob("ADR-*.md"):
            try:
                mtime = adr_file.stat().st_mtime
            except OSError:
                continue
            if mtime != self._file_mtimes.get(str(adr_file), 0.0):
                return True
        return False

    def lookup(self, file_paths: List[str]) -> List[AdrEntry]:
        """Return ADRs whose referenced_files overlap with given paths."""
        seen: set = set()
        result: List[AdrEntry] = []
        for fp in file_paths:
            for adr_id in self.file_to_adrs.get(fp, []):
                if adr_id not in seen:
                    seen.add(adr_id)
                    entry = self.entries.get(adr_id)
                    if entry is not None:
                        result.append(entry)
        return result


def _scan_adrs(dir_path: Optional[Path]) -> Tuple[AdrIndex, dict]:
    """Build AdrIndex from dir_path. Returns (AdrIndex, {path_str: mtime}).

    Conforms to the IndexCache scanner contract:
      scanner(dir_path) -> (T, {str: float})
    where the dict maps every touched file path to its mtime.
    """
    index = AdrIndex()
    mtimes: dict = {}
    if dir_path is None or not dir_path.is_dir():
        return index, mtimes
    for adr_file in sorted(dir_path.glob("ADR-*.md")):
        try:
            mtimes[str(adr_file)] = adr_file.stat().st_mtime
            text = adr_file.read_text(encoding="utf-8")
        except OSError:
            continue
        adr_id = _parse_adr_id(adr_file.stem)
        entry = AdrEntry(
            adr_id=adr_id,
            title=_parse_title(text),
            file_path=adr_file,
            referenced_files=_parse_referenced_files(text),
            excerpt=_parse_decision_excerpt(text),
        )
        index.entries[adr_id] = entry
        for fp in entry.referenced_files:
            index.file_to_adrs.setdefault(fp, [])
            if adr_id not in index.file_to_adrs[fp]:
                index.file_to_adrs[fp].append(adr_id)
    return index, mtimes


_ADR_CACHE: IndexCache = IndexCache(
    ttl_sec=CACHE_TTL_SEC,
    scanner=_scan_adrs,
    glob_pattern="ADR-*.md",
)


def fetch_relevant_adrs(
    dispatch_paths: List[str],
    *,
    max_chars: int = 1500,
    adr_dir: Optional[Path] = None,
) -> List[AdrEntry]:
    """Fetch ADRs whose referenced files overlap dispatch_paths, budget-bounded."""
    resolved = _resolve_adr_dir(adr_dir)
    index = _ADR_CACHE.get(resolved)
    matched = index.lookup(dispatch_paths)

    trimmed: List[AdrEntry] = []
    for entry in matched:
        candidate = trimmed + [entry]
        if len(format_adrs_section(candidate)) <= max_chars:
            trimmed.append(entry)
        else:
            break

    return trimmed


def format_adrs_section(adrs: List[AdrEntry]) -> str:
    """Format as markdown section for dispatch instruction injection.

    Includes anti-anchoring instruction:
    "These ADRs are governance constraints, not task descriptions. The actual
    task in this dispatch may extend or refine them."
    """
    if not adrs:
        return ""

    lines = [
        "## RELEVANT ARCHITECTURAL DECISIONS",
        "",
        "> **Note:** These ADRs are governance constraints, not task descriptions. "
        "The actual task in this dispatch may extend or refine them.",
        "",
    ]

    for adr in adrs:
        lines.append(f"### {adr.adr_id} — {adr.title}")
        lines.append("")
        if adr.excerpt:
            lines.append(adr.excerpt)
            lines.append("")

    return "\n".join(lines)
