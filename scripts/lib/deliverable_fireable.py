"""deliverable_fireable — the ready deliverables a T0 can fire together now (ADR-096 §18, B5).

Reuses the predecessor rule (``deliverable_predecessor_gate``), the path/overlap core
(``blast_radius``) and the live-work and open-PR sources (``blast_radius_sources``); it only
adds the selection: drop per blocking reason, then greedily pick a mutually overlap-free set
by track priority and age. Read-only: it fires nothing, claims nothing and writes no ledger line.
Every reason that cannot be established on evidence is ``unknown: <why>``, never "no overlap".
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import blast_radius as br
import blast_radius_gate as brg
import blast_radius_sources as brs
import deliverable_predecessor_gate as dpg

REASON_PREFIX_OPEN, REASON_PREFIX_OVERLAP, REASON_UNKNOWN_PREFIX = "predecessor_open: ", "overlap: ", "unknown: "
_LIVE, _OPEN_PR = "live_work", "open_pr"
_P_RANK = re.compile(r"p([0-9]+)")
_NAMED_RANK = {"high": 0, "medium": 1, "low": 2}  # `vnx track add --priority` uses these words


@dataclass(frozen=True)
class Entry:
    deliverable_id: str
    track: str
    reason: Optional[str] = None  # None for a fireable one; the drop reason otherwise


@dataclass(frozen=True)
class Selection:
    fireable: Tuple[Entry, ...]
    dropped: Tuple[Entry, ...]


@dataclass(frozen=True)
class _Candidate:
    deliverable_id: str
    track: str
    created_at: str
    priority: Optional[str]


def priority_rank(priority: Optional[str]) -> int:
    """P0..P9 first in order, then high/medium/low; anything else (incl. None) sorts last, never first."""
    text = (priority or "").strip().lower()
    match = _P_RANK.fullmatch(text)
    if match:
        return int(match.group(1))
    return _NAMED_RANK.get(text, 99)


def _ready(state_dir: Path, project_id: str, horizon: Optional[str]) -> List[_Candidate]:
    """The ``ready`` deliverables of one project (never another's) and their track's priority/horizon."""
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT d.dispatch_id, d.track, d.created_at, d.metadata_json, t.priority, t.horizon "
            "FROM dispatches d LEFT JOIN tracks t ON t.project_id = d.project_id AND t.track_id = d.track "
            "WHERE d.project_id = ? AND d.state = 'ready' ORDER BY d.created_at, d.dispatch_id",
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    out: List[_Candidate] = []
    for did, track, created, meta, priority, track_horizon in rows:
        try:
            parsed = json.loads(meta or "{}")
        except ValueError:
            continue  # a malformed blob is no evidence of a deliverable, and not a candidate to guess at
        if not (isinstance(parsed, dict) and parsed.get("deliverable") is True):
            continue
        if horizon is not None and track_horizon != horizon:
            continue
        out.append(_Candidate(did, track or "", created or "", priority))
    return out


def _normalised(raw: Sequence[str], repo_root: Path) -> Tuple[str, ...]:
    """A path that is a directory on disk keeps its trailing "/" (the door does the same)."""
    return tuple(p if p.endswith("/") else p + "/" if (repo_root / p).is_dir() else p for p in raw)


def _predecessor_reason(candidate_id: str, state_dir: Path, project_id: str) -> Optional[str]:
    first = next((f for f in dpg.assess(candidate_id, project_id, state_dir) if f.hard), None)
    if first is None:
        return None
    if first.reason == dpg.REASON_RETIRED:
        return dpg.REASON_RETIRED
    if first.reason == dpg.REASON_UNMERGED:
        return REASON_PREFIX_OPEN + first.predecessor
    return f"{REASON_UNKNOWN_PREFIX}{first.detail or first.reason}"


def _label(source: str, other: br.Subject) -> str:
    if source == _OPEN_PR and other.pr_number is not None:
        return f"#{other.pr_number}"
    return other.deliverable_id or other.branch or "?"


def _reasons_key(other: br.Subject) -> str:
    return other.deliverable_id or (f"pr-{other.pr_number}" if other.pr_number is not None else other.branch or "?")


def _source_reason(candidate: br.Subject, sources: Iterable[Tuple[str, brs.Sources]],
                   repo_root: Path) -> Optional[str]:
    """The first overlap, else the first unknown; an unreadable source never clears a candidate."""
    overlaps: List[str] = []
    unknowns: List[str] = []
    for name, src in sources:
        if src.unavailable is not None:  # the source names itself and why (live_work / gh ...)
            unknowns.append(f"{REASON_UNKNOWN_PREFIX}{src.unavailable}")
            continue
        for other in src.subjects:
            for result in br.check(candidate, (other,), repo_root):
                if result.outcome == br.OVERLAP and result.paths:
                    overlaps.append(f"{REASON_PREFIX_OVERLAP}{result.paths[0]} with {_label(name, other)}")
                elif result.outcome != br.CLEAR:  # UNKNOWN or an outcome this command does not know
                    unknowns.append(f"{REASON_UNKNOWN_PREFIX}"
                                    f"{src.reasons.get(_reasons_key(other)) or result.reason or result.outcome}")
    return (overlaps or unknowns or [None])[0]


def _scope_or_reason(c: _Candidate, state_dir: Path, repo_root: Path) -> Tuple[Optional[br.Scope], Optional[str]]:
    """The complete scope of the staged spec, or the reason it cannot be one."""
    raw, reason = brs.spec_scope(state_dir.parent, c.deliverable_id)
    if raw is None:
        return None, reason
    try:
        scope = br.scope_of(_normalised(raw, repo_root), repo_root, br.DEFAULT_GENERIC_DIRS)
    except br.ScopeInvalid as exc:
        return None, f"{REASON_UNKNOWN_PREFIX}{exc}"
    return (None, br.REASON_SCOPE_INCOMPLETE) if scope.incomplete else (scope, None)


def select(*, state_dir: "str | Path", project_id: str, repo_root: "str | Path",
           sources: Iterable[Tuple[str, brs.Sources]], horizon: Optional[str] = None) -> Selection:
    """Drop with a reason, then greedily pick a mutually overlap-free set (priority, then age).

    ``sources`` are ``(name, Sources)`` pairs; an unreadable source drops every candidate with
    ``unknown: <why>``. Raises ``sqlite3.Error`` when the store cannot be read: an unreadable
    store is not "nothing fireable".
    """
    state, root = Path(state_dir), Path(repo_root)
    sources = tuple(sources)
    dropped: List[Entry] = []
    passed: List[Tuple[_Candidate, br.Scope]] = []
    for c in _ready(state, project_id, horizon):
        reason = _predecessor_reason(c.deliverable_id, state, project_id)
        scope = None
        if reason is None:
            scope, reason = _scope_or_reason(c, state, root)
        if reason is None:
            reason = _source_reason(br.Subject(c.deliverable_id, scope.paths,
                                               br._branch_of(c.deliverable_id)), sources, root)
        if reason is not None:
            dropped.append(Entry(c.deliverable_id, c.track, reason))
        else:
            passed.append((c, scope))
    selected: List[Tuple[_Candidate, br.Scope]] = []
    for c, scope in sorted(passed, key=lambda p: (priority_rank(p[0].priority), p[0].created_at,
                                                  p[0].deliverable_id)):
        hit: Optional[Tuple[str, str]] = None
        for sel, sel_scope in selected:
            hits = br.overlaps(scope.paths, sel_scope.paths)
            if hits:
                hit = (hits[0], sel.deliverable_id)
                break
        if hit is not None:
            dropped.append(Entry(c.deliverable_id, c.track, f"{REASON_PREFIX_OVERLAP}{hit[0]} with {hit[1]}"))
        else:
            selected.append((c, scope))
    return Selection(tuple(Entry(c.deliverable_id, c.track) for c, _ in selected), tuple(dropped))


def default_sources(state_dir: "str | Path", project_id: str, repo_root: "str | Path",
                    gh: Optional[brs.Gh] = None) -> Tuple[Tuple[str, brs.Sources], ...]:
    """The live-work and open-PR sources the door compares against, for the fireable list."""
    state = Path(state_dir)
    return ((_LIVE, brs.live_subjects(state, project_id)),
            (_OPEN_PR, brs.open_pr_subjects(repo_root, project_id, brs.default_cache_path(state),
                                            gh or brg.gh)))
