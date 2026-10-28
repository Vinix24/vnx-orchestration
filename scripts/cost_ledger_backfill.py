#!/usr/bin/env python3
"""cost_ledger_backfill.py — the harness session transcripts, read as runs (K6b, ADR-049).

A run on a harness lane (``deepseek-harness``, ``glm-harness``) leaves a Claude Code
session transcript under ``<lane>/projects/<encoded-cwd>/<session-id>.jsonl``; that
transcript is the complete per-message record of what the run spent. This module turns
those files into one :class:`TranscriptRun` per session. It only reads the transcripts
and has no CLI: :func:`plan_runs` decides, per transcript and without writing, what a
backfill would book — a correction on a live line, a charge beside it, or nothing.
K6b-3 adds the writer and the command.

**What makes a line claimable (K6b-2, OI-2419).** A live line is corrected only on
evidence that it measures *this* transcript:

1. it names this run's ``transcript_identity`` — a verified match;
2. it names no identity and its tokens equal the run's exactly — the token evidence a
   legacy line can still offer;
3. it names no identity and its window overlaps the run's window.

A legacy line that matches on neither (2) nor (3) is left alone and the run books its
own charge: a line whose window is unrelated to this session is not this session's
spend, and superseding it would remove another session's run from every total. A line
that names *another* identity is never claimed at all.

**A skip needs the identity.** A line is skipped only when it carries this run's
identity and says the same thing (provider, model, role, tokens, segments). A matched
line without an identity is always written as a correction that stamps the identity on
it, even when every other field is equal — that write is how the match is persisted,
so a later ``--since`` that leaves this transcript out can no longer claim the line.

**Which field carries the dispatch id (measured 2026-10-27).** The top-level ``cwd``
of the transcript entries. Of the 1,169 deepseek transcripts then on this machine,
506 ran in ``<repo>/.vnx-data/worktrees/dispatch-<dispatch-id>``, 551 with ``cwd``
``/`` (analyzer and classifier calls) and 111 in the repo root. The rule: the last
path component of ``cwd`` with the ``dispatch-`` prefix stripped is the dispatch id;
no other ``cwd`` has one. A transcript without a dispatch id uses its session id (the
file stem) as its reference, and is counted, never dropped. Session metadata exists
only for live sessions, so ``cwd`` is the durable carrier; the first user message is
never read.

**One scan (OI-2419).** The roots may overlap: a parent directory and one of its lane
dirs, the same lane given twice, or two stores holding one session. Transcripts are
deduplicated on their resolved path and on ``<provider>:<session id>``, so one
session is one run however many roots reach it.

**Provider and privacy.** The provider is the lane the transcript lives in (its
directory name), never the transcript's self-reported name. Only token counts and
timestamps leave a transcript: no message text is copied into a run or printed.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    import cost_ledger
    import harness_run_cost
    import provider_costs
    import token_harvest
    from provider_spawns import deepseek_harness_spawn, glm_harness_spawn
    from providers import provider_registry
except Exception as exc:
    raise SystemExit(f"Failed to load the cost ledger modules: {exc}")

logger = logging.getLogger(__name__)

DEFAULT_SINCE = "2026-10-02"
BACKFILL_PRODUCER = "backfill"
UNKNOWN_ROLE = "unknown:no_dispatch_row"
UNKNOWN_ROLE_NO_PURPOSE = "unknown:no_ledger_role"
CORRECTION_REASON = "backfill: the harness transcript's tokens replace the live line"
LANES = ("deepseek-harness", "glm-harness")
DISPATCH_PREFIX = "dispatch-"

# Every worker role the fabric dispatches books as the ledger's ``builder`` purpose.
_ROLE_PURPOSE: Dict[str, str] = {
    **{role: "builder" for role in (
        "builder", "backend-developer", "frontend-developer", "quality-engineer",
        "system-architect", "backend-architect", "security-engineer", "database-engineer",
        "research-analyst", "technical-writer", "root-cause-analyst", "implementer",
        "test-engineer", "debugger", "intelligence-engineer")},
    **{role: "review" for role in (
        "review-gate", "code-reviewer", "quality-reviewer", "security-reviewer",
        "architecture-reviewer")},
    "plan-reviewer": "plan_gate",
    "deliberation-panelist": "panel",
    "panelist": "panel",
    "tiebreaker": "tiebreaker",
}
_LANE_DEFAULT = {
    "deepseek-harness": deepseek_harness_spawn.resolve_harness_model,
    "glm-harness": glm_harness_spawn.resolve_harness_model,
}
# What a live line must already say, identity included, for a run to be skipped.
_BOOKED_FIELDS = ("provider", "model", "role", "tokens", "segments", "transcript_identity")


@dataclass(frozen=True)
class DispatchRow:
    """What the dispatch's ``dispatch-spec.json`` says about the run."""

    role: Optional[str]
    model: Optional[str]


