"""The tiebreaker's verdicts at the B3 cap, and the one extra round ``verkleinen`` grants (R2a).

Track ``review-stopregel-en-lichte-stoelen``, plan section 2, ADR-041. R1
(``review_rounds``) counts the codex-seat rounds and puts a PR at the cap under
B3.1 or B3.2. From there one registry model decides the next step
(``review_cap_tiebreaker``); this module holds what it decided and what that
means for the gate, the writer and the doors:

- **The store.** ``review_cap_verdicts`` in the project's runtime store
  (migration 0035), keyed by (``project_id``, ``pr_number``, ``verdict_id``)
  per ADR-007. ``verdict_id`` is ``cap-pr<N>-<head>``: one verdict per head the
  cap is decided on, so a repeat after a crash finds the row and writes nothing.
- **The outcomes.** B3.1 is ``verkleinen`` or ``terug-naar-plan``, B3.2 is
  ``verkleinen`` or ``mergen-met-OI``. R2a carries out ``verkleinen`` only. The
  other outcome of a rule is recorded as ``outcome_unavailable_in_r2a`` with the
  model's choice beside it, and the cap stays closed.
- **The grant.** A ``verkleinen`` verdict grants one codex-seat round, bound to
  the dispatch the T0 named. A head qualifies when it carries a commit of that
  dispatch (a ``Dispatch-ID: <id>`` trailer) that the head the verdict decided on
  does not carry. The writer books the first codex-seat verdict on such a head
  with ``granted_by`` (``review_rounds._check_append``), and that line is the
  only one past the cap the merge door counts.
- **After the round.** PASS can merge. FAIL goes back to the tiebreaker, which
  can then only pick the other outcome of its rule; a second ``verkleinen`` on
  one PR is refused. A blocker of the second seat on that head binds first: the
  next verdict is decided under B3.2 (state contract rule 3).

BILLING SAFETY: no Anthropic SDK, no network beyond ``git fetch`` of a PR head
the repo does not have yet.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

import review_rounds

logger = logging.getLogger(__name__)

TABLE = "review_cap_verdicts"

VERKLEINEN = "verkleinen"
TERUG_NAAR_PLAN = "terug-naar-plan"
MERGEN_MET_OI = "mergen-met-OI"
UNAVAILABLE = "outcome_unavailable_in_r2a"
RULE_OUTCOMES = {
    review_rounds.B3_1: (VERKLEINEN, TERUG_NAAR_PLAN),
    review_rounds.B3_2: (VERKLEINEN, MERGEN_MET_OI),
}

# What the CLI does next (next_step).
BELOW_CAP = "below_cap"
DECIDE = "decide"
ROUND_OPEN = "round_open"
ROUND_PASSED = "round_passed"
CLOSED = "closed"

_COLUMNS = (
    "project_id", "pr_number", "verdict_id", "head_sha", "cap_rule", "outcome", "chosen_outcome",
    "intervention", "oi_ids", "granted_dispatch_id", "tiebreaker_model", "rationale", "created_at",
)
_GIT_TIMEOUT = 30


class CapVerdictError(RuntimeError):
    """The verdict store cannot be read or written; no verdict counts from it."""


class CapVerdictRefused(ValueError):
    """A verdict the store may not take: a second ``verkleinen`` on one PR."""


@dataclass(frozen=True)
class CapVerdict:
    """One tiebreaker verdict on one head of a PR at the cap."""

    project_id: str
    pr_number: int
    verdict_id: str
    head_sha: str
    cap_rule: str
    outcome: str
    chosen_outcome: str
    intervention: str = ""
    oi_ids: Tuple[str, ...] = ()
    granted_dispatch_id: str = ""
    tiebreaker_model: str = ""
    rationale: str = ""
    created_at: str = ""

    def grant(self) -> Optional[review_rounds.Grant]:
        """The extra round this verdict grants; ``None`` for anything but ``verkleinen``."""
        if self.outcome != VERKLEINEN:
            return None
        return review_rounds.Grant(self.verdict_id, self.granted_dispatch_id, self.head_sha)

    def row(self) -> Tuple[Any, ...]:
        values = {**self.__dict__, "oi_ids": json.dumps(list(self.oi_ids))}
        return tuple(values[name] for name in _COLUMNS)

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "CapVerdict":
        values = dict(zip(_COLUMNS, row))
        values["oi_ids"] = tuple(json.loads(values["oi_ids"] or "[]"))
        return cls(**values)

    def to_dict(self) -> dict:
        return {**self.__dict__, "oi_ids": list(self.oi_ids)}


def verdict_id_for(pr_number: int, head_sha: str) -> str:
    """The key of the verdict on ``head_sha``: one per head the cap is decided on."""
    return f"cap-pr{pr_number}-{head_sha}"


def utc_now() -> str:
    """The ledger's timestamp format, so a verdict compares with ``Ledger.since``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def _db_path(state_dir: Path) -> Path:
    from coordination_db import db_path_from_state_dir

    return db_path_from_state_dir(Path(state_dir))


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def read_verdicts(state_dir: Path, pr_number: int) -> Tuple[CapVerdict, ...]:
    """This store's verdicts for PR ``pr_number``, oldest first; ``()`` without a store or table.

    Scoped to the store's own project id (ADR-007). A store that exists but
    cannot be read, or cannot name its project, raises :class:`CapVerdictError`.
    """
    from coordination_db import stamp_project_id

    path = _db_path(state_dir)
    if not path.is_file():
        return ()
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        try:
            if not _has_table(conn):
                return ()
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} WHERE project_id = ? AND pr_number = ? "
                "ORDER BY created_at, verdict_id", (stamp_project_id(conn), pr_number),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        raise CapVerdictError(f"{TABLE} in {path} could not be read: {exc}") from exc
    return tuple(CapVerdict.from_row(row) for row in rows)


