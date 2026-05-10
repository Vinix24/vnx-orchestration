#!/usr/bin/env python3
"""Wave 5 P2 — Code anchor finder for context injection.

Given a list of dispatch_paths and instruction terms, locates the most
relevant file:line snippets and returns them as bounded CodeAnchor entries.

Strategy (per smart-context-design §3.3 step 2):
1. For each path in dispatch_paths (cap 5 files), grep-search for instruction
   terms (function names, class names, identifiers extracted from instruction).
2. For each match, slice out the surrounding ±5 lines as the anchor body.
3. Cap 3 anchors per file. Cap 1500 chars total budget.
4. Recency-rank ties by file mtime (newer first).

Anti-anchoring instruction is included in the formatted section: "These code
anchors are current-state evidence to ground decisions, not the only source
of truth — re-read the file if anchors look incomplete or stale."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_CODE_ANCHOR_CHARS = 1500
MAX_FILES = 5
MAX_ANCHORS_PER_FILE = 3
ANCHOR_CONTEXT_LINES = 5  # ±5 lines around the match

# Identifier extraction: pull function-like / class-like / snake_case_token names
# from the dispatch instruction text. Avoid common English words.
_IDENT_RE = re.compile(r'\b([A-Z][a-zA-Z0-9_]{3,}|[a-z_][a-z_0-9]{4,})\b')
_STOPWORDS = frozenset({
    'should', 'would', 'could', 'where', 'which', 'their', 'these', 'those',
    'after', 'before', 'about', 'above', 'below', 'while', 'until', 'every',
    'first', 'second', 'third', 'never', 'always', 'check', 'verify',
    'return', 'value', 'other', 'given', 'using', 'makes', 'tests', 'test',
    'when', 'will', 'have', 'been', 'also', 'with', 'from', 'that', 'into',
    'each', 'must', 'need', 'such', 'both', 'then', 'than', 'does',
    'files', 'false', 'lines', 'match', 'right', 'found', 'issue',
    'error', 'class', 'fetch', 'list', 'dict', 'none', 'true', 'path',
    'type', 'name', 'data', 'text', 'item', 'items', 'args', 'kwargs',
    'self', 'this', 'init', 'call', 'load', 'read', 'write', 'send',
    'note', 'pass', 'base', 'case', 'code', 'file', 'line', 'page',
    'step', 'only', 'same', 'more', 'less', 'over', 'like', 'make',
})


@dataclass(frozen=True)
class CodeAnchor:
    file_path: str        # "scripts/lib/foo.py"
    line_start: int
    line_end: int
    matched_term: str     # the identifier that triggered the anchor
    body: str             # the actual file slice (line_start..line_end inclusive)


def extract_terms(instruction_text: str) -> list[str]:
    """Pull identifier-like tokens from the dispatch instruction.

    Filters: ≥4 char snake_case, ≥4 char CamelCase, deduped, stopwords removed.
    """
    seen: set[str] = set()
    result: list[str] = []
    for m in _IDENT_RE.finditer(instruction_text or ""):
        token = m.group(1)
        lower = token.lower()
        if lower in _STOPWORDS:
            continue
        # Require minimum length of 4 chars (regex already enforces but explicit)
        if len(token) < 4:
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return result


def _is_binary(content_bytes: bytes) -> bool:
    """Return True if first 1KB contains a null byte (binary file indicator)."""
    return b'\x00' in content_bytes[:1024]


def _is_inside_repo_root(path: Path, repo_root: Path) -> bool:
    """Return True if path resolves to a location inside repo_root.

    Resolves symlinks before checking so symlink-escape attacks are caught.
    Silent (no exception) — caller skips on False.
    """
    try:
        resolved_path = path.resolve()
        resolved_root = repo_root.resolve()
        resolved_path.relative_to(resolved_root)
        return True
    except (ValueError, OSError):
        return False


def _read_lines(file_path: Path) -> Optional[list[str]]:
    """Read file as lines; return None if unreadable or binary."""
    try:
        raw = file_path.read_bytes()
    except OSError:
        return None
    if _is_binary(raw):
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    return text.splitlines()


def _find_term_matches(lines: list[str], term: str) -> list[int]:
    """Return 0-based line indices where term appears as a whole token."""
    pattern = re.compile(r'\b' + re.escape(term) + r'\b')
    return [i for i, line in enumerate(lines) if pattern.search(line)]


def _slice_anchor(
    lines: list[str],
    match_line: int,
    file_path_str: str,
    term: str,
) -> CodeAnchor:
    """Build a CodeAnchor for match_line (0-based) with ±ANCHOR_CONTEXT_LINES context."""
    start = max(0, match_line - ANCHOR_CONTEXT_LINES)
    end = min(len(lines) - 1, match_line + ANCHOR_CONTEXT_LINES)
    body_lines = lines[start : end + 1]
    body = "\n".join(body_lines)
    return CodeAnchor(
        file_path=file_path_str,
        line_start=start + 1,   # 1-based
        line_end=end + 1,       # 1-based
        matched_term=term,
        body=body,
    )


def fetch_code_anchors(
    dispatch_paths: list[str],
    instruction_text: str,
    *,
    max_chars: int = MAX_CODE_ANCHOR_CHARS,
    repo_root: Optional[Path] = None,
) -> list[CodeAnchor]:
    """Fetch file:line anchors for files in dispatch_paths matching instruction terms.

    Order:
    1. Cap dispatch_paths to MAX_FILES (recency by mtime tie-break)
    2. For each file, extract instruction-term matches via grep
    3. Cap MAX_ANCHORS_PER_FILE per file (best-match first by term-rarity)
    4. Slice ±ANCHOR_CONTEXT_LINES around each match
    5. Trim to fit max_chars total
    """
    if not dispatch_paths or not instruction_text:
        return []

    terms = extract_terms(instruction_text)
    if not terms:
        return []

    # Resolve repo_root
    if repo_root is None:
        try:
            from vnx_paths import resolve_paths
            paths = resolve_paths()
            repo_root = Path(paths["PROJECT_ROOT"])
        except Exception:
            repo_root = Path(".")

    # Build candidate file list, sorted by mtime descending (newer first)
    resolved_files: list[tuple[float, str, Path]] = []
    for dp in dispatch_paths:
        full_path = Path(repo_root) / dp
        if not _is_inside_repo_root(full_path, repo_root):
            continue  # silent skip — traversal escape or symlink outside root
        if not full_path.is_file():
            continue
        try:
            mtime = full_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        resolved_files.append((mtime, dp, full_path))

    resolved_files.sort(key=lambda t: t[0], reverse=True)
    resolved_files = resolved_files[:MAX_FILES]

    all_anchors: list[CodeAnchor] = []

    for _mtime, dp, full_path in resolved_files:
        lines = _read_lines(full_path)
        if lines is None:
            continue

        # Collect (term, match_line_0based) pairs across all terms for this file
        raw_matches: list[tuple[str, int]] = []
        for term in terms:
            for match_line in _find_term_matches(lines, term):
                raw_matches.append((term, match_line))

        if not raw_matches:
            continue

        # Deduplicate by line number (keep first term that matched each line)
        seen_lines: set[int] = set()
        unique_matches: list[tuple[str, int]] = []
        for term, ln in raw_matches:
            if ln not in seen_lines:
                seen_lines.add(ln)
                unique_matches.append((term, ln))

        # Cap per file
        unique_matches = unique_matches[:MAX_ANCHORS_PER_FILE]

        for term, match_line in unique_matches:
            anchor = _slice_anchor(lines, match_line, dp, term)
            all_anchors.append(anchor)

    # Trim to fit max_chars budget
    return _trim_to_budget(all_anchors, max_chars)


def _trim_to_budget(anchors: list[CodeAnchor], max_chars: int) -> list[CodeAnchor]:
    """Keep as many anchors as fit within max_chars when formatted."""
    if not anchors:
        return []
    trimmed: list[CodeAnchor] = []
    for anchor in anchors:
        candidate = trimmed + [anchor]
        if len(format_code_anchors_section(candidate)) <= max_chars:
            trimmed.append(anchor)
        else:
            break
    return trimmed


def format_code_anchors_section(anchors: list[CodeAnchor]) -> str:
    """Format as markdown section for dispatch instruction injection.

    Includes anti-anchoring instruction at top per Codex Q4 epistemic-failure-mode mitigation.
    """
    if not anchors:
        return ""

    lines = [
        "## CODE ANCHORS (current-state grounding)",
        "",
        "> **Anti-anchoring notice:** These code anchors are current-state evidence "
        "to ground decisions, not the only source of truth — re-read the file if "
        "anchors look incomplete or stale.",
        "",
    ]

    for anchor in anchors:
        header = f"### `{anchor.file_path}:{anchor.line_start}-{anchor.line_end}` (matched: `{anchor.matched_term}`)"
        lines.append(header)
        lines.append("")
        lines.append("```")
        lines.append(anchor.body)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