@dataclass(frozen=True)
class TranscriptRun:
    """One session transcript read as one run, deduplicated per ``message.id``."""

    provider: str
    session_id: str
    path: str
    dispatch_id: Optional[str]
    messages: Sequence[token_harvest.MessageUsage]
    reported_model: Optional[str]
    occurred_from: str
    occurred_to: str

    @property
    def ref(self) -> str:
        """The run's reference: the dispatch id, or the session id when there is none."""
        return self.dispatch_id or self.session_id

    @property
    def identity(self) -> str:
        """The run's durable identity: its lane and its session id, never its transcript path.

        The shape is K6a's (``harness_run_cost.transcript_identity``), the one the
        booked line stores, so the reader and the writer cannot drift apart.
        """
        return harness_run_cost.transcript_identity(self.provider, self.session_id)


@dataclass(frozen=True)
class PlannedRun:
    """One transcript's outcome: the line to write, or a skip with the line it already matches."""

    provider: str
    run: Optional[cost_ledger.RunCost]
    live: Optional[Mapping[str, Any]]
    skipped: bool


# ---------------------------------------------------------------------------
# Reading the transcripts
# ---------------------------------------------------------------------------


def lane_roots(values: Iterable[str]) -> List[Path]:
    """The lane dirs for the given roots: a lane dir itself, or a parent of lane dirs.

    A lane reached twice — given itself and through its parent, or through two
    parents that resolve to one directory — is scanned once. The list is sorted
    on the resolved path so the same arguments always scan in the same order.
    """
    seen: Dict[str, Path] = {}
    for raw in values:
        path = Path(raw).expanduser()
        if path.name in LANES or (path / "projects").is_dir():
            found = [path]
        else:
            found = sorted(item.parent for item in path.glob("*/projects") if item.is_dir())
        for root in found:
            try:
                key = str(Path(root).resolve())
            except OSError as exc:
                logger.warning("cost_ledger_backfill: cannot resolve %s: %s", root, exc)
                continue
            seen.setdefault(key, Path(root))
    return [seen[key] for key in sorted(seen)]


def dispatch_id_from_cwd(cwd: Optional[str]) -> Optional[str]:
    """The dispatch id a transcript's ``cwd`` names, or ``None`` when it names no worktree."""
    name = Path(cwd).name if cwd else ""
    if not name.startswith(DISPATCH_PREFIX) or len(name) == len(DISPATCH_PREFIX):
        return None
    return name[len(DISPATCH_PREFIX):]


def _parse(value: object) -> Optional[datetime]:
    """An ISO-8601 timestamp with a timezone, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def transcript_meta(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """The transcript's ``cwd`` and its dominant reported model, from one read."""
    cwd: Optional[str] = None
    models: collections.Counter = collections.Counter()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if cwd is None and isinstance(entry.get("cwd"), str) and entry["cwd"]:
                    cwd = entry["cwd"]
                message = entry.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    model = message.get("model")
                    if isinstance(model, str) and model and model != "<synthetic>":
                        models[model] += 1
    except OSError as exc:
        logger.warning("cost_ledger_backfill: cannot read %s: %s", path, exc)
    return cwd, (models.most_common(1)[0][0] if models else None)


