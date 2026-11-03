"""The round ledger: one line per review verdict, per seat, per PR head (R1).

Track ``review-stopregel-en-lichte-stoelen``, plan section 1 ("Tellen en
weigeren"). B3.1 and B3.2 (``docs/core/DISPATCH_RULES.md`` §3) stood in the T0
role and nowhere in code. This module is what makes them countable and
enforceable:

- **The ledger.** ``review_gates/rounds.ndjson`` per project, beside
  ``results/``, append-only. One line per verdict (PASS or FAIL) of every seat,
  keyed by (``project_id``, ``pr_number``, ``head_sha``, ``seat``) per ADR-007.
  The result writers (``gate_recorder.write_result_guarded`` and
  ``record_terminal_result``) append the line BEFORE the result record, under
  the ledger lock, with fsync: when the line cannot be written, the run fails
  and no verdict is published. A run without a verdict (unavailable, quota,
  crash, ``chain_exhausted``, a partial review) writes no line, and neither
  does a rewrite of a record that already carries its verdict on its head (a
  takeover annotation): that is no new verdict. A re-anchor
  (``gate_reanchor_cli``) carries a counted verdict to a new head: it gets a
  line for that head that names the head it came from (``reanchored_from``).
- **The count.** A round is a verdict in the codex seat on its own head. The
  seat counts, not the vendor: a takeover in the codex seat counts as codex. A
  blocker of the second seat does not count (plan, open question 2), and a
  re-anchored line is no round: its verdict was counted on the head it came
  from. B3.1 (three or more blockers in one round) is checked first, then B3.2
  (the latest round has a blocker and the sum over all rounds is six or more,
  or -- since RG-1 -- the number of FAIL rounds on distinct heads reaches the
  project's round cap, ``VNX_REVIEW_ROUND_CAP``, default 3). The cap state
  names which threshold fired (``blockers`` or ``rounds``). The cap is sticky:
  it falls at the first round where either rule holds, and only R2's tiebreaker
  (:func:`round_granted_past_cap`) or ``--override-b3`` gets a codex-seat round
  past it. No fingerprint: a returning blocker counts again.
- **The granted round** (R2a, ADR-041). A ``verkleinen`` verdict in
  ``review_cap_verdicts`` grants one codex-seat round past the cap. The writer
  books the verdict that uses it with ``granted_by`` under the ledger lock, so
  the first one wins and the ledger says which line the door may count.
- **No second verdict on a head.** A key holds one counted verdict. The request
  side refuses a run in a seat that already has a verdict on the head
  (``gate_request_handler``); the writer refuses a second, different verdict on
  the same key as the backstop for every path that starts a run without a
  request.
- **A PR's history is recorded, not inferred.** A second line kind,
  ``{"kind": "opened", ...}``, says from where the ledger counts this PR:
  written by the gate for a PR that has no earlier review anywhere
  (``basis`` ``new``), and by the operator for one that has (``basis``
  ``attested``, ``vnx review rounds open --pr N --reason "..."``, with actor
  and reason). Nothing reconstructs earlier verdicts: after an attested line
  the cap starts at zero, and every reader (:meth:`Ledger.for_pr`,
  :meth:`Ledger.line_for`) leaves out what was booked for the PR before it.
  The gate refuses every seat of a PR that is not
  opened while any earlier review is found (:func:`prior_evidence`,
  ``round_history_missing``), and the merge door counts no PASS of a PR that
  is not opened. The writer books every verdict either way: no verdict is
  lost, the door decides whether the PR counts.
- **An override is recorded, not just logged.** A third line kind,
  ``{"kind": "override", ...}``, books a build-door ``--override-b3`` that let
  a build dispatch out on a capped PR's branch (:func:`book_b3_override`): the
  PR, the head the cap fell on, the reason, what carried it (the env var), and
  the cumulative blocker count. It is not a verdict and never counts toward the
  cap. The door refuses the override when the line cannot be written.
- **One file, framed records, and a bad line is a stop.** The ledger is a
  regular file or absent (:func:`ledger_state`); anything else is refused by
  every reader and writer. Every append starts on a record boundary
  (:func:`_append_text`), so a torn line of one writer never swallows the next
  record. A line that does not parse refuses every PR it can touch (the PR it
  names, every PR when it names none) until the operator sets the file aside.
  Nothing removes, moves or rewrites a line.

- **The counts per verdict (OI-2476, track tweede-stoel-op-signaal).** Every
  verdict line also carries ``warning_count`` and ``prescan_notice_count``,
  read from that verdict's own report (``review_report_counts``) and never from
  an earlier run on the PR. The field is absent when the count is unknown -- an
  old line, a missing or unreadable report, or a takeover -- and absent is
  never read as zero.

Readers: ``gate_request_handler`` (refusal where a run starts),
``review_seat_evaluation`` (the merge door, ``vnx-gate/review``,
``vnx pr-ready``), ``dispatch_cli`` (the early warning for a build on a capped
PR's branch), and ``review_rounds_cli`` (``vnx review rounds open``).

BILLING SAFETY: no Anthropic SDK, no network, no subprocess in this module.
The writer's grant check (``review_cap_verdicts``) reads commit messages with
``git``, before it takes the ledger lock: no git runs under it (OI-2202).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from review_report_counts import counts_for_payload

logger = logging.getLogger(__name__)

LEDGER_NAME = "rounds.ndjson"
CODEX_SEAT = "codex_gate"

B3_1 = "B3.1"
B3_2 = "B3.2"
B3_1_ROUND_BLOCKERS = 3
B3_2_TOTAL_BLOCKERS = 6

# The round cap (RG-1, ADR-041 amendment R1e): B3.2 also caps a PR once this
# many codex FAIL rounds fell on distinct heads since the PR's last `opened`
# line. Per project: `VNX_REVIEW_ROUND_CAP` through config_runtime, default 3.
ROUND_CAP_KEY = "VNX_REVIEW_ROUND_CAP"
ROUND_CAP_DEFAULT = 3
# Which of B3.2's two thresholds fired: the blocker sum, or the round count.
THRESHOLD_BLOCKERS = "blockers"
THRESHOLD_ROUNDS = "rounds"

OVERRIDE_FLAG = "--override-b3"
OVERRIDE_USAGE = '--override-b3 --reason "..."'
# What carried the reason: the documented flag, or the env carrier RG-5 adds
# next to it (``VNX_OVERRIDE_B3=1`` + ``VNX_OVERRIDE_B3_REASON``).
OVERRIDE_CARRIER_FLAG = "flag"
OVERRIDE_CARRIER_ENV = "env"
OPEN_COMMAND = 'vnx review rounds open --pr {pr} --reason "..."'

SOURCE_GATE = "gate"
SOURCE_OPERATOR = "operator"

# The ``opened`` line: from here on the ledger holds this PR's review history.
KIND_OPENED = "opened"
BASIS_NEW = "new"
BASIS_ATTESTED = "attested"
_BASES = (BASIS_NEW, BASIS_ATTESTED)

# The ``override`` line: a build-door ``--override-b3`` let a build dispatch out
# on the branch of a PR at the B3 cap (R1d, OI-2180 point 3).
KIND_OVERRIDE = "override"

# An evidence entry for a source that exists but cannot be read (prior_evidence).
UNREADABLE_PREFIX = "unreadable:"

# Request-time refusal reasons (gate_request_handler books them on the seat's payload).
REFUSED_VERDICT_ON_HEAD = "verdict_on_head"
REFUSED_B3_CAP = "b3_cap"
REFUSED_LEDGER_UNREADABLE = "round_ledger_unreadable"
REFUSED_HISTORY_MISSING = "round_history_missing"
# Write-time only: a re-anchor of a verdict the ledger does not count.
REFUSED_REANCHOR_UNCOUNTED = "reanchor_origin_uncounted"

EVIDENCE_REANCHORED = "reanchored"

VERDICT_FAIL = "fail"
_VERDICTS = ("pass", VERDICT_FAIL)
_STRING_FIELDS = (
    "project_id", "head_sha", "seat", "reviewer", "takeover_from", "verdict",
    "report_path", "dispatch_id", "recorded_at", "branch", "source", "reanchored_from", "granted_by",
)


class RoundLedgerError(OSError):
    """The ledger could not be read or written: no verdict is published on it."""


class RoundLedgerUnreadable(RoundLedgerError):
    """A line of the ledger does not parse; every PR it may belong to is refused until the file is set aside."""


class RoundRefusal(ValueError):
    """A verdict the ledger may not take: the key already holds another one, the cap holds, or a re-anchor carries nothing counted."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class StoreConflict(ValueError):
    """``--data-dir`` and ``VNX_STATE_DIR`` name two different round ledgers: the command cannot tell which one is meant."""


