#!/usr/bin/env python3
"""cost_ledger_backfill.py — ``vnx costs backfill``: book the harness transcripts (K6, ADR-043).

A run on a harness lane (``deepseek-harness``, ``glm-harness``) leaves a Claude Code
session transcript under ``<lane>/projects/<encoded-cwd>/<session-id>.jsonl``; that
transcript is the complete per-message record of what the run spent. This script
books each transcript from ``--since`` on exactly once:

- a transcript whose dispatch already has a live ledger line becomes a *correction*
  on that line's ``run_id`` (``supersedes`` its ``record_id``, ``backfill: true``),
  so the ledger keeps one effective line per run — but only after a verified match
  (see below), so a *different* session of the same dispatch can never take that
  line away from it;
- a transcript with no live line becomes a *charge* under the ``backfill`` producer,
  numbered per reference after the attempts the ledger already holds; the plan reserves
  every attempt it hands out, so two unbooked sessions of one dispatch book ``:1`` and
  ``:2`` and never collapse into one run;
- a line that already says the same thing (tokens, model, role, segments) is
  skipped, so a second ``--apply`` writes nothing.

**Identity of a booked run (OI-2419).** Every line this script writes carries
``transcript_identity`` — ``<provider>:<session id>``, the lane and the transcript's
file stem, never its path. A line that names an identity is only ever corrected by a
transcript of *that* identity: a later run of the same dispatch (a new session, or a
``--since`` that excludes the booked one) books its own attempt beside it instead of
superseding it, so its spend is added and never removed. A line without the field was
written before the field existed (the envelope's own line, which measured the same run
from the receipt): it can be claimed once, and the correction stamps the identity on it
so it is verified from then on. Tokens still break ties among those legacy lines, so a
small follow-up session never takes the big run's line.

**One scan (OI-2419).** ``--harness-root`` arguments may overlap: a parent directory and
one of its lane dirs, the same lane given twice, or two stores holding one session. The
transcripts are deduplicated on their resolved path and on ``<provider>:<session id>``
before anything is planned, so one session is one run however many roots reach it.

**Which field carries the dispatch id (measured 2026-10-27).** The top-level ``cwd``
of the transcript entries. Of the 1,169 deepseek transcripts then on this machine,
506 ran in ``<repo>/.vnx-data/worktrees/dispatch-<dispatch-id>`` (494 distinct
worktrees), 551 with ``cwd`` ``/`` (conversation analyzer and classifier calls) and
111 in the repo root. The rule: the last path component of ``cwd`` with the
``dispatch-`` prefix stripped is the dispatch id; no other ``cwd`` has one. A
transcript without a dispatch id uses its session id (the file stem) as its
reference — the same reference a classifier run books — and is counted, never
dropped. Session metadata files exist only for live sessions, so ``cwd`` is the
durable carrier; the first user message is never read.

**Provider, model, role.** The provider is the lane the transcript lives in (its directory name),
never the transcript's self-reported name. The model is the dispatch row's when the
registry knows it, else the live line's, else the transcript's dominant reported
model, else the lane default — each resolved through ``wave7_models.yaml``
(ADR-036), so nothing but a registry key reaches the ledger. The role is the
dispatch row's role mapped to a ledger purpose (a worker role is ``builder``,
``review-gate``/``code-reviewer`` is ``review``, ``plan-reviewer`` is
``plan_gate``, a panelist is ``panel``); without a row it is the live line's role,
else ``unknown:no_dispatch_row``.

**Privacy.** Only token counts, timestamps and amounts leave a transcript. No
message text is copied into the ledger or printed.

Exit codes: 0 clean (a dry run too), 1 a failed write or an unreadable ledger,
2 usage (a root that is not a harness lane, a bad ``--since``).

Usage: python3 scripts/cost_ledger_backfill.py [--since YYYY-MM-DD]
       [--harness-root PATH ...] [--dry-run | --apply] [--ledger PATH]
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402
import provider_costs  # noqa: E402
import token_harvest  # noqa: E402
import vnx_paths  # noqa: E402
from project_root import resolve_project_id  # noqa: E402
from provider_spawns import deepseek_harness_spawn, glm_harness_spawn, harness_config_dir  # noqa: E402
from providers import provider_registry  # noqa: E402

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
        """The live run's reference: the dispatch id, or the session id when there is none."""
        return self.dispatch_id or self.session_id

    @property
    def identity(self) -> str:
        """The run's durable identity: its lane and its session id, never its transcript path."""
        return f"{self.provider}:{self.session_id}"


