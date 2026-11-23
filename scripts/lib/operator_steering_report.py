"""Operator-steering report: the ``steering_report/1`` document as a library (ADR-072, OB-4a).

Composes OB-1 (bound, contained reading), OB-2 (classes) and OB-3 (groups, notes, cases). Ids, classes,
counts and note names only (decision 11): no prompt text, note description or tool command. No model, no network.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, TextIO, Tuple

from atomic_io import atomic_write_json
from operator_steering_class import STEERING_CLASSES, UNCLASSIFIED, primary_class
from operator_steering_match import (
    MAX_CONTEXT_CALLS, AssistantTurn, Case, MemoryNote, SteeringPrompt, ToolCall,
    build_cases, build_groups, find_notes)
from operator_steering_source import (
    Refused, SourceResult, _prompt_id, classify_line, load_binding, open_contained, read_sources)
from vnx_paths import project_id_from_state_dir

SCHEMA = "steering_report/1"
REPORT_FILE = "steering_report.json"
LOW_COVERAGE_PERCENT = 30
TURN_TEXT_KEEP, MAX_NOTE_BYTES = 8000, 1_000_000
MEMORY_INDEX, NOTE_KEYS = "MEMORY.md", ("name", "description")


@dataclass
class _Collected:
    prompts: List[SteeringPrompt] = field(default_factory=list)
    turns: Dict[str, AssistantTurn] = field(default_factory=dict)
    excluded: Counter = field(default_factory=Counter)


def _parse_ts(value: object) -> Optional[datetime]:
    """UTC time of an ISO timestamp that names its zone; None for anything else (never guessed)."""
    text = value.strip() if isinstance(value, str) else ""
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(text)  # "" raises ValueError: no stamp, no prompt
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


class _TurnBuilder:
    """Assistant lines since the last operator prompt of one file: tool_use blocks and text, never a result."""

    def __init__(self) -> None:
        self.calls: Deque[ToolCall] = deque(maxlen=MAX_CONTEXT_CALLS)
        self.text = ""

    def feed(self, obj: dict) -> None:
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                self.text = (self.text + "\n" + block["text"])[-TURN_TEXT_KEEP:]
            elif block.get("type") == "tool_use":
                self._call(block)

    def _call(self, block: dict) -> None:
        call_id, name = block.get("id"), block.get("name")
        if not (isinstance(call_id, str) and call_id and isinstance(name, str) and name):
            return
        tool_input = block.get("input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        self.calls.append(ToolCall(call_id, name, command if name == "Bash" and isinstance(command, str) else ""))

    def take(self) -> AssistantTurn:
        turn = AssistantTurn(tuple(self.calls), self.text)
        self.calls.clear()
        self.text = ""
        return turn


def _consumer(collected: _Collected) -> Callable[[TextIO, str], None]:
    seen: set = set()

    def consume(handle: TextIO, name: str) -> None:
        builder = _TurnBuilder()
        for raw in handle:
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            reason, text = classify_line(obj)
            if reason == "not_user" and isinstance(obj, dict) and obj.get("type") == "assistant":
                builder.feed(obj)
            uuid = obj.get("uuid") if isinstance(obj, dict) else None
            if reason is None and not (isinstance(uuid, str) and uuid):
                reason = "no_uuid"  # same rule as read_prompts: identity absent, never a prompt
            if reason:
                collected.excluded[reason] += 1
                continue
            turn = builder.take()
            session = str(obj.get("sessionId") or Path(name).stem)
            stamp = _parse_ts(obj.get("timestamp"))
            prompt_id = _prompt_id(session, uuid)
            if stamp is None:
                collected.excluded["no_timestamp"] += 1
            elif prompt_id in seen:
                collected.excluded["duplicate"] += 1
            else:
                seen.add(prompt_id)
                collected.prompts.append(SteeringPrompt(prompt_id, session, stamp, primary_class(text), text))
                collected.turns[prompt_id] = turn

    return consume


def _frontmatter(body: str) -> Dict[str, str]:
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: Dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key in NOTE_KEYS:
            found[key] = value.strip().strip("\"'")
    return found


def _read_note(path: Path, root: Path) -> MemoryNote:
    with open_contained(path, root) as handle:
        mtime = datetime.fromtimestamp(os.fstat(handle.fileno()).st_mtime, tz=timezone.utc)
        meta = _frontmatter(handle.read(MAX_NOTE_BYTES))
    return MemoryNote(meta.get("name") or path.stem, meta.get("description") or "", mtime)


def read_notes(binding: dict) -> Tuple[SourceResult, List[MemoryNote]]:
    """Notes of the bound memory dir; one note outside the dir makes the source `error` and yields no notes."""
    memory = Path(binding["memory_dir"])
    if not memory.is_dir():
        return SourceResult("absent", "memory-map ontbreekt"), []
    if memory.resolve() != memory:
        return SourceResult("error", "containment geweigerd", {"outside_bound_dir": 1}, [memory.name]), []
    refused: Counter = Counter()
    refused_files: List[str] = []
    notes: List[MemoryNote] = []
    for path in sorted(memory.glob("*.md")):
        if path.name == MEMORY_INDEX:
            continue
        try:
            notes.append(_read_note(path, memory))
        except Refused as exc:
            refused[exc.args[0]] += 1
            refused_files.append(path.name)
    if refused:
        return SourceResult("error", "containment geweigerd", dict(refused), refused_files), []
    return SourceResult("ok"), notes


def _source_block(r: SourceResult) -> dict:
    return {"state": r.state, "reason": r.reason, "refused": dict(r.refused), "refused_files": list(r.refused_files)}


def _aware_utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} moet een tijdzone-bewuste datetime zijn")
    return value.astimezone(timezone.utc)


def _coverage(counts: Dict[str, int]) -> Tuple[Optional[float], str]:
    """(dekking, status); no prompts means unknown (None), never 0 or 1."""
    total = sum(counts.values())
    if not total:
        return None, "ok"
    unclassified = counts.get(UNCLASSIFIED, 0)
    status = "low_coverage" if unclassified * 100 > LOW_COVERAGE_PERCENT * total else "ok"
    return round(1 - unclassified / total, 4), status


def case_id(project_id: Optional[str], klasse: str, anchor_prompt_id: str) -> str:
    """ADR-072 decision 16: sha256 (hex) of ``project_id | klasse | anchor_prompt_id``; the one definition."""
    return hashlib.sha256(f"{project_id or ''}|{klasse}|{anchor_prompt_id}".encode("utf-8")).hexdigest()


def _case_json(case: Case, project_id: Optional[str]) -> dict:
    return {"case_id": case_id(project_id, case.klasse, case.anchor_prompt_id), "klasse": case.klasse, "reden": case.reason, "group_id": case.group_id, "sessies": case.sessions,
            "anker_prompt_id": case.anchor_prompt_id, "prompt_ids": list(case.prompt_ids), "notities": list(case.notes),
            "context": [{"prompt_id": c.prompt_id, "tool_calls": [{"id": t.call_id, "naam": t.name} for t in c.tool_calls],
                         "dispatch_ids": list(c.dispatch_ids), "pr_nummers": list(c.pr_numbers)} for c in case.contexts]}


def _analysis(prompts: List[SteeringPrompt], notes: List[MemoryNote], turns: Dict[str, AssistantTurn],
              project_id: Optional[str]) -> dict:
    counts = {c.name: 0 for c in STEERING_CLASSES}
    for p in prompts:
        counts[p.klasse] = counts.get(p.klasse, 0) + 1
    groups = build_groups(prompts)
    matches = find_notes(notes, prompts)
    cases = build_cases(prompts, groups, matches, turns)
    dekking, status = _coverage(counts)
    return {
        "tellingen": counts, "dekking": dekking, "status": status,
        "herhalingsgroepen": [{"group_id": g.group_id, "klasse": g.klasse, "sessies": g.sessions,
                               "prompt_ids": list(g.prompt_ids)} for g in groups],
        "notities": [{"naam": m.name, "status": m.status, "prompt_ids": list(m.prompt_ids),
                      "eigen_misser": m.eigen_misser} for m in matches],
        "gevallen": [_case_json(c, project_id) for c in cases]}


def build_report(state_dir: Path, projects_root: Path, since: datetime, until: datetime,
                 now: Optional[datetime] = None) -> dict:
    """The ``steering_report/1`` dict for ``since <= timestamp < until``; a source that is not `ok` is the ``status``."""
    start, end = _aware_utc(since, "since"), _aware_utc(until, "until")
    stamp = _aware_utc(now, "now") if now is not None else datetime.now(timezone.utc)
    collected = _Collected()
    result = read_sources(state_dir, projects_root, _consumer(collected))
    binding = load_binding(state_dir, projects_root)[0] if result.state != "unbound" else None
    memory, notes = read_notes(binding) if binding else (SourceResult("unbound", result.reason), [])
    usable = result.state == "ok"
    prompts = [p for p in collected.prompts if start <= p.ts < end] if usable else []
    project_id = project_id_from_state_dir(state_dir) or None
    analysis = _analysis(prompts, notes if usable else [], collected.turns, project_id)
    report = {"schema": SCHEMA, "project_id": project_id,
              "venster": {"since": start.isoformat(), "until": end.isoformat()}, "gegenereerd": stamp.isoformat(),
              "bronstaat": {"transcripts": _source_block(result), "memory": _source_block(memory)},
              "uitgesloten": dict(sorted(collected.excluded.items())) if usable else {},
              "suppressed_rejected": 0, **analysis}
    if not usable:
        report["status"] = result.state
    return report


def write_report(state_dir: Path, report: dict) -> Path:
    """Atomically replace ``steering_report.json`` in the state dir; nothing else is written."""
    path = Path(state_dir) / REPORT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, report)
    return path