class RoundCapError(ValueError):
    """``VNX_REVIEW_ROUND_CAP`` is not a positive integer: the round cap cannot be read.

    The cap never falls back to the default on a malformed value: a silent 3
    would cap (or not cap) on a number nobody chose.
    """


def open_command(pr_number: Any) -> str:
    return OPEN_COMMAND.format(pr=pr_number)


def ledger_way_out(path: Path) -> str:
    """The one way out of an unreadable ledger, as every refusal names it."""
    return f"set {path} aside, then reopen every open PR with `{open_command('N')}`"


# ---------------------------------------------------------------------------
# Paths and identity
# ---------------------------------------------------------------------------


def ledger_path(review_gates_dir: Path) -> Path:
    """``review_gates/rounds.ndjson``: the ledger beside ``results/`` and ``requests/``."""
    return Path(review_gates_dir) / LEDGER_NAME


def ledger_path_for_results(results_dir: Path) -> Optional[Path]:
    """The ledger of a ``review_gates/results`` dir, or ``None`` outside that layout.

    The ledger lives in the canonical store layout only. A results dir that is
    not ``.../review_gates/results`` (an ad-hoc dir a script or a test writes
    into) has no ledger, and the merge door never reads one there either.
    """
    results_dir = Path(results_dir)
    if results_dir.name != "results" or results_dir.parent.name != "review_gates":
        return None
    return ledger_path(results_dir.parent)


def ledger_state_dir(path: Path) -> Optional[Path]:
    """The state dir a ledger path lives in (``<state>/review_gates/rounds.ndjson``), else ``None``.

    The writer's cap decision resolves the project's round cap from this store
    (RG-1 round 2), so it is the same state dir the result store's layout names,
    never the environment's. A path outside that layout names no store.
    """
    path = Path(path)
    if path.name != LEDGER_NAME or path.parent.name != "review_gates":
        return None
    return path.parent.parent


def store_state_dir(data_dir: Optional[Path] = None) -> Path:
    """The state dir the round ledger lives in, resolved the way the gate does.

    Without a data dir: ``ensure_env()['VNX_STATE_DIR']``, the same resolution
    :class:`ReviewGateManager` and the merge door use for ``state/review_gates``.
    The state dir may sit outside ``<VNX_DATA_DIR>/state`` (``VNX_STATE_DIR``),
    so it is never reconstructed from the data dir.

    With a data dir (``vnx review rounds open --data-dir``): ``<data_dir>/state``,
    unless ``VNX_STATE_DIR`` overrides the state dir with another one. Then the
    command has two ledgers: the gate of this environment reads the one
    ``VNX_STATE_DIR`` names, ``--data-dir`` names the other. An ``opened`` line
    in the wrong one is read by nobody or opens a PR of another store, and no
    line is ever removed, so :class:`StoreConflict` names both and nothing is
    written. ``VNX_STATE_DIR`` equal to ``<VNX_DATA_DIR>/state`` is no override:
    ``bin/vnx`` exports exactly that for the project it runs in, and it says
    nothing about the store ``--data-dir`` names.
    """
    if data_dir is None:
        from vnx_paths import ensure_env

        return Path(ensure_env()["VNX_STATE_DIR"])
    named = Path(data_dir).expanduser() / "state"
    override = _state_dir_override()
    if override is None or _same_dir(override, named):
        return named
    raise StoreConflict(
        f"--data-dir names the round ledger under {named}, but VNX_STATE_DIR names another state dir, "
        f"{override}, and the gate of this environment reads the ledger there. Drop --data-dir to open the "
        f"PR under {override}, or unset VNX_STATE_DIR to open it under {named}"
    )


def _state_dir_override() -> Optional[Path]:
    """``VNX_STATE_DIR`` when it departs from ``<VNX_DATA_DIR>/state``, else ``None``."""
    raw = (os.environ.get("VNX_STATE_DIR") or "").strip()
    if not raw:
        return None
    state = Path(raw).expanduser()
    data = (os.environ.get("VNX_DATA_DIR") or "").strip()
    if data and _same_dir(state, Path(data).expanduser() / "state"):
        return None
    return state


def _same_dir(left: Path, right: Path) -> bool:
    return os.path.realpath(left) == os.path.realpath(right)


def store_data_dir() -> Path:
    """The data dir (``ensure_env()['VNX_DATA_DIR']``) whose reports :func:`prior_evidence` reads for the gate.

    Resolved the way the gate does, never by walking up from a state dir: the
    state dir can sit outside ``<VNX_DATA_DIR>/state`` (``VNX_STATE_DIR``), and
    walking up from it would land beside it. The state dir itself is the
    caller's to name (``prior_evidence(..., state_dir=)``).
    """
    from vnx_paths import ensure_env

    return Path(ensure_env()["VNX_DATA_DIR"])


_FILE_KINDS = (
    (stat.S_ISDIR, "directory"), (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
    (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "character device"), (stat.S_ISBLK, "block device"),
)


def ledger_state(path: Path) -> str:
    """``"absent"`` or ``"file"``: the one answer every reader and writer gives for the ledger path.

    ``os.lstat``, so a symlink is never followed. Anything that is not a
    regular file (a directory, a symlink, a fifo), and a path that cannot be
    inspected, raises :class:`RoundLedgerError`: an existing ledger nobody can
    read is never taken for an absent one, which would drop the count.
    """
    path = Path(path)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise RoundLedgerError(f"round ledger {path} cannot be inspected ({exc}): {ledger_way_out(path)}") from exc
    if stat.S_ISREG(mode):
        return "file"
    kind = next((name for test, name in _FILE_KINDS if test(mode)), "unknown file type")
    raise RoundLedgerError(f"round ledger {path} is not a regular file ({kind}): {ledger_way_out(path)}")


def store_project_id(review_gates_dir: Path) -> str:
    """The project the store names itself as (ADR-007): ``""`` when it names none.

    Read from the state dir the ledger lives in, never from the environment, so
    the writer and every reader of one store derive the same key.
    """
    import vnx_paths

    return vnx_paths.project_id_from_state_dir(Path(review_gates_dir).parent) or ""


def ledger_seat_gates() -> frozenset:
    """The gates that can sit in a review seat: the review peers, minus the optional ones."""
    from review_seats import OPTIONAL_REVIEW_GATES, review_peer_gates

    return frozenset(review_peer_gates()) - OPTIONAL_REVIEW_GATES


# ---------------------------------------------------------------------------
# One line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundLine:
    """One verdict in one seat on one head of one PR."""

    project_id: str
    pr_number: int
    head_sha: str
    seat: str
    reviewer: str
    takeover_from: str
    verdict: str
    blocking_count: int
    report_path: str
    dispatch_id: str
    recorded_at: str
    branch: str = ""
    source: str = SOURCE_GATE
    override: Optional[Dict[str, Any]] = None
    # A re-anchored verdict: the head it was given on, where it was counted.
    reanchored_from: str = ""
    # The extra round past the cap this verdict consumed: the id of the
    # ``verkleinen`` verdict in ``review_cap_verdicts`` (R2a). Stamped by the
    # writer under the ledger lock, never taken from a payload.
    granted_by: str = ""
    # OI-2476: counted from this verdict's own report (``report_path``). ``None``
    # is unknown -- the field is then not written at all, and an old line
    # without it reads the same way. Unknown is never zero.
    warning_count: Optional[int] = None
    prescan_notice_count: Optional[int] = None

    @property
    def key(self) -> Tuple[str, int, str, str]:
        return (self.project_id, self.pr_number, self.head_sha, self.seat)

    @property
    def report_name(self) -> str:
        return Path(self.report_path).name if self.report_path else ""

    def same_verdict(self, other: "RoundLine") -> bool:
        """The same verdict booked twice (a retried write), not a second run."""
        return (
            self.key == other.key and self.verdict == other.verdict
            and self.reviewer == other.reviewer and self.report_name == other.report_name
        )

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "project_id": self.project_id, "pr_number": self.pr_number,
            "head_sha": self.head_sha, "seat": self.seat, "reviewer": self.reviewer,
            "takeover_from": self.takeover_from, "verdict": self.verdict,
            "blocking_count": self.blocking_count, "report_path": self.report_path,
            "dispatch_id": self.dispatch_id, "recorded_at": self.recorded_at,
            "branch": self.branch, "source": self.source,
        }
        if self.override:
            record["override"] = dict(self.override)
        if self.reanchored_from:
            record["reanchored_from"] = self.reanchored_from
        if self.granted_by:
            record["granted_by"] = self.granted_by
        if self.warning_count is not None:
            record["warning_count"] = self.warning_count
        if self.prescan_notice_count is not None:
            record["prescan_notice_count"] = self.prescan_notice_count
        return record

    @classmethod
    def from_dict(cls, data: Any) -> "RoundLine":
        """Strict: a line that misses a field or carries a wrong type is not a line."""
        if not isinstance(data, dict):
            raise ValueError("a ledger line is a JSON object")
        values: Dict[str, Any] = {}
        for name in _STRING_FIELDS:
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"field {name!r} is not a string")
            values[name] = value
        pr_number, blocking = data.get("pr_number"), data.get("blocking_count")
        if not _is_int(pr_number) or not _is_int(blocking) or blocking < 0:
            raise ValueError("pr_number and blocking_count must be non-negative integers")
        if values["verdict"] not in _VERDICTS or not values["head_sha"] or not values["seat"]:
            raise ValueError("verdict must be pass or fail, head_sha and seat must be set")
        override = data.get("override")
        if override is not None and not isinstance(override, dict):
            raise ValueError("override must be an object")
        warning = _optional_count(data, "warning_count")
        notices = _optional_count(data, "prescan_notice_count")
        return cls(
            pr_number=pr_number, blocking_count=blocking, override=override,
            warning_count=warning, prescan_notice_count=notices, **values,
        )


