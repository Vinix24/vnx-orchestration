#!/usr/bin/env python3
"""xdist parity history: does the cutover rule of track ci-profile-a-xdist hold?

Fetches the ``xdist-parity-verdict`` artifacts of VNX CI runs and applies the
cutover rule (plan "The cutover rule", D4's merge condition), so the
condition is computed, not counted by eye.

The window is every VNX CI run whose verdict is on a heavy profile, whose
``selection_fingerprint`` equals the target fingerprint (the one the checkout
of the given branch emits, or ``--fingerprint``), and whose head commit has
the merge commit of the last merged D3 PR (``--last-d3-merge``) as an
ancestor (``git merge-base --is-ancestor``). The rule holds only if:

* every run in the window has verdict exit 0 and every step outcome success.
  One exit 1 or 2, or a failed prelude, selection, download, comparison or
  shadow step, or a Profile A run that failed (also one that failed an audit
  after uploading green evidence), anywhere in the window blocks. The step
  failures are the verdict's list plus what ``check_xdist_parity.step_failures``
  gives on the steps, shadow and reference the verdict records, over
  ``RECORDED_STEPS``: the five steps before the comparison and the comparison
  step itself, whose outcome only the record step can add
  (``record_xdist_parity_verdict.py``). A verdict that does not carry that
  outcome, or carries one that is not success, is not clean: the comparison
  can fail after writing exit 0 (OI-2145);
* the window holds at least 10 distinct head SHAs (a re-run of one commit
  counts once), over at least two UTC calendar days, the latest run on main;
* the shadow's suite wall time is at most 15 minutes on every run in it;
* every watch-list OI from triage is closed (status ``done``) in the
  open-items ledger.

The two inputs that are a judgment, not a measurement, are flags and never
guessed here: the last D3 merge commit and the watch-list OIs.

Option A of the cutover rule (operator decision, 2026-10-22): a run whose
serial reference job failed, or in which either Profile A job received a
runner shutdown signal, did not measure parity. It is ``not_measurable``: set
aside from every condition of the window and listed with its reason. Only
positive evidence sets a run aside: the reference job's conclusion, or the
verdict's ``reference.job_result``, is exactly ``failure``, or its reference
result is present with a whole-number exit other than 0. Any other
conclusion, an unknown or missing job result, and an exit that is missing,
malformed or not a whole number are no cause and never set a run aside (codex
round 3 on #2184). A shutdown is proven by the runner's own line in the job
log (``##[error]The runner has received a shutdown signal``), read per job
from ``actions/jobs/<id>/logs`` when the check-run annotations do not carry
that text themselves: GitHub's ``The operation was canceled.`` annotation
names no cause, and a read that fails is not a shutdown. Two things are never
set aside: a real parity difference (any entry in the verdict's
``differences``), and an xdist shadow job that is not known to have succeeded
(the conclusion, or with none the verdict's job result, is exactly
``success``) without a shutdown of its own: a failed shadow blocks, and an
unknown one does too.

A light run never counts, whatever its step list says: the light early return
of the comparison names its skipped steps (OI-2147), and a light run is
outside the window either way. A profile that is not ``light`` (``heavy``, or
``unknown`` when the prelude gave none) is never excluded on profile.

Fail closed: a run that cannot be placed (a heavy verdict without a
fingerprint, such as the record step's exit-2 emergency verdict) and a run
list that may be cut off by ``--limit`` both keep the rule from holding.

Ancestry is read only from history that can prove it. ``git merge-base
--is-ancestor`` exit 1 means "not an ancestor" only in a complete repository
that holds both commits: a shallow clone cuts the path from a head to the
base and answers 1 for a descendant, which would move a failed run out of the
window. So before any run is placed, a shallow clone, and a window base or
run head that does not resolve with ``^{commit}``, is a tool error that
names the fetch (``git fetch --unshallow``, or ``git fetch origin`` with the
missing commits). The tool never fetches; the operator decides that.

Missing evidence never leaves the window. Profile and fingerprint are
properties of the verdict, so a run without a current verdict cannot be
excluded on them; only git ancestry, which does not come from the artifact,
may exclude it (its head does not descend from the window base). Every other
such run blocks, each under its own condition: a run still running
(``every_run_completed``), a completed run whose verdict is missing,
unreadable, failed to download or lacks a field every verdict writer writes
(``every_run_has_verdict``), and a verdict that does not prove it is of this
run's latest attempt (``every_verdict_current``).

Artifacts upload with ``overwrite: true``, so a re-run attempt replaces the
earlier attempt's verdict, but a re-run that fails before its upload leaves
the earlier one in place. A verdict counts only when its own ``run_id`` and
``run_attempt`` are the run's and its latest attempt; a cache entry only when
its stamp agrees with both. That rule is
``check_xdist_parity.stale_evidence_reason``, the one the parity job applies
to each of its own inputs. The verdict of an earlier attempt that a re-run
replaced cannot be read at all; the table notes it on every run with more
than one attempt, so the reader sees which runs carry an unread attempt.

The run list is cut at the window base's committer time: a run created before
the base existed cannot test a descendant of it. For a squash merge GitHub
made, that time is GitHub's own clock, the clock of the run times.

Exit 0 when the rule holds, 1 when it does not, 2 with a reason and never a
traceback on a usage or tool error: gh output that is not the JSON this tool
reads, a ``--limit`` below 1, a shallow checkout, a window base or run head
that is no commit in this checkout, an OI id that is not ``OI-<n>``, git
failing while the rule asks for ancestry, no project root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from check_xdist_parity import (RECORDED_STEPS, VERDICT_FILE, VERDICT_SCHEMA, fingerprint_of_args_dir,
                                stale_evidence_reason, stamp_attempt, step_failures, whole_number)

VERDICT_ARTIFACT = "xdist-parity-verdict"
WORKFLOW_NAME = "VNX CI"
MIN_DISTINCT_HEADS = 10
MIN_CALENDAR_DAYS = 2
MAX_SUITE_WALL_S = 15 * 60
MAIN_BRANCH = "main"
CLOSED_OI_STATUS = "done"
OI_ID = re.compile(r"OI-[1-9][0-9]*")

# Option A of the cutover rule (operator decision, 2026-10-22): a run whose serial
# reference job failed, or in which either Profile A job received a runner shutdown
# signal, did not measure parity and is set aside. The two Profile A jobs are named
# as .github/workflows/vnx-ci.yml names them. A shutdown is proven by the runner's
# own line in the job log; GitHub's "The operation was canceled." annotation is
# posted for a timeout and a manual or concurrency cancel too, so it names no cause
# and is never proof by itself (codex round 2 on #2184).
NOT_MEASURABLE = "not_measurable"
PROFILE_A_JOBS = (("reference", "Profile A (doctor + core tests)"),
                  ("shadow", "Profile A (xdist shadow)"))
SHUTDOWN_MESSAGE = "the runner has received a shutdown signal"
SHUTDOWN_LOG_LINE = "##[error]the runner has received a shutdown signal"


class ToolError(Exception):
    """gh or git failed in a way that leaves no answer."""


@dataclass
class Run:
    run_id: str
    attempt: int
    head_sha: str
    head_branch: str
    event: str
    created_at: str
    status: str
    conclusion: str | None
    verdict: dict | None = None
    verdict_problem: str = ""


@dataclass
class JobEvidence:
    """What a run's two Profile A jobs say about the run being measurable.

    *conclusions* maps each role (``reference``, ``shadow``) to its job's
    conclusion, or to None when the job is not in the run or was not read;
    *shutdowns* names the roles whose job proves a runner shutdown (a shutdown
    annotation or the runner's own line in the job log). *problem* says why a
    read that would have answered failed: a read that failed is never a
    shutdown (fail-closed).
    """
    conclusions: dict = field(default_factory=dict)
    shutdowns: tuple = ()
    problem: str = ""

    def conclusion(self, role: str) -> str | None:
        return self.conclusions.get(role)


@dataclass
class Placement:
    run: Run
    place: str
    reasons: list = field(default_factory=list)


@dataclass
class Condition:
    name: str
    holds: bool
    detail: str


@dataclass
class Evaluation:
    placements: list
    conditions: list

    @property
    def holds(self) -> bool:
        return all(c.holds for c in self.conditions)

    @property
    def window(self) -> list:
        return [p.run for p in self.placements if p.place == "in_window"]


# --- the rule (pure) ---------------------------------------------------------------------


def real_difference(verdict: dict) -> bool:
    """Whether the verdict records any parity difference (option A never sets such a run aside)."""
    differences = verdict.get("differences") or {}
    return isinstance(differences, dict) and any(differences.values())


def reference_failure_reason(verdict: dict | None, evidence: JobEvidence | None) -> str | None:
    """Why the serial reference job failed (option A cause (a)); None when no positive evidence says it did.

    Only positive evidence is a cause: a job conclusion of exactly
    ``failure``, a verdict ``reference.job_result`` of exactly ``failure``, or
    a present reference result whose exit is a whole number other than 0. The
    conclusion comes first because it is what GitHub measured, also when the
    parity job never ran; the verdict's own reference block says the same when
    the job list could not be read. A cancelled, timed-out or skipped job, any
    other conclusion or job result, a missing one, and an exit that is None or
    not a whole number are no evidence of a failure and never a cause (codex
    round 3 on #2184): missing evidence must not set a run aside. A cancelled
    job that a runner shutdown cut short is still set aside through cause (b),
    the shutdown evidence.
    """
    conclusion = evidence.conclusion("reference") if evidence is not None else None
    if conclusion == "failure":
        return "the serial reference job concluded failure"
    if verdict is None:
        return None
    reference = verdict["reference"]
    if reference.get("job_result") == "failure":
        return "the serial reference job failed (reference.job=failure)"
    if reference.get("result") == "present":
        exit_code = whole_number(reference.get("exit"))
        if exit_code is not None and exit_code != 0:
            return f"the serial reference suite exited {reference.get('exit')}"
    return None


def shutdown_reason(evidence: JobEvidence | None) -> str | None:
    """Why a Profile A job is no measurement because its runner was shut down (option A cause (b))."""
    roles = list(evidence.shutdowns) if evidence is not None else []
    if not roles:
        return None
    names = [f"the {'xdist shadow' if role == 'shadow' else 'serial Profile A'} job" for role in roles]
    return f"a runner shutdown signal in {' and '.join(names)}"


def shadow_job_failed(verdict: dict | None, evidence: JobEvidence | None) -> bool:
    """Whether the xdist shadow job is not known to have succeeded; any doubt keeps the run dirty.

    A shadow job that a shutdown cut short only lets a run be set aside
    through its own proven shutdown (cause (b)); this predicate is what keeps
    it dirty otherwise. Positive evidence of success is a job conclusion of
    exactly ``success``, or, with no conclusion read, a verdict
    ``shadow.job_result`` of exactly ``success``. Missing evidence is not
    success (codex round 3 on #2184): an unknown conclusion with no verdict,
    an unknown job result, and any other value keep the run dirty just as a
    failed shadow does.
    """
    conclusion = evidence.conclusion("shadow") if evidence is not None else None
    if conclusion == "success":
        return False
    if conclusion is not None:
        return True
    if verdict is None:
        return True
    return verdict["shadow"].get("job_result") != "success"


def not_measurable_reason(run: Run, evidence: JobEvidence | None) -> str | None:
    """Why *run* is set aside as not a measurement (option A, operator 2026-10-22); None when it counts or blocks.

    A run is set aside only when the serial reference job failed on positive
    evidence or a Profile A job received a runner shutdown signal, and only
    when neither a real parity difference nor a shadow job that is not known to
    have succeeded without a shutdown of its own keeps it dirty: never more
    than those two causes, and never a real difference. Missing or malformed
    evidence is neither a cause nor a reason to leave a run set aside.
    """
    verdict = run.verdict if evidence_gap(run) is None else None
    if verdict is not None and not run_blockers(run):
        return None
    if verdict is not None and real_difference(verdict):
        return None
    causes = [why for why in (reference_failure_reason(verdict, evidence), shutdown_reason(evidence)) if why]
    if not causes:
        return None
    shadow_shutdown = evidence is not None and "shadow" in evidence.shutdowns
    if shadow_job_failed(verdict, evidence) and not shadow_shutdown:
        return None
    return "; ".join(causes)


def evidence_note(evidence: JobEvidence | None) -> str | None:
    """The read failure to show beside a run, if any; never a cause."""
    return f"evidence: {evidence.problem}" if evidence is not None and evidence.problem else None


def _with_note(reason: str, evidence: JobEvidence | None) -> str:
    note = evidence_note(evidence)
    return f"{reason}; {note}" if note else reason


def place_run(run: Run, target_fingerprint: str, is_ancestor, job_evidence=None) -> Placement:
    """Where one run stands relative to the window; *is_ancestor(head)* is True, False or None.

    A run without a current verdict (still running, none, one that does not
    prove this run's latest attempt, or one that lacks a field) is placed on
    git ancestry alone: outside the window when its head does not descend
    from the base, otherwise ``pending``, ``no_verdict`` or ``stale_verdict``,
    each blocking. Nothing read from a missing or stale verdict excludes a run.

    A light verdict is outside the window, whatever its step list says.

    A heavy verdict without a fingerprint (the record step's emergency
    verdict, a checkout or selection that failed) cannot be matched to a
    selection; when its head descends from the window base it is
    ``unplaceable`` and blocks, so a broken run is never read as another
    selection.

    A run that is not clean may instead be ``not_measurable`` (option A) when
    *job_evidence* names a failed serial reference job or a runner shutdown in
    a Profile A job and nothing keeps it dirty. A run that is clean never
    reads the job evidence: it counts as it is.
    """
    gap = evidence_gap(run)
    if gap is not None:
        return _place_on_ancestry(run, *gap, is_ancestor=is_ancestor, job_evidence=job_evidence)
    verdict = run.verdict
    if verdict["profile"] == "light":
        return Placement(run, "light")
    fingerprint = verdict["selection_fingerprint"]
    if fingerprint is not None and fingerprint != target_fingerprint:
        return Placement(run, "other_selection")
    descends = is_ancestor(run.head_sha)
    if descends is None:
        return Placement(run, "ancestry_unknown", [f"head {run.head_sha} cannot be found"])
    if not descends:
        return Placement(run, "before_window")
    reasons = run_blockers(run)
    if reasons or fingerprint is None:
        readable = job_evidence(run) if job_evidence is not None and not real_difference(verdict) else None
        set_aside = not_measurable_reason(run, readable)
        if set_aside is not None:
            return Placement(run, NOT_MEASURABLE, [_with_note(set_aside, readable)])
        note = evidence_note(readable)
        if note:
            reasons = reasons + [note]
    if fingerprint is None:
        return Placement(run, "unplaceable", [f"no selection fingerprint, exit {verdict['exit']}{_difference_note(verdict)}"])
    return Placement(run, "in_window", reasons)


def evidence_gap(run: Run) -> tuple | None:
    """``(place, reason)`` when *run* has no current verdict to place it on; None when it has one."""
    if run.status != "completed":
        return "pending", f"status {run.status or 'unknown'}: re-run this check when the run completes"
    if run.verdict is None:
        return "no_verdict", f"{run.verdict_problem or 'no verdict'} (run conclusion {run.conclusion or 'none'})"
    stale = stale_reason(run.verdict, run)
    if stale is not None:
        return "stale_verdict", stale
    malformed = verdict_shape_problem(run.verdict)
    if malformed is not None:
        return "no_verdict", f"malformed verdict: {malformed}"
    return None


def _place_on_ancestry(run: Run, place: str, reason: str, is_ancestor, job_evidence=None) -> Placement:
    """Ancestry is the one exclusion that does not come from the verdict."""
    descends = is_ancestor(run.head_sha)
    if descends is None:
        return Placement(run, "ancestry_unknown", [f"head {run.head_sha} cannot be found", reason])
    if not descends:
        return Placement(run, "before_window", [reason])
    if place != "pending" and job_evidence is not None:
        evidence = job_evidence(run)
        set_aside = not_measurable_reason(run, evidence)
        if set_aside is not None:
            return Placement(run, NOT_MEASURABLE, [_with_note(set_aside, evidence)])
    return Placement(run, place, [reason])


def stale_reason(verdict: dict, run: Run) -> str | None:
    """Why *verdict* is not evidence of *run*'s latest attempt; None when its own run_id and run_attempt prove it is."""
    return stale_evidence_reason("verdict", verdict, run.run_id, run.attempt)


def _text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _object_or_none(value) -> bool:
    return value is None or isinstance(value, dict)


def _shadow_reads(value) -> bool:
    """A shadow block: an object whose suite_wall_s, when given, is a whole number."""
    if not isinstance(value, dict):
        return False
    return value.get("suite_wall_s") is None or whole_number(value.get("suite_wall_s")) is not None


def _steps_read(value) -> bool:
    """A steps block: an object of step name -> outcome text."""
    return isinstance(value, dict) and all(isinstance(o, str) for o in value.values())


def _problems_read(value) -> bool:
    """A problem list: a list of objects each naming its reason as text."""
    return isinstance(value, list) and all(isinstance(p, dict) and isinstance(p.get("reason"), str) for p in value)


def _differences_read(value) -> bool:
    return value is None or (isinstance(value, dict) and all(isinstance(e, list) for e in value.values()))


# Every field every verdict writer writes (the comparison, heavy and light, and
# the record step, recorded and emergency), with the test its value must pass.
# A verdict without one of them is not one this pipeline wrote.
VERDICT_FIELDS = (
    ("schema", lambda v: v == VERDICT_SCHEMA),
    ("head_sha", _text_or_none),
    ("tested_sha", _text_or_none),
    ("run_id", _text_or_none),
    ("run_attempt", lambda v: _text_or_none(v) or whole_number(v) is not None),
    ("event", _text_or_none),
    ("ref", _text_or_none),
    ("profile", _text_or_none),
    ("exit", lambda v: v is None or whole_number(v) is not None),
    ("verdict", _text_or_none),
    ("step_failures", lambda v: isinstance(v, list) and all(isinstance(f, str) for f in v)),
    ("steps", _steps_read),
    ("shadow", _shadow_reads),
    ("reference", lambda v: isinstance(v, dict)),
    ("selection_fingerprint", _text_or_none),
    ("selection_fingerprint_source", _text_or_none),
    ("differences", _differences_read),
    ("counts", _object_or_none),
    ("tool_versions", _object_or_none),
    ("invalid_evidence", _problems_read),
)


def verdict_shape_problem(verdict: dict) -> str | None:
    """Which field keeps *verdict* from being read by the rule (missing or of the wrong type); None when it reads."""
    for name, reads in VERDICT_FIELDS:
        if name not in verdict:
            return f"{name} is missing"
        if not reads(verdict[name]):
            return f"{name} is {verdict[name]!r:.80}"
    return None


def run_blockers(run: Run) -> list:
    """Why one run in the window is not clean (empty when it is)."""
    verdict = run.verdict
    reasons = []
    if verdict["exit"] != 0:
        reasons.append(f"exit {verdict['exit']} ({verdict['verdict']}){_difference_note(verdict)}")
    reasons += [f"step {failure}" for failure in recorded_step_failures(verdict)]
    wall = suite_wall_s(verdict)
    if wall is None:
        reasons.append("no shadow suite wall time")
    elif wall > MAX_SUITE_WALL_S:
        reasons.append(f"shadow suite {wall} s > {MAX_SUITE_WALL_S} s")
    return reasons


def recorded_step_failures(verdict: dict) -> list:
    """The verdict's step_failures, then each failure its own blocks give under the parity job's step rule.

    The verdict records every outcome its step_failures is computed from (the
    parity job's steps, the shadow, the reference). Applying
    check_xdist_parity.step_failures over ``RECORDED_STEPS`` to those blocks
    again means a failure recorded there (a Profile A job that failed after
    its evidence was uploaded green, a comparison step that failed after
    writing exit 0) blocks even when the list leaves it out, and a verdict
    without the comparison step's outcome is not clean.
    """
    listed = list(verdict["step_failures"])
    derived = step_failures(verdict["steps"], verdict["shadow"], verdict["reference"], RECORDED_STEPS)
    return listed + [failure for failure in derived if failure not in listed]


def _difference_note(verdict: dict) -> str:
    differences = verdict["differences"] or {}
    counted = [f"{kind}: {len(entries)}" for kind, entries in differences.items() if entries]
    problems = [p["reason"] for p in verdict["invalid_evidence"]]
    parts = counted + [f"invalid: {', '.join(sorted(set(problems)))}"] * bool(problems)
    return f" [{'; '.join(parts)}]" if parts else ""


def suite_wall_s(verdict: dict) -> int | None:
    shadow = verdict.get("shadow")
    return whole_number(shadow.get("suite_wall_s")) if isinstance(shadow, dict) else None


def _created(run: Run) -> datetime:
    return datetime.fromisoformat(run.created_at.replace("Z", "+00:00")).astimezone(timezone.utc)


# Each kind of run the window cannot do without and cannot place, with its own condition:
# (condition, the places that block it, what the detail says when none does).
EVIDENCE_CONDITIONS = (
    ("every_run_completed", ("pending",), "no run of the window still running"),
    ("every_run_has_verdict", ("no_verdict",), "every completed run of the window has a readable verdict"),
    ("every_verdict_current", ("stale_verdict",), "every verdict is of its run's latest attempt"),
    ("every_run_placed", ("ancestry_unknown", "unplaceable"), "every run placed"),
)


def evidence_conditions(placements: list) -> list:
    """One blocking condition per kind of run without usable evidence: none is ever just left out."""
    conditions = []
    for name, places, clear in EVIDENCE_CONDITIONS:
        blocked = [p for p in placements if p.place in places]
        conditions.append(Condition(name, not blocked, _blocked_detail(blocked) or clear))
    return conditions


def _blocked_detail(blocked: list) -> str:
    return "; ".join(f"run {p.run.run_id}: {', '.join(p.reasons)}" for p in blocked)


def window_conditions(window: list, placements: list) -> list:
    """The conditions on the window itself: clean runs, size, spread, latest on main, wall time."""
    dirty = [p for p in placements if p.place == "in_window" and p.reasons]
    heads = {run.head_sha for run in window}
    days = sorted({_created(run).date().isoformat() for run in window})
    latest = max(window, key=_created) if window else None
    on_main = latest is not None and latest.event == "push" and latest.head_branch == MAIN_BRANCH
    slow = [r for r in window if suite_wall_s(r.verdict) is None or suite_wall_s(r.verdict) > MAX_SUITE_WALL_S]
    return [
        Condition("every_run_clean", not dirty, _blocked_detail(dirty) or "every run exit 0, every step success"),
        *evidence_conditions(placements),
        Condition("distinct_heads", len(heads) >= MIN_DISTINCT_HEADS,
                  f"{len(heads)} distinct head SHAs in the window, at least {MIN_DISTINCT_HEADS} needed"),
        Condition("calendar_days", len(days) >= MIN_CALENDAR_DAYS,
                  f"{len(days)} UTC calendar day(s) {days}, at least {MIN_CALENDAR_DAYS} needed"),
        Condition("latest_on_main", on_main,
                  "no run in the window" if latest is None
                  else f"latest run {latest.run_id}: {latest.event} on {latest.head_branch}"),
        Condition("suite_wall_time", not slow and bool(window),
                  "no run in the window" if not window
                  else f"{len(slow)} run(s) over {MAX_SUITE_WALL_S} s or without a wall time"),
    ]


def watch_list_condition(watch_list: list | None, oi_status: dict | None) -> Condition:
    """Every watch-list OI closed; an absent watch list is not taken as an empty one."""
    if watch_list is None:
        return Condition("watch_list_closed", False, "no watch list given: pass --watch-list OI-<n> or --no-watch-list")
    if not watch_list:
        return Condition("watch_list_closed", True, "no watch-list OI (stated with --no-watch-list)")
    if oi_status is None:
        return Condition("watch_list_closed", False, "the open-items ledger could not be read")
    open_ones = [f"{oi}={oi_status.get(oi, 'not found')}" for oi in watch_list if oi_status.get(oi) != CLOSED_OI_STATUS]
    return Condition("watch_list_closed", not open_ones,
                     f"not closed: {', '.join(open_ones)}" if open_ones else f"closed: {', '.join(watch_list)}")


def evaluate(runs: list, *, target_fingerprint: str, is_ancestor, watch_list, oi_status, list_truncated: bool,
             job_evidence=None) -> Evaluation:
    """Apply the cutover rule to *runs* (newest first or in any order)."""
    placements = [place_run(run, target_fingerprint, is_ancestor, job_evidence) for run in runs]
    window = [p.run for p in placements if p.place == "in_window"]
    conditions = [Condition("run_list_complete", not list_truncated,
                            "the run list may be cut off: raise --limit" if list_truncated else "every run since the window base listed")]
    conditions += window_conditions(window, placements)
    conditions.append(watch_list_condition(watch_list, oi_status))
    return Evaluation(placements, conditions)


# --- report -------------------------------------------------------------------------------------


def render(evaluation: Evaluation, header: dict) -> str:
    lines = [
        "xdist parity history: the cutover rule of track ci-profile-a-xdist",
        f"window base: {header['window_base']} ({header['window_base_source']})",
        f"selection fingerprint: {header['fingerprint']} ({header['fingerprint_source']})",
        f"watch list: {header['watch_list']}",
        f"runs listed: {len(evaluation.placements)} of workflow {header['workflow']!r} in {header['repo']}",
        "",
        f"{'run_id':<12} {'attempt':<15} {'event':<13} {'head':<9} {'created':<21} {'place':<17} "
        f"{'exit':<5} {'wall_s':<7} branch / why",
    ]
    for p in evaluation.placements:
        lines.append(_row(p))
    set_aside = [p for p in evaluation.placements if p.place == NOT_MEASURABLE]
    if set_aside:
        lines += ["", "set aside (not measurable, option A):"]
        lines += [f"  run {p.run.run_id}: {', '.join(p.reasons)}" for p in set_aside]
    lines += ["", "conditions:"]
    for c in evaluation.conditions:
        lines.append(f"  [{'ok' if c.holds else 'x'}] {c.name}: {c.detail}")
    lines += ["", f"cutover rule: {'HOLDS' if evaluation.holds else 'DOES NOT HOLD'}"]
    return "\n".join(lines) + "\n"


def _row(p: Placement) -> str:
    run, verdict = p.run, p.run.verdict or {}
    verdict_attempt = verdict.get("run_attempt") or "-"
    attempt = f"{verdict_attempt} (of {run.attempt})"
    exit_code = "-" if run.verdict is None else str(verdict.get("exit"))
    wall = suite_wall_s(verdict)
    wall_text = "-" if wall is None else str(wall)
    notes = list(p.reasons)
    if run.attempt > 1 and run.verdict is not None and stale_reason(run.verdict, run) is None:
        notes.append(f"attempt(s) before {run.attempt} not read: the artifact is overwritten")
    why = run.head_branch + (f" / {'; '.join(notes)}" if notes else "")
    return (f"{run.run_id:<12} {attempt:<15} {run.event:<13} {run.head_sha[:8]:<9} {run.created_at:<21} "
            f"{p.place:<17} {exit_code:<5} {wall_text:<7} {why}")


def as_json(evaluation: Evaluation, header: dict) -> str:
    document = {
        **header,
        "holds": evaluation.holds,
        "conditions": [vars(c) for c in evaluation.conditions],
        "runs": [
            {
                "run_id": p.run.run_id,
                "run_attempt_latest": p.run.attempt,
                "verdict_run_attempt": (p.run.verdict or {}).get("run_attempt"),
                "event": p.run.event,
                "head_branch": p.run.head_branch,
                "head_sha": p.run.head_sha,
                "created_at": p.run.created_at,
                "place": p.place,
                "reasons": p.reasons,
                "exit": (p.run.verdict or {}).get("exit"),
                "suite_wall_s": suite_wall_s(p.run.verdict or {}),
            }
            for p in evaluation.placements
        ],
    }
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


# --- gh and git ----------------------------------------------------------------------------------


def _run(cmd: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ToolError(f"{cmd[0]} is not installed: {exc}") from exc


def _gh_text(args: list) -> str:
    """gh's plain-text output of *args*; ToolError when gh fails (an expired job log is such a failure)."""
    proc = _run(["gh", *args])
    if proc.returncode != 0:
        raise ToolError(f"gh {' '.join(args)}: {_last_line(proc.stderr)}")
    return proc.stdout


def _gh_json(args: list, shape: type):
    """gh's JSON output of *args*; ToolError when gh fails or prints anything but JSON of *shape*."""
    command = f"gh {' '.join(args[:2])}"
    proc = _run(["gh", *args])
    if proc.returncode != 0:
        raise ToolError(f"gh {' '.join(args)}: {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ToolError(f"{command} printed no JSON ({exc}): {proc.stdout.strip()[:200]!r}") from exc
    if not isinstance(data, shape):
        raise ToolError(f"{command} printed {type(data).__name__}, not a {shape.__name__}: {proc.stdout.strip()[:200]!r}")
    return data


def default_repo() -> str:
    name = _gh_json(["repo", "view", "--json", "nameWithOwner"], dict).get("nameWithOwner")
    if not isinstance(name, str) or not name:
        raise ToolError(f"gh repo view gave no nameWithOwner: {name!r}")
    return name


def list_runs(repo: str, workflow: str, limit: int) -> list:
    fields = "databaseId,headSha,headBranch,event,status,conclusion,createdAt,attempt"
    rows = _gh_json(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit), "--json", fields], list)
    return [run_of_row(row) for row in rows]


def run_of_row(row) -> Run:
    """One row of ``gh run list`` as a Run; ToolError when a field the rule reads is missing or malformed."""
    if not isinstance(row, dict):
        raise ToolError(f"gh run list: a run is not an object: {row!r:.200}")
    checks = (
        ("databaseId", whole_number(row.get("databaseId")) is not None),
        ("attempt", stamp_attempt(row.get("attempt")) is not None),
        ("headSha", isinstance(row.get("headSha"), str) and bool(row.get("headSha"))),
        ("createdAt", _is_timestamp(row.get("createdAt"))),
    )
    bad = [name for name, ok in checks if not ok]
    if bad:
        shown = ", ".join(f"{name} {row.get(name)!r}" for name in bad)
        raise ToolError(f"gh run list: run {row.get('databaseId')!r} has a missing or malformed {shown}")
    return Run(str(row["databaseId"]), stamp_attempt(row["attempt"]), row["headSha"], str(row.get("headBranch") or ""),
               str(row.get("event") or ""), row["createdAt"], str(row.get("status") or ""), row.get("conclusion"))


def _is_timestamp(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def download_verdict(repo: str, run: Run, cache: Path) -> tuple:
    """``(verdict, problem)`` of *run*: the cached copy when it proves the latest attempt, else a fresh download.

    A cache entry counts only when its stamp, the verdict's own run_id and
    run_attempt, and the run's latest attempt all agree. A download lands in a
    scratch directory and is cached only when it proves the latest attempt; an
    earlier attempt's verdict (a re-run that failed before its upload) is
    returned uncached, for place_run to name stale. *problem* says why there
    is no verdict, empty when there is one.
    """
    target = cache / run.run_id
    cached = _cached_verdict(target, run)
    if cached is not None:
        return cached, ""
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix=f".download-{run.run_id}-") as scratch:
        proc = _run(["gh", "run", "download", run.run_id, "--repo", repo, "--name", VERDICT_ARTIFACT, "--dir", scratch])
        fetched = Path(scratch) / VERDICT_FILE
        if proc.returncode != 0:
            return None, f"gh run download exit {proc.returncode}: {_last_line(proc.stderr)}"
        if not fetched.is_file():
            return None, f"the {VERDICT_ARTIFACT} artifact holds no {VERDICT_FILE}"
        verdict = _read_verdict(fetched)
        if verdict is None:
            return None, f"{VERDICT_FILE} is not a readable JSON object"
        if stale_reason(verdict, run) is None:
            _store(target, fetched, run.attempt)
        return verdict, ""


def _cached_verdict(target: Path, run: Run) -> dict | None:
    """The cached verdict when stamp, verdict and run agree on the attempt; None otherwise (download again)."""
    stamp = _read_text(target / ".attempt")
    if stamp is None or stamp.strip() != str(run.attempt):
        return None
    verdict = _read_verdict(target / VERDICT_FILE)
    if verdict is None or stale_reason(verdict, run) is not None:
        return None
    return verdict


def _store(target: Path, fetched: Path, attempt: int) -> None:
    """Cache *fetched* for *attempt*: verdict and stamp each replaced atomically, the verdict first.

    Interrupted between the two, the entry holds a new verdict under an old
    stamp; _cached_verdict rejects that and the next call downloads again.
    """
    target.mkdir(parents=True, exist_ok=True)
    os.replace(fetched, target / VERDICT_FILE)
    fd, stamp_tmp = tempfile.mkstemp(dir=target, prefix=".attempt-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{attempt}\n")
    os.replace(stamp_tmp, target / ".attempt")


def _last_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1][:300] if lines else "no message"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_verdict(path: Path) -> dict | None:
    try:
        verdict = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return verdict if isinstance(verdict, dict) else None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, document) -> None:
    """Cache *document* at *path* atomically; a cache that cannot be written stays uncached."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        return
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def is_shutdown_annotation(message: str) -> bool:
    """Whether *message* is a check-run annotation that names a runner shutdown itself.

    Only the shutdown text counts. GitHub posts ``The operation was canceled.``
    both for a shutdown and for a timeout, a manual cancel and a concurrency
    cancel; that annotation names no cause, so it never proves a shutdown and
    read_job_evidence falls through to the job log (codex round 2 on #2184).
    """
    return SHUTDOWN_MESSAGE in message.lower()


def is_shutdown_log(log: str) -> bool:
    """Whether *log* carries the runner's own shutdown line; nothing else in a job log announces one."""
    return any(SHUTDOWN_LOG_LINE in line.lower() for line in log.splitlines())


def profile_a_jobs(payload: dict) -> dict | None:
    """The two Profile A jobs of a run's job list, by role; None when the payload cannot be read."""
    rows = payload.get("jobs")
    if not isinstance(rows, list):
        return None
    jobs = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        role = next((role for role, name in PROFILE_A_JOBS if row.get("name") == name), None)
        if role is None:
            continue
        job_id = whole_number(row.get("id"))
        if job_id is None:
            return None
        jobs[role] = {"id": job_id, "conclusion": row.get("conclusion")}
    return jobs


def _cached_jobs(repo: str, run: Run, cache: Path) -> dict | None:
    """The two Profile A jobs of *run* (cached per attempt); None when gh cannot answer."""
    path = cache / run.run_id / f"jobs-{run.attempt}.json"
    cached = _read_json(path)
    if isinstance(cached, dict) and all(isinstance(job, dict) for job in cached.values()):
        return cached
    try:
        payload = _gh_json(["api", f"repos/{repo}/actions/runs/{run.run_id}/jobs?per_page=100"], dict)
    except ToolError:
        return None
    jobs = profile_a_jobs(payload)
    if jobs is None:
        return None
    _write_json(path, jobs)
    return jobs


def _cached_annotations(repo: str, run: Run, job: dict, cache: Path) -> tuple:
    """``(messages, problem)`` of *job*'s check-run annotations; a failed read writes no cache entry."""
    path = cache / run.run_id / f"annotations-{job['id']}.json"
    cached = _read_json(path)
    if isinstance(cached, list) and all(isinstance(message, str) for message in cached):
        return cached, ""
    try:
        payload = _gh_json(["api", f"repos/{repo}/check-runs/{job['id']}/annotations"], list)
    except ToolError as exc:
        return [], str(exc)
    messages = [row["message"] for row in payload if isinstance(row, dict) and isinstance(row.get("message"), str)]
    _write_json(path, messages)
    return messages, ""


def _cached_shutdown(repo: str, run: Run, job: dict, cache: Path) -> tuple:
    """``(shutdown, problem)`` from *job*'s log; the boolean is cached, the log body never is.

    *shutdown* is True only when a line of the log carries the runner's own
    shutdown line. A fetch that fails, or an expired log, writes no cache entry:
    the answer stays unknown, which is never a shutdown (fail-closed).
    """
    path = cache / run.run_id / f"shutdown-{job['id']}.json"
    cached = _read_json(path)
    if isinstance(cached, dict) and isinstance(cached.get("shutdown"), bool):
        return cached["shutdown"], ""
    try:
        log = _gh_text(["api", f"repos/{repo}/actions/jobs/{job['id']}/logs"])
    except ToolError as exc:
        return False, str(exc)
    shutdown = is_shutdown_log(log)
    _write_json(path, {"shutdown": shutdown})
    return shutdown, ""


def read_job_evidence(repo: str, run: Run, cache: Path) -> JobEvidence:
    """The two Profile A jobs' conclusions and proven runner shutdowns; a failed read leaves *problem*, never a shutdown.

    Evidence is read only for a job that did not succeed: a job that ran to
    success had no shutdown. The job's check-run annotations come first, and
    only one that carries the shutdown text itself counts. GitHub's ``The
    operation was canceled.`` names no cause, so a job whose annotations prove
    nothing has its own log read (``actions/jobs/<id>/logs``, one job, not the
    run archive): only a line carrying ``##[error]The runner has received a
    shutdown signal`` makes it a shutdown. Both answers are cached per job, the
    log body never is; a read that fails is never a shutdown.
    """
    jobs = _cached_jobs(repo, run, cache)
    if jobs is None:
        return JobEvidence(problem="the run's jobs could not be read")
    evidence = JobEvidence()
    problems = []
    for role, name in PROFILE_A_JOBS:
        job = jobs.get(role)
        if job is None:
            problems.append(f"no job {name!r} in the run")
            continue
        evidence.conclusions[role] = job.get("conclusion")
        if job.get("conclusion") in (None, "success", "skipped"):
            continue
        messages, problem = _cached_annotations(repo, run, job, cache)
        if problem:
            problems.append(f"{role} job annotations: {problem}")
            continue
        if any(is_shutdown_annotation(message) for message in messages):
            evidence.shutdowns += (role,)
            continue
        shutdown, problem = _cached_shutdown(repo, run, job, cache)
        if problem:
            problems.append(f"{role} job log: {problem}")
        elif shutdown:
            evidence.shutdowns += (role,)
    evidence.problem = "; ".join(problems)
    return evidence


def job_evidence_provider(repo: str, cache: Path):
    """A memoised *run* -> JobEvidence reader for one evaluation; it never raises on a read that fails."""
    memo = {}

    def provide(run: Run) -> JobEvidence:
        if run.run_id not in memo:
            memo[run.run_id] = read_job_evidence(repo, run, cache)
        return memo[run.run_id]

    return provide


SHALLOW_HISTORY = (
    "this checkout is a shallow clone: git answers 'not an ancestor' for a descendant whose path to the window "
    "base the clone cut off, so no run can be placed on it; run git fetch --unshallow, then this check again"
)


def git_is_ancestor(repo_root: Path, base: str, head: str) -> bool:
    """True when *head* descends from *base* (``git merge-base --is-ancestor``); ToolError when git cannot say.

    Exit 0 is proof in any history: the path git walked exists. Exit 1 is
    proof of the opposite only in a complete history that holds both commits:
    a shallow clone cuts the path at its boundary and git answers 1 for a
    descendant (codex round 2 on #2105). Nothing is fetched here.
    """
    proc = _run(["git", "merge-base", "--is-ancestor", base, head], cwd=repo_root)
    if proc.returncode == 0:
        return True
    problem = history_problem(repo_root, {base: "window base", head: "head"})
    if problem is not None:
        raise ToolError(problem)
    if proc.returncode == 1:
        return False
    raise ToolError(f"git merge-base --is-ancestor {base} {head}: exit {proc.returncode}: {_last_line(proc.stderr)}")


def history_problem(repo_root: Path, commits: dict) -> str | None:
    """Why this checkout cannot prove ancestry for *commits* (commit -> what it is); None when it can."""
    if is_shallow(repo_root):
        return SHALLOW_HISTORY
    return missing_commits(repo_root, commits)


def is_shallow(repo_root: Path) -> bool:
    proc = _run(["git", "rev-parse", "--is-shallow-repository"], cwd=repo_root)
    answer = proc.stdout.strip()
    if proc.returncode != 0 or answer not in ("true", "false"):
        raise ToolError(f"git rev-parse --is-shallow-repository: exit {proc.returncode}, {answer!r}: "
                        f"{_last_line(proc.stderr)}")
    return answer == "true"


def missing_commits(repo_root: Path, commits: dict) -> str | None:
    """The commits of *commits* that do not resolve with ``^{commit}``, named with the fetch that brings them."""
    missing = [commit for commit in commits
               if _run(["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], cwd=repo_root).returncode]
    if not missing:
        return None
    named = ", ".join(f"{commits[commit]} {commit}" for commit in missing)
    return (f"{len(missing)} commit(s) the rule needs are not in this checkout: {named}; "
            f"run git fetch origin {' '.join(missing)}, then this check again")


def run_heads(runs: list) -> dict:
    """Each head commit of *runs* -> the runs it is the head of, as missing_commits names it."""
    heads = {}
    for run in runs:
        heads.setdefault(run.head_sha, []).append(run.run_id)
    return {head: f"head of run {', '.join(ids)}" for head, ids in heads.items()}


def commit_time(repo_root: Path, commit: str) -> datetime | None:
    proc = _run(["git", "show", "-s", "--format=%cI", f"{commit}^{{commit}}"], cwd=repo_root)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return datetime.fromisoformat(proc.stdout.strip()).astimezone(timezone.utc)


def checkout_label(repo_root: Path) -> str:
    """``<branch> at <short sha>`` of the checkout whose emission is the target fingerprint."""
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root).stdout.strip() or "?"
    head = _run(["git", "rev-parse", "--short=8", "HEAD"], cwd=repo_root).stdout.strip() or "?"
    return f"{branch} at {head}"


def local_fingerprint(repo_root: Path) -> str:
    """The fingerprint this checkout's args script emits on the xdist path."""
    with tempfile.TemporaryDirectory(prefix="xdist-parity-history-") as tmp:
        env = {**_clean_env(), "VNX_CI_XDIST": "1"}
        proc = subprocess.run(
            ["bash", str(repo_root / "scripts" / "ci" / "profile_a_pytest_args.sh"), f"{tmp}/args"],
            cwd=str(repo_root), env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise ToolError(f"profile_a_pytest_args.sh exit {proc.returncode}: {proc.stderr.strip()[-400:]}")
        try:
            return fingerprint_of_args_dir(Path(tmp) / "args", repo_root / "scripts" / "ci" / "profile_a_constraints.txt",
                                           str(repo_root))
        except (OSError, ValueError) as exc:
            raise ToolError(f"the emitted args cannot be fingerprinted: {exc}") from exc


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "VNX_CI_XDIST"}


def read_oi_status(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    return {str(i.get("id")): str(i.get("status")) for i in items if isinstance(i, dict)}


def default_open_items(repo_root: Path) -> Path:
    from vnx_paths import resolve_data_root

    return resolve_data_root(repo_root) / "state" / "open_items.json"


# --- CLI ---------------------------------------------------------------------------------------------

LAST_D3_MERGE_HELP = (
    "the merge commit of the last merged D3 PR of track ci-profile-a-xdist: the window starts there. "
    "The D3 PRs are the track's D3a..n deliverables (vnx deliverable list --objective ci-profile-a-xdist); "
    "take the merge commit of the one merged last with gh pr view <n> --json mergeCommit --jq .mergeCommit.oid. "
    "Example of that lookup, never a default: #2103 (D3a) gives ff48a463"
)
WATCH_LIST_HELP = (
    "a watch-list OI from triage (plan 'Triage of a difference': a difference stable serially and not "
    "reproduced under the shape gets an OI on the watch list); repeatable. The T0 names these OIs when it "
    "triages a parity difference; their status is read from the open-items ledger "
    "(python3 scripts/open_items_manager.py list)"
)


def _positive_int(value: str) -> int:
    """A run count: a whole number of at least 1."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value} is not a whole number of at least 1")
    return number


def _oi_id(value: str) -> str:
    if not OI_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an OI id of the form OI-<n>")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--last-d3-merge", required=True, metavar="COMMIT", help=LAST_D3_MERGE_HELP)
    watch = parser.add_mutually_exclusive_group(required=True)
    watch.add_argument("--watch-list", action="append", metavar="OI", type=_oi_id, help=WATCH_LIST_HELP)
    watch.add_argument("--no-watch-list", action="store_true", help="state that triage left no watch-list OI")
    parser.add_argument("--repo", default="", help="owner/name (default: gh repo view)")
    parser.add_argument("--workflow", default=WORKFLOW_NAME)
    parser.add_argument("--fingerprint", default="",
                        help="target fingerprint (default: what this checkout emits; run it from the D4 branch)")
    parser.add_argument("--open-items", default="", help="open_items.json (default: this project's ledger)")
    parser.add_argument("--limit", type=_positive_int, default=300, help="how many runs to list (newest first)")
    parser.add_argument("--cache-dir", default="", help="keep downloaded verdicts here between calls")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the table")
    return parser


def _inputs(args, repo_root: Path) -> tuple:
    repo = args.repo or default_repo()
    if args.fingerprint:
        fingerprint, fp_source = args.fingerprint, "given"
    else:
        fingerprint, fp_source = local_fingerprint(repo_root), f"emitted by this checkout: {checkout_label(repo_root)}"
    watch_list = [] if args.no_watch_list else list(args.watch_list)
    oi_status = None
    if watch_list:
        oi_status = read_oi_status(Path(args.open_items) if args.open_items else default_open_items(repo_root))
    header = {"repo": repo, "workflow": args.workflow, "window_base": args.last_d3_merge,
              "window_base_source": "the last D3 merge, given", "fingerprint": fingerprint,
              "fingerprint_source": fp_source, "watch_list": ", ".join(watch_list) or "none (--no-watch-list)"}
    return header, watch_list, oi_status


def _collect(header: dict, args, repo_root: Path, cache: Path) -> tuple:
    """(runs since the window base with their verdicts, whether the list may be cut off).

    Before any run is placed, the history must be able to answer ancestry
    for each: ToolError on a shallow clone, and on a window base or run head
    that is no commit in this checkout.
    """
    if is_shallow(repo_root):
        raise ToolError(SHALLOW_HISTORY)
    since = commit_time(repo_root, header["window_base"])
    if since is None:
        raise ToolError(f"window base {header['window_base']} is not a commit in this checkout: git fetch origin, "
                        f"or name another base")
    listed = list_runs(header["repo"], header["workflow"], args.limit)
    runs = [r for r in listed if _created(r) >= since]
    missing = missing_commits(repo_root, run_heads(runs))
    if missing is not None:
        raise ToolError(missing)
    truncated = len(listed) >= args.limit and len(runs) == len(listed)
    completed = [r for r in runs if r.status == "completed"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for run, fetched in zip(completed, pool.map(lambda r: download_verdict(header["repo"], r, cache), completed)):
            run.verdict, run.verdict_problem = fetched
    return runs, truncated


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        from project_root import resolve_project_root

        repo_root = resolve_project_root(__file__)
        header, watch_list, oi_status = _inputs(args, repo_root)
        with tempfile.TemporaryDirectory(prefix="xdist-parity-verdicts-") as tmp:
            cache = Path(args.cache_dir) if args.cache_dir else Path(tmp)
            runs, truncated = _collect(header, args, repo_root, cache)
            # The rule asks git for each run's ancestry: a git that fails there is a tool error too.
            evaluation = evaluate(
                runs,
                target_fingerprint=header["fingerprint"],
                is_ancestor=lambda head: git_is_ancestor(repo_root, header["window_base"], head),
                watch_list=watch_list,
                oi_status=oi_status,
                list_truncated=truncated,
                job_evidence=job_evidence_provider(header["repo"], cache),
            )
    except (ToolError, OSError, RuntimeError) as exc:
        print(f"xdist_parity_history: {exc}", file=sys.stderr)
        return 2
    print(as_json(evaluation, header) if args.json else render(evaluation, header), end="")
    return 0 if evaluation.holds else 1


if __name__ == "__main__":
    sys.exit(main())
