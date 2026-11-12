"""The case ledger ``state/steering_cases.ndjson`` (ADR-072 decision 16, OB-6a): append-only, ids only, never text.

A case keeps its id when a later component holds its anchor or a member; an open ``rca_staged`` is not staged again;
five ``rca_staged`` per project per ISO week (UTC) is the cap. A bad line fails the read with its line number (ADR-063).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from operator_steering_report import _parse_ts, case_id  # the one case_id (OB-4b), imported, never copied

LEDGER_FILE, WEEKLY_CAP = "steering_cases.ndjson", 5
FIELDS = {"case_opened": ("klasse", "anker_prompt_id"), "case_members": ("prompt_ids",), "rca_staged": ("dispatch_id", "snapshot_sha256",
          "iso_week"), "rca_ingested": ("dispatch_id",), "rca_refused": ("dispatch_id", "reden")}
_FORMS = {"case_id": re.compile(r"[0-9a-f]{64}"), "snapshot_sha256": re.compile(r"[0-9a-f]{64}"), "iso_week": re.compile(r"\d{4}-W\d{2}")}


class LedgerUnreadable(ValueError):
    """A ledger line is not a valid next event of decision 16; the message names the file and the line."""


class StageRefused(Exception):
    """This case is not staged now; the message names why. Nothing was staged."""


@dataclass
class LedgerCase:
    case_id: str
    klasse: str
    anchor: str
    members: List[str] = field(default_factory=list)
    staged: List[dict] = field(default_factory=list)
    open_dispatch: str = ""  # an rca_staged without a later rca_ingested or rca_refused


def iso_week(now: datetime) -> str:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now moet een tijdzone-bewuste datetime zijn")
    year, week, _ = now.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def event(kind: str, cid: str, project_id: str, now: datetime, **fields: object) -> dict:
    return {"event": kind, "case_id": cid, "project_id": project_id, "ts": now.astimezone(timezone.utc).isoformat(), **fields}


def _ok(key: str, value: object) -> bool:
    if key == "prompt_ids":
        return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)
    return isinstance(value, str) and bool(value.strip()) and (key not in _FORMS or bool(_FORMS[key].fullmatch(value)))


def _step(ledger: Dict[str, LedgerCase], obj: object, project_id: Optional[str]) -> None:
    """Validate one event and apply it in ledger order; ``ValueError`` names why it is invalid or breaks the sequence."""
    if not isinstance(obj, dict) or obj.get("event") not in FIELDS:
        raise ValueError("geen object met een bekend event")
    kind, cid = obj["event"], obj.get("case_id")
    if not (isinstance(project_id, str) and project_id and obj.get("project_id") == project_id):
        raise ValueError(f"project_id {obj.get('project_id')!r} is niet {project_id!r}")
    if not (_parse_ts(obj.get("ts")) and all(_ok(k, obj.get(k)) for k in ("case_id", *FIELDS[kind]))):
        raise ValueError(f"case_id, ts of {', '.join(FIELDS[kind])} ontbreekt of heeft de verkeerde vorm")
    case = ledger.get(cid)
    if kind == "case_opened":
        if case is not None or cid != case_id(project_id, obj["klasse"], obj["anker_prompt_id"]):
            raise ValueError("case is al geopend of case_id past niet bij project, klasse en anker")
        ledger[cid] = LedgerCase(cid, obj["klasse"], obj["anker_prompt_id"])
    elif case is None:
        raise ValueError("case is nooit geopend")
    elif kind == "case_members":
        case.members.extend(p for p in dict.fromkeys(obj["prompt_ids"]) if p not in case.members)
    elif kind == "rca_staged":
        if case.open_dispatch:
            raise ValueError(f"rca_staged terwijl {case.open_dispatch} nog open staat")
        case.staged.append(obj)
        case.open_dispatch = obj["dispatch_id"]
    elif obj["dispatch_id"] != case.open_dispatch:
        raise ValueError(f"{kind} voor {obj['dispatch_id']}, open staat {case.open_dispatch or 'niets'}")
    else:
        case.open_dispatch = ""


def read_ledger(state_dir: Path, project_id: str) -> Dict[str, LedgerCase]:
    """Cases in ledger order; no file is no cases. A line that is not a valid next event raises ``LedgerUnreadable``."""
    path, ledger = Path(state_dir) / LEDGER_FILE, {}
    try:
        lines = path.read_text(encoding="utf-8").split("\n") if path.exists() else []
    except (OSError, ValueError) as exc:
        raise LedgerUnreadable(f"{path}: onleesbaar ({exc})") from exc
    for number, line in ((n, ln) for n, ln in enumerate(lines, 1) if ln.strip()):
        try:
            _step(ledger, json.loads(line), project_id)
        except ValueError as exc:  # not JSON (JSONDecodeError) or not a valid next event
            raise LedgerUnreadable(f"{path}:{number}: {exc}") from exc
    return ledger


def cases(report: dict, ledger: Dict[str, LedgerCase], now: datetime) -> Tuple[List[Tuple[dict, LedgerCase]], List[dict]]:
    """Fold ``report["gevallen"]`` into ``ledger`` in place: (each entry with its case, the events to book)."""
    project_id, pairs, events = report.get("project_id"), [], []
    for entry in report.get("gevallen") if isinstance(report.get("gevallen"), list) else [None]:
        klasse, anchor, ids = (entry.get(k) for k in ("klasse", "anker_prompt_id", "prompt_ids")) if isinstance(entry, dict) else (None,) * 3
        if not (_ok("klasse", klasse) and _ok("prompt_ids", ids) and anchor in ids):
            raise ValueError("rapport-geval zonder geldige klasse, anker of prompt-id's")
        same = [c for c in ledger.values() if c.klasse == klasse]  # the anchor's case first, then a member's
        case = next((c for c in same if c.anchor in ids), None) or next((c for c in same if set(c.members) & set(ids)), None)
        cid = case.case_id if case else case_id(project_id, klasse, anchor)
        new = [p for p in ids if all(p not in c.members for c in ledger.values())]
        new_events = ([] if case else [event("case_opened", cid, project_id, now, klasse=klasse, anker_prompt_id=anchor)]) + (
            [event("case_members", cid, project_id, now, prompt_ids=new)] if new else [])
        for obj in new_events:
            _step(ledger, obj, project_id)
        events += new_events
        pairs.append((entry, ledger[cid]))
    return pairs, events


@contextmanager
def _locked(state_dir: Path) -> Iterator[Callable[[List[dict]], None]]:
    """The ledger itself, opened with O_APPEND and flocked; closing releases the lock. Yields an fsyncing append."""
    fd = os.open(Path(state_dir) / LEDGER_FILE, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    def append(events: List[dict]) -> None:  # after a full read: a last line without newline was parsed whole
        size = os.fstat(fd).st_size
        data = (b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b"") + "".join(
            json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
        if events and os.write(fd, data) != len(data):
            raise OSError(f"{LEDGER_FILE}: korte schrijfactie, het grootboek is niet volledig bijgewerkt")
        os.fsync(fd)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield append
    finally:
        os.close(fd)


def book(state_dir: Path, project_id: str, events: List[dict]) -> None:
    """Validate ``events`` as the next lines of the ledger and append them, under its lock."""
    with _locked(state_dir) as append:
        ledger = read_ledger(state_dir, project_id)
        for obj in events:
            _step(ledger, obj, project_id)
        append(events)


def stage_case(state_dir: Path, report: dict, wanted: str, now: datetime,
               stage_fn: Callable[[LedgerCase, dict, str, str], None], dry_run: bool = False) -> Tuple[LedgerCase, str]:
    """Fold, refuse an open RCA or a full week, ``stage_fn(case, entry, dispatch_id, snapshot)``, book ``rca_staged``: one
    lock around all of it, so a repeated or parallel run never stages a sixth. ``dry_run`` takes no lock and books nothing."""
    project_id, week = report.get("project_id"), iso_week(now)
    with nullcontext(lambda _events: None) if dry_run else _locked(state_dir) as append:
        ledger = read_ledger(state_dir, project_id)
        pairs, events = cases(report, ledger, now)
        append(events)
        case = next((c for e, c in pairs if wanted in (c.case_id, e.get("case_id"))), None)
        entries = [e for e, c in pairs if c is case]
        if case is None or len(entries) != 1:
            raise StageRefused(f"case {wanted} staat {len(entries)} keer in het venster van het rapport, niet één keer")
        if case.open_dispatch:
            raise StageRefused(f"case {case.case_id} heeft al een open RCA {case.open_dispatch}: gestaged, nog niet ingenomen of geweigerd")
        used = sum(1 for c in ledger.values() for s in c.staged if s["iso_week"] == week)
        if used >= WEEKLY_CAP:
            raise StageRefused(f"weekplafond bereikt: {used} van {WEEKLY_CAP} rca_staged in {week}")
        dispatch_id = f"steering-rca-{case.case_id[:12]}-{week.lower().replace('-', '')}-{len(case.staged) + 1}"
        snapshot = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        stage_fn(case, entries[0], dispatch_id, snapshot)
        staged = event("rca_staged", case.case_id, project_id, now, dispatch_id=dispatch_id, snapshot_sha256=snapshot, iso_week=week)
        _step(ledger, staged, project_id)
        append([staged])
    return case, dispatch_id