@dataclass(frozen=True)
class OpenedLine:
    """From here on the ledger holds this PR's review history (``kind`` ``opened``).

    ``basis`` ``new``: the gate found no earlier review of the PR.
    ``basis`` ``attested``: the operator opened a PR with earlier reviews
    (``vnx review rounds open``), naming ``actor`` and ``reason``; what came
    before is not counted. ``evidence`` is the number of earlier-review
    entries :func:`prior_evidence` found when the line was written.
    """

    project_id: str
    pr_number: int
    basis: str
    evidence: int
    recorded_at: str
    source: str
    actor: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "kind": KIND_OPENED, "project_id": self.project_id, "pr_number": self.pr_number,
            "basis": self.basis, "evidence": self.evidence, "recorded_at": self.recorded_at,
            "source": self.source,
        }
        if self.basis == BASIS_ATTESTED:
            record.update(actor=self.actor, reason=self.reason)
        return record

    @classmethod
    def from_dict(cls, data: Any) -> "OpenedLine":
        """As strict as a verdict line: a missing or wrong field is no line, and so is an attested line without actor or reason."""
        if not isinstance(data, dict):
            raise ValueError("a ledger line is a JSON object")
        values: Dict[str, Any] = {}
        for name in ("project_id", "basis", "recorded_at", "source", "actor", "reason"):
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"field {name!r} is not a string")
            values[name] = value
        pr_number, evidence = data.get("pr_number"), data.get("evidence")
        if not _is_int(pr_number) or not _is_int(evidence) or pr_number < 0 or evidence < 0:
            raise ValueError("pr_number and evidence must be non-negative integers")
        if values["basis"] not in _BASES or not values["recorded_at"] or not values["source"]:
            raise ValueError("basis must be new or attested, recorded_at and source must be set")
        if values["basis"] == BASIS_ATTESTED and not (values["actor"].strip() and values["reason"].strip()):
            raise ValueError("an attested line names its actor and its reason")
        return cls(pr_number=pr_number, evidence=evidence, **values)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_count(data: Dict[str, Any], name: str) -> Optional[int]:
    """An optional count field: absent is unknown (``None``), present must be a non-negative int.

    OI-2476: unknown is represented by the field's absence, so an old line
    reads exactly like a new line whose report could not be read. A present
    but malformed value is no count at all: the line does not parse. A silent
    ``0`` there would be the very "unknown read as clean" this field exists to
    prevent.
    """
    value = data.get(name)
    if value is None:
        return None
    if not _is_int(value) or value < 0:
        raise ValueError(f"field {name!r} must be a non-negative integer when present")
    return value


@dataclass(frozen=True)
class OverrideLine:
    """A build-door ``--override-b3``: a build dispatch went out on a PR at the B3 cap anyway.

    ``set_by`` names what carried the reason (the env var ``VNX_OVERRIDE_B3_REASON``),
    ``total`` the cumulative codex-seat blocker count the cap stood at, and
    ``head_sha`` the head the cap fell on. Not a verdict: it never counts toward
    the cap, and it is not subject to the ``opened`` boundary.
    """

    project_id: str
    pr_number: int
    head_sha: str
    reason: str
    set_by: str
    total: int
    source: str = SOURCE_OPERATOR
    rule: str = ""
    recorded_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "kind": KIND_OVERRIDE, "project_id": self.project_id, "pr_number": self.pr_number,
            "head_sha": self.head_sha, "reason": self.reason, "set_by": self.set_by,
            "total": self.total, "source": self.source, "recorded_at": self.recorded_at,
        }
        if self.rule:
            record["rule"] = self.rule
        return record

    @classmethod
    def from_dict(cls, data: Any) -> "OverrideLine":
        """As strict as a verdict line: a missing or wrong field is no line, and so is an override without its reason, head or setter."""
        if not isinstance(data, dict):
            raise ValueError("a ledger line is a JSON object")
        values: Dict[str, Any] = {}
        for name in ("project_id", "head_sha", "reason", "set_by", "source", "recorded_at", "rule"):
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"field {name!r} is not a string")
            values[name] = value
        pr_number, total = data.get("pr_number"), data.get("total")
        if not _is_int(pr_number) or not _is_int(total) or pr_number < 0 or total < 0:
            raise ValueError("pr_number and total must be non-negative integers")
        if not values["reason"].strip() or not values["head_sha"] or not values["set_by"] or not values["recorded_at"]:
            raise ValueError("reason, head_sha, set_by and recorded_at must be set")
        return cls(pr_number=pr_number, total=total, **values)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Reading the ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BadLine:
    """A line that does not parse, and the PR it names when that can still be read off it."""

    number: int
    pr_number: Optional[int]
    error: str


