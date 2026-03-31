#!/usr/bin/env python3
"""VNX Conversation Resume — deterministic resume with worktree validation.

PR-3 deliverable: one-click resume that opens the selected conversation in the
correct worktree context, blocking cross-worktree mistakes deterministically.

Design principles:
  - Resume command is built from session metadata, never from terminal injection.
  - Cross-worktree resume is blocked before any CLI invocation happens.
  - Errors are explicit and operator-readable.
  - The resume path uses `claude --resume <session_id>` with validated cwd.

Governance:
  RESUME-1: Resume target is session_id from conversation-index.db (SOT-2).
  RESUME-2: Worktree context validated via cwd path containment (LINK-1).
  RESUME-3: Cross-worktree resume blocked deterministically — never silently.
  RESUME-4: No terminal injection — returns command + validated cwd for operator.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

from conversation_read_model import (
    ConversationReadModel,
    ConversationSession,
    derive_worktree_root,
)


class ResumeError(Enum):
    """Categorized resume failure reasons."""
    SESSION_NOT_FOUND = "session_not_found"
    WORKTREE_MISMATCH = "worktree_mismatch"
    WORKTREE_MISSING = "worktree_missing"
    SESSION_NO_CWD = "session_no_cwd"
    CLAUDE_CLI_NOT_FOUND = "claude_cli_not_found"


@dataclass
class ResumeResult:
    """Result of a resume validation attempt."""
    ok: bool
    session_id: str
    command: Optional[str] = None
    cwd: Optional[str] = None
    worktree_root: Optional[str] = None
    error: Optional[ResumeError] = None
    message: str = ""

    def to_dict(self) -> dict:
        result = {
            "ok": self.ok,
            "session_id": self.session_id,
            "message": self.message,
        }
        if self.ok:
            result["command"] = self.command
            result["cwd"] = self.cwd
            result["worktree_root"] = self.worktree_root
        else:
            result["error"] = self.error.value if self.error else None
        return result


def _find_claude_cli() -> Optional[str]:
    """Locate the claude CLI binary."""
    return shutil.which("claude")


def validate_resume_context(
    session: ConversationSession,
    operator_worktree: Optional[str],
    worktree_roots: List[str],
) -> ResumeResult:
    """Validate that a session can be safely resumed from the operator's context.

    Args:
        session: The conversation session to resume.
        operator_worktree: The worktree root the operator is currently in
                          (None if not in a known worktree).
        worktree_roots: All known worktree root paths.

    Returns:
        ResumeResult with ok=True and command if safe, or ok=False with error details.
    """
    # RESUME-1: Session must have a cwd to resume into
    if not session.cwd:
        return ResumeResult(
            ok=False,
            session_id=session.session_id,
            error=ResumeError.SESSION_NO_CWD,
            message=(
                f"Session {session.session_id} has no working directory recorded. "
                "Cannot determine the correct context for resume."
            ),
        )

    # Derive the session's worktree root
    session_worktree = derive_worktree_root(session.cwd, worktree_roots)

    # RESUME-2: If session has a worktree, check it still exists on disk
    if session_worktree and not Path(session_worktree).is_dir():
        return ResumeResult(
            ok=False,
            session_id=session.session_id,
            error=ResumeError.WORKTREE_MISSING,
            message=(
                f"Session worktree '{session_worktree}' no longer exists on disk. "
                "The worktree may have been cleaned up. "
                "Recreate it with `vnx new-worktree` or choose a different session."
            ),
        )

    # Check that the session cwd itself exists (terminal dir may be gone)
    resume_cwd = session.cwd
    if not Path(resume_cwd).is_dir():
        # Fall back to the worktree root if the terminal subdir is gone
        if session_worktree and Path(session_worktree).is_dir():
            resume_cwd = session_worktree
        else:
            return ResumeResult(
                ok=False,
                session_id=session.session_id,
                error=ResumeError.WORKTREE_MISSING,
                message=(
                    f"Session directory '{session.cwd}' no longer exists. "
                    "Cannot resume without a valid working directory."
                ),
            )

    # RESUME-3: Cross-worktree blocking
    if operator_worktree is not None and session_worktree is not None:
        op_normalized = operator_worktree.rstrip("/")
        sess_normalized = session_worktree.rstrip("/")
        if op_normalized != sess_normalized:
            return ResumeResult(
                ok=False,
                session_id=session.session_id,
                error=ResumeError.WORKTREE_MISMATCH,
                message=(
                    f"Cross-worktree resume blocked. "
                    f"You are in '{op_normalized}' but the session belongs to "
                    f"'{sess_normalized}'. "
                    f"Switch to the correct worktree first, or use --force to override."
                ),
            )

    # RESUME-4: Build command without terminal injection
    claude_bin = _find_claude_cli()
    if not claude_bin:
        return ResumeResult(
            ok=False,
            session_id=session.session_id,
            error=ResumeError.CLAUDE_CLI_NOT_FOUND,
            message="Claude CLI binary not found in PATH. Install Claude Code first.",
        )

    command = f"claude --resume {session.session_id}"

    return ResumeResult(
        ok=True,
        session_id=session.session_id,
        command=command,
        cwd=resume_cwd,
        worktree_root=session_worktree,
        message=f"Ready to resume session in {resume_cwd}",
    )


def resume_conversation(
    session_id: str,
    model: ConversationReadModel,
    operator_cwd: str,
    worktree_roots: List[str],
    force: bool = False,
) -> ResumeResult:
    """Top-level resume action: look up session, validate, return command.

    Args:
        session_id: The session to resume (SOT-2 foreign key).
        model: The conversation read model instance.
        operator_cwd: The operator's current working directory.
        worktree_roots: Known worktree root paths.
        force: If True, skip cross-worktree validation (operator override).

    Returns:
        ResumeResult with validated command or error.
    """
    # Look up the session
    sessions = model.list_sessions(limit=500)
    session = next((s for s in sessions if s.session_id == session_id), None)

    if session is None:
        return ResumeResult(
            ok=False,
            session_id=session_id,
            error=ResumeError.SESSION_NOT_FOUND,
            message=f"Session '{session_id}' not found in conversation index.",
        )

    operator_worktree = derive_worktree_root(operator_cwd, worktree_roots)

    if force:
        # Skip cross-worktree check but still validate cwd/worktree existence
        operator_worktree = None

    return validate_resume_context(session, operator_worktree, worktree_roots)
