"""The tiebreaker's verdicts at the B3 cap, and the one extra round ``verkleinen`` grants (R2a, R2b).

Track ``review-stopregel-en-lichte-stoelen``, plan section 2, ADR-041. R1
(``review_rounds``) counts the codex-seat rounds and puts a PR at the cap under
B3.1 or B3.2. From there one registry model decides the next step
(``review_cap_tiebreaker``); this module holds what it decided and what that
means for the gate, the writer and the doors:

- **The store.** ``review_cap_verdicts`` in the project's runtime store
  (migration 0035), keyed by (``project_id``, ``pr_number``, ``verdict_id``)
  per ADR-007. ``verdict_id`` is ``cap-pr<N>-<head>``: one verdict per head the
  cap is decided on, so a repeat after a crash finds the row and writes nothing.
- **The receipt first** (ADR-005, OI-2201). A verdict is booked as its
  ``review_cap_verdict`` receipt in the store's receipt ledger, then its row:
  the grant a ``verkleinen`` row carries exists only once its receipt landed.
  The receipt is the canonical record and the row its projection, so a
  receipt whose row did not land is booked from the receipt, never decided again.
  Only a receipt of this store's project (ADR-007) and of the PR's current
  count (at or after ``since``) is such a record (OI-2207).
- **The outcomes.** B3.1 is ``verkleinen`` or ``terug-naar-plan``, B3.2 is
  ``verkleinen`` or ``mergen-met-OI``. R2a carries out ``verkleinen``, R2b
  ``terug-naar-plan`` (``review_cap_aftermath``: the remaining codex blockers
  become open items, whose ids the row carries, the deliverable goes back to
  ``proposed`` and the PR closes). ``mergen-met-OI`` is recorded as
  ``outcome_unavailable_in_r2a`` with the model's choice beside it, and the cap
  stays closed.
- **Sent back to plan.** A ``terug-naar-plan`` verdict that stands closes the
  PR at the merge door: no PASS of any seat counts on any head, an
  ``--override-b3`` included (:func:`door_return`).
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
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import review_rounds

logger = logging.getLogger(__name__)

TABLE = "review_cap_verdicts"
RECEIPT_EVENT = "review_cap_verdict"

VERKLEINEN = "verkleinen"
TERUG_NAAR_PLAN = "terug-naar-plan"
MERGEN_MET_OI = "mergen-met-OI"
UNAVAILABLE = "outcome_unavailable_in_r2a"
RULE_OUTCOMES = {
    review_rounds.B3_1: (VERKLEINEN, TERUG_NAAR_PLAN),
    review_rounds.B3_2: (VERKLEINEN, MERGEN_MET_OI),
}
# The outcomes that are carried out; any other choice is stored as UNAVAILABLE.
CARRIED_OUT = (VERKLEINEN, TERUG_NAAR_PLAN)

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
# Seconds for one grant lookup in git: the log, the fetch it may need and the log again share it.
_GIT_BUDGET = 30
# A commit SHA as the store and the ledger hold it; nothing else goes to git.
_SHA = re.compile(r"[0-9a-f]{7,40}")

#: Does ``head_sha`` carry a commit of ``dispatch_id`` that ``since_head`` does
#: not carry? Called as ``carries(since_head, head_sha, dispatch_id)``.
CarriesFn = Callable[[str, str, str], bool]


class CapVerdictError(RuntimeError):
    """The verdict store cannot be read or written; no verdict counts from it."""


class CapVerdictRefused(ValueError):
    """A verdict the store may not take: a second ``verkleinen`` on one PR."""


class CapVerdictReceiptError(RuntimeError):
    """The verdict's receipt did not land: no row was written, so no grant stands (ADR-005)."""


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