@dataclass(frozen=True)
class Ledger:
    """The ledger as read: its verdict lines and ``opened`` lines in file order, and every line that did not parse.

    ``opened_after`` holds, per ``opened`` line, how many verdict lines came
    before it in the file. Appends are serialized under the ledger lock, so
    file order is booking order.
    """

    path: Path
    exists: bool
    lines: Tuple[RoundLine, ...] = ()
    bad: Tuple[BadLine, ...] = ()
    opened: Tuple[OpenedLine, ...] = ()
    opened_after: Tuple[int, ...] = ()
    overrides: Tuple[OverrideLine, ...] = ()

    def opened_line(self, project_id: str, pr_number: int) -> Optional[OpenedLine]:
        """This PR's ``opened`` line: from there on the ledger holds its review history."""
        return next(
            (line for line in self.opened if line.project_id == project_id and line.pr_number == pr_number), None,
        )

    def is_opened(self, project_id: str, pr_number: int) -> bool:
        return self.opened_line(project_id, pr_number) is not None

    def counted_from(self, project_id: str, pr_number: int) -> int:
        """The first verdict line (an index into ``lines``) that counts for this PR.

        An attested ``opened`` line is the boundary (plan section 1): what was
        booked for the PR before ``vnx review rounds open`` counts for nothing,
        no blocker, no FAIL and no PASS, and the cap starts there at zero. A
        ``new`` line has nothing before it, and a PR that is not opened is
        counted whole: the gate and the door refuse it on its own grounds.
        The last attested line is the boundary: a release of an interrupted
        publication (:func:`open_pr` with ``results_dir``) is a second one.
        """
        return self._boundary(project_id, pr_number)[0]

    def since(self, project_id: str, pr_number: int) -> str:
        """When this PR's count starts: the boundary line's ``recorded_at``, ``""`` without an attested line.

        A tiebreaker verdict recorded before it belongs to a count that no
        longer stands (``review_cap_verdicts``).
        """
        return self._boundary(project_id, pr_number)[1]

    def _boundary(self, project_id: str, pr_number: int) -> Tuple[int, str]:
        found = (0, "")
        for line, after in zip(self.opened, self.opened_after):
            if line.project_id == project_id and line.pr_number == pr_number and line.basis == BASIS_ATTESTED:
                found = (after, line.recorded_at)
        return found

    def for_pr(self, project_id: str, pr_number: int) -> Tuple[RoundLine, ...]:
        """This PR's counted lines in verdict order (``recorded_at``, then file order).

        Every reader of the count goes through here (the gate's cap and
        standing-verdict checks, the writer's cap check, the merge door, the
        dispatch door), so they all draw the boundary of :meth:`counted_from`.
        """
        start = self.counted_from(project_id, pr_number)
        found = [
            (line.recorded_at, index, line) for index, line in enumerate(self.lines)
            if index >= start and line.project_id == project_id and line.pr_number == pr_number
        ]
        return tuple(line for _at, _index, line in sorted(found, key=lambda item: item[:2]))

    def line_for(self, key: Tuple[str, int, str, str]) -> Optional[RoundLine]:
        """The counted verdict on ``key``: a line before the PR's boundary holds no key."""
        start = self.counted_from(key[0], key[1])
        return next((line for line in self.lines[start:] if line.key == key), None)

    def booked(self, line: RoundLine) -> bool:
        """Is this very verdict in the file already, before or after the boundary (a retried write)?"""
        return any(existing.same_verdict(line) for existing in self.lines)

    def overrides_for(self, project_id: str, pr_number: int) -> Tuple[OverrideLine, ...]:
        """This PR's build-door override lines, in file order. Not counted, so no ``opened`` boundary."""
        return tuple(
            line for line in self.overrides if line.project_id == project_id and line.pr_number == pr_number
        )


def parse_ledger(path: Path, text: str) -> Ledger:
    """Every line by its ``kind``: none is a verdict, ``opened`` an opened PR, ``override`` a build-door override, anything else a bad line."""
    lines: List[RoundLine] = []
    opened: List[OpenedLine] = []
    opened_after: List[int] = []
    overrides: List[OverrideLine] = []
    bad: List[BadLine] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        data: Any = None
        try:
            data = json.loads(raw)
            kind = data.get("kind") if isinstance(data, dict) else None
            if kind == KIND_OPENED:
                opened.append(OpenedLine.from_dict(data))
                opened_after.append(len(lines))
            elif kind == KIND_OVERRIDE:
                overrides.append(OverrideLine.from_dict(data))
            elif kind is None:
                lines.append(RoundLine.from_dict(data))
            else:
                raise ValueError(f"unknown line kind {kind!r}")
        except ValueError as exc:
            bad.append(BadLine(number, _pr_named(data, raw), str(exc)))
    return Ledger(Path(path), True, tuple(lines), tuple(bad), tuple(opened), tuple(opened_after), tuple(overrides))


_PR_NUMBER_IN_TEXT = re.compile(r'"pr_number"\s*:\s*(\d+)\s*[,}]')


def _pr_named(data: Any, raw: str) -> Optional[int]:
    """The PR a bad line names, when it still names one; ``None`` refuses every PR.

    A torn append still carries its PR near the start (``to_dict`` writes
    ``pr_number`` second), so a cut line refuses its own PR, not every PR.
    """
    if isinstance(data, dict) and _is_int(data.get("pr_number")):
        return data["pr_number"]
    match = _PR_NUMBER_IN_TEXT.search(raw)
    return int(match.group(1)) if match else None


def read_ledger(path: Path) -> Ledger:
    """The ledger at ``path``, read under its lock. Absent is a ledger that does not exist yet.

    Raises :class:`RoundLedgerError` when the path is not a regular file
    (:func:`ledger_state`) or the file cannot be read.
    """
    from atomic_io import slot_lock

    path = Path(path)
    if ledger_state(path) == "absent":
        return Ledger(path, False)
    try:
        with slot_lock(path):
            text = _ledger_text(path)
    except RoundLedgerError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be read ({exc}): {ledger_way_out(path)}") from exc
    return parse_ledger(path, text)


def _ledger_text(path: Path) -> str:
    """The ledger's text, ``""`` when it is absent; the caller holds the lock. A non-file raises."""
    return path.read_text(encoding="utf-8") if ledger_state(path) == "file" else ""


def unreadable_text(ledger: Ledger, pr_number: int) -> str:
    """Why this PR's rounds cannot be counted, naming the way out; ``""`` when they can.

    A bad line touches the PR it names and, when it names none, every PR.
    """
    bad = [item for item in ledger.bad if item.pr_number in (None, pr_number)]
    if not bad:
        return ""
    where = ", ".join(f"line {item.number} ({item.error})" for item in bad[:3])
    return (
        f"the round ledger {ledger.path} is unreadable for PR #{pr_number}: {where}. "
        f"No line is ever removed or rewritten: {ledger_way_out(ledger.path)}"
    )


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------


def _cap_error(value: Optional[str]) -> RoundCapError:
    return RoundCapError(
        f"{ROUND_CAP_KEY} must be a positive integer, got {value!r}; "
        f"unset it for the default {ROUND_CAP_DEFAULT}"
    )


def parse_round_cap(value: Optional[str]) -> int:
    """The round cap as a positive integer; a malformed value raises :class:`RoundCapError`.

    No silent default: an empty, zero, negative or non-integer value is a
    setting nobody chose. The same contract the plan gate's ``max_rounds`` has.
    """
    text = (value or "").strip()
    try:
        cap = int(text)
    except ValueError:
        raise _cap_error(value) from None
    if not text or cap < 1:
        raise _cap_error(value)
    return cap


def round_cap(project_id: Optional[str] = None, state_dir: Optional[Path] = None) -> int:
    """The configured round cap for the project store the caller names.

    ``state_dir`` and ``project_id`` are the round ledger's own store and
    project (:func:`store_project_id`): a ledger read from another project's
    state dir (``cap-verdict --data-dir``, a merge door on another store) is
    judged against THAT project's threshold, never the launching process's.
    With a state dir named, the DB source is read from its
    ``runtime_coordination.db`` (``config_registry.get_for_project``), so the
    process's own wiring does not leak in. Without either, the process's
    configuration resolves, the behaviour for a caller with no ledger in hand.

    The chain: ``VNX_OVERRIDE_REVIEW_ROUND_CAP`` (the rollback brake) > the
    project's ``project_config`` row > ``VNX_REVIEW_ROUND_CAP`` > default ``3``.
    A malformed value raises; it never falls back to the default. The imports
    are local so this module needs no config layer to be imported.
    """
    if state_dir is not None:
        import config_registry

        pid = project_id or _state_dir_project_id(state_dir)
        raw = config_registry.get_for_project(ROUND_CAP_KEY, pid, state_dir)
    elif project_id is not None:
        import config_registry

        raw = config_registry.get(ROUND_CAP_KEY, project_id)
    else:
        import config_runtime

        raw = config_runtime.get(ROUND_CAP_KEY)
    return parse_round_cap(raw)


def _state_dir_project_id(state_dir: Path) -> str:
    """The project a state dir names itself as (ADR-007), ``""`` when it names none."""
    import vnx_paths

    return vnx_paths.project_id_from_state_dir(Path(state_dir)) or ""


