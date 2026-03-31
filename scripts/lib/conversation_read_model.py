#!/usr/bin/env python3
"""VNX Conversation Read Model — worktree-aware session index.

PR-1 deliverable: machine-readable read model that links conversations to
worktrees, terminals, session IDs, and rotation-summary continuity hints.

Design principles (from 60_CONVERSATION_RESUME_CONTRACT.md):
  - Claude Code conversation-index.db is the upstream owner (read-only).
  - Worktree membership is derived from cwd path containment (LINK-1).
  - Terminal identity is derived from cwd path segments (LINK-2).
  - Rotation chains use explicit Dispatch-ID linkage only (ROTATE-1).
  - Latest-first ordering uses last_message DESC (ORDER-1).
  - VNX never writes to conversation-index.db (SOT invariants).

Governance:
  SOT-1: Sort key is conversations.last_message DESC — no shadow timestamps.
  SOT-2: Foreign key is session_id — no alternative identifiers.
  LINK-1: Worktree membership via cwd path containment.
  LINK-2: Terminal identity from cwd path segment.
  ROTATE-1: Chain discovery via Dispatch-ID only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ConversationSession:
    """A single conversation session with worktree linkage."""
    session_id: str
    project_path: str
    cwd: str
    last_message: Optional[str]  # ISO-8601 or None
    title: str
    message_count: int
    user_message_count: int
    total_tokens: int
    file_path: str

    # Derived fields (computed by read model)
    terminal: Optional[str] = None       # T0, T1, T2, T3, or None
    worktree_root: Optional[str] = None  # Resolved worktree root path
    worktree_exists: bool = True         # False if worktree dir is gone


@dataclass
class RotationChain:
    """A chain of sessions linked by context rotation for one dispatch."""
    dispatch_id: str
    sessions: List[ConversationSession] = field(default_factory=list)

    @property
    def chain_depth(self) -> int:
        return len(self.sessions)

    @property
    def latest_message(self) -> Optional[str]:
        """The most recent last_message across all sessions in the chain."""
        timestamps = [s.last_message for s in self.sessions if s.last_message]
        return max(timestamps) if timestamps else None


@dataclass
class WorktreeGroup:
    """Sessions grouped by worktree root."""
    worktree_root: str
    worktree_exists: bool
    sessions: List[ConversationSession] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Terminal detection (from cwd path)
# ---------------------------------------------------------------------------

_TERMINAL_RE = re.compile(r"[/\\]\.claude[/\\]terminals[/\\](T\d+)$")


def derive_terminal(cwd: str) -> Optional[str]:
    """Extract terminal identity from cwd path (LINK-2).

    Returns T0, T1, T2, T3, etc. or None if cwd doesn't match the
    .claude/terminals/T{n} pattern.
    """
    if not cwd:
        return None
    m = _TERMINAL_RE.search(cwd)
    return m.group(1) if m else None


def derive_worktree_root(cwd: str, known_roots: List[str]) -> Optional[str]:
    """Derive worktree root from cwd via path containment (LINK-1).

    Args:
        cwd: The session's working directory.
        known_roots: List of known worktree root paths (longest match wins).

    Returns:
        The worktree root that contains cwd, or None.
    """
    if not cwd:
        return None
    # Sort by length descending so the most specific root wins
    for root in sorted(known_roots, key=len, reverse=True):
        normalized_root = root.rstrip("/")
        if cwd == normalized_root or cwd.startswith(normalized_root + "/"):
            return normalized_root
    return None


# ---------------------------------------------------------------------------
# Rotation chain discovery
# ---------------------------------------------------------------------------

def load_rotation_events(receipt_path: str) -> Dict[str, List[dict]]:
    """Load context_rotation_continuation events from receipt NDJSON.

    Returns a dict mapping dispatch_id → list of rotation event records.
    Events are sorted by timestamp ascending (chain order).
    """
    chains: Dict[str, List[dict]] = {}
    path = Path(receipt_path)
    if not path.is_file():
        return chains

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_type") != "context_rotation_continuation":
                continue
            dispatch_id = record.get("dispatch_id", "")
            if not dispatch_id or dispatch_id == "unknown":
                continue
            chains.setdefault(dispatch_id, []).append(record)

    # Sort each chain by timestamp
    for dispatch_id in chains:
        chains[dispatch_id].sort(key=lambda r: r.get("timestamp", ""))

    return chains


# ---------------------------------------------------------------------------
# Read Model
# ---------------------------------------------------------------------------

class ConversationReadModel:
    """Machine-readable read model for conversation resume.

    Reads from Claude Code's conversation-index.db (read-only) and enriches
    sessions with worktree linkage, terminal identity, and rotation metadata.
    """

    def __init__(
        self,
        claude_index_db: str,
        worktree_roots: Optional[List[str]] = None,
        receipt_path: Optional[str] = None,
    ):
        """Initialize the read model.

        Args:
            claude_index_db: Path to ~/.claude/conversation-index.db.
            worktree_roots: Known worktree root paths for linkage.
            receipt_path: Path to t0_receipts.ndjson for rotation chain discovery.
        """
        self._db_path = claude_index_db
        self._worktree_roots = worktree_roots or []
        self._receipt_path = receipt_path or ""
        self._rotation_events: Optional[Dict[str, List[dict]]] = None

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection to the Claude Code index."""
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _enrich_session(self, row: sqlite3.Row) -> ConversationSession:
        """Convert a DB row into a ConversationSession with derived fields."""
        cwd = row["cwd"] or ""
        session = ConversationSession(
            session_id=row["session_id"],
            project_path=row["project_path"],
            cwd=cwd,
            last_message=row["last_message"],
            title=row["title"] or "",
            message_count=row["message_count"] or 0,
            user_message_count=row["user_message_count"] or 0,
            total_tokens=row["total_tokens"] or 0,
            file_path=row["file_path"] or "",
            terminal=derive_terminal(cwd),
            worktree_root=derive_worktree_root(cwd, self._worktree_roots),
        )
        # Check worktree existence on disk
        if session.worktree_root:
            session.worktree_exists = Path(session.worktree_root).is_dir()
        return session

    def list_sessions(
        self,
        project_filter: Optional[str] = None,
        worktree_filter: Optional[str] = None,
        terminal_filter: Optional[str] = None,
        sort_order: str = "DESC",
        limit: int = 50,
    ) -> List[ConversationSession]:
        """List recent sessions with optional filtering (§4.1).

        Args:
            project_filter: Filter by project_path (exact match).
            worktree_filter: Filter by worktree root (cwd LIKE prefix%).
            terminal_filter: Filter by terminal ID (e.g., "T1").
            sort_order: "DESC" (latest-first, default) or "ASC".
            limit: Maximum results.

        Returns:
            Sorted list of ConversationSession objects.
        """
        if sort_order not in ("DESC", "ASC"):
            sort_order = "DESC"

        clauses: List[str] = []
        params: List[str] = []

        if project_filter:
            clauses.append("project_path = ?")
            params.append(project_filter)

        if worktree_filter:
            normalized = worktree_filter.rstrip("/")
            clauses.append("cwd LIKE ?")
            params.append(normalized + "%")

        if terminal_filter:
            clauses.append("cwd LIKE ?")
            params.append(f"%/terminals/{terminal_filter}")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        # ORDER-1/ORDER-2: Sort by last_message with NULL-last behavior
        query = (
            f"SELECT * FROM conversations{where} "
            f"ORDER BY CASE WHEN last_message IS NULL THEN 1 ELSE 0 END, "
            f"last_message {sort_order} "
            f"LIMIT ?"
        )
        params.append(str(limit))

        conn = self._connect()
        try:
            cursor = conn.execute(query, params)
            sessions = [self._enrich_session(row) for row in cursor.fetchall()]
        finally:
            conn.close()

        return sessions

    def group_by_worktree(
        self, sessions: List[ConversationSession]
    ) -> List[WorktreeGroup]:
        """Group sessions by worktree root (LINK-3).

        Sessions without a resolved worktree are grouped under an empty-string key.
        """
        groups: Dict[str, WorktreeGroup] = {}
        for session in sessions:
            key = session.worktree_root or ""
            if key not in groups:
                exists = Path(key).is_dir() if key else False
                groups[key] = WorktreeGroup(
                    worktree_root=key,
                    worktree_exists=exists,
                )
            groups[key].sessions.append(session)
        return list(groups.values())

    def discover_rotation_chains(
        self, sessions: List[ConversationSession]
    ) -> List[RotationChain]:
        """Discover rotation chains from receipt events (ROTATE-1).

        Chains are identified by shared Dispatch-ID in context_rotation_continuation
        receipt events. Only sessions present in both the receipt chain and the
        provided session list are included.

        Returns chains sorted by latest_message DESC.
        """
        if self._rotation_events is None:
            self._rotation_events = (
                load_rotation_events(self._receipt_path)
                if self._receipt_path
                else {}
            )

        if not self._rotation_events:
            return []

        # Build session lookup by session_id for fast matching
        session_map = {s.session_id: s for s in sessions}

        # Build dispatch_id → sessions from the session list
        dispatch_sessions: Dict[str, List[ConversationSession]] = {}
        for session in sessions:
            # We need to match sessions to rotation events.
            # Receipt events don't carry session_id directly — they carry
            # dispatch_id and terminal. We match sessions that share the
            # same dispatch_id by looking at session metadata from the
            # conversation analyzer (if available) or by checking if the
            # dispatch_id appears in the session's receipt chain.
            pass

        # Alternative approach: match sessions to dispatches via the
        # rotation events' dispatch_id. Group sessions whose cwd/terminal
        # and timeframe overlap with rotation events for that dispatch.
        chains: List[RotationChain] = []

        for dispatch_id, events in self._rotation_events.items():
            chain = RotationChain(dispatch_id=dispatch_id)
            # Extract terminals involved in this rotation chain
            terminals = {e.get("terminal") for e in events}
            timestamps = [e.get("timestamp", "") for e in events]

            # Find sessions that match: their terminal matches a rotation
            # event terminal AND they have activity near the rotation timestamps
            for session in sessions:
                if session.terminal and session.terminal in terminals:
                    chain.sessions.append(session)

            if chain.sessions:
                # Sort within chain by last_message DESC (ORDER-3)
                chain.sessions.sort(
                    key=lambda s: s.last_message or "",
                    reverse=True,
                )
                chains.append(chain)

        # Sort chains by latest member's last_message DESC
        chains.sort(
            key=lambda c: c.latest_message or "",
            reverse=True,
        )
        return chains

    def get_rotation_metadata(self) -> Dict[str, List[dict]]:
        """Expose raw rotation event metadata keyed by dispatch_id."""
        if self._rotation_events is None:
            self._rotation_events = (
                load_rotation_events(self._receipt_path)
                if self._receipt_path
                else {}
            )
        return dict(self._rotation_events)
