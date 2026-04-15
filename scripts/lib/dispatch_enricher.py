#!/usr/bin/env python3
"""dispatch_enricher.py — Pre-dispatch enrichment pipeline for VNX.

Applies structured enrichment layers to dispatch instructions before delivery,
providing workers with additional context (repo map, future: memory, embeddings).

Usage:
    from dispatch_enricher import DispatchEnricher, extract_target_files
    enricher = DispatchEnricher()
    enriched = enricher.enrich(instruction, metadata)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

_SCRIPTS_LIB = Path(__file__).parent
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from repo_map import build_repo_map, format_repo_map  # noqa: E402

logger = logging.getLogger(__name__)

# Roles for which repo map adds no value (review/research dispatches)
_REVIEW_ROLES = frozenset({
    "reviewer", "architect", "code-reviewer",
    "security-engineer", "quality-engineer",
})

# Regex patterns for extracting .py file paths from dispatch text
_KEY_FILES_HEADING_RE = re.compile(
    r"^###\s+Key files to read first", re.MULTILINE | re.IGNORECASE
)
_BACKTICK_PY_RE = re.compile(r"`([^`]+\.py)`")
_BARE_PY_RE = re.compile(r"\b((?:[\w./\-]+/)?[\w\-]+\.py)\b")


def extract_target_files(instruction: str, metadata: Dict) -> List[str]:
    """Return list of .py paths referenced by this dispatch.

    Sources (in priority order):
    1. ``context_files`` key in metadata
    2. Items listed under ``### Key files to read first`` section
    3. Backtick-quoted .py paths anywhere in instruction (fallback)
    """
    files: List[str] = []
    seen: set = set()

    def _add(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            files.append(p)

    # Source 1: explicit metadata list
    for f in metadata.get("context_files", []):
        _add(str(f))

    # Source 2: items under "### Key files to read first" heading
    m = _KEY_FILES_HEADING_RE.search(instruction)
    if m:
        section = instruction[m.end():]
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):  # next section heading — stop
                break
            # backtick-quoted paths take precedence
            for bt in _BACKTICK_PY_RE.finditer(stripped):
                _add(bt.group(1))
            # bare paths (require a directory separator or scripts/ prefix)
            for bp in _BARE_PY_RE.finditer(stripped):
                candidate = bp.group(1)
                if "/" in candidate:
                    _add(candidate)

    # Source 3: fallback — backtick .py paths anywhere in instruction
    if not files:
        for bt in _BACKTICK_PY_RE.finditer(instruction):
            _add(bt.group(1))

    return [f for f in files if f.endswith(".py")]


class DispatchEnricher:
    """Pre-dispatch enrichment pipeline.

    Applies independent enrichment layers to a dispatch instruction before it
    is handed to the delivery adapter.  Layers are applied in order; each layer
    may be skipped based on dispatch metadata flags.

    Extension point for future layers (F56 memory, F57 Karpathy embeddings):
    add a new ``# Layer N`` block inside ``enrich()`` following the same
    guard/try/log pattern used by Layer 1.
    """

    def enrich(self, instruction: str, metadata: Dict) -> str:
        """Apply all enrichment layers to a dispatch instruction.

        Args:
            instruction: Raw dispatch instruction text.
            metadata:    Dispatch metadata dict with keys:
                           role, track, gate, no_repo_map (bool),
                           context_files (list[str]), project_root (str|Path).

        Returns:
            Enriched instruction string (original instruction preserved verbatim,
            enrichment sections appended).
        """
        enriched = instruction

        # ------------------------------------------------------------------
        # Layer 1: Repo map (structural code context)
        # ------------------------------------------------------------------
        if self._should_add_repo_map(metadata):
            target_files = extract_target_files(instruction, metadata)
            if target_files:
                project_root = Path(metadata.get("project_root") or Path.cwd())
                try:
                    repo_map = build_repo_map(target_files, project_root)
                    formatted = format_repo_map(repo_map)
                    enriched = enriched + f"\n\n{formatted}"
                    symbol_count = len(repo_map.symbols)
                    file_count = len({s.file_path for s in repo_map.symbols})
                    logger.info(
                        "Injected repo map: %d symbols from %d files",
                        symbol_count, file_count,
                    )
                except Exception as exc:
                    logger.warning("Repo map injection failed: %s — skipping", exc)
            else:
                logger.debug(
                    "Repo map skipped: no target .py files found in dispatch"
                )

        # ------------------------------------------------------------------
        # Layer 2: Intelligence injection (existing — handled by dispatch
        #           bundle builder, not duplicated here)
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # Layer 3: Similar dispatch outcomes (future — F56 memory)
        # ------------------------------------------------------------------
        # Placeholder: no-op until F56 implements dispatch memory retrieval.

        # ------------------------------------------------------------------
        # Layer 4: File affinity suggestions (from behavioral analysis)
        # ------------------------------------------------------------------
        target_files_for_enrichment = extract_target_files(instruction, metadata)
        if target_files_for_enrichment and not metadata.get("no_repo_map", False):
            try:
                affinity_section = self._build_file_affinity_section(target_files_for_enrichment)
                if affinity_section:
                    enriched = enriched + f"\n\n{affinity_section}"
            except Exception as exc:
                logger.warning("Layer 4 file affinity injection failed: %s — skipping", exc)

        # ------------------------------------------------------------------
        # Layer 5: Duration baseline (from behavioral analysis)
        # ------------------------------------------------------------------
        role = (metadata.get("role") or "").lower()
        if role and role not in _REVIEW_ROLES:
            try:
                duration_section = self._build_duration_baseline_section(role)
                if duration_section:
                    enriched = enriched + f"\n\n{duration_section}"
            except Exception as exc:
                logger.warning("Layer 5 duration baseline injection failed: %s — skipping", exc)

        # ------------------------------------------------------------------
        # Layer 6: Behavioral prevention rules
        # ------------------------------------------------------------------
        try:
            prevention_section = self._build_prevention_rules_section()
            if prevention_section:
                enriched = enriched + f"\n\n{prevention_section}"
        except Exception as exc:
            logger.warning("Layer 6 prevention rules injection failed: %s — skipping", exc)

        return enriched

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _intelligence_db_path() -> Optional[Path]:
        """Return quality_intelligence.db path or None if not found."""
        state_dir = os.environ.get("VNX_STATE_DIR", "")
        if state_dir:
            p = Path(state_dir) / "quality_intelligence.db"
            if p.exists():
                return p
        # Fallback: repo-relative
        here = Path(__file__).resolve()
        candidate = here.parent.parent.parent / ".vnx-data" / "state" / "quality_intelligence.db"
        return candidate if candidate.exists() else None

    def _build_file_affinity_section(self, target_files: List[str]) -> str:
        """Query success_patterns for file affinity pairs overlapping target_files.

        Returns formatted markdown section or empty string.
        """
        db_path = self._intelligence_db_path()
        if not db_path:
            return ""

        target_set = set(target_files)
        suggestions: List[str] = []

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT pattern_data FROM success_patterns
                WHERE category='behavior_analysis' AND title LIKE 'Files%co-occur%'
                ORDER BY confidence_score DESC
                LIMIT 100
                """
            ).fetchall()
            con.close()
        except Exception:
            return ""

        seen_suggestions: set = set()
        for row in rows:
            try:
                data = json.loads(row["pattern_data"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            files = data.get("files") or []
            if len(files) != 2:
                continue
            a, b = files[0], files[1]
            # If one file in the pair overlaps with target_files, suggest the other
            if a in target_set and b not in target_set and b not in seen_suggestions:
                suggestions.append(
                    f"- `{b}` (co-occurs with `{a}`, rate {data.get('co_occurrence', 0):.0%})"
                )
                seen_suggestions.add(b)
            elif b in target_set and a not in target_set and a not in seen_suggestions:
                suggestions.append(
                    f"- `{a}` (co-occurs with `{b}`, rate {data.get('co_occurrence', 0):.0%})"
                )
                seen_suggestions.add(a)

        if not suggestions:
            return ""

        return (
            "### Suggested Additional Context Files\n"
            "Files frequently modified together with your target files:\n"
            + "\n".join(suggestions[:10])
        )

    def _build_duration_baseline_section(self, role: str) -> str:
        """Query success_patterns for duration baseline matching this role.

        Returns formatted markdown section or empty string.
        """
        db_path = self._intelligence_db_path()
        if not db_path:
            return ""

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute(
                """
                SELECT pattern_data FROM success_patterns
                WHERE category='behavior_analysis'
                  AND title LIKE 'Expected duration:%'
                  AND pattern_data LIKE ?
                LIMIT 1
                """,
                (f"%{role}%",),
            ).fetchone()
            con.close()
        except Exception:
            return ""

        if not row:
            return ""

        try:
            data = json.loads(row["pattern_data"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return ""

        count = data.get("count", 0)
        avg_s = data.get("avg_seconds", 0)
        avg_min = round(avg_s / 60, 1) if avg_s else 0
        if not count or not avg_min:
            return ""

        return (
            f"### Expected Duration\n"
            f"Based on {count} previous dispatches: ~{avg_min} minutes for {role} tasks"
        )

    def _build_prevention_rules_section(self) -> str:
        """Query prevention_rules with source='behavior_analysis'.

        Returns formatted markdown section or empty string.
        """
        db_path = self._intelligence_db_path()
        if not db_path:
            return ""

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            # Check if source column exists
            col_names = {row[1] for row in con.execute("PRAGMA table_info(prevention_rules)").fetchall()}
            if "source" not in col_names:
                con.close()
                return ""
            rows = con.execute(
                """
                SELECT description, recommendation FROM prevention_rules
                WHERE source='behavior_analysis'
                ORDER BY triggered_count DESC
                LIMIT 10
                """
            ).fetchall()
            con.close()
        except Exception:
            return ""

        if not rows:
            return ""

        lines = []
        for row in rows:
            rec = (row["recommendation"] or "").strip()
            if rec:
                lines.append(f"- {rec}")

        if not lines:
            return ""

        return (
            "### Prevention Rules (from behavioral analysis)\n"
            + "\n".join(lines)
        )

    def _should_add_repo_map(self, metadata: Dict) -> bool:
        """Return True when this dispatch should receive a repo map.

        Returns False when:
        - metadata["no_repo_map"] is True  (explicit opt-out)
        - role is a review/research role   (no code context needed)
        - track is "C"                     (review/gate track)
        """
        if metadata.get("no_repo_map", False):
            return False

        role = (metadata.get("role") or "").lower()
        if role in _REVIEW_ROLES:
            return False

        track = (metadata.get("track") or "").upper()
        if track == "C":
            return False

        return True