@dataclass(frozen=True)
class CapState:
    """The codex-seat rounds of one PR and whether, and where, B3 put it at the cap.

    ``threshold`` is which threshold fired: ``blockers`` (B3.1 or B3.2's six-blocker
    sum) or ``rounds`` (the round cap, RG-1); ``round_limit`` is that cap.
    """

    pr_number: int
    rounds: Tuple[RoundLine, ...] = ()
    rule: str = ""
    cap_round: int = 0
    threshold: str = ""
    round_limit: int = 0

    @property
    def capped(self) -> bool:
        return bool(self.rule)

    @property
    def blockers(self) -> Tuple[int, ...]:
        return tuple(line.blocking_count for line in self.rounds)

    @property
    def total(self) -> int:
        return sum(self.blockers)

    @property
    def head(self) -> str:
        """The head the cap fell on; ``""`` below the cap (there is no cap head)."""
        if not self.capped:
            return ""
        return self.rounds[self.cap_round - 1].head_sha

    def summary(self) -> str:
        """The rule, which threshold fired, the round it fell in and the counts it fell on."""
        if not self.capped:
            return (
                f"PR #{self.pr_number} is below the cap: {len(self.rounds)} codex-seat round(s), "
                f"blockers {_counts(self.blockers)}, sum {self.total}"
            )
        at_cap = self.blockers[: self.cap_round]
        line = self.rounds[self.cap_round - 1]
        if self.rule == B3_1:
            why = (
                f"round {self.cap_round} had {at_cap[-1]} blockers (B3.1: three or more in one round; "
                f"threshold: {self.threshold})"
            )
        elif self.threshold == THRESHOLD_ROUNDS:
            failed = sum(1 for item in self.rounds[: self.cap_round] if item.verdict == VERDICT_FAIL)
            why = (
                f"round {self.cap_round} is codex FAIL round {failed} and the round cap is "
                f"{self.round_limit} (B3.2: the round cap; threshold: {self.threshold})"
            )
        else:
            why = (
                f"round {self.cap_round} had {at_cap[-1]} blocker(s) and the sum over "
                f"{self.cap_round} rounds is {sum(at_cap)} (B3.2: six or more, latest round blocking; "
                f"threshold: {self.threshold})"
            )
        return (
            f"PR #{self.pr_number} is at the cap under {self.rule}: {why}; codex-seat blockers per "
            f"round {_counts(self.blockers)} on {len(self.rounds)} head(s), cap fell on head "
            f"{line.head_sha[:12]}"
        )

    def to_record(self) -> Dict[str, Any]:
        return {
            "rule": self.rule, "cap_round": self.cap_round, "rounds": len(self.rounds),
            "blockers": list(self.blockers), "total": self.total,
            "threshold": self.threshold, "round_cap": self.round_limit,
        }


def _counts(blockers: Sequence[int]) -> str:
    return "(" + ", ".join(str(count) for count in blockers) + ")" if blockers else "(none)"


def codex_rounds(lines: Iterable[RoundLine]) -> Tuple[RoundLine, ...]:
    """The rounds: codex-seat verdicts in verdict order, one per head (the first).

    A re-anchored line is no round: it is the verdict already counted on the
    head it came from, carried to an identical diff on a new head.
    """
    seen: set = set()
    rounds: List[RoundLine] = []
    for line in lines:
        if line.seat != CODEX_SEAT or line.reanchored_from or line.head_sha in seen:
            continue
        seen.add(line.head_sha)
        rounds.append(line)
    return tuple(rounds)


def cap_state(
    lines: Iterable[RoundLine], pr_number: int, *, project_id: Optional[str] = None,
    state_dir: Optional[Path] = None,
) -> CapState:
    """Walk the rounds in order; the first round where B3.1, else B3.2, holds is the cap.

    ``lines`` are one PR's lines in verdict order (:meth:`Ledger.for_pr`), so the
    last attested ``opened`` line already reset the count. B3.2 has two thresholds
    and either caps: the six-blocker sum, and since RG-1 the codex FAIL rounds on
    distinct heads reaching :func:`round_cap`. The blocker thresholds are checked
    first, so a round satisfying both is named ``blockers``. A malformed cap
    raises :class:`RoundCapError`; it never quietly counts against 3.

    ``project_id`` and ``state_dir`` are the ledger's project and store: every
    caller that read a ledger passes them, so the round cap is that project's
    threshold, not the launching process's (RG-1 round 2).
    """
    rounds = codex_rounds(lines)
    limit = round_cap(project_id=project_id, state_dir=state_dir)
    total = 0
    failed = 0
    for index, line in enumerate(rounds, start=1):
        total += line.blocking_count
        if line.verdict == VERDICT_FAIL:
            failed += 1
        if line.blocking_count >= B3_1_ROUND_BLOCKERS:
            return CapState(pr_number, rounds, B3_1, index, THRESHOLD_BLOCKERS, limit)
        if line.blocking_count >= 1 and total >= B3_2_TOTAL_BLOCKERS:
            return CapState(pr_number, rounds, B3_2, index, THRESHOLD_BLOCKERS, limit)
        if failed >= limit:
            return CapState(pr_number, rounds, B3_2, index, THRESHOLD_ROUNDS, limit)
    return CapState(pr_number, rounds, round_limit=limit)


@dataclass(frozen=True)
class Grant:
    """The one extra codex-seat round a ``verkleinen`` verdict granted past the cap (R2a, ADR-041).

    ``verdict_id`` names the verdict in ``review_cap_verdicts``, ``dispatch_id``
    the dispatch that carries out its one intervention, ``since_head`` the head
    the verdict decided on. ``qualified_head`` is the head the caller found to
    carry a commit of that dispatch after ``since_head`` (``""``: none checked).
    """

    verdict_id: str
    dispatch_id: str
    since_head: str
    qualified_head: str = ""


def consumed_by(grant: Grant, lines: Iterable[RoundLine]) -> Optional[RoundLine]:
    """The codex-seat verdict that used up the extra round, or ``None`` while it is open.

    Only a codex-seat verdict consumes it (plan, state contract rule 3): a
    verdict of the second seat on the same head never does.
    """
    return next((line for line in lines if line.seat == CODEX_SEAT and line.granted_by == grant.verdict_id), None)


def round_granted_past_cap(
    cap: CapState, *, head_sha: str, grant: Optional[Grant] = None, lines: Iterable[RoundLine] = (),
) -> bool:
    """Has a tiebreaker granted the codex seat one round on ``head_sha`` past the cap?

    Plan section 2: a ``verkleinen`` verdict grants one extra round, bound to
    the dispatch that carries out the named intervention, consumed by the first
    codex-seat verdict on a head with a commit of that dispatch. ``lines`` are
    the PR's counted lines. While the round is open, it is granted on the head
    the caller qualified (the gate before a run, the writer before a booking);
    once consumed, only on the head of the verdict that consumed it (the merge
    door counting that verdict). Without a grant nothing is granted: past the
    cap only ``--override-b3`` lets a codex-seat round run or count.
    """
    if grant is None or not cap.capped or not head_sha:
        return False
    used = consumed_by(grant, lines)
    if used is not None:
        return used.head_sha == head_sha
    return grant.qualified_head == head_sha


CAP_VERDICT_COMMAND = "vnx review cap-verdict --pr {pr} --for-dispatch <dispatch-id>"


def cap_refusal_text(cap: CapState, *, where: str) -> str:
    """The refusal a capped PR gets, naming the rule, the threshold, the counts, the tiebreaker and the override."""
    return (
        f"{where}: {cap.summary()}. The cap fired on the `{cap.threshold}` threshold; every codex-seat run "
        f"on this PR is refused on every head, except the one round a tiebreaker verdict grants: "
        f"`{CAP_VERDICT_COMMAND.format(pr=cap.pr_number)}`. To go on "
        f"anyway, pass {OVERRIDE_USAGE}: the override wins over the cap and is recorded with its reason"
    )


def override_record(reason: str, cap: CapState, *, carrier: str = OVERRIDE_CARRIER_FLAG) -> Dict[str, Any]:
    """What an applied ``--override-b3`` records: the carrier, the reason and the cap it won over.

    ``carrier`` names what carried the reason — ``flag`` for the documented
    ``--override-b3`` and ``env`` for the ``VNX_OVERRIDE_B3`` pair — so an
    operator reading the record can tell the two doors apart.
    """
    return {
        "flag": OVERRIDE_FLAG, "carrier": carrier, "reason": reason, **cap.to_record(),
    }