def stored_outcome(chosen: str) -> str:
    """The outcome a verdict is stored with: the choice when it is carried out, else UNAVAILABLE."""
    return chosen if chosen in CARRIED_OUT else UNAVAILABLE


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
            return tuple(CapVerdict.from_row(row) for row in rows)
        finally:
            conn.close()
    except (sqlite3.Error, RuntimeError, ValueError, TypeError) as exc:
        raise CapVerdictError(f"{TABLE} in {path} could not be read: {exc}") from exc


def store_project_id(state_dir: Path) -> str:
    """The project id this store stamps on its rows (ADR-007); raises :class:`CapVerdictError` without one."""
    from coordination_db import stamp_project_id

    path = _db_path(state_dir)
    if not path.is_file():
        raise CapVerdictError(f"no runtime store at {path}: it names no project")
    try:
        conn = sqlite3.connect(str(path), timeout=10.0)
        try:
            return stamp_project_id(conn)
        finally:
            conn.close()
    except (sqlite3.Error, RuntimeError) as exc:
        raise CapVerdictError(f"the project of {path} cannot be resolved: {exc}") from exc


def record_verdict(state_dir: Path, verdict: CapVerdict, *, since: str = "") -> Tuple[str, CapVerdict]:
    """Book ``verdict``, receipt first: ``("recorded" | "restored" | "present", verdict that stands)``.

    ADR-005, OI-2201. Under the store's booking lock, from the check to the
    commit: a verdict whose row is already there is returned as it stands and
    nothing is written. Otherwise the ``review_cap_verdict`` receipt goes into
    the store's receipt ledger, and only once it landed is the row committed;
    a ``verkleinen`` grants its round from that commit on. A receipt that does
    not land raises :class:`CapVerdictReceiptError` and leaves no row. A receipt
    of this store's project that landed for this ``verdict_id`` at or after
    ``since`` (the PR's last attested ``opened`` line) while its row did not is
    the verdict: its row is booked from it, ``"restored"``, and ``verdict`` is
    dropped (OI-2207).

    The insert is tried and rolled back before the receipt, so a row the table
    refuses never gets one. A second ``verkleinen`` on one PR raises
    :class:`CapVerdictRefused`. A store without the table (migration 0035 not
    applied), one that cannot be written, or a receipt ledger that cannot be
    read raises :class:`CapVerdictError`.
    """
    from atomic_io import slot_lock

    path = _db_path(state_dir)
    if not path.is_file():
        raise CapVerdictError(f"no runtime store at {path}: the verdict has nowhere to go")
    with slot_lock(Path(state_dir) / TABLE):
        landed = receipted_verdict(
            state_dir, verdict.pr_number, verdict.verdict_id, project_id=store_project_id(state_dir), since=since,
        )
        status, standing = _book_row(path, landed or verdict, commit=False)
        if status == "present":
            return status, standing
        if landed is None:
            _emit_receipt(state_dir, standing)
        try:
            _book_row(path, standing, commit=True)
        except CapVerdictError as exc:
            raise CapVerdictError(
                f"{exc}. The {RECEIPT_EVENT} receipt of {standing.verdict_id} stands: the next run books the row "
                f"from it"
            ) from exc
    return ("restored" if landed is not None else "recorded"), standing


def _book_row(path: Path, verdict: CapVerdict, *, commit: bool) -> Tuple[str, CapVerdict]:
    """Insert ``verdict`` with the store's project id in one transaction; committed only with ``commit``."""
    from coordination_db import stamp_project_id

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
            conn.execute("COMMIT" if commit else "ROLLBACK")
        finally:
            conn.close()
    except CapVerdictError:
        raise
    except (sqlite3.Error, RuntimeError) as exc:
        raise CapVerdictError(f"{TABLE} in {path} could not be written: {exc}") from exc
    return result


def _emit_receipt(state_dir: Path, verdict: CapVerdict) -> None:
    """The verdict's ``review_cap_verdict`` receipt in the store's receipt ledger; raises when it did not land."""
    from governance_receipts import AppendReceiptError, emit_governance_receipt

    try:
        emit_governance_receipt(
            RECEIPT_EVENT, receipt_kind="state_mutation",
            receipts_file=str(Path(state_dir) / review_rounds.RECEIPTS_NAME), **verdict.to_dict(),
        )
    except (AppendReceiptError, OSError, ValueError) as exc:
        raise CapVerdictReceiptError(
            f"the {RECEIPT_EVENT} receipt of {verdict.verdict_id} was not written ({exc}): no row, no granted round"
        ) from exc