def record_verdict(state_dir: Path, verdict: CapVerdict) -> Tuple[str, CapVerdict]:
    """Insert ``verdict`` with the store's project id: ``("recorded" | "present", verdict that stands)``.

    Idempotent on the key: a verdict that is already there is returned as it
    stands and nothing is written. A second ``verkleinen`` on one PR raises
    :class:`CapVerdictRefused`. A store without the table (migration 0035 not
    applied) or one that cannot be written raises :class:`CapVerdictError`.
    """
    from coordination_db import stamp_project_id

    path = _db_path(state_dir)
    if not path.is_file():
        raise CapVerdictError(f"no runtime store at {path}: the verdict has nowhere to go")
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _insert_once(conn, replace(verdict, project_id=stamp_project_id(conn)), path)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
    except CapVerdictError:
        raise
    except (sqlite3.Error, RuntimeError) as exc:
        raise CapVerdictError(f"{TABLE} in {path} could not be written: {exc}") from exc
    return result


def _insert_once(conn: sqlite3.Connection, verdict: CapVerdict, path: Path) -> Tuple[str, CapVerdict]:
    if not _has_table(conn):
        raise CapVerdictError(f"{path} has no {TABLE} table: migration 0035 is not applied to this store")
    where = "WHERE project_id = ? AND pr_number = ?"
    key = (verdict.project_id, verdict.pr_number)
    stored = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} {where} AND verdict_id = ?", (*key, verdict.verdict_id),
    ).fetchone()
    if stored is not None:
        return "present", CapVerdict.from_row(stored)
    if verdict.outcome == VERKLEINEN and conn.execute(
        f"SELECT verdict_id FROM {TABLE} {where} AND outcome = ?", (*key, VERKLEINEN),
    ).fetchone() is not None:
        raise CapVerdictRefused(
            f"PR #{verdict.pr_number} already had its one verkleinen: a second extra round is refused"
        )
    conn.execute(
        f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})", verdict.row(),
    )
    return "recorded", verdict


# ---------------------------------------------------------------------------
# Where the cap stands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapStep:
    """Where a capped PR stands: a verdict to decide, a round open or passed, or the cap closed."""

    kind: str
    rule: str = ""
    head_sha: str = ""
    allowed: Tuple[str, ...] = ()
    verdict: Optional[CapVerdict] = None
    round_line: Optional[review_rounds.RoundLine] = None


def _on_head(verdicts: Iterable[CapVerdict], head_sha: str) -> Optional[CapVerdict]:
    return next((verdict for verdict in verdicts if verdict.head_sha == head_sha), None)


def _second_seat_blocks(lines: Iterable[review_rounds.RoundLine], head_sha: str) -> bool:
    return any(
        line.seat != review_rounds.CODEX_SEAT and line.head_sha == head_sha and line.verdict == "fail"
        for line in lines
    )


def next_step(
    cap: review_rounds.CapState, lines: Sequence[review_rounds.RoundLine],
    verdicts: Iterable[CapVerdict], *, since: str = "",
) -> CapStep:
    """What the tiebreaker has to do for this PR, from its counted lines and the verdicts that stand.

    A verdict recorded before ``since`` (the PR's last attested ``opened`` line)
    belongs to a count that no longer stands and is left out.
    """
    if not cap.capped:
        return CapStep(BELOW_CAP)
    current = [verdict for verdict in verdicts if verdict.created_at >= since]
    first = _on_head(current, cap.head)
    if first is None:
        return CapStep(DECIDE, cap.rule, cap.head, RULE_OUTCOMES[cap.rule])
    grant = first.grant()
    if grant is None:
        return CapStep(CLOSED, verdict=first)
    used = review_rounds.consumed_by(grant, lines)
    if used is None:
        return CapStep(ROUND_OPEN, verdict=first)
    second_seat = _second_seat_blocks(lines, used.head_sha)
    if used.verdict == "pass" and not second_seat:
        return CapStep(ROUND_PASSED, verdict=first, round_line=used)
    second = _on_head(current, used.head_sha)
    if second is not None:
        return CapStep(CLOSED, verdict=second, round_line=used)
    rule = review_rounds.B3_2 if second_seat else first.cap_rule
    left = tuple(outcome for outcome in RULE_OUTCOMES[rule] if outcome != VERKLEINEN)
    return CapStep(DECIDE, rule, used.head_sha, left, round_line=used)