@dataclass(frozen=True)
class PlannedRun:
    """One transcript's outcome: the line to write, or a skip with the line it already matches."""

    provider: str
    run: Optional[cost_ledger.RunCost]
    live: Optional[Mapping[str, Any]]
    skipped: bool


@dataclass
class ProviderSummary:
    """The per-provider counts ``--dry-run`` prints and ``--apply`` reports."""

    provider: str
    runs_found: int = 0
    with_live: int = 0
    corrections: int = 0
    charges: int = 0
    skipped: int = 0
    unavailable: int = 0
    failures: int = 0
    total_usd: float = 0.0

    def line(self) -> str:
        """One line: what was found, planned/written and what it is worth."""
        return (
            f"{self.provider}: runs_found={self.runs_found} with_live={self.with_live} "
            f"corrections={self.corrections} charges={self.charges} skipped={self.skipped} "
            f"unavailable={self.unavailable} total_usd={self.total_usd:.6f} "
            f"failures={self.failures}"
        )


# ---------------------------------------------------------------------------
# Reading the transcripts
# ---------------------------------------------------------------------------


def lane_roots(values: Iterable[str]) -> List[Path]:
    """The lane dirs for ``--harness-root`` values: a lane dir itself, or a parent of lane dirs.

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

    Overlapping ``--harness-root`` arguments, a copy of a session in a second
    store and a lane reached through a symlink all name one run; planning it
    twice would book two charges for one session's spend. The first entry in
    (provider, session id, path) order wins, so the same roots always book the
    same run.
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


# ---------------------------------------------------------------------------
# Identity and the live line
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
    """``ref -> effective live records`` of this project, oldest window first."""
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

    A line whose identity is missing, empty or not a string names no session:
    it is a legacy line, and :func:`plan_runs` may claim it once (stamping the
    identity on the correction). Anything else would let one session supersede
    another's booked run.
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
    """How far apart two runs' windows are, in seconds; an unreadable timestamp counts as 0."""
    live_from, live_to = _parse(record.get("occurred_from")), _parse(record.get("occurred_to"))
    run_from, run_to = _parse(run.occurred_from), _parse(run.occurred_to)
    if None in (live_from, live_to, run_from, run_to):
        return 0.0
    return abs((live_to - run_to).total_seconds()) + abs((live_from - run_from).total_seconds())


def _already_booked(live: Mapping[str, Any], base: Mapping[str, Any]) -> bool:
    """True when the live line already says exactly what the transcript says."""
    return all(live.get(field) == base[field]
               for field in ("provider", "model", "role", "tokens", "segments"))


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
# Planning and writing
# ---------------------------------------------------------------------------


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
    if live is not None:
        if _already_booked(live, base):
            return PlannedRun(run.provider, None, live, skipped=True)
        planned = cost_ledger.RunCost(run_id=live["run_id"], kind="correction",
                                      supersedes=live["record_id"], reason=CORRECTION_REASON,
                                      backfill=True, **base)
        return PlannedRun(run.provider, planned, live, skipped=False)
    attempt = _reserve_attempt(live_index.get(run.ref, ()), run.ref, reserved)
    run_id = cost_ledger.run_id_for(project_id, BACKFILL_PRODUCER, run.ref, attempt)
    return PlannedRun(run.provider, cost_ledger.RunCost(run_id=run_id, backfill=True, **base),
                      None, skipped=False)