def book_b3_override(path: Path, *, project_id: str, cap: CapState, reason: str, set_by: str) -> str:
    """Book one build-door ``--override-b3``: an ``override`` line for the PR the cap names.

    ``set_by`` is what carried the reason (the env var ``VNX_OVERRIDE_B3_REASON``);
    the line names the head the cap fell on (``cap.head``) and the cumulative
    blocker count (``cap.total``). Same lock, same framing, same validation and
    same refusal contract as a verdict or ``opened`` line: an unreadable ledger
    raises :class:`RoundLedgerUnreadable`, any other failure to write the line
    :class:`RoundLedgerError`. The caller refuses the override on either.
    """
    from atomic_io import slot_lock

    line = OverrideLine(
        project_id=project_id, pr_number=cap.pr_number, head_sha=cap.head,
        reason=reason, set_by=set_by, total=cap.total, source=SOURCE_OPERATOR,
        rule=cap.rule, recorded_at=_utc_now(),
    )
    path = Path(path)
    try:
        with slot_lock(path):
            ledger = parse_ledger(path, _ledger_text(path))
            text = unreadable_text(ledger, line.pr_number)
            if text:
                raise RoundLedgerUnreadable(text)
            _append_text(path, ndjson(line.to_dict()))
    except RoundLedgerError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be written: {exc}") from exc
    return "appended"


# ---------------------------------------------------------------------------
# Writing a verdict
# ---------------------------------------------------------------------------


#: Resolves the grant standing on a capped PR for the line being booked: called
#: under the ledger lock with the PR's cap, its counted lines and ``Ledger.since``.
GrantResolver = Callable[[CapState, Tuple[RoundLine, ...], str], Optional[Grant]]


def append_verdict(path: Path, line: RoundLine, *, grant_for: Optional[GrantResolver] = None) -> str:
    """Append ``line`` under the ledger lock, fsynced: ``"appended"`` or ``"present"``.

    ``"present"`` is the same verdict booked again (a retried write), also when
    its first booking lies before the PR's attested ``opened`` line: it stays
    there, uncounted. A key that holds another counted verdict raises
    :class:`RoundRefusal` (``verdict_on_head``); so does a re-anchor of a verdict
    the ledger does not count (``reanchor_origin_uncounted``), and a codex-seat
    verdict on a PR at the cap without an override or a granted round
    (``b3_cap``). A granted round is booked with ``granted_by``: the first
    verdict that reaches the lock consumes it, every later one is refused.
    A ledger that is unreadable for this PR raises
    :class:`RoundLedgerUnreadable`, any I/O failure :class:`RoundLedgerError`.
    """
    from atomic_io import slot_lock

    path = Path(path)
    try:
        with slot_lock(path):
            ledger = parse_ledger(path, _ledger_text(path))
            booking = _check_append(ledger, line, grant_for)
            if booking is None:
                return "present"
            _append_line(path, booking)
    except RoundLedgerError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be written: {exc}") from exc
    return "appended"


def _check_append(ledger: Ledger, line: RoundLine, grant_for: Optional[GrantResolver] = None) -> Optional[RoundLine]:
    """Judge ``line`` against the ledger read under its lock: ``None`` when this verdict is booked already.

    Raises what :func:`append_verdict` names; otherwise returns the line to
    append, with ``granted_by`` when it is the codex-seat round a tiebreaker
    granted past the cap.
    """
    text = unreadable_text(ledger, line.pr_number)
    if text:
        raise RoundLedgerUnreadable(text)
    existing = ledger.line_for(line.key)
    if existing is not None and not existing.same_verdict(line):
        raise RoundRefusal(REFUSED_VERDICT_ON_HEAD, (
            f"seat {line.seat} of PR #{line.pr_number} already has a {existing.verdict.upper()} "
            f"on head {line.head_sha[:12]} ({existing.reviewer}, {existing.report_name}); a second "
            f"verdict on the same head is refused"
        ))
    if ledger.booked(line):
        return None
    if line.reanchored_from:
        _check_reanchor_origin(ledger, line)
    if line.seat != CODEX_SEAT or line.override:
        return line
    lines = ledger.for_pr(line.project_id, line.pr_number)
    cap = cap_state(
        lines, line.pr_number, project_id=line.project_id, state_dir=ledger_state_dir(ledger.path),
    )
    if not cap.capped:
        return line
    # A re-anchor carries a verdict given on another head; it never uses the granted round.
    grant = None if line.reanchored_from or grant_for is None else grant_for(
        cap, lines, ledger.since(line.project_id, line.pr_number),
    )
    if grant is not None and round_granted_past_cap(cap, head_sha=line.head_sha, grant=grant, lines=lines):
        return replace(line, granted_by=grant.verdict_id)
    raise RoundRefusal(REFUSED_B3_CAP, cap_refusal_text(cap, where="verdict not booked"))


def _check_reanchor_origin(ledger: Ledger, line: RoundLine) -> None:
    """A re-anchor carries a counted verdict or nothing: the line it came from is counted, same reviewer, report and verdict.

    A verdict booked before the PR's attested ``opened`` line, or never booked
    at all, is not counted; carrying it to a new head would make it count there.
    """
    origin = ledger.line_for((line.project_id, line.pr_number, line.reanchored_from, line.seat))
    if origin is not None and (origin.reviewer, origin.verdict, origin.report_name) == (
        line.reviewer, line.verdict, line.report_name,
    ):
        return
    raise RoundRefusal(REFUSED_REANCHOR_UNCOUNTED, (
        f"the {line.verdict.upper()} of {line.reviewer} (seat {line.seat}) on head {line.reanchored_from[:12]} "
        f"that this re-anchor carries to {line.head_sha[:12]} is no counted line of PR #{line.pr_number} in the "
        f"round ledger, so there is nothing to carry: run the gate on {line.head_sha[:12]}"
    ))


def ndjson(record: Dict[str, Any]) -> str:
    """One ledger record as one NDJSON line."""
    return json.dumps(record, separators=(",", ":")) + "\n"


def _append_line(path: Path, line: RoundLine) -> None:
    _append_text(path, ndjson(line.to_dict()))


_TORN_TAIL_BYTES = 160


