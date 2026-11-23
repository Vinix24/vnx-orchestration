"""blast_radius_gate — verdict clear, blast_radius_overlap or blast_radius_unknown per candidate (ADR-096 B4b); unknown is local to it and never clear."""
from __future__ import annotations

import fcntl
import subprocess
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

import blast_radius as br
import blast_radius_sources as brs
from deliverable_predecessor_gate import append_lines

MODE_KEY, MODES, DEFAULT_MODE, LEDGER = "VNX_BLAST_RADIUS_MODE", ("off", "warn", "refuse"), "warn", "blast_radius.ndjson"
LOCK_FILENAME = "blast_radius.lock"  # the check-and-claim section (ADR-096 §15)
CLEAR, OVERLAP, UNKNOWN = "clear", "blast_radius_overlap", "blast_radius_unknown"
Hit = namedtuple("Hit", "source other path reason")  # reason "overlap", or why it is unknown


def gh(argv: List[str], cwd: Path) -> str:
    """``gh <argv>`` in ``cwd``; raises on a non-zero exit or a timeout."""
    return subprocess.run(["gh", *argv], cwd=str(cwd), capture_output=True, text=True, timeout=30, check=True).stdout


@contextmanager
def section(state_dir: "str | Path") -> Iterator[None]:
    """Serialise the blast-radius check and the dispatch claim (ADR-096 §15).

    An exclusive ``flock`` on ``<state_dir>/blast_radius.lock``: the first fire holds
    it from its check through its claim, so a second fire in the same second blocks
    here and then reads the first as ``starting`` in ``live_work``. The lock is
    released on every exit, an exception inside the section included.
    """
    path = Path(state_dir) / LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def judge(candidate: br.Subject, sources: Iterable[Tuple[str, brs.Sources]], repo_root: "str | Path") -> Tuple[str, Tuple[Hit, ...]]:
    """The verdict and every hit; an incomplete or invalid candidate scope is unknown even with nothing to compare."""
    try:
        scope = br.scope_of(candidate.paths, repo_root, br.DEFAULT_GENERIC_DIRS) if candidate.paths is not None else None
    except br.ScopeInvalid as exc:
        return UNKNOWN, (Hit("candidate", candidate.deliverable_id, "", str(exc)),)
    if scope is None or scope.incomplete:
        return UNKNOWN, (Hit("candidate", candidate.deliverable_id, "", br.REASON_SCOPE_INCOMPLETE),)
    hits: List[Hit] = []
    for name, src in sources:
        if src.unavailable is not None or not isinstance(src.subjects, tuple):
            hits.append(Hit(name, "", "", f"source unavailable: {src.unavailable}"))
            continue
        for other in src.subjects:
            label = other.deliverable_id or other.branch or "?"
            for r in br.check(candidate, (other,), repo_root):
                if r.outcome == br.OVERLAP and r.paths:
                    hits.extend(Hit(name, label, p, "overlap") for p in r.paths)
                elif r.outcome != br.CLEAR:  # unknown, or an outcome this gate does not know: never clear
                    hits.append(Hit(name, label, "", src.reasons.get(other.deliverable_id) or r.reason or r.outcome))
    return (OVERLAP if any(h.reason == "overlap" for h in hits) else UNKNOWN if hits else CLEAR), tuple(hits)


def describe(hits: Sequence[Hit]) -> str:
    return "; ".join(f"{h.source} {h.other}: {h.path if h.reason == 'overlap' else h.reason}" for h in hits)


def record(state_dir: Path, project_id: str, dispatch_id: str, hits: Iterable[Hit], *, mode: str, verdict: str,
           outcome: str, override_reason: str = "") -> None:
    """One line per hit, key (project_id, ts, dispatch_id, seq), in one flocked append; raises OSError."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    append_lines(Path(state_dir) / LEDGER, [
        {"project_id": project_id, "ts": ts, "dispatch_id": dispatch_id, "seq": seq, "candidate": br._round_base(dispatch_id),
         "source": h.source, "other": h.other, "path": h.path or None, "reason": h.reason, "verdict": verdict,
         "mode": mode, "outcome": outcome, "override_reason": override_reason or None}
        for seq, h in enumerate(hits)])