def receipts_naming(state_dir: Path, event_type: str, key: str) -> List[Dict[str, Any]]:
    """Every ``event_type`` receipt in the store's receipt ledger whose line names ``key``, in file order.

    A ledger that cannot be read raises :class:`CapVerdictError`: whether
    something was booked cannot be told. A line that names both and does not
    parse is a torn append, logged and skipped.
    """
    receipts = Path(state_dir) / review_rounds.RECEIPTS_NAME
    event, needle = event_type.encode(), key.encode()
    found: List[Dict[str, Any]] = []
    try:
        with receipts.open("rb") as handle:
            for raw in handle:
                if event in raw and needle in raw:
                    receipt = _parsed_receipt(raw, event_type, key)
                    if receipt is not None and receipt.get("event_type") == event_type:
                        found.append(receipt)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CapVerdictError(f"the receipt ledger {receipts} could not be read: {exc}") from exc
    return found


def _parsed_receipt(raw: bytes, event_type: str, key: str) -> Optional[Dict[str, Any]]:
    try:
        receipt = json.loads(raw)
    except ValueError as exc:
        logger.warning("review_cap_verdicts: a %s line of %s does not parse (%s): skipped", event_type, key, exc)
        return None
    return receipt if isinstance(receipt, dict) else None


def receipted_verdict(
    state_dir: Path, pr_number: int, verdict_id: str, *, project_id: str, since: str = "",
) -> Optional[CapVerdict]:
    """The verdict the store's receipt ledger holds for ``verdict_id``: the first that landed; ``None`` without one.

    A receipt is the canonical record of a verdict (ADR-005), but only for its
    own project and its own count (OI-2207): a receipt of another project with
    the same PR and verdict id is not this store's verdict (ADR-007), and one
    created before ``since`` belongs to a count that no longer stands, as in
    :func:`next_step`. Neither is booked. A ledger that cannot be read raises
    :class:`CapVerdictError`: whether the verdict was decided cannot be told,
    so nothing is decided.
    """
    for receipt in receipts_naming(state_dir, RECEIPT_EVENT, verdict_id):
        found = _verdict_in_receipt(receipt, pr_number, verdict_id, project_id)
        if found is not None and found.created_at >= since:
            return found
    return None


def _verdict_in_receipt(receipt: Dict[str, Any], pr_number: int, verdict_id: str, project_id: str) -> Optional[CapVerdict]:
    if (receipt.get("verdict_id"), receipt.get("pr_number"), receipt.get("project_id")) != (
        verdict_id, pr_number, project_id,
    ):
        return None
    missing = [name for name in _COLUMNS if name not in receipt]
    if missing:
        logger.warning("review_cap_verdicts: the %s receipt of %s lacks %s: skipped", RECEIPT_EVENT, verdict_id, missing)
        return None
    values = {name: receipt[name] for name in _COLUMNS}
    if not isinstance(values["created_at"], str):
        logger.warning("review_cap_verdicts: the %s receipt of %s has no created_at: skipped", RECEIPT_EVENT, verdict_id)
        return None
    return CapVerdict(**{**values, "oi_ids": tuple(values["oi_ids"] or ())})