def standing_grant(
    cap: review_rounds.CapState, verdicts: Iterable[CapVerdict], *, since: str = "",
) -> Optional[review_rounds.Grant]:
    """The extra round granted on this cap: the ``verkleinen`` verdict on the head the cap fell on."""
    if not cap.capped:
        return None
    first = _on_head((verdict for verdict in verdicts if verdict.created_at >= since), cap.head)
    return first.grant() if first is not None else None


# ---------------------------------------------------------------------------
# Does a head carry a commit of the dispatch?
# ---------------------------------------------------------------------------


def _log_bodies(repo: Path, since_head: str, head_sha: str) -> Optional[str]:
    args = ["git", "-C", str(repo), "log", "--format=%B%x00", f"{since_head}..{head_sha}"]
    for attempt in range(2):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("review_cap_verdicts: git log in %s failed: %s", repo, exc)
            return None
        if proc.returncode == 0:
            return proc.stdout
        if attempt == 0:
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", "origin", head_sha, since_head],
                capture_output=True, text=True, timeout=_GIT_TIMEOUT * 4, check=False,
            )
    logger.warning(
        "review_cap_verdicts: %s..%s cannot be read in %s (%s): no granted round on that head",
        since_head[:12], head_sha[:12], repo, (proc.stderr or "").strip()[:200],
    )
    return None


def head_carries_dispatch(repo: Path, since_head: str, head_sha: str, dispatch_id: str) -> bool:
    """Does ``head_sha`` carry a commit of ``dispatch_id`` that ``since_head`` does not carry?

    A commit of a dispatch has the trailer ``Dispatch-ID: <id>`` in its message
    (the commit convention every worker follows). A head or base git cannot
    read, after one fetch from ``origin``, carries nothing: no grant on a head
    nobody could read.
    """
    if not (since_head and head_sha and dispatch_id) or head_sha == since_head:
        return False
    text = _log_bodies(Path(repo), since_head, head_sha)
    marker = re.compile(rf"^Dispatch-ID:\s+{re.escape(dispatch_id)}\s*$", re.MULTILINE)
    return text is not None and marker.search(text) is not None


def _repo() -> Path:
    from vnx_paths import ensure_env

    return Path(ensure_env()["PROJECT_ROOT"])


def grant_or_none(
    state_dir: Path, cap: review_rounds.CapState, lines: Sequence[review_rounds.RoundLine], *,
    head_sha: str, since: str = "", repo: Optional[Path] = None,
) -> Optional[review_rounds.Grant]:
    """The grant standing on this cap, qualified for ``head_sha`` when the head carries the dispatch.

    For the gate before a codex-seat run and the writer before a booking. A
    store that cannot be read grants nothing (logged): the cap stays closed.
    A round that is already used is returned unqualified; it is never granted
    to another head (``review_rounds.round_granted_past_cap``).
    """
    try:
        grant = standing_grant(cap, read_verdicts(state_dir, cap.pr_number), since=since)
    except CapVerdictError as exc:
        logger.warning("review_cap_verdicts: no granted round for PR #%s: %s", cap.pr_number, exc)
        return None
    if grant is None or review_rounds.consumed_by(grant, lines) is not None:
        return grant
    if head_carries_dispatch(repo or _repo(), grant.since_head, head_sha, grant.dispatch_id):
        return replace(grant, qualified_head=head_sha)
    return grant


def door_grant(state_dir: Path, cap: review_rounds.CapState, *, since: str = "") -> Optional[review_rounds.Grant]:
    """The grant the merge door checks a codex-seat PASS against: no git, only the line that used it counts."""
    try:
        return standing_grant(cap, read_verdicts(state_dir, cap.pr_number), since=since)
    except CapVerdictError as exc:
        logger.warning("review_cap_verdicts: the door finds no granted round for PR #%s: %s", cap.pr_number, exc)
        return None


def round_open_for(state_dir: Path, cap: review_rounds.CapState, dispatch_id: str, *, since: str = "") -> bool:
    """Is the extra round on this cap granted to ``dispatch_id`` and not yet used? (the dispatch door)."""
    grant = door_grant(state_dir, cap, since=since)
    return (
        grant is not None and bool(dispatch_id) and grant.dispatch_id == dispatch_id
        and review_rounds.consumed_by(grant, cap.rounds) is None
    )


def grant_note(grant: Optional[review_rounds.Grant], lines: Sequence[review_rounds.RoundLine], head_sha: str) -> str:
    """Why a granted round does not cover ``head_sha``, for the refusal text; ``""`` without a grant."""
    if grant is None:
        return ""
    used = review_rounds.consumed_by(grant, lines)
    if used is not None:
        return (
            f"The extra round of {grant.verdict_id} was used by the codex-seat {used.verdict.upper()} on head "
            f"{used.head_sha[:12]}"
        )
    return (
        f"The extra round of {grant.verdict_id} is open for a head with a commit of dispatch {grant.dispatch_id} "
        f"after {grant.since_head[:12]}; head {head_sha[:12]} carries none"
    )
