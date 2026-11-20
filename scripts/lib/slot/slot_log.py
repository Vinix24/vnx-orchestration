"""The host slotlog: monthly NDJSON under ``<store>/slotlog/`` (ADR-062 "Slotlog", besluit 14; RS-8).

One line per event, never a token, path or command. Every line carries the full ADR field set
(``null`` where the event has none). A line that cannot be written whole raises ``SlotError``
(``store_invalid``): a lost audit line is never silent.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator

from .slot_admission import _FIELDS
from .slot_store import SlotError

SECRET_KEYS = ("token", "argv", "command", "path", "env")


def month_file(store_dir: Path, ts: float) -> Path:
    return Path(store_dir) / "slotlog" / (time.strftime("%Y-%m", time.gmtime(ts)) + ".ndjson")


def _line(event: Dict[str, Any]) -> bytes:
    for key in ("ts", "event"):
        if event.get(key) is None:
            raise SlotError("store_invalid", f"slotlog event without {key}")
    record = dict.fromkeys(_FIELDS)
    record.update({k: v for k, v in event.items() if k not in SECRET_KEYS})
    try:
        return (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SlotError("store_invalid", f"slotlog event not writable as JSON: {exc}") from exc


class SlotLog:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = Path(store_dir)

    def write(self, event: Dict[str, Any]) -> None:
        data = _line(event)
        path = month_file(self.store_dir, event["ts"])
        try:
            if not path.parent.is_dir():
                try:
                    os.mkdir(path.parent, 0o770)
                    os.chmod(path.parent, 0o2770)
                except FileExistsError:  # vnx-silent-except: another client made it first; open below decides
                    pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o660)
        except OSError as exc:
            raise SlotError("store_invalid", f"slotlog not writable: {exc}") from exc
        try:
            os.fchmod(fd, 0o660)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise SlotError("store_invalid", "slotlog write wrote nothing")
                view = view[written:]
        except OSError as exc:
            raise SlotError("store_invalid", f"slotlog write failed: {exc}") from exc
        finally:
            os.close(fd)


def read_events(store_dir: Path) -> Iterator[Dict[str, Any]]:
    """Every slotlog line, oldest month first; an unreadable or corrupt line raises ``SlotError``."""
    folder = Path(store_dir) / "slotlog"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.ndjson")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SlotError("store_invalid", f"slotlog unreadable: {exc}") from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise SlotError("store_invalid", f"slotlog line not JSON in {path.name}") from exc
            if not isinstance(record, dict):
                raise SlotError("store_invalid", f"slotlog line not an object in {path.name}")
            yield record