def plan_runs(runs: Sequence[TranscriptRun], rows: Mapping[str, DispatchRow],
              live_index: Mapping[str, Sequence[Mapping[str, Any]]],
              project_id: str) -> List[PlannedRun]:
    """One planned line per transcript, in deterministic order; each live line is claimed once.

    Three passes, in this order, over the transcripts of one reference:

    1. a *verified* match — the live line stores this transcript's identity. This is
       the only way a run that already names a session is corrected; a
       session of the same dispatch with another identity (or a ``--since`` that
       leaves the booked transcript out of the scan) never takes its line;
    2. an exact token match on a line without an identity, across all transcripts
       of a reference, so a small follow-up session can never take the live line
       of the big run it shares a reference with;
    3. the nearest remaining line without an identity. A line written before the
       identity was stored (the envelope's own line) can be claimed once this way,
       and the correction stamps the identity on it.

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
    for index, run in enumerate(ordered):
        candidates = _claimable(live_index.get(run.ref, ()), run, claimed)
        verified = [record for record in candidates if _record_identity(record) == run.identity]
        if verified:
            matched[index] = min(verified, key=lambda record: _distance(record, run))
            claimed.add(matched[index]["record_id"])
    for index, run in enumerate(ordered):
        if index in matched:
            continue
        matches = [record for record in _claimable(live_index.get(run.ref, ()), run, claimed)
                   if _record_identity(record) is None and record.get("tokens") == dict(tokens[index])]
        if matches:
            matched[index] = min(matches, key=lambda record: _distance(record, run))
            claimed.add(matched[index]["record_id"])
    planned: List[PlannedRun] = []
    reserved: Dict[str, int] = {}
    for index, run in enumerate(ordered):
        live = matched.get(index)
        if live is None:
            legacy = [record for record in _claimable(live_index.get(run.ref, ()), run, claimed)
                      if _record_identity(record) is None]
            live = min(legacy, key=lambda record: _distance(record, run)) if legacy else None
        row = rows.get(run.dispatch_id) if run.dispatch_id else None
        planned.append(plan_run(run, row, live, tokens[index], project_id, live_index, reserved))
        if live is not None:
            claimed.add(live["record_id"])
    return planned


def apply_runs(plans: Sequence[PlannedRun], ledger_path: Path, *, dry_run: bool) -> Dict[str, ProviderSummary]:
    """Write (or, in a dry run, price) every planned line and count it per provider."""
    summaries: Dict[str, ProviderSummary] = {}
    for planned in plans:
        summary = summaries.setdefault(planned.provider, ProviderSummary(planned.provider))
        summary.runs_found += 1
        if planned.live is not None:
            summary.with_live += 1
        if planned.skipped:
            summary.skipped += 1
            continue
        try:
            record = (cost_ledger.build_record(planned.run) if dry_run
                      else cost_ledger.record_run(planned.run, ledger_path=ledger_path))
        except (cost_ledger.CostLedgerRefusal, OSError) as exc:
            summary.failures += 1
            print(f"{planned.provider}: {planned.run.run_id}: {exc}", file=sys.stderr)
            continue
        if planned.run.kind == "correction":
            summary.corrections += 1
        else:
            summary.charges += 1
        amount = record.get("cost_usd")
        if amount is None:
            summary.unavailable += 1
        else:
            summary.total_usd += float(amount)
    return summaries


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vnx costs backfill",
        description="Book the harness transcript runs the ledger misses (ADR-043 K6).",
    )
    parser.add_argument("--since", type=date.fromisoformat, default=date.fromisoformat(DEFAULT_SINCE),
                        metavar="YYYY-MM-DD", help=f"first day of runs to book (default {DEFAULT_SINCE})")
    parser.add_argument("--harness-root", action="append", metavar="PATH",
                        help="a harness lane dir or a parent of lane dirs (repeatable; default: "
                             "the deepseek-harness and glm-harness dirs under <VNX_DATA_DIR>/harness-config)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the plan, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="write the planned lines")
    parser.add_argument("--ledger", metavar="PATH",
                        help="the ledger to read and write (default: this project's store)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Plan (and with ``--apply`` write) one line per harness transcript from ``--since`` on."""
    args = _parse_args(argv)
    try:
        project_id = resolve_project_id()
        paths = vnx_paths.resolve_paths()
    except (RuntimeError, OSError) as exc:
        print(f"vnx costs backfill: cannot resolve the project store: {exc}", file=sys.stderr)
        return 1
    defaults = [item.parent for item in harness_config_dir.harness_projects_dirs()
                if item.parent.name in LANES]
    roots = lane_roots(args.harness_root) if args.harness_root else defaults
    unknown = sorted({root.name for root in roots if root.name not in LANES})
    if unknown:
        print(f"vnx costs backfill: not a harness lane: {', '.join(unknown)}", file=sys.stderr)
        return 2
    ledger_path = Path(args.ledger) if args.ledger else provider_costs.costs_path_in(Path(paths["VNX_DATA_DIR"]))
    try:
        live_index = load_live_runs(ledger_path, project_id)
    except (OSError, ValueError) as exc:
        print(f"vnx costs backfill: cannot read {ledger_path}: {exc}", file=sys.stderr)
        return 1
    rows = load_dispatch_rows(Path(paths["VNX_DISPATCH_DIR"]))
    runs = dedupe_transcripts(transcript for root in roots
                              for transcript in scan_lane(root, args.since))
    if not args.apply:
        print("dry run: no line written; pass --apply to write")
    summaries = apply_runs(plan_runs(runs, rows, live_index, project_id), ledger_path,
                           dry_run=not args.apply)
    if not summaries:
        print(f"no transcript on or after {args.since} under {', '.join(str(root) for root in roots)}")
        return 0
    for provider in sorted(summaries):
        print(summaries[provider].line())
    return 0 if not any(summary.failures for summary in summaries.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
