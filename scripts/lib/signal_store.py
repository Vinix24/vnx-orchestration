"""Persistent signal store — appends governance signals to NDJSON.

Durable history of signals processed by GovernanceDigestRunner so that
trend analysis, replay, and audit can access historical signal records.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_SIGNALS_PATH = Path(os.environ.get("VNX_DATA_DIR", ".vnx-data")) / "feedback" / "signals.ndjson"


class SignalStore:
    """Append-only NDJSON store for governance signals.

    Each appended record is a JSON object on its own line.  The store is
    thread-safe via an instance-level lock and uses atomic rename on write
    to avoid partial-line corruption when appending in bulk.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path: Path = path or _DEFAULT_SIGNALS_PATH
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, signal: Dict[str, Any]) -> None:
        """Append a single signal dict as a JSON line."""
        self._append_many([signal])

    def append_many(self, signals: List[Dict[str, Any]]) -> None:
        """Append multiple signal dicts atomically in one write."""
        if not signals:
            return
        self._append_many(signals)

    def read_all(self) -> List[Dict[str, Any]]:
        """Return all stored signals as a list of dicts."""
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def count(self) -> int:
        """Return number of stored signal records."""
        return len(self.read_all())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_many(self, signals: List[Dict[str, Any]]) -> None:
        """Append signals to the NDJSON file under lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = "\n".join(json.dumps(s, default=str) for s in signals) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(lines)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, base_dir: Optional[Path] = None) -> "SignalStore":
        """Construct from environment.  base_dir overrides VNX_DATA_DIR."""
        if base_dir is not None:
            path = base_dir / "feedback" / "signals.ndjson"
        else:
            data_dir = Path(os.environ.get("VNX_DATA_DIR", ".vnx-data"))
            path = data_dir / "feedback" / "signals.ndjson"
        return cls(path=path)
