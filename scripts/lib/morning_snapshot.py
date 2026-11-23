"""Morning round day snapshot, decision status derived from the inbox ledger, event ledger (ADR-095)."""
from __future__ import annotations

import json, os, re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import operator_queue as oq
from operator_queue import QueueUnreadable, _locked, read_state

SNAPSHOT_SCHEMA = "morning_snapshot/1"
EVENTS_FILENAME = "morning_round.ndjson"
SNAPSHOT_DIR = "morning_round"
EVENT_KINDS = frozenset({"pause_start", "pause_end", "refused", "override"})
PAUSE_END_REASONS = frozenset({"decided", "deadline"})
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


class SnapshotUnreadable(ValueError):
    """The snapshot file exists but is not valid."""


@dataclass(frozen=True)
class DayStatus:
    project_id: str
    day: str
    snapshot: str  # "ok" | "missing" | "unreadable"
    ledger: str  # "ok" | "unknown" | "not_read"
    points: Dict[str, str] = field(default_factory=dict)  # point_id -> decided | open | unknown
    reason: Optional[str] = None

    @property
    def all_decided(self) -> Optional[bool]:
        """None without a readable snapshot; False on an unreadable ledger (ADR-095 d4, ADR-063).

        An empty snapshot with a readable ledger is True: there is nothing to decide.
        """
        if self.snapshot != "ok":
            return None  # no snapshot is unknown, never True (ADR-063)
        if self.ledger != "ok":
            return False  # an unreadable ledger is not decided, also with zero points
        return all(status == "decided" for status in self.points.values())


def _check_day(day: Any) -> str:
    if not isinstance(day, str) or not _DAY.fullmatch(day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")


def _snapshot_path(state_dir: Path, day: str) -> Path:
    return Path(state_dir) / SNAPSHOT_DIR / f"{_check_day(day)}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:  # vnx-atomic-write: temp file, fsync, replace
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_snapshot(project_id: str, day: str, *, state_dir: Path, now: str) -> Path:
    """Write the day snapshot once; later calls return the path. QueueUnreadable if the inbox cannot be read."""
    path = _snapshot_path(state_dir, day)
    with _locked(state_dir):
        if path.exists():
            return path
        state = read_state(project_id, state_dir=state_dir)
        if state["status"] != "ok":
            raise QueueUnreadable(f"inbox ledger unreadable: {state['reason']}", state.get("line") or 0)
        points: List[Dict[str, Any]] = [
            {"point_id": p["point_id"], "bron": p["bron"], "ref": p["ref"], "vraag": p["vraag"],
             "advies": "", "klasse": ""}
            for p in sorted(state["points"].values(), key=lambda p: (p["asked_at"], p["point_id"]))
            if p["state"] == "open"]
        _atomic_write(path, json.dumps({"schema": SNAPSHOT_SCHEMA, "project_id": project_id, "day": day,
                                        "taken_at": now, "points": points}, indent=2, sort_keys=True))
    return path


def _load_snapshot(path: Path, project_id: str, day: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotUnreadable(f"{path}: {type(exc).__name__}") from exc
    ok = isinstance(data, dict) and (data.get("schema"), data.get("project_id"), data.get("day")) == (
        SNAPSHOT_SCHEMA, project_id, day)
    points = data.get("points") if ok else None
    if not isinstance(points, list) or not all(isinstance(p, dict) and p.get("point_id") for p in points):
        raise SnapshotUnreadable(f"{path}: not a valid {SNAPSHOT_SCHEMA} of this project and day")
    return points


def decision_status(project_id: str, day: str, *, state_dir: Path) -> DayStatus:
    path = _snapshot_path(state_dir, day)
    if not path.exists():
        return DayStatus(project_id, day, "missing", "not_read", {}, "no snapshot for this day")
    try:
        points = _load_snapshot(path, project_id, day)
    except SnapshotUnreadable as exc:
        return DayStatus(project_id, day, "unreadable", "not_read", {}, str(exc))
    state = read_state(project_id, state_dir=state_dir)
    ledger_ok = state["status"] == "ok"
    result: Dict[str, str] = {}
    for point in points:
        entry, pid = state["points"].get(point["point_id"]) if ledger_ok else None, point["point_id"]
        if entry is None:
            result[pid] = "unknown"  # missing from the ledger or ledger unreadable
        else:
            result[pid] = "open" if entry["state"] == "open" else ("decided" if entry["state"] in oq.OUTCOMES else "unknown")
    return DayStatus(project_id, day, "ok", "ok" if ledger_ok else "unknown", result,
                     None if ledger_ok else state["reason"])


def _next_seq(path: Path, project_id: str, day: str, kinds: Optional[set] = None) -> int:
    """Next seq for (project, day); the kinds already written for it are added to ``kinds`` when given."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 1
    seq = 0
    for number, raw in enumerate(text.split("\n"), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
            if not isinstance(line["seq"], int) or isinstance(line["seq"], bool):
                raise ValueError("seq is no integer")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"{path}:{number}: event ledger line invalid ({exc!r})") from exc
        if line.get("project_id") == project_id and line.get("day") == day:
            seq = max(seq, line["seq"])
            if kinds is not None:
                kinds.add(line.get("kind"))
    return seq + 1


def append_event(project_id: str, day: str, kind: str, fields: Mapping[str, Any], *, state_dir: Path,
                 now: str, once: bool = False) -> Optional[Dict[str, Any]]:
    """Append one event line keyed (project_id, day, seq). Raises ValueError on an unknown kind or reason.
    ``once``: under the same lock, write nothing and return None when (project, day) already has this kind."""
    _check_day(day)
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    fields = dict(fields)
    if kind == "pause_end" and fields.get("reason") not in PAUSE_END_REASONS:
        raise ValueError(f"pause_end needs reason in {sorted(PAUSE_END_REASONS)}, got {fields.get('reason')!r}")
    if {"project_id", "day", "seq", "kind", "ts"} & fields.keys():
        raise ValueError("project_id, day, seq, kind and ts are set by append_event")
    path = Path(state_dir) / EVENTS_FILENAME
    with _locked(state_dir):
        seen: set = set()
        seq = _next_seq(path, project_id, day, seen)
        if once and kind in seen:
            return None
        line = {**fields, "project_id": project_id, "day": day, "seq": seq, "kind": kind, "ts": now}
        payload = (json.dumps(line, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with open(path, "ab+") as handle:  # vnx-atomic-write: append-only line, fsynced
            end = os.fstat(handle.fileno()).st_size
            if end and os.pread(handle.fileno(), 1, end - 1) != b"\n":
                payload = b"\n" + payload
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return line