def set_oi_ids(state_dir: Path, verdict: CapVerdict, oi_ids: Sequence[str]) -> None:
    """Write ``oi_ids`` onto the stored ``terug-naar-plan`` row of ``verdict`` (R2b); idempotent.

    Under the store's booking lock. The row must stand: a verdict whose row is
    gone (the receipt books it again on the next run) raises
    :class:`CapVerdictError`, as does a store that cannot be written.
    """
    from atomic_io import slot_lock

    path = _db_path(state_dir)
    value = json.dumps(list(oi_ids))
    with slot_lock(Path(state_dir) / TABLE):
        try:
            conn = sqlite3.connect(str(path), timeout=10.0)
            try:
                with conn:
                    changed = conn.execute(
                        f"UPDATE {TABLE} SET oi_ids = ? WHERE project_id = ? AND pr_number = ? AND verdict_id = ? "
                        "AND outcome = ?",
                        (value, verdict.project_id, verdict.pr_number, verdict.verdict_id, TERUG_NAAR_PLAN),
                    ).rowcount
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise CapVerdictError(f"{TABLE} in {path} could not be written: {exc}") from exc
    if changed != 1:
        raise CapVerdictError(
            f"{TABLE} in {path} holds no terug-naar-plan row {verdict.verdict_id} of project {verdict.project_id}: "
            f"its open items are not on the row"
        )


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


def standing_return(
    cap: review_rounds.CapState, verdicts: Iterable[CapVerdict], *, since: str = "",
) -> Optional[CapVerdict]:
    """The ``terug-naar-plan`` verdict that sent this capped PR back to plan, ``None`` without one (R2b).

    Any head: the first verdict decides on the cap head, the forced one after
    the extra round on that round's head. A verdict before ``since`` belongs to
    a count that no longer stands.
    """
    if not cap.capped:
        return None
    return next(
        (verdict for verdict in verdicts if verdict.created_at >= since and verdict.outcome == TERUG_NAAR_PLAN), None,
    )


def door_return(state_dir: Path, cap: review_rounds.CapState, *, since: str = "") -> Optional[CapVerdict]:
    """The ``terug-naar-plan`` verdict the merge door refuses this PR on; ``None`` when none stands.

    A store that cannot be read raises :class:`CapVerdictError`: whether the PR
    was sent back cannot be told, so the door counts no PASS of a capped PR.
    """
    if not cap.capped:
        return None
    return standing_return(cap, read_verdicts(state_dir, cap.pr_number), since=since)


# ---------------------------------------------------------------------------
# Does a head carry a commit of the dispatch?
# ---------------------------------------------------------------------------


def _git(repo: Path, args: Sequence[str], deadline: float) -> Optional[subprocess.CompletedProcess]:
    """One git call in what is left of the lookup's budget; ``None``, logged, when it cannot run or finish."""
    left = deadline - time.monotonic()
    if left <= 0:
        logger.warning(
            "review_cap_verdicts: the %ss git budget of this lookup is spent before git %s in %s: no granted round "
            "on that head", _GIT_BUDGET, args[0], repo,
        )
        return None
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=left, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        # OI-2207: a commit message that is no UTF-8 is no answer either.
        logger.warning(
            "review_cap_verdicts: git %s in %s failed (%s): no granted round on that head", args[0], repo, exc,
        )
        return None


