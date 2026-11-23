"""Operator-steering matching: repeat groups, memory-note lookup, cases, context (ADR-072, OB-3).

Pure functions over records handed in. Reads no transcript, calls no model, uses no network and
writes nothing. Return values carry ids, classes, counts and note names only: prompt text stays with
OB-1's ``show`` (decision 11). Decisions 5, 6, 7, 13 and 15.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from operator_steering_class import normalize_text

JACCARD_MIN = 0.5
SHORT_TEXT_WORDS = 3
NOTE_WINDOW = timedelta(minutes=45)
MIN_SHARED_WORDS = 2
MAX_CONTEXT_CALLS = 5
FIRST_LINE_MAX = 200
# Classes whose repeat needs three sessions before it is a case (decision 7).
CASE_MIN_SESSIONS = {"vraag": 3, "vrijgave": 3}
DEFAULT_CASE_MIN_SESSIONS = 2
NOTE_MARKERS = ("OPERATOR", "EIGEN MISSER")
MISSER_CLASSES = ("correctie", "handmatige_actie")
STOPWORDS = frozenset(
    "de het een en van ik je jij we wij is dat die dit te in op met voor niet maar ook nog als om aan "
    "bij naar of er zo dan nu wat hoe the an and to of it that this for on with not but be are was "
    "you your we do does at as by from".split())
NOTE_WORDS = frozenset({"operator", "eigen", "misser"})
DATE_TIME = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?:-(\d{4}))?(?![\d-])(?:\s*~?\s*(\d{1,2}):(\d{2})\s*Z)?")
DISPATCH_ID = re.compile(r"\b(?:dlv-[0-9a-f]{12}(?:-[a-z])?|\d{8}-\d{6}-[a-z0-9][a-z0-9-]*)")
PR_NUMBER = re.compile(r"(?:#|\bPR[ -]?|/pull/)(\d{1,6})\b")


@dataclass(frozen=True)
class SteeringPrompt:
    prompt_id: str
    session_id: str
    ts: datetime  # timezone-aware, from the transcript
    klasse: str
    text: str


@dataclass(frozen=True)
class MemoryNote:
    name: str
    description: str
    mtime: datetime


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    command: str = ""


@dataclass(frozen=True)
class AssistantTurn:
    tool_calls: Tuple[ToolCall, ...]
    text: str = ""


@dataclass(frozen=True)
class RepeatGroup:
    group_id: str
    klasse: str
    sessions: int
    prompt_ids: Tuple[str, ...]


@dataclass(frozen=True)
class NoteMatch:
    name: str
    status: str  # found | ambiguous | not_found
    prompt_ids: Tuple[str, ...] = ()
    eigen_misser: bool = False


@dataclass(frozen=True)
class ToolRef:
    call_id: str
    name: str
    first_line: str


@dataclass(frozen=True)
class PromptContext:
    prompt_id: str
    tool_calls: Tuple[ToolRef, ...]
    dispatch_ids: Tuple[str, ...]
    pr_numbers: Tuple[int, ...]


@dataclass(frozen=True)
class Case:
    klasse: str
    reason: str  # repeat | eigen_misser
    group_id: Optional[str]
    anchor_prompt_id: str
    sessions: int
    prompt_ids: Tuple[str, ...]
    notes: Tuple[str, ...]
    contexts: Tuple[PromptContext, ...]


def _utc(value: object) -> Optional[datetime]:
    """UTC form of an aware datetime; None for anything else (a naive time is no evidence)."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return None


def _usable(prompts: Sequence[SteeringPrompt]) -> List[SteeringPrompt]:
    return [p for p in prompts if p.prompt_id and p.session_id and isinstance(p.text, str)]


def _tokens(text: str) -> Tuple[str, ...]:
    words = [w for w in normalize_text(text).split() if not w.isdigit()]
    return tuple(w for w in words if w not in STOPWORDS) or tuple(words)


def _features(tokens: Tuple[str, ...]) -> frozenset:
    return frozenset(tokens) | frozenset(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))


def _related(a: Tuple[Tuple[str, ...], frozenset], b: Tuple[Tuple[str, ...], frozenset]) -> bool:
    (ta, fa), (tb, fb) = a, b
    if not ta or not tb:
        return False  # no words, no kinship
    if len(ta) < SHORT_TEXT_WORDS or len(tb) < SHORT_TEXT_WORDS:
        return ta == tb
    return len(fa & fb) / len(fa | fb) >= JACCARD_MIN


