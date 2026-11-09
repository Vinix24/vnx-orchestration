"""The per-project, per-day store: immutable final files and numbered revisions.

Layout: ``<root>/<YYYY-MM-DD>.json`` (revision 1) and
``<root>/<YYYY-MM-DD>.r<N>.json`` (N >= 2). The highest revision wins and
nothing is ever deleted (decision 20).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from project_root import resolve_central_data_dir

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AlreadyFinal(Exception):
    """Raised when a `final` day file would be replaced or rewritten."""


def default_store_dir(project_id: str) -> Path:
    """Decision 12/13: under the central data dir, never under the cwd."""
    return resolve_central_data_dir(project_id) / "state" / "daily_metrics" / project_id


class DayStore:
    """One project's day files under ``root``; ``project_id`` names the owner."""

    def __init__(self, root: Path, project_id: str) -> None:
        self.root = Path(root)
        self.project_id = project_id

    @staticmethod
    def _validate_day(day: str) -> None:
        if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
            raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")

    def _existing_status(self, path: Path) -> str | None:
        """The stored status, ``None`` when absent; ``unreadable`` never reads as replaceable."""
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            return "unreadable"
        if not isinstance(doc, dict) or not isinstance(doc.get("status"), str):
            return "unreadable"
        return doc["status"]

    def _ensure_replaceable(self, path: Path) -> None:
        status = self._existing_status(path)
        if status is not None and status != "incomplete":
            raise AlreadyFinal(f"{path.name} is already final")

    def _atomic_write(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _payload(
        doc: Mapping[str, Any], revision: int, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        payload = dict(doc)
        payload["revision"] = revision
        payload["status"] = status
        if reason is not None:
            payload["reason"] = reason
        return payload

    def write_final(self, day: str, doc: Mapping[str, Any]) -> Path:
        self._validate_day(day)
        path = self.root / f"{day}.json"
        self._ensure_replaceable(path)
        self._atomic_write(path, self._payload(doc, 1, "final"))
        return path

    def write_incomplete(self, day: str, doc: Mapping[str, Any]) -> Path:
        self._validate_day(day)
        path = self.root / f"{day}.json"
        self._ensure_replaceable(path)
        self._atomic_write(path, self._payload(doc, 1, "incomplete"))
        return path

    def _next_revision(self, day: str) -> int:
        highest = 1
        for path in self.root.glob(f"{day}.r*.json"):
            match = re.fullmatch(rf"{re.escape(day)}\.r(\d+)\.json", path.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def write_revision(self, day: str, doc: Mapping[str, Any], reason: str) -> Path:
        self._validate_day(day)
        if not (reason or "").strip():
            raise ValueError("a revision requires a non-empty reason")
        revision = self._next_revision(day)
        path = self.root / f"{day}.r{revision}.json"
        status = doc.get("status") if isinstance(doc.get("status"), str) else "incomplete"
        self._atomic_write(path, self._payload(doc, revision, status, reason=reason))
        return path

    def load(self, day: str) -> dict[str, Any] | None:
        """The highest revision (the original counts as revision 1), or ``None``."""
        self._validate_day(day)
        chosen: Path | None = None
        chosen_revision = 0
        base = self.root / f"{day}.json"
        if base.exists():
            chosen, chosen_revision = base, 1
        for path in self.root.glob(f"{day}.r*.json"):
            match = re.fullmatch(rf"{re.escape(day)}\.r(\d+)\.json", path.name)
            if match and int(match.group(1)) > chosen_revision:
                chosen, chosen_revision = path, int(match.group(1))
        if chosen is None:
            return None
        with chosen.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None

    def is_final(self, day: str) -> bool:
        doc = self.load(day)
        return isinstance(doc, dict) and doc.get("status") == "final"

    def list_days(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.stem for path in self.root.glob("*.json") if _DAY_RE.fullmatch(path.stem)
        )
