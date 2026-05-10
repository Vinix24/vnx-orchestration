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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

    return OperatorMemory(
        name=name,
        description=description,
        type=mem_type,
        file_path=file_path,
        body=body.strip(),
        tags=tags,
        referenced_files=frozenset(referenced),
    )


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
) -> List[OperatorMemory]:
    """Fetch operator memories matching this dispatch's role + scope + instruction.

    Selection per smart-context-design §3.3 step 5.

    If memory_dir is not provided, it is derived from cwd using the Claude Code
    slug convention. Returns empty list if memory dir is missing (graceful skip).
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
    candidates = [m for _, m in scored[:MAX_MEMORIES_PER_DISPATCH * 2]]
    trimmed: List[OperatorMemory] = []
    for mem in candidates:
        if len(trimmed) >= MAX_MEMORIES_PER_DISPATCH:
            break
        candidate_list = trimmed + [mem]
        if len(format_memories_section(candidate_list)) <= max_chars:
            trimmed.append(mem)

    return trimmed


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
        if mem.body:
            lines.append(mem.body)
            lines.append("")

    return "\n".join(lines)
