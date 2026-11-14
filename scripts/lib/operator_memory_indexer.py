#!/usr/bin/env python3
"""Wave 5 P3 — Operator memory indexer for context injection.

Scans ~/.claude/projects/<project-slug>/memory/*.md for operator-curated
feedback / project / user / reference memories. Builds a per-memory tag set
from frontmatter + content, then matches against worker role + dispatch_paths
+ instruction_text to pick the most relevant ones.

Memory file format expected:
    ---
    name: ...
    description: ...
    type: feedback|project|user|reference
    ---
    body markdown...

Selection algorithm (mirrors smart-context-design §3.3 step 5):
1. Parse frontmatter from each *.md (YAML between --- markers)
2. Compute tag set per memory:
   - explicit tags from frontmatter (if present)
   - role inference from name/description (e.g. "database" → matches database-engineer)
   - file_path references in body (matches dispatch_paths)
3. Score each memory by:
   - role match: +3
   - dispatch_path overlap: +2 per match
   - instruction_text term overlap with name/description: +1 per match
   - feedback type weighted 1.5x (operator's hard-won corrections)
4. Top-K selected (cap 3) within MAX_MEMORY_CHARS budget (1200 chars total)
5. Format as markdown section for injection

Cached for 60s with mtime-change invalidation (mirrors adr_indexer pattern).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

import lesson_refs
from vnx_paths import project_id_from_state_dir, resolve_paths

MAX_MEMORY_CHARS = 1200       # leaves budget for prior_round + adr + code_anchor
MAX_MEMORIES_PER_DISPATCH = 3
CACHE_TTL_SEC = 60

_FRONTMATTER_RE = re.compile(r'^---\n(.*?)\n---\n?(.*)', re.DOTALL)
_FILE_REF_RE = re.compile(r'\b([\w./][\w./-]*\.(?:py|md|sql|sh|yaml|yml|ts|js|tsx|jsx))\b')
_KV_RE = re.compile(r'^(\w+)\s*:\s*(.+)$', re.MULTILINE)
# Identifier tokens for instruction-term overlap (≥4 chars, snake_case or CamelCase)
_IDENT_RE = re.compile(r'\b([A-Z][a-zA-Z0-9_]{3,}|[a-z_][a-z_0-9]{4,})\b')
# Stopwords to filter from inferred tags
_STOPWORDS = frozenset({
    'about', 'always', 'never', 'should', 'would', 'could', 'where', 'which',
    'these', 'those', 'first', 'second', 'third', 'after', 'before', 'using',
    'via', 'through', 'memory', 'memories', 'operator', 'return', 'value',
    'other', 'given', 'makes', 'tests', 'test', 'with', 'from', 'that',
    'into', 'each', 'must', 'need', 'such', 'both', 'then', 'than', 'does',
    'files', 'false', 'lines', 'match', 'right', 'found', 'error', 'class',
    'fetch', 'list', 'dict', 'none', 'true', 'path', 'type', 'name', 'data',
    'text', 'item', 'args', 'self', 'this', 'init', 'call', 'load', 'read',
    'write', 'send', 'note', 'pass', 'base', 'case', 'code', 'file', 'line',
    'step', 'only', 'same', 'more', 'less', 'over', 'like', 'make',
})

# Role-keyword mapping: keywords that appear in memory name/description → role tags
_ROLE_KEYWORDS: Dict[str, List[str]] = {
    "database": ["database-engineer", "database", "migration", "schema", "sqlite"],
    "migration": ["database-engineer", "migration"],
    "backend": ["backend-developer", "backend"],
    "frontend": ["frontend-developer", "frontend", "dashboard"],
    "security": ["security-engineer", "security"],
    "testing": ["test-engineer", "tests", "pytest"],
    "codex": ["codex", "gate", "review"],
    "dispatch": ["dispatch", "vnx", "orchestrat"],
    "lease": ["lease", "terminal", "runtime_coordination"],
    "docker": ["docker", "container"],
    "api": ["api", "endpoint"],
    "github": ["github", "pull_request", "merge"],
    "subprocess": ["subprocess", "adapter", "headless"],
}


@dataclass(frozen=True)
class OperatorMemory:
    name: str
    description: str
    type: str               # 'feedback' / 'project' / 'user' / 'reference'
    file_path: Path
    body: str
    tags: frozenset         # inferred + explicit
    referenced_files: frozenset
    open_item_refs: frozenset = frozenset()   # OI-<n> named in the body
    # one tuple per ref: (ref, resolved, status, closed_at, pr_id, reason)
    open_item_status: tuple = ()
    # Validity fields as written in the frontmatter (ADR-078 § 5/6), raw and
    # unvalidated: ``None`` when unset, absent or the frontmatter did not parse.
    valid_from: object = None
    invalid_at: object = None
    superseded_by: object = None


@dataclass
class _MemoryCache:
    """In-memory cache for operator memories with mtime-invalidation."""
    entries: List[OperatorMemory] = field(default_factory=list)
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
        for mem_file in d.glob("*.md"):
            try:
                mtime = mem_file.stat().st_mtime
            except OSError:
                continue
            if mtime != self._file_mtimes.get(str(mem_file), 0.0):
                return True
        return False

    def load(self, memory_dir: Path) -> None:
        self._loaded_dir = memory_dir
        new_entries: List[OperatorMemory] = []
        new_mtimes: Dict[str, float] = {}

        if memory_dir.is_dir():
            for mem_file in sorted(memory_dir.glob("*.md")):
                if mem_file.name == "MEMORY.md":
                    continue  # index file — not a memory itself
                try:
                    stat = mem_file.stat()
                    new_mtimes[str(mem_file)] = stat.st_mtime
                    text = mem_file.read_text(encoding="utf-8")
                except OSError:
                    continue

                entry = _parse_memory_file(mem_file, text)
                if entry is not None:
                    new_entries.append(entry)

        self.entries = new_entries
        self._file_mtimes = new_mtimes
        self.loaded_at = time.time()


# Module-level caches keyed by memory_dir (str)
_CACHES: Dict[str, _MemoryCache] = {}


def _project_memory_dir(cwd: Optional[Path] = None) -> Optional[Path]:
    """Find current project's memory dir under ~/.claude/projects/.

    Derives the project slug from cwd by replacing '/' with '-' (Claude Code
    convention). Returns None if the directory does not exist (graceful skip —
    not all installs have memories yet).
    """
    if cwd is None:
        cwd = Path.cwd()
    cwd = cwd.resolve()

    # Claude Code slug: posix path with each '/' replaced by '-'
    # e.g. <HOME>/Development/foo → -<HOME>-Development-foo
    slug = cwd.as_posix().replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / slug / "memory"
    if candidate.is_dir():
        return candidate

    return None


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Extract YAML frontmatter key-value pairs and body from a memory file.

    Returns (kv_dict, body_text). If no frontmatter present, returns ({}, text).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    fm_text = m.group(1)
    body = m.group(2)

    kv: Dict[str, str] = {}
    for kv_match in _KV_RE.finditer(fm_text):
        key = kv_match.group(1).strip()
        val = kv_match.group(2).strip()
        kv[key] = val

    return kv, body


def _infer_tags(name: str, description: str) -> frozenset:
    """Infer tag set from memory name and description.

    Extracts identifier tokens and maps them to role/domain tags via
    _ROLE_KEYWORDS; also includes the raw lowercased tokens for term overlap.
    """
    combined = f"{name} {description}".lower()
    tokens: set = set()

    # Raw identifier tokens (≥4 chars, not stopwords)
    for m in _IDENT_RE.finditer(combined):
        tok = m.group(1).lower()
        if tok not in _STOPWORDS and len(tok) >= 4:
            tokens.add(tok)

    # Role/domain keywords
    role_tags: set = set()
    for role_key, keywords in _ROLE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            role_tags.add(role_key)

    return frozenset(tokens | role_tags)


def _parse_memory_file(file_path: Path, text: str) -> Optional[OperatorMemory]:
    """Parse a single memory .md file into an OperatorMemory.

    Returns None if the file cannot be parsed meaningfully.
    """
    kv, body = _parse_frontmatter(text)

    name = kv.get("name", file_path.stem)
    description = kv.get("description", "")
    mem_type = kv.get("type", "reference").strip().lower()

    # Explicit tags from frontmatter (if present as comma-separated list)
    explicit_tags: set = set()
    if "tags" in kv:
        for t in kv["tags"].split(","):
            t = t.strip().lower()
            if t:
                explicit_tags.add(t)

    inferred = _infer_tags(name, description)
    tags = frozenset(explicit_tags | inferred)

    # File references extracted from body
    referenced: set = set()
    for ref_m in _FILE_REF_RE.finditer(body):
        referenced.add(ref_m.group(1))

    validity = _read_validity_fields(text)

    return OperatorMemory(
        name=name,
        description=description,
        type=mem_type,
        file_path=file_path,
        body=body.strip(),
        tags=tags,
        referenced_files=frozenset(referenced),
        open_item_refs=frozenset(lesson_refs.find_oi_refs(body)),
        valid_from=validity["valid_from"],
        invalid_at=validity["invalid_at"],
        superseded_by=validity["superseded_by"],
    )


class _NoTimestampLoader(yaml.SafeLoader):
    """Safe loader that keeps timestamps as text so the zone stays checkable."""

_NoTimestampLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

_VALIDITY_FIELDS = ("valid_from", "invalid_at", "superseded_by")


def _read_validity_fields(text: str) -> Dict[str, object]:
    """``metadata.<f>`` first, then top level (ADR-078 § 5); unparseable gives all None."""
    result: Dict[str, object] = {f: None for f in _VALIDITY_FIELDS}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return result
    try:
        doc = yaml.load(m.group(1), Loader=_NoTimestampLoader)  # SafeLoader subclass: no object construction
    except yaml.YAMLError:
        return result
    if not isinstance(doc, dict):
        return result
    meta = doc.get("metadata")
    for f in _VALIDITY_FIELDS:
        value = meta.get(f) if isinstance(meta, dict) else None
        result[f] = value if value is not None else doc.get(f)
    return result


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(Z|\+00:00))?$")
MAX_SUPERSEDE_LINKS = 10


def _parse_utc(value: object) -> Optional[datetime]:
    """Parse an ISO 8601 UTC date (date only = 00:00Z); ``None`` when unreadable."""
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return None
    try:
        return datetime.fromisoformat(value.strip()).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _temporarily_out(mem: OperatorMemory, now: datetime, warnings: List[dict]) -> bool:
    """Rule (a)/(b); an unreadable date counts as unset, with a warning."""
    out = False
    for field_name in ("invalid_at", "valid_from"):
        raw = getattr(mem, field_name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        when = _parse_utc(raw)
        if when is None:
            warnings.append({"kind": "unreadable_date", "note": mem.file_path.name,
                             "field": field_name})
        elif field_name == "invalid_at" and when <= now:
            out = True
        elif field_name == "valid_from" and when > now:
            out = True
    return out


def _successor_name(raw: object) -> Optional[str]:
    """File name of the successor, or ``None`` for a path / non-text value."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    return name if name.endswith(".md") else name + ".md"