def run_window(messages: Sequence[token_harvest.MessageUsage], path: Path) -> Tuple[datetime, datetime]:
    """First and last readable message timestamp; the file's mtime when no message carries one."""
    stamps = [moment for message in messages if (moment := _parse(message.timestamp)) is not None]
    if stamps:
        return min(stamps), max(stamps)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return mtime, mtime


def scan_lane(root: Path, since: date) -> List[TranscriptRun]:
    """Every transcript of one lane whose run ends on or after ``since``.

    ``path`` is the transcript's resolved path, so two roots that reach one file
    (a parent and its lane dir, a symlinked lane) name the same transcript.
    """
    found: List[TranscriptRun] = []
    for path in sorted((Path(root) / "projects").glob("*/*.jsonl")):
        messages = token_harvest.read_transcript_messages(path)
        if not messages:
            continue
        try:
            started, ended = run_window(messages, path)
        except OSError as exc:
            logger.warning("cost_ledger_backfill: cannot stat %s: %s", path, exc)
            continue
        if ended.date() < since:
            continue
        cwd, reported = transcript_meta(path)
        try:
            resolved = str(path.resolve())
        except OSError as exc:
            logger.warning("cost_ledger_backfill: cannot resolve %s: %s", path, exc)
            resolved = str(path.absolute())
        found.append(TranscriptRun(
            provider=Path(root).name, session_id=path.stem, path=resolved,
            dispatch_id=dispatch_id_from_cwd(cwd),
            messages=messages, reported_model=reported,
            occurred_from=_iso(started), occurred_to=_iso(ended),
        ))
    return found


def dedupe_transcripts(runs: Iterable[TranscriptRun]) -> List[TranscriptRun]:
    """One entry per resolved transcript path and per ``<provider>:<session id>``, in stable order.

    Overlapping scan roots, a copy of a session in a second store and a lane
    reached through a symlink all name one run; reading it twice would book two
    charges for one session's spend. The first entry in (provider, session id,
    path) order wins, so the same roots always yield the same run.
    """
    kept: List[TranscriptRun] = []
    paths: set = set()
    identities: set = set()
    for run in sorted(runs, key=lambda item: (item.provider, item.session_id, item.path)):
        if run.path in paths or run.identity in identities:
            continue
        paths.add(run.path)
        identities.add(run.identity)
        kept.append(run)
    return kept