def _find(parent: Dict[int, int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _group_id(prompt_ids: Sequence[str]) -> str:
    return hashlib.sha256("|".join(sorted(prompt_ids)).encode("utf-8")).hexdigest()[:16]


def build_groups(prompts: Sequence[SteeringPrompt]) -> Tuple[RepeatGroup, ...]:
    """Connected components of related prompts that span at least two sessions (decisions 7 and 15)."""
    items = _usable(prompts)
    feats = [(t, _features(t)) for t in (_tokens(p.text) for p in items)]
    parent = {i: i for i in range(len(items))}
    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            b = items[j]
            if a.klasse == b.klasse and a.session_id != b.session_id and _related(feats[i], feats[j]):
                parent[_find(parent, i)] = _find(parent, j)
    components: Dict[int, List[SteeringPrompt]] = {}
    for i, p in enumerate(items):
        components.setdefault(_find(parent, i), []).append(p)
    groups = []
    for members in components.values():
        sessions = {p.session_id for p in members}
        if len(sessions) >= 2:
            ids = tuple(sorted(p.prompt_id for p in members))
            groups.append(RepeatGroup(_group_id(ids), members[0].klasse, len(sessions), ids))
    return tuple(sorted(groups, key=lambda g: g.group_id))


def _day(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _note_window(description: str, mtime: datetime) -> Optional[Tuple[datetime, datetime]]:
    """[start, end) in UTC for the first DD-MM[ ~HH:MMZ] in the description; None when unreadable."""
    hit = DATE_TIME.search(description)
    if hit is None or not isinstance(mtime, datetime):
        return None
    day, month = int(hit.group(1)), int(hit.group(2))
    year = int(hit.group(3)) if hit.group(3) else mtime.year
    found = _day(year, month, day)
    if found is not None and not hit.group(3) and found > mtime.date():
        found = _day(year - 1, month, day)  # decision 15: a date after the mtime is last year's
    if found is None:
        return None
    midnight = datetime(found.year, found.month, found.day, tzinfo=timezone.utc)
    if hit.group(4) is None:
        return midnight, midnight + timedelta(days=1)
    hour, minute = int(hit.group(4)), int(hit.group(5))
    if hour > 23 or minute > 59:
        return None
    end = midnight + timedelta(hours=hour, minutes=minute)
    return end - NOTE_WINDOW, end + timedelta(microseconds=1)


def _content_words(text: str) -> frozenset:
    return frozenset(w for w in normalize_text(text).split()
                     if len(w) >= 3 and not w.isdigit() and w not in STOPWORDS and w not in NOTE_WORDS)


def _is_steering_note(note: MemoryNote) -> bool:
    return isinstance(note.description, str) and any(m in note.description for m in NOTE_MARKERS)


def find_notes(notes: Sequence[MemoryNote], prompts: Sequence[SteeringPrompt]) -> Tuple[NoteMatch, ...]:
    """Per OPERATOR / EIGEN MISSER note: the prompts in its window that share two content words (decision 6)."""
    items = _usable(prompts)
    results = []
    for note in sorted((n for n in notes if _is_steering_note(n)), key=lambda n: n.name):
        window = _note_window(note.description, note.mtime)
        words = _content_words(note.description)
        hits = []
        for p in items:
            ts = _utc(p.ts)
            if window and ts and window[0] <= ts < window[1] and len(words & _content_words(p.text)) >= MIN_SHARED_WORDS:
                hits.append(p.prompt_id)
        status = "not_found" if not hits else "found" if len(hits) == 1 else "ambiguous"
        results.append(NoteMatch(note.name, status, tuple(sorted(hits)), "EIGEN MISSER" in note.description))
    return tuple(results)


def _scan_ids(*texts: str) -> Tuple[Tuple[str, ...], Tuple[int, ...]]:
    dispatches: set = set()
    prs: set = set()
    for text in texts:
        dispatches.update(DISPATCH_ID.findall(text))
        prs.update(int(n) for n in PR_NUMBER.findall(text))
    return tuple(sorted(dispatches)), tuple(sorted(prs))


def prompt_context(prompt: SteeringPrompt, turn: Optional[AssistantTurn]) -> PromptContext:
    """At most five tool calls nearest the prompt (name, and the first command line for Bash), plus named ids."""
    calls = tuple(turn.tool_calls)[-MAX_CONTEXT_CALLS:] if turn else ()
    refs = tuple(ToolRef(c.call_id, c.name, c.command.strip().splitlines()[0][:FIRST_LINE_MAX]
                         if c.name == "Bash" and c.command.strip() else "") for c in calls)
    scanned = [prompt.text] + ([turn.text] + [c.command for c in turn.tool_calls] if turn else [])
    dispatches, prs = _scan_ids(*scanned)
    return PromptContext(prompt.prompt_id, refs, dispatches, prs)


def _anchor(members: Sequence[SteeringPrompt]) -> str:
    dated = [(_utc(p.ts) or datetime.max.replace(tzinfo=timezone.utc), p.prompt_id) for p in members]
    return min(dated)[1]


def _make_case(klasse: str, reason: str, group_id: Optional[str], members: Sequence[SteeringPrompt],
               notes: Sequence[str], turns: Mapping[str, AssistantTurn]) -> Case:
    ordered = sorted(members, key=lambda p: p.prompt_id)
    return Case(klasse, reason, group_id, _anchor(members), len({p.session_id for p in members}),
                tuple(p.prompt_id for p in ordered), tuple(sorted(notes)),
                tuple(prompt_context(p, turns.get(p.prompt_id)) for p in ordered))


def build_cases(prompts: Sequence[SteeringPrompt], groups: Sequence[RepeatGroup], matches: Sequence[NoteMatch],
                turns: Mapping[str, AssistantTurn]) -> Tuple[Case, ...]:
    """Cases by the threshold of decision 7; everything below it stays a count."""
    by_id = {p.prompt_id: p for p in _usable(prompts)}
    misser = {m.name: m.prompt_ids[0] for m in matches
              if m.status == "found" and m.eigen_misser and m.prompt_ids[0] in by_id}
    cases: List[Case] = []
    covered: set = set()
    for g in groups:
        if g.sessions < CASE_MIN_SESSIONS.get(g.klasse, DEFAULT_CASE_MIN_SESSIONS):
            continue
        covered.update(g.prompt_ids)
        names = [n for n, pid in misser.items() if pid in g.prompt_ids and g.klasse in MISSER_CLASSES]
        cases.append(_make_case(g.klasse, "repeat", g.group_id, [by_id[i] for i in g.prompt_ids], names, turns))
    for pid in sorted(set(misser.values()) - covered):
        p = by_id[pid]
        if p.klasse in MISSER_CLASSES:
            names = [n for n, other in misser.items() if other == pid]
            cases.append(_make_case(p.klasse, "eigen_misser", None, [p], names, turns))
    return tuple(cases)

