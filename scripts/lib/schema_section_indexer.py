#!/usr/bin/env python3
"""Wave 5 P4 — Schema section indexer for context injection.

Scans schemas/*.sql + schemas/migrations/*.sql, extracts CREATE TABLE /
ALTER TABLE / CREATE INDEX / CREATE VIEW sections keyed by table name.
Builds inverted index table_name -> list[SchemaSection] (a single table may
appear in multiple migration files).

When dispatch references DB-related work (dispatch_paths in scripts/migrate*,
schemas/, importers; OR instruction_text mentions table names found in
the index), inject the relevant CREATE/ALTER sections.

Cached for 60s with mtime-change invalidation (mirrors adr_indexer +
operator_memory_indexer pattern).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

MAX_SCHEMA_CHARS = 1500          # parity with code_anchor budget
MAX_SECTIONS_PER_DISPATCH = 4    # max table sections injected
CACHE_TTL_SEC = 60

# Match SQL DDL statements through the next semicolon.
# Capture: full statement body (group 1) and table/object name (group 2).
_DDL_RE = re.compile(
    r'(CREATE\s+(?:TABLE|UNIQUE\s+INDEX|INDEX|VIEW|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?(\w+)[`"]?[^;]*;)|'
    r'(ALTER\s+TABLE\s+[`"]?(\w+)[`"]?[^;]*;)',
    re.IGNORECASE | re.DOTALL,
)

# Classify DDL kind from the statement prefix
_KIND_RE = re.compile(
    r'^(CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)|ALTER\s+TABLE)',
    re.IGNORECASE,
)

# For CREATE INDEX statements: extract the ON <table_name> target so we can
# also index the section under the table being indexed, not just the index name.
_INDEX_ON_RE = re.compile(r'\bON\s+[`"]?(\w+)[`"]?\s*\(', re.IGNORECASE)

# Patterns indicating a dispatch is DB-related
_DB_PATH_HINTS = ('schemas/', 'scripts/migrate', '_import_table', 'sqlite', 'migration')

# Token extraction for table-name candidates in instruction text
_TABLE_NAME_RE = re.compile(r'\b([a-z_][a-z_0-9]{3,})\b')

# Stopwords: common English and SQL words that are not table names
_STOPWORDS = frozenset({
    'create', 'table', 'index', 'view', 'alter', 'insert', 'select', 'update',
    'delete', 'where', 'from', 'into', 'values', 'column', 'primary', 'foreign',
    'unique', 'default', 'null', 'not', 'exists', 'pragma', 'schema',
    'scripts', 'should', 'would', 'could', 'which', 'these', 'those',
    'after', 'before', 'using', 'other', 'given', 'makes', 'tests', 'test',
    'with', 'that', 'each', 'must', 'need', 'such', 'both', 'then', 'than',
    'does', 'false', 'lines', 'match', 'right', 'found', 'error', 'class',
    'fetch', 'list', 'dict', 'none', 'true', 'path', 'type', 'name', 'data',
    'text', 'item', 'args', 'self', 'this', 'init', 'call', 'load', 'read',
    'write', 'send', 'note', 'pass', 'base', 'case', 'code', 'file', 'line',
    'step', 'only', 'same', 'more', 'less', 'over', 'like', 'make', 'when',
    'will', 'have', 'been', 'also', 'rows', 'columns', 'tables', 'indexes',
    'result', 'return', 'value', 'field', 'block', 'check', 'gate', 'scan',
    'migration', 'migrations', 'dispatch', 'importer', 'import', 'export',
})


@dataclass(frozen=True)
class SchemaSection:
    table_name: str
    statement_kind: str    # 'CREATE TABLE' / 'ALTER TABLE' / 'CREATE INDEX' / etc.
    file_path: str         # 'schemas/migrations/0016_central_state.sql'
    body: str              # full DDL statement


@dataclass
class _SchemaIndex:
    """In-memory index for schema sections with mtime-invalidation."""
    # table_name -> list of SchemaSection (across all files)
    table_index: Dict[str, List[SchemaSection]] = field(default_factory=dict)
    loaded_at: float = 0.0
    _loaded_dir: Optional[Path] = None
    _file_mtimes: Dict[str, float] = field(default_factory=dict)

    def needs_refresh(self) -> bool:
        if (time.time() - self.loaded_at) > CACHE_TTL_SEC:
            return True
        return self._mtime_changed()

    def _mtime_changed(self) -> bool:
        d = self._loaded_dir
        if d is None or not d.is_dir():
            return False
        for sql_file in d.glob("**/*.sql"):
            try:
                mtime = sql_file.stat().st_mtime
            except OSError:
                continue
            if mtime != self._file_mtimes.get(str(sql_file), 0.0):
                return True
        return False

    def load(self, schemas_dir: Path) -> None:
        self._loaded_dir = schemas_dir
        new_index: Dict[str, List[SchemaSection]] = {}
        new_mtimes: Dict[str, float] = {}

        if not schemas_dir.is_dir():
            self.table_index = new_index
            self._file_mtimes = new_mtimes
            self.loaded_at = time.time()
            return

        for sql_file in sorted(schemas_dir.glob("**/*.sql")):
            resolved = sql_file.resolve()
            # Path-traversal guard: file must stay within schemas_dir
            try:
                resolved.relative_to(schemas_dir.resolve())
            except ValueError:
                continue

            try:
                stat = sql_file.stat()
                new_mtimes[str(sql_file)] = stat.st_mtime
                text = sql_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Derive a repo-relative path for display (e.g. "schemas/migrations/0010.sql")
            try:
                rel_path = str(sql_file.relative_to(schemas_dir.parent))
            except ValueError:
                rel_path = str(sql_file)

            for match in _DDL_RE.finditer(text):
                # group(1) = CREATE ... ; group(2) = object name for CREATE
                # group(3) = ALTER ... ; group(4) = table name for ALTER
                if match.group(1):
                    full_stmt = match.group(1)
                    obj_name = match.group(2)
                else:
                    full_stmt = match.group(3)
                    obj_name = match.group(4)

                if not obj_name:
                    continue

                obj_name = obj_name.lower()
                kind_m = _KIND_RE.match(full_stmt.strip())
                kind = kind_m.group(1).upper() if kind_m else "DDL"
                # Normalize: collapse whitespace runs into single spaces
                kind = re.sub(r'\s+', ' ', kind)

                section = SchemaSection(
                    table_name=obj_name,
                    statement_kind=kind,
                    file_path=rel_path,
                    body=full_stmt.strip(),
                )
                new_index.setdefault(obj_name, []).append(section)

                # For CREATE INDEX, also index under the ON-table name so callers
                # looking for the indexed table find the relevant index statement.
                if "INDEX" in kind and match.group(1):
                    on_m = _INDEX_ON_RE.search(full_stmt)
                    if on_m:
                        on_table = on_m.group(1).lower()
                        if on_table != obj_name:
                            new_index.setdefault(on_table, []).append(section)

        self.table_index = new_index
        self._file_mtimes = new_mtimes
        self.loaded_at = time.time()


# Module-level singleton index
_INDEX = _SchemaIndex()


def _resolve_schemas_dir(schemas_dir: Optional[Path] = None) -> Path:
    """Resolve schemas/ directory via vnx_paths if available; else cwd fallback."""
    if schemas_dir is not None:
        return schemas_dir.resolve()
    try:
        from vnx_paths import resolve_paths
        paths = resolve_paths()
        return Path(paths["PROJECT_ROOT"]) / "schemas"
    except Exception:
        return Path.cwd() / "schemas"


def _is_db_related_dispatch(dispatch_paths: List[str], instruction_text: str) -> bool:
    """Quick gate: does this dispatch touch DB-related files or mention DB terms?"""
    if any(any(hint in p.lower() for hint in _DB_PATH_HINTS) for p in dispatch_paths):
        return True
    if any(hint in instruction_text.lower() for hint in _DB_PATH_HINTS):
        return True
    return False


def _extract_table_candidates(
    dispatch_paths: List[str],
    instruction_text: str,
    known_table_names: frozenset,
) -> List[str]:
    """Extract candidate table names from instruction text and dispatch path basenames.

    Matches tokens from the instruction against the known table index. Also checks
    path basenames (e.g. 'dispatch_metadata.py' yields 'dispatch_metadata').
    """
    candidates: List[str] = []
    seen: set = set()

    # From instruction text: extract identifier-like tokens
    for m in _TABLE_NAME_RE.finditer(instruction_text or ""):
        token = m.group(1).lower()
        if token in _STOPWORDS or len(token) < 4:
            continue
        if token in known_table_names and token not in seen:
            candidates.append(token)
            seen.add(token)

    # From dispatch path basenames (strip extension, split on underscores/hyphens)
    for p in dispatch_paths:
        stem = Path(p).stem.lower()
        # Try the whole stem first (e.g. "dispatch_metadata")
        if stem in known_table_names and stem not in seen:
            candidates.append(stem)
            seen.add(stem)
        # Try all consecutive segment combinations (pairs, triples, etc.)
        parts = re.split(r'[_\-]', stem)
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                compound = "_".join(parts[start:end])
                if len(compound) >= 4 and compound in known_table_names and compound not in seen:
                    candidates.append(compound)
                    seen.add(compound)

    return candidates


def fetch_relevant_schema_sections(
    dispatch_paths: List[str],
    instruction_text: str,
    *,
    max_chars: int = MAX_SCHEMA_CHARS,
    schemas_dir: Optional[Path] = None,
) -> List[SchemaSection]:
    """Fetch schema sections matching this dispatch's tables.

    Strategy:
    1. Quick gate via _is_db_related_dispatch() — return [] for non-DB dispatches
    2. Build/refresh index from schemas_dir (cache 60s + mtime invalidation)
    3. Extract candidate table names from instruction_text and dispatch_paths basenames
    4. Match candidates against index keys
    5. Cap MAX_SECTIONS_PER_DISPATCH, prefer most-recent migration file (basename sort descending)
    6. Trim to fit max_chars
    """
    if not _is_db_related_dispatch(dispatch_paths, instruction_text):
        return []

    resolved_dir = _resolve_schemas_dir(schemas_dir)
    if _INDEX.needs_refresh():
        _INDEX.load(resolved_dir)

    if not _INDEX.table_index:
        return []

    known_names = frozenset(_INDEX.table_index.keys())
    candidates = _extract_table_candidates(dispatch_paths, instruction_text, known_names)

    # Collect all sections for candidate tables
    raw_sections: List[SchemaSection] = []
    for table_name in candidates:
        secs = _INDEX.table_index.get(table_name, [])
        raw_sections.extend(secs)

    if not raw_sections:
        return []

    # Deduplicate (same body can appear twice if index loaded multiple times)
    seen_bodies: set = set()
    unique_sections: List[SchemaSection] = []
    for s in raw_sections:
        key = (s.table_name, s.body)
        if key not in seen_bodies:
            seen_bodies.add(key)
            unique_sections.append(s)

    # Sort: prefer newer migration files (basename sort descending, so 0016 > 0010)
    # Stable secondary sort by table_name for determinism.
    unique_sections.sort(
        key=lambda s: (Path(s.file_path).name, s.table_name),
        reverse=True,
    )

    # Cap by count
    selected = unique_sections[:MAX_SECTIONS_PER_DISPATCH]

    # Cap by chars
    total = 0
    final: List[SchemaSection] = []
    for sec in selected:
        body_len = len(sec.body)
        if total + body_len > max_chars and final:
            break
        final.append(sec)
        total += body_len

    return final


def format_schema_sections(sections: List[SchemaSection]) -> str:
    """Format as markdown section for dispatch instruction injection.

    Includes anti-anchoring note: these sections show CREATE/ALTER statements as
    they appear in the migration files. Live DB schema may differ if migrations
    are incomplete; verify via PRAGMA table_info() if exact runtime shape matters.
    """
    if not sections:
        return ""

    lines = [
        "## Schema sections (auto-selected)",
        "",
        "> These schema sections show the CREATE/ALTER statements as they appear in"
        " migration files. Live DB schema may differ if migrations are incomplete;"
        " verify via PRAGMA table_info() if exact runtime shape matters.",
        "",
    ]

    for sec in sections:
        lines.append(f"### {sec.file_path} ({sec.table_name})")
        lines.append("")
        lines.append("```sql")
        lines.append(sec.body)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