def _is_superseded(
    mem: OperatorMemory,
    by_name: Dict[str, OperatorMemory],
    now: datetime,
    warnings: List[dict],
) -> bool:
    """Rule (c): the ``superseded_by`` chain reaches a valid successor."""
    def warn(kind: str) -> bool:
        warnings.append({"kind": kind, "note": mem.file_path.name})
        return False

    current, visited = mem, {mem.file_path.name}
    for _ in range(MAX_SUPERSEDE_LINKS):
        name = _successor_name(current.superseded_by)
        nxt = by_name.get(name) if name else None
        if nxt is None:
            return warn("successor_missing")
        if nxt.file_path.name in visited:
            return warn("cycle")
        if not _temporarily_out(nxt, now, []):
            return True
        if nxt.superseded_by is None:
            return warn("successor_not_valid")
        visited.add(nxt.file_path.name)
        current = nxt
    return warn("chain_too_long")


def filter_valid_memories(
    entries: List[OperatorMemory],
    now: datetime,
    warnings: Optional[List[dict]] = None,
) -> List[OperatorMemory]:
    """Drop notes that are out of force at ``now`` (ADR-078 § 6).

    ``now`` must be timezone-aware. Notes that cannot be judged (cycle, missing
    successor, unreadable date, chain too long) stay in, with a warning appended
    to ``warnings``. Successors are never added.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    now = now.astimezone(timezone.utc)
    sink: List[dict] = warnings if warnings is not None else []
    by_name = {m.file_path.name: m for m in entries}
    kept: List[OperatorMemory] = []
    for mem in entries:
        if _temporarily_out(mem, now, sink):
            continue
        if mem.superseded_by is not None and _is_superseded(mem, by_name, now, sink):
            continue
        kept.append(mem)
    return kept


def _score_memory(
    mem: OperatorMemory,
    role: Optional[str],
    dispatch_paths: List[str],
    instruction_terms: List[str],
) -> float:
    """Compute relevance score for a memory against dispatch context.

    Scoring:
    - role match: +3 (if role keyword appears in memory tags or name/description)
    - dispatch_path overlap: +2 per matching path
    - instruction_term overlap with name+description: +1 per term
    - feedback type: 1.5x multiplier (operator hard-won corrections carry more weight)
    """
    score = 0.0

    # Role match
    if role:
        role_lower = role.lower()
        role_parts = set(re.split(r'[-_]', role_lower))
        # Direct role tag match
        if role_lower in mem.tags:
            score += 3
        # Partial match on role fragments (e.g. "database" from "database-engineer")
        elif role_parts & mem.tags:
            score += 2
        # Fallback: check if role appears in name or description
        elif role_lower in f"{mem.name} {mem.description}".lower():
            score += 1

    # Dispatch path overlap (+2 per match)
    if dispatch_paths:
        dispatch_path_set = set(dispatch_paths)
        # Check if any referenced file in memory matches a dispatch path
        for ref_file in mem.referenced_files:
            if ref_file in dispatch_path_set:
                score += 2
        # Also check if dispatch path fragments appear in memory name/description
        mem_text = f"{mem.name} {mem.description}".lower()
        for dp in dispatch_paths:
            dp_parts = re.split(r'[/._-]', dp.lower())
            for part in dp_parts:
                if len(part) >= 4 and part in mem_text and part not in _STOPWORDS:
                    score += 0.5
                    break

    # Instruction term overlap (+1 per term found in name or description)
    if instruction_terms:
        mem_text = f"{mem.name} {mem.description}".lower()
        for term in instruction_terms:
            if term.lower() in mem_text:
                score += 1

    # Feedback type multiplier
    if mem.type == "feedback":
        score *= 1.5

    return score


def _extract_instruction_terms(instruction_text: str) -> List[str]:
    """Extract identifier-like tokens from instruction text for overlap scoring."""
    seen: set = set()
    result: List[str] = []
    for m in _IDENT_RE.finditer(instruction_text or ""):
        tok = m.group(1).lower()
        if tok not in _STOPWORDS and len(tok) >= 4 and tok not in seen:
            seen.add(tok)
            result.append(tok)
    return result


def _get_cache(memory_dir: Path) -> _MemoryCache:
    key = str(memory_dir.resolve())
    if key not in _CACHES:
        _CACHES[key] = _MemoryCache()
    cache = _CACHES[key]
    if cache.loaded_at == 0.0 or cache.needs_refresh():
        cache.load(memory_dir)
    return cache


def fetch_relevant_memories(
    role: Optional[str],
    dispatch_paths: List[str],
    instruction_text: str,
    *,
    max_chars: int = MAX_MEMORY_CHARS,
    memory_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    warnings: Optional[List[dict]] = None,
) -> List[OperatorMemory]:
    """Fetch operator memories matching this dispatch's role + scope + instruction.

    Selection per smart-context-design §3.3 step 5.

    If memory_dir is not provided, it is derived from cwd using the Claude Code
    slug convention. Returns empty list if memory dir is missing (graceful skip).

    Notes out of force at ``now`` (default: current UTC time) are left out
    (``invalid_at``, ``valid_from``, ``superseded_by``; ADR-078 § 6). Validity
    warnings are appended to ``warnings`` when a list is given.
    """
    # Resolve memory directory
    if memory_dir is None:
        memory_dir = _project_memory_dir()
        if memory_dir is None:
            return []
    else:
        memory_dir = Path(memory_dir).resolve()

    if not memory_dir.is_dir():
        return []

    # Path traversal safety: ensure memory_dir is resolved to an absolute path;
    # individual file reads enforce is_relative_to below.
    resolved_memory_dir = memory_dir.resolve()

    # Load (possibly cached) entries
    cache = _get_cache(resolved_memory_dir)
    entries = cache.entries

    if not entries:
        return []

    # Path-traversal guard: filter to only files within resolved_memory_dir
    safe_entries: List[OperatorMemory] = []
    for mem in entries:
        try:
            resolved_fp = mem.file_path.resolve()
            resolved_fp.relative_to(resolved_memory_dir)
            safe_entries.append(mem)
        except ValueError:
            continue  # escape attempt — silently skip

    if not safe_entries:
        return []

    safe_entries = filter_valid_memories(
        safe_entries, now if now is not None else datetime.now(timezone.utc), warnings
    )
    if not safe_entries:
        return []

    # Extract instruction terms for overlap scoring
    instruction_terms = _extract_instruction_terms(instruction_text)

    # Score all memories
    scored: List[Tuple[float, OperatorMemory]] = []
    for mem in safe_entries:
        s = _score_memory(mem, role, dispatch_paths or [], instruction_terms)
        if s > 0:
            scored.append((s, mem))

    # Sort by score descending, then by type (feedback first on ties)
    scored.sort(key=lambda x: (-x[0], x[1].type != "feedback"))

    # Cap at MAX_MEMORIES_PER_DISPATCH and fit within budget
    candidates = _attach_open_item_status(
        [m for _, m in scored[:MAX_MEMORIES_PER_DISPATCH * 2]]
    )
    trimmed: List[OperatorMemory] = []
    for mem in candidates:
        if len(trimmed) >= MAX_MEMORIES_PER_DISPATCH:
            break
        candidate_list = trimmed + [mem]
        if len(format_memories_section(candidate_list)) <= max_chars:
            trimmed.append(mem)

    return trimmed


def _attach_open_item_status(candidates: List[OperatorMemory]) -> List[OperatorMemory]:
    """Hang the live status of every named OI on the candidates (read-only).

    The store of this process (``VNX_STATE_DIR``) is read once per call and only
    when a candidate names an OI. Without a valid project id nothing is read and
    every ref is ``onbekend (project_unknown)``. Unknown is never open.
    """
    if not any(m.open_item_refs for m in candidates):
        return candidates
    state_dir = Path(resolve_paths()["VNX_STATE_DIR"])
    if project_id_from_state_dir(state_dir):
        index, state = lesson_refs.load_oi_index(state_dir)
    else:
        index, state = {}, lesson_refs.REASON_PROJECT_UNKNOWN
    result: List[OperatorMemory] = []
    for mem in candidates:
        if not mem.open_item_refs:
            result.append(mem)
            continue
        resolved = lesson_refs.resolve_oi_refs(sorted(mem.open_item_refs), index, state)
        status = tuple(
            (ref, r["resolved"], r["status"], r["closed_at"], r["pr_id"], r["reason"])
            for ref, r in resolved.items()
        )
        result.append(replace(mem, open_item_status=status))
    return result


def _format_open_item_line(entry: tuple) -> str:
    ref, resolved, status, closed_at, pr_id, reason = entry
    if not resolved:
        return f"- {ref}: onbekend ({reason})"
    if status not in lesson_refs.CLOSED_STATUSES:
        return f"- {ref}: {status}"
    parts = []
    if closed_at:
        parts.append(f"gesloten {closed_at[:10]}")
    if pr_id:
        parts.append(f"#{pr_id.lstrip('#')}")
    return f"- {ref}: {status} ({', '.join(parts)})" if parts else f"- {ref}: {status}"


def format_memories_section(memories: List[OperatorMemory]) -> str:
    """Format as markdown section for dispatch instruction injection.

    Anti-anchoring instruction included: operator memories from past sessions
    may no longer apply — verify against current code state before treating
    as binding.
    """
    if not memories:
        return ""

    lines = [
        "## OPERATOR MEMORIES (curated wisdom from past sessions)",
        "",
        "> **Anti-anchoring notice:** These are operator memories from past sessions. "
        "Apply where relevant; the current task may have shifted context — verify "
        "against current code state before treating as binding.",
        "",
    ]

    for mem in memories:
        type_label = f"[{mem.type}]" if mem.type else ""
        lines.append(f"### {type_label} {mem.name}")
        lines.append("")
        if mem.description:
            lines.append(f"*{mem.description}*")
            lines.append("")
        if mem.open_item_status:
            lines.append("Open items named in this note (live status):")
            lines.extend(_format_open_item_line(e) for e in mem.open_item_status)
            lines.append("")
        if mem.body:
            lines.append(mem.body)
            lines.append("")

    return "\n".join(lines)