def load_dispatch_rows(dispatch_dir: Path) -> Dict[str, DispatchRow]:
    """``dispatch_id -> DispatchRow`` from every ``dispatch-spec.json`` under the dispatch dir."""
    rows: Dict[str, DispatchRow] = {}
    for path in sorted(Path(dispatch_dir).glob("**/dispatch-spec.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("cost_ledger_backfill: unreadable dispatch spec %s: %s", path, exc)
            continue
        dispatch_id = spec.get("dispatch_id") if isinstance(spec, dict) else None
        if isinstance(dispatch_id, str) and dispatch_id:
            rows[dispatch_id] = DispatchRow(spec.get("role"), spec.get("model"))
    return rows


def scan(roots: Sequence[Path], since: date) -> List[TranscriptRun]:
    """Every run the roots hold from ``since`` on, once each, in a deterministic order.

    ``roots`` may name a lane directory or a parent of lane dirs (see
    :func:`lane_roots`). The transcripts are deduplicated before the list is
    ordered on the run's end timestamp and then on its path, so the same tree
    always yields the same list.
    """
    found: List[TranscriptRun] = []
    for root in lane_roots(str(path) for path in roots):
        found.extend(scan_lane(root, since))
    return sorted(dedupe_transcripts(found), key=lambda run: (run.occurred_to, run.path))


# ---------------------------------------------------------------------------
# The live ledger
# ---------------------------------------------------------------------------


def ledger_role(row: Optional[DispatchRow], live: Optional[Mapping[str, Any]]) -> str:
    """The ledger purpose role: the dispatch row's when it maps, the live line's, else unknown."""
    mapped = _ROLE_PURPOSE.get((row.role if row is not None else "") or "")
    if mapped:
        return mapped
    live_role = (live or {}).get("role")
    if live_role in cost_ledger.ROLES:
        return live_role
    return UNKNOWN_ROLE if row is None else UNKNOWN_ROLE_NO_PURPOSE


def lane_model(provider: str, *candidates: Optional[str]) -> Optional[str]:
    """The first candidate the lane's registry section knows, as its registry model key."""
    registry_key = provider_costs.registry_key_for_provider(provider)
    if registry_key is None:
        return None
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            return provider_registry.resolve_model_identity(registry_key, candidate)
        except provider_registry.RegistryLookupError:
            continue
    return None


def load_live_runs(ledger_path: Path, project_id: str) -> Dict[str, List[Mapping[str, Any]]]:
    """``ref -> effective live records`` of this project, oldest window first. Reads, writes nothing."""
    index: Dict[str, List[Mapping[str, Any]]] = collections.defaultdict(list)
    for record in cost_ledger.aggregate_runs(ledger_path).runs.values():
        try:
            project, _producer, ref, _attempt = cost_ledger.split_run_id(record.get("run_id"))
        except cost_ledger.InvalidRecord:
            continue
        if project == project_id:
            index[ref].append(record)
    for records in index.values():
        records.sort(key=lambda record: (record.get("occurred_from") or "", record.get("run_id") or ""))
    return dict(index)


def _record_identity(record: Mapping[str, Any]) -> Optional[str]:
    """The transcript identity a live line names, or ``None`` for a line written before it was stored.

    A line whose identity is missing, empty or not a string names no session: it
    is a legacy line, and :func:`_claimable` may claim it only on the token or
    window evidence of :func:`_match_pass`. Anything else would let one session
    supersede another's booked run.
    """
    value = record.get(cost_ledger.TRANSCRIPT_IDENTITY)
    return value if isinstance(value, str) and value.strip() else None


def _candidates(records: Sequence[Mapping[str, Any]], provider: str,
                claimed: set) -> List[Mapping[str, Any]]:
    """The unclaimed live lines of this lane among a reference's records."""
    return [record for record in records
            if record.get("provider") == provider and record.get("record_id") not in claimed]


def _claimable(records: Sequence[Mapping[str, Any]], run: TranscriptRun,
               claimed: set) -> List[Mapping[str, Any]]:
    """The unclaimed lines of this lane a transcript may take: its own identity, or a legacy line.

    A line that names another session is never a candidate: that session's run
    is booked elsewhere (or its transcript is not in this scan), and taking its
    line would remove its spend from the ledger instead of adding this run.
    """
    return [record for record in _candidates(records, run.provider, claimed)
            if _record_identity(record) in (None, run.identity)]


def _distance(record: Mapping[str, Any], run: TranscriptRun) -> float:
    """How far apart two runs' windows are, in seconds; an unreadable window is never the nearest.

    Only ever breaks a tie between lines that already have evidence, so an
    unreadable timestamp can neither create a match nor win one: it sorts last.
    """
    live_from, live_to = _parse(record.get("occurred_from")), _parse(record.get("occurred_to"))
    run_from, run_to = _parse(run.occurred_from), _parse(run.occurred_to)
    if None in (live_from, live_to, run_from, run_to):
        return math.inf
    return abs((live_to - run_to).total_seconds()) + abs((live_from - run_from).total_seconds())


def _overlaps(record: Mapping[str, Any], run: TranscriptRun) -> bool:
    """True only when the line's window and the run's window intersect, both read in full.

    An unreadable window on either side is no evidence, so it claims nothing and
    the line stays as it is (fail-closed, OI-2419).
    """
    live_from, live_to = _parse(record.get("occurred_from")), _parse(record.get("occurred_to"))
    run_from, run_to = _parse(run.occurred_from), _parse(run.occurred_to)
    if None in (live_from, live_to, run_from, run_to):
        return False
    return live_from <= run_to and run_from <= live_to


def _pick(records: Sequence[Mapping[str, Any]], run: TranscriptRun) -> Mapping[str, Any]:
    """The nearest of some equally evidenced lines, the ``run_id`` breaking an exact tie."""
    return min(records, key=lambda record: (_distance(record, run), str(record.get("run_id") or "")))


def _already_booked(live: Mapping[str, Any], base: Mapping[str, Any]) -> bool:
    """True when the live line names this session and says exactly what the transcript says.

    The identity is part of the comparison: a line that matches on every other
    field but carries no identity is a legacy line, and it is written as a
    correction carrying the identity rather than skipped — the write is what
    persists the match.
    """
    return all(live.get(field) == base[field] for field in _BOOKED_FIELDS)


def _next_attempt(records: Sequence[Mapping[str, Any]]) -> int:
    """The first attempt number no recorded ``backfill`` run of this reference holds."""
    used = 0
    for record in records:
        try:
            _project, producer, _ref, attempt = cost_ledger.split_run_id(record.get("run_id"))
        except cost_ledger.InvalidRecord:
            continue
        if producer == BACKFILL_PRODUCER:
            used = max(used, attempt)
    return used + 1


def _reserve_attempt(records: Sequence[Mapping[str, Any]], ref: str,
                     reserved: MutableMapping[str, int]) -> int:
    """The next free attempt of this reference, held for this plan from now on.

    ``reserved`` is the plan-wide ledger of charges already planned but not yet
    written: the first charge of a reference starts after the attempts the
    ledger holds (``_next_attempt``), and every later charge of the same
    reference takes the number after it. Without this, two unbooked sessions of
    one dispatch would both be planned as ``:1`` and collapse in the aggregate.
    """
    attempt = reserved.get(ref)
    if attempt is None:
        attempt = _next_attempt(records)
    reserved[ref] = attempt + 1
    return attempt


# ---------------------------------------------------------------------------
# The plan: what one transcript would write
# ---------------------------------------------------------------------------


def _verified(record: Mapping[str, Any], run: TranscriptRun, _index: int) -> bool:
    """Pass 1: the line names this transcript's identity."""
    return _record_identity(record) == run.identity


def _exact_tokens(tokens: Sequence[Mapping[str, Optional[int]]]):
    """Pass 2: a line without an identity whose tokens are exactly this run's."""
    def evidence(record: Mapping[str, Any], _run: TranscriptRun, index: int) -> bool:
        return _record_identity(record) is None and record.get("tokens") == dict(tokens[index])
    return evidence


def _overlapping(record: Mapping[str, Any], run: TranscriptRun, _index: int) -> bool:
    """Pass 3: a line without an identity whose window overlaps this run's."""
    return _record_identity(record) is None and _overlaps(record, run)


def _match_pass(ordered: Sequence[TranscriptRun], live_index: Mapping[str, Sequence[Mapping[str, Any]]],
                claimed: set, matched: MutableMapping[int, Mapping[str, Any]], evidence) -> None:
    """Claim, per run in order, the nearest unclaimed line ``evidence`` accepts.

    ``matched`` is filled in place, and a run it already holds is left alone, so
    the passes after the first never take a second line for one transcript.
    """
    for index, run in enumerate(ordered):
        if index in matched:
            continue
        near = [record for record in _claimable(live_index.get(run.ref, ()), run, claimed)
                if evidence(record, run, index)]
        if not near:
            continue
        matched[index] = _pick(near, run)
        claimed.add(matched[index]["record_id"])


def plan_run(run: TranscriptRun, row: Optional[DispatchRow], live: Optional[Mapping[str, Any]],
             tokens: Mapping[str, Optional[int]], project_id: str,
             live_index: Mapping[str, Sequence[Mapping[str, Any]]],
             reserved: MutableMapping[str, int]) -> PlannedRun:
    """The planned line for one transcript: a correction on its live run, a charge, or a skip.

    A charge's attempt number comes from ``reserved`` — the plan-wide reservations
    ``plan_runs`` keeps — so it never reuses a number another charge already holds.
    """
    model = lane_model(run.provider, row.model if row is not None else None,
                       (live or {}).get("model"), run.reported_model) or _LANE_DEFAULT[run.provider]()
    base = {
        "project_id": project_id, "role": ledger_role(row, live), "provider": run.provider,
        "model": model, "occurred_from": run.occurred_from, "occurred_to": run.occurred_to,
        "tokens": tokens, "usage_completeness": harness_run_cost.usage_completeness(tokens),
        "billing": harness_run_cost.BILLING_BY_PROVIDER[run.provider],
        "segments": harness_run_cost.price_segments(run.provider, model, run.messages, tokens),
        "transcript_identity": run.identity,
    }
    if live is None:
        attempt = _reserve_attempt(live_index.get(run.ref, ()), run.ref, reserved)
        run_id = cost_ledger.run_id_for(project_id, BACKFILL_PRODUCER, run.ref, attempt)
        return PlannedRun(run.provider, cost_ledger.RunCost(run_id=run_id, backfill=True, **base),
                          None, skipped=False)
    if _already_booked(live, base):
        return PlannedRun(run.provider, None, live, skipped=True)
    planned = cost_ledger.RunCost(run_id=live["run_id"], kind="correction",
                                  supersedes=live["record_id"], reason=CORRECTION_REASON,
                                  backfill=True, **base)
    return PlannedRun(run.provider, planned, live, skipped=False)


def plan_runs(runs: Sequence[TranscriptRun], rows: Mapping[str, DispatchRow],
              live_index: Mapping[str, Sequence[Mapping[str, Any]]],
              project_id: str) -> List[PlannedRun]:
    """One planned line per transcript, in deterministic order; each live line is claimed once.

    Three passes, in this order, over the transcripts of one reference:

    1. a *verified* match — the live line stores this transcript's identity. This is
       the only way a run that already names a session is corrected; a session of
       the same dispatch with another identity (or a ``--since`` that leaves the
       booked transcript out of the scan) never takes its line;
    2. an exact token match on a line without an identity, across all transcripts
       of a reference, so a small follow-up session can never take the live line
       of the big run it shares a reference with;
    3. a line without an identity whose window overlaps the run's. A line written
       before the identity was stored (the envelope's own line) can be claimed
       once this way, and the correction stamps the identity on it. A line whose
       window does not overlap stays untouched: no evidence it measures this
       session.

    Anything left over books a charge: the run is added beside the existing ones,
    never written as a correction that would remove their spend.

    Order is the run's end timestamp, then its transcript path: the same transcripts in
    the same store always plan the same run ids. Every charge planned for a reference
    reserves its attempt in ``reserved`` before the next one is numbered, so the second
    unbooked session of a dispatch gets the next free attempt instead of a duplicate.
    """
    ordered = sorted(runs, key=lambda item: (item.occurred_to, item.path))
    tokens = [harness_run_cost.run_tokens(run.messages) for run in ordered]
    claimed: set = set()
    matched: Dict[int, Mapping[str, Any]] = {}
    _match_pass(ordered, live_index, claimed, matched, _verified)
    _match_pass(ordered, live_index, claimed, matched, _exact_tokens(tokens))
    _match_pass(ordered, live_index, claimed, matched, _overlapping)
    reserved: Dict[str, int] = {}
    return [
        plan_run(run, rows.get(run.dispatch_id) if run.dispatch_id else None, matched.get(index),
                 tokens[index], project_id, live_index, reserved)
        for index, run in enumerate(ordered)
    ]
