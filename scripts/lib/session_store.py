#!/usr/bin/env python3
"""SessionStore — persistent per-terminal session ID storage for subprocess dispatch.

Persists the Claude session_id captured from ``--output-format stream-json`` init
events so subsequent dispatches can pass ``--resume <session_id>`` and skip the
cold-start latency of a fresh session.

Storage: .vnx-data/state/subprocess_sessions.json (JSON, atomic write).
Activation: gated by ``VNX_SESSION_RESUME=1`` — callers must check before loading.

BILLING SAFETY: No Anthropic SDK. Pure file I/O.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SESSIONS_FILENAME = "subprocess_sessions.json"
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state_dir() -> Path:
    """Resolve VNX state dir from environment, falling back to project-relative path."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return Path(__file__).resolve().parent.parent.parent / ".vnx-data" / "state"


class SessionStore:
    """File-backed store for per-terminal subprocess session IDs.

    All methods are non-fatal: exceptions are caught, logged, and a safe
    fallback value is returned.  This ensures session persistence failures
    never interrupt dispatch delivery.

    Thread safety: load/save use atomic rename writes (write-to-tmp then rename)
    so concurrent readers always see a complete file.
    """

    def __init__(self, state_dir: "Path | str | None" = None) -> None:
        self._state_dir: Optional[Path] = Path(state_dir) if state_dir else None

    def _path(self) -> Path:
        base = self._state_dir if self._state_dir is not None else _default_state_dir()
        return base / SESSIONS_FILENAME

    def _read_raw(self) -> Dict[str, Any]:
        """Read and parse the sessions file. Returns empty dict on any error."""
        try:
            path = self._path()
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return data
        except Exception as exc:
            logger.debug("SessionStore._read_raw: %s", exc)
            return {}

    def _write_raw(self, data: Dict[str, Any]) -> None:
        """Atomically write sessions data to disk."""
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("SessionStore._write_raw: failed to write %s: %s", path, exc)

    def load(self, terminal_id: str) -> Optional[str]:
        """Return the persisted session_id for terminal_id, or None.

        Returns None when:
        - No sessions file exists yet
        - The terminal has no stored session
        - The file is corrupt or unreadable
        """
        try:
            data = self._read_raw()
            terminals = data.get("terminals", {})
            entry = terminals.get(terminal_id)
            if not isinstance(entry, dict):
                return None
            session_id = entry.get("session_id", "")
            return session_id if session_id else None
        except Exception as exc:
            logger.debug("SessionStore.load(%s): %s", terminal_id, exc)
            return None

    def save(self, terminal_id: str, session_id: str, dispatch_id: str = "") -> None:
        """Persist session_id for terminal_id.

        Overwrites any prior entry for this terminal.  Never raises.
        """
        if not session_id:
            return
        try:
            data = self._read_raw()
            data.setdefault("schema_version", SCHEMA_VERSION)
            terminals = data.setdefault("terminals", {})
            terminals[terminal_id] = {
                "session_id": session_id,
                "dispatch_id": dispatch_id,
                "updated_at": _now_iso(),
            }
            self._write_raw(data)
            logger.info(
                "SessionStore.save: %s session_id=%s dispatch=%s",
                terminal_id, session_id, dispatch_id,
            )
        except Exception as exc:
            logger.warning("SessionStore.save(%s): %s", terminal_id, exc)

    def clear(self, terminal_id: str) -> None:
        """Remove persisted session for terminal_id.  Never raises."""
        try:
            data = self._read_raw()
            terminals = data.get("terminals", {})
            if terminal_id in terminals:
                del terminals[terminal_id]
                self._write_raw(data)
                logger.info("SessionStore.clear: removed %s", terminal_id)
        except Exception as exc:
            logger.debug("SessionStore.clear(%s): %s", terminal_id, exc)

    def all_sessions(self) -> Dict[str, str]:
        """Return {terminal_id: session_id} for all stored terminals."""
        try:
            data = self._read_raw()
            terminals = data.get("terminals", {})
            result: Dict[str, str] = {}
            for tid, entry in terminals.items():
                if isinstance(entry, dict) and entry.get("session_id"):
                    result[tid] = entry["session_id"]
            return result
        except Exception as exc:
            logger.debug("SessionStore.all_sessions: %s", exc)
            return {}