def _append_text(path: Path, text: str) -> None:
    """Append ``text`` at a record boundary, fsynced; the caller holds the ledger lock.

    A file that does not end in a newline ends in a torn line: a writer died
    in the middle of its record. Appending onto it would make the next record
    part of that line, and it would vanish from its PR's count while the file
    reads clean afterwards. So the text starts on a line of its own, and the
    torn fragment stays one bad line, refusing the PR it names (every PR when
    it names none) until the operator sets the file aside. A torn fragment is
    never a published verdict: its writer died before the record landed.
    """
    if not text:
        return
    created = ledger_state(path) == "absent"
    data = text.encode("utf-8")
    with Path(path).open("a+b") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size:
            handle.seek(max(0, size - _TORN_TAIL_BYTES))
            tail = handle.read()
            if not tail.endswith(b"\n"):
                logger.warning(
                    "review_rounds: %s ends in a torn line (%r); the next record starts on a line of its own, "
                    "the fragment stays a bad line: %s", path,
                    tail.rsplit(b"\n", 1)[-1].decode("utf-8", errors="replace"), ledger_way_out(path),
                )
                data = b"\n" + data
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_dir(Path(path).parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_ledger(review_gates_dir: Path) -> Path:
    """Create an empty ledger when the store has none, so the store counts rounds from now on.

    A ledger path that is no regular file is logged and left alone: the gate
    and the door refuse every PR on it with the way out, so one bad path does
    not stop the manager for every other command.
    """
    from atomic_io import slot_lock

    path = ledger_path(review_gates_dir)
    try:
        if ledger_state(path) == "file":
            return path
        with slot_lock(path):
            if ledger_state(path) == "absent":
                path.touch()
                _fsync_dir(path.parent)
    except RoundLedgerError as exc:
        logger.error("review_rounds: %s", exc)
    return path


# ---------------------------------------------------------------------------
# Opening a PR: its review history is in the ledger
# ---------------------------------------------------------------------------


def unpublished(ledger: Ledger, project_id: str, pr_number: int, results_dir: Path) -> Tuple[RoundLine, ...]:
    """Counted verdicts whose publication was interrupted: each reviewer's latest line its record does not carry.

    State contract rule 2 (plan section 2): the line reached the ledger, the
    result record did not. The gate refuses a new run on that head (the line
    stands, the record does not carry it) and the door counts no PASS (no
    record). A record that cannot be read carries nothing.
    """
    latest: Dict[str, RoundLine] = {}
    for line in ledger.for_pr(project_id, pr_number):
        latest[line.reviewer] = line
    return tuple(line for line in latest.values() if not _record_carries(Path(results_dir), line))


def _record_carries(results_dir: Path, line: RoundLine) -> bool:
    try:
        record = json.loads((results_dir / f"pr-{line.pr_number}-{line.reviewer}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and _head(record) == line.head_sha and (
        Path(str(record.get("report_path") or "")).name == line.report_name
    ) and ledger_verdict(record) == line.verdict


def open_pr(
    path: Path, project_id: str, pr_number: int, *, basis: str, evidence: int, actor: str = "", reason: str = "",
    results_dir: Optional[Path] = None,
) -> Tuple[str, OpenedLine]:
    """Write PR ``pr_number``'s ``opened`` line under the ledger lock: ``("opened" | "released" | "present", line)``.

    Idempotent: the ledger is re-read under the lock and a PR that is already
    opened gets no second line; ``"present"`` returns the line that stands.
    The one exception is the release of state contract rule 2: an operator
    line (``attested``, with ``results_dir``) on an opened PR that has an
    interrupted publication (:func:`unpublished`) is written as a new boundary,
    ``"released"``. What came before it no longer counts, the cap included.
    ``basis`` ``new`` is the gate's, ``attested`` the operator's (with
    ``actor`` and ``reason``). A ledger that is unreadable for this PR raises
    :class:`RoundLedgerUnreadable`, any other failure to write the line
    :class:`RoundLedgerError`.
    """
    from atomic_io import slot_lock

    if basis not in _BASES:
        raise ValueError(f"basis must be one of {_BASES}, not {basis!r}")
    if basis == BASIS_ATTESTED and not (actor.strip() and reason.strip()):
        raise ValueError("an attested opened line needs an actor and a reason")
    path = Path(path)
    line = OpenedLine(
        project_id=project_id, pr_number=pr_number, basis=basis, evidence=evidence, recorded_at=_utc_now(),
        source=SOURCE_GATE if basis == BASIS_NEW else SOURCE_OPERATOR, actor=actor.strip(), reason=reason.strip(),
    )
    try:
        with slot_lock(path):
            ledger = parse_ledger(path, _ledger_text(path))
            text = unreadable_text(ledger, pr_number)
            if text:
                raise RoundLedgerUnreadable(text)
            standing = ledger.opened_line(project_id, pr_number)
            release = standing is not None and basis == BASIS_ATTESTED and results_dir is not None and bool(
                unpublished(ledger, project_id, pr_number, results_dir)
            )
            if standing is not None and not release:
                return "present", standing
            _append_text(path, ndjson(line.to_dict()))
    except RoundLedgerError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be written: {exc}") from exc
    return ("released" if release else "opened"), line


RECEIPTS_NAME = "t0_receipts.ndjson"
_RESULT_EVENT = "review_gate_result"


def prior_evidence(data_dir: Path, pr_number: int, *, state_dir: Optional[Path] = None) -> Tuple[str, ...]:
    """What shows that PR ``pr_number`` was reviewed before the ledger counted it; ``()`` only when nothing does.

    Three sources; any one entry is enough for the gate to refuse the PR as
    ``round_history_missing``:

    - the headless reports of the seat gates, under the data dir ``data_dir``
      (``unified_reports/headless/*-HEADLESS-<gate>-pr-N-*.md``);
    - the result records of the seat gates that hold a decided verdict, under
      both names the merge door reads (``state/review_gates/results/pr-N-<gate>.json``
      and ``N-<gate>-contract.json``);
    - the ``review_gate_result`` receipts of the seat gates for this PR in the
      project's receipt ledger (``state/t0_receipts.ndjson``, the file
      ``receipt_query.py`` reads), on ``pr_number`` or ``pr_id``, any status:
      a report can be gone and a record overwritten, the receipt stays.

    ``state_dir`` is the state dir the ledger lives in. Every caller names it:
    the gate the parent of the ``review_gates`` dir it reads the ledger from,
    ``vnx review rounds open`` the one :func:`store_state_dir` resolved. Without
    one it is resolved the way the gate does (``ensure_env()['VNX_STATE_DIR']``).

    Fails closed. A source that exists but cannot be read is evidence, named
    ``unreadable:<path>`` and listed first: a reports dir that cannot be
    listed, a record that does not parse, a receipt ledger that cannot be
    opened, a receipt line that does not parse and names this PR. Only an
    absent source is no evidence. The receipt ledger is scanned here rather
    than through ``receipt_query``'s reader because that reader skips a line
    that does not parse and matches ``pr_id`` only.
    """
    data_dir = Path(data_dir)
    state = Path(state_dir) if state_dir is not None else store_state_dir()
    gates = sorted(ledger_seat_gates())
    found = list(dict.fromkeys(
        _report_evidence(data_dir / "unified_reports" / "headless", pr_number, gates)
        + _record_evidence(state / "review_gates" / "results", pr_number, gates)
        + _receipt_evidence(state / RECEIPTS_NAME, pr_number, gates)
    ))
    unreadable = [entry for entry in found if entry.startswith(UNREADABLE_PREFIX)]
    return tuple(unreadable) + tuple(entry for entry in found if entry not in unreadable)


def _report_evidence(headless: Path, pr_number: int, gates: Sequence[str]) -> List[str]:
    patterns = [f"*-HEADLESS-{gate}-pr-{pr_number}-*.md" for gate in gates]
    try:
        with os.scandir(headless) as entries:
            names = sorted(entry.name for entry in entries)
    except FileNotFoundError:
        return []
    except OSError:
        return [f"{UNREADABLE_PREFIX}{headless}"]
    return [name for name in names if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)]


def _record_evidence(results: Path, pr_number: int, gates: Sequence[str]) -> List[str]:
    from gate_status import decided_verdict, is_test_run_record

    found: List[str] = []
    for name in (f"{prefix}{gate}{suffix}" for gate in gates for prefix, suffix in (
        (f"pr-{pr_number}-", ".json"), (f"{pr_number}-", "-contract.json"),
    )):
        path = results / name
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            found.append(f"{UNREADABLE_PREFIX}{path}")
            continue
        if not isinstance(record, dict):
            found.append(f"{UNREADABLE_PREFIX}{path}")
        elif not is_test_run_record(record) and decided_verdict(record) in _VERDICTS:
            found.append(name)
    return found


def _receipt_evidence(receipts: Path, pr_number: int, gates: Sequence[str]) -> List[str]:
    event, marker = _RESULT_EVENT.encode(), str(pr_number).encode()
    found: List[str] = []
    try:
        with receipts.open("rb") as handle:
            for raw in handle:
                if event not in raw or marker not in raw:
                    continue
                try:
                    receipt = json.loads(raw)
                except ValueError:
                    found.append(f"{UNREADABLE_PREFIX}{receipts}")
                    continue
                if _is_seat_result_receipt(receipt, pr_number, gates):
                    report = Path(str(receipt.get("report_path") or "")).name
                    found.append(f"receipt:{receipt['gate']}:{report or receipt.get('timestamp', '')}")
    except FileNotFoundError:
        return []
    except OSError:
        found.append(f"{UNREADABLE_PREFIX}{receipts}")
    return found


def _is_seat_result_receipt(receipt: Any, pr_number: int, gates: Sequence[str]) -> bool:
    if not isinstance(receipt, dict) or _RESULT_EVENT not in (receipt.get("event_type"), receipt.get("event")):
        return False
    pr_fields = {str(receipt.get("pr_number") or "").strip(), str(receipt.get("pr_id") or "").strip()}
    return receipt.get("gate") in gates and str(pr_number) in pr_fields


def history_missing_text(pr_number: int, evidence: Sequence[str]) -> str:
    """The refusal of a PR that was reviewed before the ledger counted it, naming ``vnx review rounds open``."""
    return (
        f"PR #{pr_number} was reviewed before the round ledger counted it ({len(evidence)} earlier report(s), "
        f"result record(s), receipt(s) or unreadable source(s), first: {evidence[0]}). Its earlier verdicts are "
        f"not counted and are never reconstructed. The operator opens it with `{open_command(pr_number)}`; "
        f"the cap starts at zero from that line. --override-b3 does not lift this"
    )


# ---------------------------------------------------------------------------
# A result payload as a ledger line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictBooking:
    """What :func:`book_result_verdict` did with a result payload, and why."""

    outcome: str
    detail: str = ""
    line: Optional[RoundLine] = field(default=None, compare=False)


def blocking_count_of(payload: Dict[str, Any]) -> int:
    """Blockers of a verdict: its ``blocking_findings``, as the result receipt counts them."""
    findings = payload.get("blocking_findings")
    if isinstance(findings, list):
        return len(findings)
    count = payload.get("blocking_count")
    return count if _is_int(count) and count > 0 else 0


def payload_pr_number(payload: Dict[str, Any]) -> Optional[int]:
    value = payload.get("pr_number")
    if _is_int(value):
        return value
    text = str(payload.get("pr_id") or "").strip()
    return int(text) if text.isdigit() else None


def payload_seat(gate: str, payload: Dict[str, Any]) -> str:
    """The seat a verdict was given in: the seat its request stated, else the takeover origin, else its gate."""
    from review_seat_evaluation import seat_for

    return seat_for(gate, stated=str(payload.get("seat") or ""), takeover_path=payload.get("takeover_path"))


def ledger_verdict(payload: Dict[str, Any]) -> str:
    """``pass``/``fail`` for a verdict the ledger books, ``""`` for anything that is no verdict.

    A verdict carries its evidence (contract hash and report): a crash booked
    ``failed`` with an empty report is no verdict, and neither is a test run.
    """
    from gate_status import decided_verdict, has_evidence_fields, is_test_run_record

    if is_test_run_record(payload) or not has_evidence_fields(payload):
        return ""
    verdict = decided_verdict(payload)
    return verdict if verdict in _VERDICTS else ""


def line_from_payload(payload: Dict[str, Any], *, gate: str, project_id: str) -> Optional[RoundLine]:
    """The ledger line of a decided result payload; ``None`` when it is no verdict or names no head."""
    verdict, pr_number = ledger_verdict(payload), payload_pr_number(payload)
    head_sha = str(payload.get("commit_sha") or "").strip()
    if not verdict or pr_number is None or not head_sha:
        return None
    takeover_from = str(payload.get("takeover_from") or "") if payload.get("takeover") is True else ""
    override = payload.get("override_b3")
    # OI-2476: this verdict's own report, never an earlier run's. Unknown stays
    # ``None`` and is not written; a takeover books unknown (review_report_counts).
    counts = counts_for_payload(payload)
    return RoundLine(
        project_id=project_id, pr_number=pr_number, head_sha=head_sha,
        seat=payload_seat(gate, payload), reviewer=gate, takeover_from=takeover_from,
        verdict=verdict, blocking_count=blocking_count_of(payload),
        report_path=str(payload.get("report_path") or ""),
        dispatch_id=str(payload.get("dispatch_id") or ""),
        recorded_at=str(payload.get("recorded_at") or "") or _utc_now(),
        branch=str(payload.get("branch") or ""),
        override=override if isinstance(override, dict) and override else None,
        reanchored_from=reanchored_from(payload),
        warning_count=counts.warnings if counts is not None else None,
        prescan_notice_count=counts.notices if counts is not None else None,
    )


def reanchored_from(payload: Dict[str, Any]) -> str:
    """The head a re-anchored verdict came from (``gate_reanchor_cli``); ``""`` for a verdict given on its own head."""
    if payload.get("evidence_source") != EVIDENCE_REANCHORED:
        return ""
    return str(payload.get("reanchored_from_commit_sha") or "").strip()


def _head(record: Dict[str, Any]) -> str:
    return str(record.get("commit_sha") or "").strip()


def _is_reanchor(payload: Dict[str, Any], on_disk: Dict[str, Any]) -> bool:
    """Does ``payload`` carry the verdict ``on_disk`` holds to a new head (``gate_reanchor_cli``)?"""
    origin = reanchored_from(payload)
    return bool(origin) and origin == _head(on_disk) and origin != _head(payload)


def _is_rewrite(payload: Dict[str, Any], on_disk: Optional[Dict[str, Any]]) -> bool:
    """A rewrite of the record that already carries this verdict: same report, same verdict, no re-anchor.

    A re-anchor carries the verdict on disk to a new head with its provenance
    (``evidence_source`` ``reanchored``, ``reanchored_from_commit_sha`` naming the
    head on disk): that is a verdict for a head the ledger has no line for yet,
    and it is booked. The same report on the same head stays a rewrite. So does
    the same report moved to another head without that provenance: it gets no
    line, and the door does not count it.
    """
    if not isinstance(on_disk, dict):
        return False
    report = str(payload.get("report_path") or "")
    return bool(report) and str(on_disk.get("report_path") or "") == report and (
        ledger_verdict(on_disk) == ledger_verdict(payload)
    ) and not _is_reanchor(payload, on_disk)


def book_result_verdict(
    result_path: Path, payload: Dict[str, Any], *, gate: str, on_disk: Optional[Dict[str, Any]],
) -> VerdictBooking:
    """Book the verdict a result writer is about to land, before it lands.

    Called by ``gate_recorder`` inside the result slot's lock, after the
    overwrite guard and before the record is written. Raises
    :class:`RoundRefusal` (the writer refuses the record) or
    :class:`RoundLedgerError` (the run fails); anything else is booked or
    skipped with the reason in the returned :class:`VerdictBooking`. A
    re-anchor is booked as a line for its new head that names the head it came
    from (:func:`reanchored_from`), and only when that head's line is counted.
    """
    path = ledger_path_for_results(Path(result_path).parent)
    if path is None:
        return VerdictBooking("skipped", "the result is not in a review_gates/results store")
    # The verdict first: an outage or a note never reads the seat gates at all.
    if not ledger_verdict(payload) or gate not in ledger_seat_gates():
        return VerdictBooking("skipped", "no review-seat verdict")
    if _is_rewrite(payload, on_disk):
        return VerdictBooking("skipped", "a rewrite of a record that already carries this verdict")
    if reanchored_from(payload) and ledger_state(path) == "absent":
        # A store that keeps no ledger yet: the door reads none, and a first line would start one.
        return VerdictBooking("skipped", "a re-anchor in a store that keeps no round ledger")
    line = line_from_payload(payload, gate=gate, project_id=store_project_id(path.parent))
    if line is None:
        logger.warning(
            "review_rounds: %s verdict for pr=%s names no head (commit_sha empty): it cannot be "
            "counted per head and gets no ledger line; the merge door cannot use it either",
            gate, payload.get("pr_number") or payload.get("pr_id"),
        )
        return VerdictBooking("headless", "the verdict names no head")
    # Only a codex-seat verdict of its own, without an override, can use a granted round (_check_append).
    uses_grant = line.seat == CODEX_SEAT and not line.override and not line.reanchored_from
    grant_for = _grant_resolver(path.parent.parent, line) if uses_grant else None
    return VerdictBooking(append_verdict(path, line, grant_for=grant_for), line=line)


def _grant_resolver(state_dir: Path, line: RoundLine) -> GrantResolver:
    """The writer's backstop: the grant standing on the cap, qualified for this verdict's head (R2a).

    The git facts are resolved here, before :func:`append_verdict` takes the
    ledger lock (OI-2202): under the lock the resolver reads the verdict store
    and those facts, and runs no git. Only the verdicts of the PR's current
    count are looked up (OI-2207): ``since`` as the ledger reads now.
    """
    import review_cap_verdicts

    carries = review_cap_verdicts.resolved_carries(
        state_dir, line.pr_number, line.head_sha, since=_since_before_lock(state_dir, line),
    )

    def resolve(cap: CapState, lines: Tuple[RoundLine, ...], since: str) -> Optional[Grant]:
        return review_cap_verdicts.grant_or_none(
            state_dir, cap, lines, head_sha=line.head_sha, since=since, carries=carries,
        )

    return resolve


def _since_before_lock(state_dir: Path, line: RoundLine) -> str:
    """The PR's count boundary as the ledger stands before the writer locks it; ``""`` when it cannot be read.

    An unreadable ledger is refused by :func:`append_verdict` under the lock;
    ``""`` here only widens the git facts looked up, it grants nothing.
    """
    try:
        ledger = read_ledger(ledger_path(Path(state_dir) / "review_gates"))
    except RoundLedgerError:
        return ""
    return ledger.since(line.project_id, line.pr_number)