def _log_bodies(repo: Path, since_head: str, head_sha: str) -> Optional[str]:
    """The commit messages of ``since_head..head_sha``, with one fetch from ``origin`` when git cannot read them.

    OI-2202: the three calls share one budget (``_GIT_BUDGET``), and a git that
    cannot run, fails or times out is ``None`` with the reason logged. OI-2203:
    the SHAs come validated and still stand after ``--end-of-options`` (a
    revision cannot follow ``--`` in ``git log``) and after ``--`` in the fetch.
    """
    deadline = time.monotonic() + _GIT_BUDGET
    log = ("log", "--format=%B%x00", "--end-of-options", f"{since_head}..{head_sha}", "--")
    proc = _git(repo, log, deadline)
    if proc is not None and proc.returncode != 0:
        if _git(repo, ("fetch", "--quiet", "--no-tags", "--", "origin", head_sha, since_head), deadline) is None:
            return None
        proc = _git(repo, log, deadline)
    if proc is None:
        return None
    if proc.returncode == 0:
        return proc.stdout
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
    nobody could read. Neither does a value that is no commit SHA
    (``[0-9a-f]{7,40}``, OI-2203): it never reaches git, the reason is logged.
    """
    if not (since_head and head_sha and dispatch_id) or head_sha == since_head:
        return False
    for name, value in (("the head the verdict decided on", since_head), ("the head", head_sha)):
        if not _SHA.fullmatch(value):
            logger.warning(
                "review_cap_verdicts: %s, %r, is no commit SHA: no git call, no granted round on it", name, value[:80],
            )
            return False
    text = _log_bodies(Path(repo), since_head, head_sha)
    marker = re.compile(rf"^Dispatch-ID:\s+{re.escape(dispatch_id)}\s*$", re.MULTILINE)
    return text is not None and marker.search(text) is not None


def resolved_carries(
    state_dir: Path, pr_number: int, head_sha: str, *, since: str = "", repo: Optional[Path] = None,
) -> CarriesFn:
    """The git facts a booking of ``head_sha`` can need, resolved now: before the writer takes the ledger lock.

    OI-2202. One fact per ``verkleinen`` verdict of the PR's current count
    (created at or after ``since``, OI-2207; one at most). The writer's grant
    check reads these under the round ledger's lock and runs no git there: a
    grant the facts do not cover (recorded while the writer waited for the
    lock) qualifies no head, logged. A store that cannot be read gives no
    facts; the check under the lock reads it again and grants nothing.
    """
    try:
        verdicts = read_verdicts(state_dir, pr_number)
    except CapVerdictError as exc:
        logger.warning("review_cap_verdicts: no git facts for PR #%s: %s", pr_number, exc)
        verdicts = ()
    known: Dict[Tuple[str, str, str], bool] = {}
    current = (verdict for verdict in verdicts if verdict.created_at >= since)
    for grant in filter(None, (verdict.grant() for verdict in current)):
        key = (grant.since_head, head_sha, grant.dispatch_id)
        known[key] = head_carries_dispatch(repo or _repo(), *key)

    def carries(since_head: str, head: str, dispatch_id: str) -> bool:
        if (since_head, head, dispatch_id) in known:
            return known[(since_head, head, dispatch_id)]
        logger.warning(
            "review_cap_verdicts: the round granted on %s to dispatch %s was not looked up before the ledger lock, "
            "and no git runs under it: no granted round on head %s", since_head[:12], dispatch_id, head[:12],
        )
        return False

    return carries


def _repo() -> Path:
    from vnx_paths import ensure_env

    return Path(ensure_env()["PROJECT_ROOT"])


def grant_or_none(
    state_dir: Path, cap: review_rounds.CapState, lines: Sequence[review_rounds.RoundLine], *,
    head_sha: str, since: str = "", repo: Optional[Path] = None, carries: Optional[CarriesFn] = None,
) -> Optional[review_rounds.Grant]:
    """The grant standing on this cap, qualified for ``head_sha`` when the head carries the dispatch.

    For the gate before a codex-seat run and the writer before a booking. A
    store that cannot be read grants nothing (logged): the cap stays closed.
    A round that is already used is returned unqualified; it is never granted
    to another head (``review_rounds.round_granted_past_cap``). ``carries``
    answers the git question; without it git is asked now (the gate, which
    holds no ledger lock). The writer passes :func:`resolved_carries`.
    """
    try:
        grant = standing_grant(cap, read_verdicts(state_dir, cap.pr_number), since=since)
    except CapVerdictError as exc:
        logger.warning("review_cap_verdicts: no granted round for PR #%s: %s", cap.pr_number, exc)
        return None
    if grant is None or review_rounds.consumed_by(grant, lines) is not None:
        return grant
    if carries is None:
        carried = head_carries_dispatch(repo or _repo(), grant.since_head, head_sha, grant.dispatch_id)
    else:
        carried = carries(grant.since_head, head_sha, grant.dispatch_id)
    return replace(grant, qualified_head=head_sha) if carried else grant


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
