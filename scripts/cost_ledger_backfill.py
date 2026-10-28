#!/usr/bin/env python3
"""cost_ledger_backfill.py — the harness session transcripts, read as runs (K6b, ADR-049).

A run on a harness lane (``deepseek-harness``, ``glm-harness``) leaves a Claude Code
session transcript under ``<lane>/projects/<encoded-cwd>/<session-id>.jsonl``; that
transcript is the complete per-message record of what the run spent. This module turns
those files into one :class:`TranscriptRun` per session. It only reads: it writes
nothing to any ledger and has no CLI. K6b-2 plans and writes the backfill on top of
:func:`scan`.

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
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    import token_harvest
except Exception as exc:
    raise SystemExit(f"Failed to load token_harvest: {exc}")

logger = logging.getLogger(__name__)

DEFAULT_SINCE = "2026-10-02"
LANES = ("deepseek-harness", "glm-harness")
DISPATCH_PREFIX = "dispatch-"


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

        K6a (``harness_run_cost.transcript_identity``) owns the same shape; K6b-2
        switches this property to that function.
        """
        return f"{self.provider}:{self.session_id}"


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
