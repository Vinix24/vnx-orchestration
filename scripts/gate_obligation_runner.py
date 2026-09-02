#!/usr/bin/env python3
"""gate_obligation_runner.py — fulfil declared review-gate obligations.

OI-876 / OI-881: a dispatch spec that declares ``gate=<name>`` must produce a
review-gate request record AND a result record. The dispatch door registers
one obligation per such dispatch (``scripts/lib/gate_obligations.py``); this
runner fulfils them:

  1. Resolve the dispatch's PR number — from the obligation itself (rework
     dispatches carry spec.pr_id), from the dispatch_metadata row the receipt
     pipeline stamps, or from GitHub by head branch (``dispatch/<id>``).
  2. Invoke ``review_gate_manager.request_and_execute`` for exactly the
     declared gate — the same path ``vnx gate`` / ``t0_gate_enforcement.sh``
     use — so the request record and the result record land in
     ``review_gates/requests`` / ``review_gates/results``.
  3. If the gate cannot run, that is a LOUD, REGISTERED outcome:
     ``gate_recorder.record_not_executable`` writes both records with status
     ``not_executable`` plus a skip-rationale audit entry. Silence is not an
     end state.
  4. If no PR exists yet the obligation stays ``pending`` — a genuine wait for
     a PR that has not been opened, which the producer freshness monitor
     (``review_gate_obligations`` producer) flags per gate key once the oldest
     pending declaration exceeds cadence.
  5. If the PR cannot be resolved because the environment is wrong — the
     project has no attributable GitHub ``owner/repo`` (no checkout registered
     in ``~/.vnx/projects.json`` and no GitHub ``origin`` remote) — the
     obligation is recorded in the distinct ``unresolvable`` status (a fault,
     NOT a wait). It stays retryable for a bounded term and then escalates to
     the loud terminal ``not_executable`` with ``reason=unresolvable_timeout``.
  6. OI-1388: "no PR yet" (4) is only a genuine wait while the dispatch could
     still produce one. If no PR ever showed up AND the dispatch's head branch
     (``dispatch/<id>``) no longer exists on GitHub, the dispatch is dead —
     nothing will ever gate this obligation. That is recorded in the terminal
     ``retired`` status (``reason=no_pr_branch_gone``), distinct from
     ``fulfilled`` (no gate ever actually reviewed it). The discriminator is
     the branch's existence, never an age threshold: a branch that still
     exists (or whose existence gh could not determine) leaves the obligation
     ``pending`` exactly as before — a live dispatch must never be closed on
     ambiguous evidence.
  7. OI-1388 residu: a retirement (6) or an unresolvable-timeout escalation
     (5) can be about to book a dispatch as never-reviewed when it was in
     fact gated — the branch was cleaned up, or the environment broke, only
     AFTER a gate already ran. Before either terminal write, the runner
     looks for an existing ``review_gates/results`` record matching the same
     ``dispatch_id`` + ``gate`` that carries complete evidence (non-empty
     ``contract_hash`` and ``report_path``, and the report file still on
     disk — the same bar ``closure_verifier`` enforces). Found, that record
     IS the discriminator: the obligation is booked ``fulfilled``
     (``reason=fulfilled_by_existing_evidence``), never retired or escalated
     over evidence that was there all along.
  8. BETA3-C2 (2026-08-26): item 7's evidence check only asked "is the
     evidence trail complete" (non-empty ``contract_hash``/``report_path``),
     never "what does the verdict say". That let a REJECTED review get
     stamped ``fulfilled`` — glm_gate and kimi_gate have emitted both
     evidence fields on a FAILED run since #1669, and PR #1692's own
     glm_gate verdict on itself (a genuine ``fail`` with complete evidence)
     is the live record that proved it. The discriminator now asks a second
     question before writing, using :func:`gate_status.is_pass` and
     :data:`gate_status.FAIL_STATES` — never a new vocabulary of its own:
     a decided PASS still books ``fulfilled`` exactly as item 7 describes; a
     decided FAIL books the distinct, already-defined terminal ``failed``
     status instead (``reason=failed_by_existing_evidence``) — the
     obligation is discharged, because a gate did review it, but the outcome
     stays visibly negative, never disguised as a clean pass. ``not_executable``
     is REJECTED as evidence outright, inside :func:`_fulfilling_result`
     itself rather than by a caller-side check (the #1688 lesson: a
     caller-side check drifts the moment a new call site is added) — the
     gate never ran, so there is no verdict to rescue anything with, no
     matter what its evidence fields contain.
  9. D2e (2026-08-30, dispatch 20260830-120000-d2e-takeover-keten-bewijs):
     items 1-8 above only ever look at the DECLARED gate's own result file
     (``manager._result_path(gate, pr_number)``). But the review-gate
     takeover chain (``codex_gate -> kimi_gate -> glm_gate -> deepseek_gate``,
     ``gate_request_handler._build_review_gate_takeover_chain``, BETA3-E1)
     can already have substituted a SUCCESSOR gate as the reader at request
     time — and that successor writes its verdict under its OWN name, never
     the declared gate's. Live evidence, PR #1726: ``pr-1726-codex_gate.json``
     stayed a stale ``lane_exhausted`` record forever while
     ``pr-1726-kimi_gate.json`` carried a real, complete-evidence verdict —
     evidence that existed, was complete, and was invisible to the only
     party looking for it. CONTRACT: when the declared gate's own record is
     not itself a decided, complete-evidence verdict, the runner now walks
     the SAME chain (never a second, locally-defined copy —
     :func:`_find_takeover_successor_evidence`) looking for the first
     successor that IS one. Found, the obligation is booked
     ``fulfilled``/``failed`` exactly as items 7/8 already do for the
     declared gate, but the record ALSO carries ``resolved_by_gate`` (the
     successor's name — never silently attributed to the declared gate) and
     ``takeover_hops`` (the full walk). Incomplete evidence at a successor
     (missing ``contract_hash``/``report_path``, or a ``report_path`` whose
     file no longer exists) does NOT count — that is a third, distinct
     outcome (chain walked, nothing usable found anywhere), never conflated
     with either "found at the declared gate" or "found via takeover".
  10. OI-1612: items 7-9's evidence lookup was keyed ONLY on
      ``(dispatch_id, gate)`` — matching a result record only when its OWN
      ``dispatch_id`` field equals the obligation's. That join holds for a
      result THIS runner just wrote itself (``request_and_execute`` stamps
      the obligation's ``dispatch_id`` onto whatever it produces), but a gate
      result file is written per ``(pr_number, gate)`` regardless of who ran
      it — a manually-triggered poortrun, a takeover successor, or any other
      process writes its OWN dispatch_id (the poortrun's, not the build
      dispatch's) into that same slot. Measured live: 345 of 524 results on
      vnx-dev carry a dispatch_id, and it is essentially always the
      poortrun's — the dispatch_id join for a RESOLVED obligation (a PR
      already known) could structurally never match pre-existing evidence
      from any run other than this runner's own, and ``would_stamp`` stayed
      at 0 across the whole store. The evidence index now also keys on
      ``(pr_number, gate)`` (gate results are one-per-(pr_number, gate),
      never one-per-dispatch — the same convention item 9 already documents
      for ``manager._result_path``), and the RESOLVED branch of
      :func:`_pre_execution_decision` looks evidence up by ITS OWN resolved
      ``pr_number`` in addition to ``dispatch_id``. AWAITING/UNRESOLVABLE
      obligations have no ``pr_number`` to look up by definition (that is
      exactly why the dispatch_id join existed in the first place) and keep
      using the dispatch_id-only lookup unchanged. The sha-binding check
      (item 9's :func:`_has_decided_evidence`) is untouched either way — a
      pr_number-matched candidate is rejected exactly as a dispatch_id-
      matched one is when its ``commit_sha`` does not match the PR's current
      head.

Scheduling: launchd ``com.vnx.gate-obligation-runner.plist`` (StartInterval
900s); also safe to run manually at any time — fulfilment is idempotent
(terminal obligations are never re-run).

Exit codes: 0 = no open obligations remain after this run;
11 = one or more obligations still open (pending or unresolvable);
20 = state dir / configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from gate_obligations import (  # noqa: E402
    NO_GATE_KEY,
    REASON_NO_PR_BRANCH_GONE,
    REASON_PR_CLOSED,
    REASON_PR_MERGED,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_RETIRED,
    STATUS_UNRESOLVABLE,
    TERMINAL_STATUSES,
    iter_obligations,
    pr_number_from_pr_id,
    update_obligation,
)
from gate_status import (  # noqa: E402
    FAIL_STATES as _GATE_RESULT_FAIL_STATES,
    PASS_STATES as _GATE_RESULT_PASS_STATES,
    UNAVAILABLE_STATES as _GATE_RESULT_UNAVAILABLE_STATES,
    canonical_status as _gate_canonical_status,
    has_complete_evidence,
    is_pass as _gate_is_pass,
)
from gate_executor import _classify_sha_binding  # noqa: E402

_LOG = logging.getLogger("gate_obligation_runner")

_GH_TIMEOUT_SECONDS = 20

# An obligation whose PR can't be resolved because the environment is wrong
# (repo unattributable, no GitHub remote, gh unusable) is a config FAULT, not a
# wait. It stays retryable in the distinct ``unresolvable`` status for a bounded
# term, then escalates to the loud terminal ``not_executable``. 96 attempts at
# the launchd 900s cadence ≈ 24h — the same window the producer-freshness
# monitor uses before flagging a silently-pending gate key.
_UNRESOLVABLE_ESCALATION_ATTEMPTS = 96

# A gate RESULT can itself report a TEMPORARY refusal rather than a real
# verdict on the code: ``status=running`` means a CI check is still in flight
# (gate_executor's ci_gate path — not all checks reached a terminal bucket
# yet), ``status=not_executable`` with a reason in
# ``_TEMPORARY_NOT_EXECUTABLE_REASONS`` means the gate could not run for a
# reason that can change by itself (gate_result_parser._classify_unavailable /
# gate_request_handler._mark_gate_unavailable), and ``status=unavailable``
# (gate_status.py's UNAVAILABLE_STATES — gate_recorder.record_failure books
# this for an execution failure: crash, timeout, stall, a failed worktree
# checkout) means the provider never produced a verdict at all. None of these
# says anything about the code; all are "not yet", not "no". Treating any of
# them as terminal burns the obligation forever — measured live against PR
# #1627 (OI-1384): CI had actually passed, but the recorded obligation was a
# dead not_executable/provider_disabled with no report_path, no contract_hash,
# no branch, no commit_sha. A consumer project separately measured two more
# shapes of the same bug (OI-1400): PR #967 booked ``fulfilled`` off an
# unavailable/worktree-checkout-failed result (fixed by the
# _GATE_RESULT_UNAVAILABLE_STATES branch below, and by the "anything I don't
# recognise as a pass is NOT a pass" fallback for a status this runner has
# never seen at all), and PR #966 booked ``fulfilled`` off a
# not_executable/provider-not-installed result — both with empty
# contract_hash and empty report_path. PR #966's shape survived that first
# OI-1400 fix: ``not_executable`` is already a known TERMINAL_STATUSES member,
# so it never reached the unknown-status fallback; it fell straight into the
# terminal branch because ``provider_not_installed`` was not yet in this
# frozenset (OI-1400 residu). A provider missing today can be installed
# tomorrow — exactly as fixable as the ``provider_disabled`` config flag — so
# it gets the identical bounded pending/escalate treatment. All temporary
# cases stay pending under a bounded retry term, then escalate to the loud
# terminal not_executable — a temporary refusal that recurs forever must
# still eventually alarm someone, just not on the first attempt.
_TEMPORARY_RESULT_STATUSES = frozenset({"running"})
_TEMPORARY_NOT_EXECUTABLE_REASONS = frozenset({
    # OI-1384: parked by config (e.g. VNX_CI_GATE_REQUIRED=0) — flipping the
    # flag is an operator action that can happen at any time, not a verdict.
    "provider_disabled",
    # OI-1400 residu: the CLI binary is not on PATH right now. A provider that
    # is not installed today can be installed tomorrow, so this is a bounded
    # wait for the environment to catch up, not a permanent refusal.
    "provider_not_installed",
})
_TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS = _UNRESOLVABLE_ESCALATION_ATTEMPTS

# OI-1569 Klaar item 8: a genuine gate run measures 91-405s; a quota refusal
# measures 3-15s; a silent stale-evidence fulfilment (the PR #1719/#1736
# defect this dispatch fixes) measured 7s. None of duration_seconds (absent
# on some lanes by design), recorded_at (cannot tell "just written" from
# "written minutes ago" on its own), or the result file's mtime alone proves
# which of these a booking is — together they can. 60s is comfortably above
# the fastest real quota-refusal bucket and comfortably below the fastest
# genuine-run bucket measured; see _flag_fast_fulfillment_if_evidence_predates_attempt.
_FAST_FULFILLMENT_MTIME_THRESHOLD_SECONDS = 60

# PR-resolution outcomes. The runner must tell "no PR yet" (a wait) apart from
# "cannot resolve because the environment is wrong" (a fault) IN THE RECORD,
# not only in a log line — a pending obligation that is actually misconfigured
# reads as "not yet" forever and never alarms anyone (OI-1253 fix-forward).
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_AWAITING = "awaiting"
RESOLUTION_UNRESOLVABLE = "unresolvable"


@dataclass
class PrResolution:
    """Outcome of resolving an obligation's PR number.

    ``branch_exists`` is only populated when ``status == RESOLUTION_AWAITING``:
    ``True``/``False`` when GitHub was actually queried, ``None`` when it
    could not be determined (or wasn't queried) — never treated as "gone"
    (OI-1388: an obligation must never be retired on ambiguous evidence).
    """

    status: str
    pr_number: Optional[int] = None
    owner_repo: Optional[str] = None
    reason: Optional[str] = None
    branch_exists: Optional[bool] = None


def utc_now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Scope filtering (OI-1378: --since / --dispatch-prefix)
# ---------------------------------------------------------------------------

_DISPATCH_ID_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-")


def _parse_declared_at_date(value: Any) -> Optional[str]:
    """Return the ``YYYY-MM-DD`` prefix of a parseable ISO8601 ``declared_at``.

    Returns None when the value is missing, blank, or not a parseable
    timestamp — the caller falls back to the dispatch_id date prefix rather
    than guess.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    from datetime import datetime  # noqa: PLC0415

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return text[:10]


def _dispatch_id_date(dispatch_id: str) -> Optional[str]:
    """Return the ``YYYY-MM-DD`` date prefix of a ``YYYYMMDD-...`` dispatch_id."""
    match = _DISPATCH_ID_DATE_RE.match(dispatch_id or "")
    if not match:
        return None
    year, month, day = match.groups()
    from datetime import date  # noqa: PLC0415

    try:
        date(int(year), int(month), int(day))
    except ValueError:
        return None
    return f"{year}-{month}-{day}"


def _obligation_scope_date(record: Dict[str, Any], dispatch_id: str) -> Optional[str]:
    """Resolve the date used to test an obligation against ``--since``.

    Prefers ``declared_at``; falls back to the dispatch_id's date prefix when
    ``declared_at`` is absent or unparseable. Returns None when neither source
    yields a real date — an obligation with no known date counts as OUT of
    scope under an active ``--since`` (scope means scope, no silent inclusion).
    """
    return _parse_declared_at_date(record.get("declared_at")) or _dispatch_id_date(dispatch_id)


def _valid_since_date(value: str) -> str:
    """argparse type= validator for ``--since``: must be a real YYYY-MM-DD date."""
    from datetime import date  # noqa: PLC0415

    parts = value.split("-")
    try:
        if len(parts) != 3:
            raise ValueError(value)
        date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --since date {value!r}: expected YYYY-MM-DD"
        ) from None
    return value


def _in_scope(
    path: Path,
    record: Dict[str, Any],
    *,
    since: Optional[str],
    dispatch_prefix: Optional[str],
) -> bool:
    """Whether an obligation is inside the ``--since`` / ``--dispatch-prefix`` scope."""
    dispatch_id = str(record.get("dispatch_id") or path.stem)
    if dispatch_prefix and not dispatch_id.startswith(dispatch_prefix):
        return False
    if since:
        scope_date = _obligation_scope_date(record, dispatch_id)
        if scope_date is None or scope_date < since:
            return False
    return True


# ---------------------------------------------------------------------------
# PR / branch resolution
# ---------------------------------------------------------------------------


def _pr_from_dispatch_metadata(state_dir: Path, dispatch_id: str) -> Optional[int]:
    """Read the pr_id the receipt pipeline stamped for this dispatch (read-only)."""
    db_path = Path(state_dir) / "quality_intelligence.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT pr_id FROM dispatch_metadata "
                "WHERE dispatch_id = ? AND pr_id IS NOT NULL AND pr_id != '' "
                "ORDER BY id DESC LIMIT 1",
                (dispatch_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _LOG.debug("dispatch_metadata lookup failed for %s: %s", dispatch_id, exc)
        return None
    if not row:
        return None
    return pr_number_from_pr_id(str(row[0]))


def _gh_json(args: List[str], *, owner_repo: Optional[str] = None) -> Optional[Any]:
    """Run a gh CLI command scoped to ``owner_repo``, returning parsed JSON.

    ``owner_repo`` (``owner/repo``) is injected as ``--repo`` so the query never
    depends on the ambient cwd. A central install's cwd is the install tree
    whose ``origin`` is a release-time temp checkout; bare ``gh`` resolves that
    as the repo and returns nothing, which read as an eternal "no PR yet"
    instead of the misconfiguration it actually is (OI-1253 fix-forward).
    Returns None on any failure (gh missing, timeout, non-zero exit, non-JSON).
    """
    if shutil.which("gh") is None:
        return None
    cmd = ["gh"]
    if owner_repo:
        cmd += ["--repo", owner_repo]
    cmd += args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _LOG.debug("gh %s failed: %s", args[:2], exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _pr_from_github(dispatch_id: str, owner_repo: str) -> Optional[int]:
    """Find the open/merged PR whose head branch is dispatch/<dispatch_id>."""
    data = _gh_json(
        ["pr", "list", "--state", "all", "--head", f"dispatch/{dispatch_id}",
         "--json", "number", "--limit", "1"],
        owner_repo=owner_repo,
    )
    if isinstance(data, list) and data:
        number = data[0].get("number")
        if isinstance(number, int):
            return number
    return None


def _branch_from_github(pr_number: int, owner_repo: str) -> Optional[str]:
    data = _gh_json(
        ["pr", "view", str(pr_number), "--json", "headRefName"],
        owner_repo=owner_repo,
    )
    if isinstance(data, dict):
        branch = data.get("headRefName")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    return None


def _branch_exists_on_github(dispatch_id: str, owner_repo: str) -> Optional[bool]:
    """Whether ``dispatch/<dispatch_id>`` still exists as a head ref on GitHub.

    Returns ``True``/``False`` on a definite answer, ``None`` when it cannot be
    determined (gh missing, timeout, network error, or any non-404 failure).
    OI-1388: a caller may only treat ``False`` as "the dispatch died without a
    PR" — ``None`` must stay a wait, never a retirement, so a transient gh
    hiccup can never close an obligation on ambiguous evidence.
    """
    if shutil.which("gh") is None:
        return None
    branch = f"dispatch/{dispatch_id}"
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/branches/{branch}"],
            capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _LOG.debug("branch existence check failed for %s: %s", branch, exc)
        return None
    if proc.returncode == 0:
        return True
    combined = f"{proc.stdout}\n{proc.stderr}"
    if "404" in combined or "Not Found" in combined:
        return False
    _LOG.debug(
        "branch existence check for %s returned an unrecognised failure "
        "(rc=%s) — treating as unknown, not gone: %s",
        branch, proc.returncode, combined.strip(),
    )
    return None


def _pr_state_from_github(pr_number: int, owner_repo: str) -> Optional[str]:
    """Whether a resolved PR is ``OPEN``, ``MERGED``, or ``CLOSED`` on GitHub.

    OI-1508: the RESOLVED branch of :func:`_pre_execution_decision` knows a
    PR number but nothing about its live disposition — measuring it here is
    the read-only ``gh pr view <n> --json state`` call that the RESOLVED
    branch was missing entirely, unconditionally treating every resolved PR
    as still open and re-attempting the gate on it.

    Returns the state string on a definite answer, ``None`` when it cannot
    be determined (gh missing, timeout, network error, or an unrecognised
    response) — mirrors :func:`_branch_exists_on_github`: a caller may only
    retire on a definite ``MERGED``/``CLOSED``, never on ``None`` (OI-1388:
    ambiguous evidence must never close an obligation).
    """
    data = _gh_json(
        ["pr", "view", str(pr_number), "--json", "state"],
        owner_repo=owner_repo,
    )
    if isinstance(data, dict):
        state = data.get("state")
        if isinstance(state, str) and state.strip():
            return state.strip().upper()
    return None


def _owner_repo_from_remote_url(url: str) -> Optional[str]:
    """Derive ``owner/repo`` from a GitHub remote URL (https or ssh form).

    Mirrors the regex in ``chain_origin_anchor._owner_repo_from_remote``; the
    runner stays a lightweight stdlib script and must not import that module.
    Returns None for any non-GitHub URL — including a local-filesystem origin,
    which is a release/install artifact, never a project identity (OI-1253).
    """
    match = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$", url.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _git_remote_origin(project_root: Path) -> Optional[str]:
    """Return the ``origin`` remote URL for ``project_root``, or None."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _project_checkout_path(project_id: str) -> Optional[Path]:
    """Resolve the project's checkout path from the operator registry.

    ``~/.vnx/projects.json`` (vnx_identity schema v2) maps ``project_id`` →
    ``path``. This is the cwd-independent link from a central-install runner's
    store (``~/.vnx-data/<project_id>/state``) back to the actual checkout whose
    ``origin`` remote is a real GitHub URL. Returns None when the id is not
    registered or the path is gone.
    """
    if not project_id:
        return None
    try:
        registry_path = Path("~/.vnx/projects.json").expanduser()
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in registry.get("projects", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("project_id") != project_id:
            continue
        raw_path = entry.get("path")
        if not raw_path:
            continue
        try:
            candidate = Path(raw_path).expanduser()
        except (OSError, ValueError):
            continue
        if candidate.is_dir():
            return candidate
    return None


def _resolve_github_owner_repo(state_dir: Path) -> Optional[str]:
    """Resolve the GitHub ``owner/repo`` whose PRs this runner's obligations live in.

    CWD-independent on purpose: ``gh`` must never infer the repo from the
    ambient cwd, because a central install's cwd is the install tree (its origin
    is a release-time temp checkout). Resolution order:

      1. The checkout registered for the runner's project_id
         (``~/.vnx/projects.json``) — its ``origin`` remote is the project's
         real GitHub URL, even when the runner runs from ``$VNX_HOME``.
      2. The current working directory — only as a convenience fallback for a
         dev checkout run from within the repo. A local-path origin never
         matches, so this fallback cannot fabricate an owner/repo from an
         install's temp checkout.

    Returns None when neither source yields a GitHub remote — the caller turns
    that into the distinct, loud ``unresolvable`` state, never a silent wait.
    """
    import vnx_paths  # noqa: PLC0415

    pid = (
        vnx_paths.project_id_from_state_dir(state_dir)
        or (os.environ.get("VNX_PROJECT_ID") or "").strip()
    )
    candidates: List[Path] = []
    if pid:
        checkout = _project_checkout_path(pid)
        if checkout is not None:
            candidates.append(checkout)
    candidates.append(Path.cwd().resolve())

    for root in candidates:
        url = _git_remote_origin(root)
        if not url:
            continue
        owner_repo = _owner_repo_from_remote_url(url)
        if owner_repo:
            return owner_repo
    return None


def resolve_pr_number(state_dir: Path, record: Dict[str, Any]) -> PrResolution:
    """Resolve the obligation's PR number from every available source.

    Returns a :class:`PrResolution` carrying one of three statuses so the caller
    can record the distinction in the obligation's state:

      - ``resolved``   — a PR number is known (record, dispatch metadata, GitHub).
      - ``awaiting``   — the repo resolves and ``gh`` works, but no PR exists yet
                         for the head branch: a genuine wait, stays ``pending``.
      - ``unresolvable`` — the environment is wrong (no GitHub owner/repo, no
                         checkout, gh missing): a fault, recorded distinctly.
    """
    pr_number = record.get("pr_number")
    if isinstance(pr_number, int) and pr_number > 0:
        return PrResolution(RESOLUTION_RESOLVED, pr_number=pr_number)
    dispatch_id = str(record.get("dispatch_id") or "")
    if not dispatch_id:
        return PrResolution(
            RESOLUTION_UNRESOLVABLE,
            reason="obligation has no dispatch_id to resolve a PR for",
        )

    from_metadata = _pr_from_dispatch_metadata(state_dir, dispatch_id)
    if from_metadata:
        return PrResolution(RESOLUTION_RESOLVED, pr_number=from_metadata)

    owner_repo = _resolve_github_owner_repo(state_dir)
    if not owner_repo:
        return PrResolution(
            RESOLUTION_UNRESOLVABLE,
            reason=(
                "cannot resolve a GitHub owner/repo for this runner: the "
                "project checkout is not registered (~/.vnx/projects.json) and "
                "no GitHub 'origin' remote resolves from the checkout or cwd. "
                "gh would otherwise infer the repo from the ambient cwd, which "
                "for a central install is a release-time temp checkout (OI-1253)"
            ),
        )
    from_github = _pr_from_github(dispatch_id, owner_repo)
    if from_github:
        return PrResolution(
            RESOLUTION_RESOLVED, pr_number=from_github, owner_repo=owner_repo,
        )
    # OI-1388: no PR exists yet, but that reads as "not yet" forever unless we
    # also ask whether the dispatch could still produce one. A dead branch is
    # the one fact that answers that without a timer.
    branch_exists = _branch_exists_on_github(dispatch_id, owner_repo)
    return PrResolution(
        RESOLUTION_AWAITING,
        owner_repo=owner_repo,
        branch_exists=branch_exists,
        reason=f"no PR yet for head branch dispatch/{dispatch_id}",
    )


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------


def _get_pr_head_sha_for_gate(pr_number: Optional[int]) -> str:
    """The PR's current GitHub head commit sha, for OI-1571 tak 3 sha-binding
    checks (:func:`_has_decided_evidence`).

    A thin wrapper around ``gate_recorder.get_pr_head_sha`` -- the SAME
    canonical source ``gate_executor._execute_requested_gates`` already uses
    for its own sha-binding check (OI-1307/B6: gh, never a local ``git
    rev-parse HEAD``, which would answer for the runner's own checkout, not
    the PR). A separate, module-level wrapper here (rather than a call
    inline) so tests can monkeypatch this runner's own resolution
    independently of gate_executor's identical call. Returns "" for a
    missing/invalid pr_number -- :func:`gate_executor._classify_sha_binding`
    already treats an empty sha on either side as ``unknown``, never a
    ``mismatch``.
    """
    if not isinstance(pr_number, int) or pr_number <= 0:
        return ""
    from gate_recorder import get_pr_head_sha  # noqa: PLC0415

    return get_pr_head_sha(pr_number)


def _flag_fast_fulfillment_if_evidence_predates_attempt(
    result_file: Path, gate: str, pr_number: int, dispatch_id: str,
) -> Optional[str]:
    """OI-1569 Klaar item 8: a fulfilment/failure booked from a result FILE
    that was not actually touched by this attempt is not provably a fresh
    run this cycle. A LOUD warning, never a new blocking mechanism — a
    dispatch with no new commits can legitimately reuse an already-current
    evidence file, and that must still fulfil — this only makes the shape
    VISIBLE (log line + returned detail for the outcome) so a silent
    stale-evidence fulfilment stays observable even if some future change
    reopens the hole the sha check in :func:`fulfill_obligation` closes.

    Returns None when the file is missing/unreadable or was touched
    recently enough (age < :data:`_FAST_FULFILLMENT_MTIME_THRESHOLD_SECONDS`)
    — the overwhelmingly common, unremarkable case.
    """
    try:
        mtime = result_file.stat().st_mtime
    except OSError:
        return None
    from datetime import datetime, timezone  # noqa: PLC0415

    age_seconds = datetime.now(timezone.utc).timestamp() - mtime
    if age_seconds < _FAST_FULFILLMENT_MTIME_THRESHOLD_SECONDS:
        return None
    detail = (
        f"{gate} fulfilment for PR #{pr_number} (dispatch {dispatch_id}) used "
        f"evidence whose file was last written {age_seconds:.0f}s before this "
        "attempt resolved — not provably a fresh run this cycle (OI-1569 Klaar item 8)"
    )
    _LOG.warning("gate_obligation_runner: %s", detail)
    return detail


def _build_manager(state_dir: Path):
    """Construct a ReviewGateManager pinned to the runner's state dir.

    ensure_env() only fills MISSING env keys, so pinning VNX_DATA_DIR /
    VNX_STATE_DIR before the import-time path resolution makes the manager
    write into exactly the store this runner was pointed at — never into an
    ambient default store.
    """
    state_dir = Path(state_dir)
    # VNX_STATE_DIR is honored directly by resolve_paths(); VNX_DATA_DIR only
    # via the explicit-override pair. Pin both so a runner pointed at store X
    # can never scatter request/result/report paths across an ambient store.
    os.environ["VNX_STATE_DIR"] = str(state_dir)
    os.environ["VNX_DATA_DIR"] = str(state_dir.parent)
    os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
    from review_gate_manager import ReviewGateManager  # noqa: PLC0415

    return ReviewGateManager()


def _record_loud_not_executable(
    state_dir: Path,
    *,
    gate: str,
    pr_number: int,
    dispatch_id: str,
    reason: str,
    reason_detail: str,
) -> Dict[str, Any]:
    """Write the loud request+result records for a gate that could not run."""
    from gate_recorder import record_not_executable  # noqa: PLC0415

    # The manager's own __init__ normally creates these, but we land here
    # precisely when the manager could not run — never let a missing dir turn
    # the loud failure path into a silent one.
    requests_dir = Path(state_dir) / "review_gates" / "requests"
    results_dir = Path(state_dir) / "review_gates" / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    request_payload: Dict[str, Any] = {
        "gate": gate,
        "pr_number": pr_number,
        "dispatch_id": dispatch_id,
        "mode": "per_pr",
        "origin": "gate_obligation_runner",
        "requested_at": utc_now_iso(),
    }
    return record_not_executable(
        gate=gate,
        pr_number=pr_number,
        pr_id="",
        reason=reason,
        reason_detail=reason_detail,
        request_payload=request_payload,
        requests_dir=requests_dir,
        results_dir=results_dir,
        state_dir=Path(state_dir),
    )


class GateResultIndex(NamedTuple):
    """Two views over ``review_gates/results``, built once per :func:`run` call.

    ``by_dispatch`` keys on ``(dispatch_id, gate)`` — the join that holds for
    a result THIS runner wrote itself (``manager.request_and_execute(...,
    dispatch_id=dispatch_id)`` stamps the obligation's own dispatch_id onto
    whatever it produces). AWAITING/UNRESOLVABLE obligations have no
    ``pr_number`` at all, so this is the only lookup available to them.

    ``by_pr`` keys on ``(pr_number, gate)`` — a gate result file is written
    one-per-(pr_number, gate) regardless of which poortrun produced it (the
    same convention :func:`_find_takeover_successor_evidence`'s docstring
    already documents via ``manager._result_path(gate, pr_number)``). OI-1612:
    this is the join a RESOLVED obligation (``pr_number`` already known)
    actually needs — its own ``dispatch_id`` almost never equals the
    dispatch_id a pre-existing result record carries, because that field
    records the POORTRUN that produced the result, not the build dispatch the
    obligation was declared for.
    """

    by_dispatch: Dict[Tuple[str, str], List[Tuple[Path, Dict[str, Any]]]]
    by_pr: Dict[Tuple[int, str], List[Tuple[Path, Dict[str, Any]]]]


def _index_gate_results(state_dir: Path) -> GateResultIndex:
    """Index ``review_gates/results`` records by ``(dispatch_id, gate)`` AND
    by ``(pr_number, gate)`` — see :class:`GateResultIndex`.

    Built once per :func:`run` call so the OI-1388 evidence discriminator
    (defect 1) costs O(results) instead of O(obligations * results). A
    malformed/unreadable result file is skipped, never raised — the same
    tolerance :func:`fulfill_obligation` already applies when it reads a
    result file's status back after invoking a gate.

    OI-1612: a record is indexed by whichever key(s) it actually carries — a
    record with a ``pr_number`` but no ``dispatch_id`` (179 of 524, measured
    live) used to be skipped entirely; it now lands in ``by_pr`` even though
    ``by_dispatch`` has nothing for it. A record needs at least ``gate`` and
    one of ``dispatch_id``/``pr_number`` to be indexed at all.
    """
    results_dir = Path(state_dir) / "review_gates" / "results"
    by_dispatch: Dict[Tuple[str, str], List[Tuple[Path, Dict[str, Any]]]] = {}
    by_pr: Dict[Tuple[int, str], List[Tuple[Path, Dict[str, Any]]]] = {}
    if not results_dir.is_dir():
        return GateResultIndex(by_dispatch, by_pr)
    for entry in sorted(results_dir.glob("*.json")):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        gate = str(record.get("gate") or "")
        if not gate:
            continue
        dispatch_id = str(record.get("dispatch_id") or "")
        if dispatch_id:
            by_dispatch.setdefault((dispatch_id, gate), []).append((entry, record))
        pr_number = record.get("pr_number")
        if isinstance(pr_number, int):
            by_pr.setdefault((pr_number, gate), []).append((entry, record))
    return GateResultIndex(by_dispatch, by_pr)


# OI-1571 tak 3: four outcomes for "is this record usable evidence", never
# collapsed to a bare bool. A record can be structurally decided (complete
# evidence, on-disk report, a real pass/fail verdict) and STILL not count —
# because it is evidence about a DIFFERENT commit than the PR head, or
# because whether it is current or not cannot even be determined.
_EVIDENCE_NOT_DECIDED = "not_decided"
_EVIDENCE_USABLE = "usable"
_EVIDENCE_MISMATCH = "mismatch"
_EVIDENCE_UNKNOWN_SHA = "unknown_sha"


def _has_decided_evidence(record: Dict[str, Any], head_sha: str) -> Tuple[str, str]:
    """Classify whether ``record`` is USABLE evidence for the PR at ``head_sha``.

    "Complete evidence" mirrors ``gate_status.has_complete_evidence``
    (non-empty ``contract_hash`` AND ``report_path`` — the same bar
    ``closure_verifier`` enforces at merge time), plus an on-disk check that
    the report file the record points to still exists, plus (BETA3-C2) a
    DECIDED-verdict check: ``not_executable`` is never evidence regardless
    of what its evidence fields contain (the gate never ran), and neither is
    any other status that is not a decided pass/fail (incomplete,
    ``unavailable``, or unrecognised) — only :func:`gate_status.is_pass`
    returning True, or a status in :data:`gate_status.FAIL_STATES`, counts.
    Any of these gaps returns :data:`_EVIDENCE_NOT_DECIDED`.

    OI-1571 tak 3: a record that clears every check above can still be
    evidence about ANOTHER commit -- measured live on PR #1719 (a glm_gate
    verdict from the day before, about a since-superseded head) and PR #1736
    (a codex_gate verdict left on disk by an earlier dispatch against the
    same PR, picked up unchanged by a later one). This function is the
    single place that decides that, via
    :func:`gate_executor._classify_sha_binding` -- the SAME function the
    merge door and the fresh-execution path already use, never a second sha
    comparison:

      - ``match``    -> :data:`_EVIDENCE_USABLE`: the ONLY outcome a caller
        may treat as found.
      - ``mismatch`` -> :data:`_EVIDENCE_MISMATCH`: provably about a
        different commit. A caller must reject it exactly as it would
        reject "no evidence", but the returned detail names both shas so
        whatever record documents the rejection can carry the reason.
      - ``unknown``  -> :data:`_EVIDENCE_UNKNOWN_SHA`: the binding itself
        could not be determined (``head_sha`` or the record's own
        ``commit_sha`` is empty). A THIRD outcome, distinct from both
        ``usable`` and ``mismatch`` -- a caller must suspend judgement
        (never silently accept, never silently refuse) rather than guess
        which of the two this unverifiable case actually is.

    Factored out of :func:`_fulfilling_result` (D2e) so the OI-1388
    evidence-index lookup and the takeover-chain walk
    (:func:`_find_takeover_successor_evidence`, which reads records fresh
    off disk rather than through an index) share exactly ONE "is this
    usable evidence" predicate instead of two that could silently drift
    apart -- and, since D2e, the declared-gate gating check inside
    :func:`fulfill_obligation` shares it too: a THIRD direct call site,
    not just the two above.
    """
    if not has_complete_evidence(record):
        return _EVIDENCE_NOT_DECIDED, "incomplete evidence (contract_hash/report_path)"
    if not Path(str(record.get("report_path"))).exists():
        return _EVIDENCE_NOT_DECIDED, "report_path no longer exists on disk"
    status = _gate_canonical_status(record)
    if status == "not_executable":
        return _EVIDENCE_NOT_DECIDED, "not_executable is never evidence — the gate never ran"
    passed, _reason = _gate_is_pass(record)
    if not (passed or status in _GATE_RESULT_FAIL_STATES):
        return _EVIDENCE_NOT_DECIDED, f"status {status!r} is not a decided pass/fail verdict"

    result_sha = str(record.get("commit_sha") or "")
    binding = _classify_sha_binding(head_sha, result_sha)
    if binding == "match":
        return _EVIDENCE_USABLE, "commit_sha matches the PR head"
    if binding == "mismatch":
        return _EVIDENCE_MISMATCH, (
            f"result records commit {result_sha[:8] or '?'} but the PR head is "
            f"{head_sha[:8] or '?'} — this verdict is about other code"
        )
    return _EVIDENCE_UNKNOWN_SHA, (
        "sha binding unknown — head_sha or the record's commit_sha is missing; "
        "cannot verify whether this evidence belongs to the current head"
    )


def _fulfilling_result(
    index: GateResultIndex,
    dispatch_id: str,
    gate: str,
    pr_number: Optional[int] = None,
) -> Dict[str, Any]:
    """Scan for a complete-evidence, DECIDED, sha-CURRENT result — a gate
    that actually ran, reached a verdict, and is provably about the PR's
    current head.

    OI-1612: candidates come from BOTH of :class:`GateResultIndex`'s views —
    ``index.by_dispatch[(dispatch_id, gate)]`` (a result THIS runner wrote
    itself for this exact obligation) and, when ``pr_number`` is given,
    ``index.by_pr[(pr_number, gate)]`` (any result for this PR+gate,
    regardless of which poortrun's dispatch_id it carries — see
    :class:`GateResultIndex`). A record present in both is scanned once, not
    twice. ``pr_number`` is only available for a RESOLVED obligation; the
    AWAITING/UNRESOLVABLE callers pass ``None`` and get exactly the
    pre-OI-1612 dispatch_id-only behaviour.

    OI-1388 defect 1: a terminal retirement/escalation must never overwrite
    evidence that a gate already produced. OI-1571 tak 3 tightens "already
    produced" to also mean "still about the current commit" — each
    candidate's PR head is resolved fresh via
    :func:`_get_pr_head_sha_for_gate` off the CANDIDATE record's own
    ``pr_number`` (never the caller's obligation, which by construction
    could not resolve one — that is exactly why this rescue path exists).
    This sha check applies identically to every candidate regardless of
    which index found it — a pr_number-matched candidate about a different
    commit is rejected exactly like a dispatch_id-matched one would be.

    Returns a dict, never a bare ``Optional[Tuple]`` (OI-1571 tak 3: an
    UNVERIFIABLE candidate must not collapse into "nothing found" — that
    would silently retire/escalate over evidence that might still be
    current):

      - ``{"kind": "found", "entry": (path, record)}`` — sha-matching,
        decided evidence. The only outcome a caller may fulfil/fail on.
      - ``{"kind": "unverifiable", "entry": (path, record), "detail": ...}``
        — decided evidence exists but its sha binding could not be
        verified. A caller must suspend judgement, not retire/escalate.
      - ``{"kind": "absent", "detail": Optional[str]}`` — nothing decided
        and current was found. ``detail`` carries the reason for the first
        MISMATCH seen (both shas), when one was, so a caller's
        retire/escalate record can document why a candidate was rejected
        rather than silently proceeding as if nothing existed at all.

    The caller determines PASS vs. FAIL on a ``"found"`` entry via
    :func:`gate_status.is_pass` — this function only answers "does usable
    evidence exist", never "what should the obligation be stamped".
    """
    candidates: List[Tuple[Path, Dict[str, Any]]] = list(index.by_dispatch.get((dispatch_id, gate), []))
    if isinstance(pr_number, int):
        seen_paths = {path for path, _record in candidates}
        for entry in index.by_pr.get((pr_number, gate), []):
            path, _record = entry
            if path not in seen_paths:
                candidates.append(entry)
                seen_paths.add(path)

    unverifiable: Optional[Tuple[Path, Dict[str, Any], str]] = None
    mismatch_detail: Optional[str] = None
    for entry, record in candidates:
        candidate_pr = record.get("pr_number")
        head_sha = (
            _get_pr_head_sha_for_gate(candidate_pr) if isinstance(candidate_pr, int) else ""
        )
        kind, detail = _has_decided_evidence(record, head_sha)
        if kind == _EVIDENCE_USABLE:
            return {"kind": "found", "entry": (entry, record)}
        if kind == _EVIDENCE_UNKNOWN_SHA and unverifiable is None:
            unverifiable = (entry, record, detail)
        if kind == _EVIDENCE_MISMATCH and mismatch_detail is None:
            mismatch_detail = detail
    if unverifiable is not None:
        entry, record, detail = unverifiable
        return {"kind": "unverifiable", "entry": (entry, record), "detail": detail}
    return {"kind": "absent", "detail": mismatch_detail}


def _find_takeover_successor_evidence(
    manager: Any,
    declared_gate: str,
    pr_number: int,
    head_sha: str,
) -> Optional[Tuple[str, Path, Dict[str, Any], List[str]]]:
    """D2e (dispatch 20260830-120000-d2e-takeover-keten-bewijs): walk the
    review-gate takeover chain FORWARD from ``declared_gate``, reading each
    successor's OWN result record FRESH off disk, looking for the first one
    carrying decided, complete evidence.

    ``gate_request_handler._dispatch_review_seat`` already substitutes a
    successor gate as the READER at request time once the declared gate's
    own last-recorded result classifies as ``lane_exhausted`` — but it
    writes the request/result records under the SUCCESSOR's own name
    (``pr-<n>-<successor>.json``), never under the declared gate's. A
    declared-gate obligation whose own result file never advances past that
    exhaustion (live evidence, PR #1726: ``pr-1726-codex_gate.json`` stayed
    the stale exhaustion record while ``pr-1726-kimi_gate.json`` carried the
    real, complete-evidence verdict) must still be able to find that
    verdict — this is the READ-side mirror of the SAME operator-configured
    chain, sourced from the single place it is built
    (``gate_request_handler._build_review_gate_takeover_chain``) rather than
    a second, locally-defined copy that could drift out of step with it.

    Read FRESH from disk via ``manager._result_path`` — never a
    pre-execution :func:`_index_gate_results` snapshot — because the caller
    invokes this immediately after ``manager.request_and_execute`` may have
    just written or updated exactly the successor record being looked for.
    Keyed by ``pr_number`` alone, matching the SAME convention the
    pre-existing declared-gate lookup two lines above this call site
    already uses (``manager._result_path(gate, pr_number)``) — gate result
    files are one-per-(pr_number, gate), never indexed by dispatch_id.

    Returns ``(gate, path, record, hops)`` for the first successor with
    usable evidence — ``hops`` is the full walk INCLUDING ``declared_gate``
    at index 0, so the obligation record can carry the whole path, not just
    the landing. Returns ``None`` when the chain runs out (or
    ``declared_gate`` has no configured successor at all) with nothing
    found — the caller falls through to the pre-existing declared-gate-only
    handling unchanged. That is its own visible outcome (the third branch
    D2e requires), never silently folded into either "found at the declared
    gate" or "found via takeover": a chain that legitimately ends at
    ``deepseek_gate`` (a named, ratified skip — its runner ships in a
    separate dispatch, E2) walks off the end of ``chain`` here exactly the
    same way a chain that never produced any evidence at all does, and
    both correctly return ``None``.

    OI-1571 tak 3: a successor's own record can ALSO be decided/complete
    but about a different commit (the live PR #1719 case: a glm_gate PASS
    from the day before) or sha-unverifiable. Both are rejected here via
    :func:`_has_decided_evidence` exactly like an incomplete/undecided
    record — the walk simply keeps going. The caller
    (:func:`fulfill_obligation`) is the one that must not silently retire a
    dispatch just because THIS walk found nothing usable; the walk itself
    only ever answers "found" or "found nothing", same as before D2e.
    """
    from gate_request_handler import _build_review_gate_takeover_chain  # noqa: PLC0415

    chain = _build_review_gate_takeover_chain()
    hops = [declared_gate]
    current = declared_gate
    while current in chain:
        current = chain[current]
        hops.append(current)
        candidate_path = manager._result_path(current, pr_number)
        if not candidate_path.exists():
            continue
        try:
            candidate_record = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(candidate_record, dict):
            continue
        kind, _detail = _has_decided_evidence(candidate_record, head_sha)
        if kind != _EVIDENCE_USABLE:
            continue
        return current, candidate_path, candidate_record, hops
    return None


def _terminal_evidence_contradictions(
    obligations: List[Tuple[Path, Dict[str, Any]]],
    result_index: GateResultIndex,
) -> List[Dict[str, Any]]:
    """Report ALREADY-terminal obligations whose evidence contradicts them.

    Read-only diagnostic (OI-1388 residu — measured live on both
    ~/.vnx-data/vnx-dev and ~/.vnx-data/mission-control 2026-08-26): a
    retirement/escalation booked before this dispatch's fix landed can have
    left an obligation ``retired``/``not_executable`` even though a
    complete-evidence gate result already existed for the same dispatch_id +
    gate. This never rewrites those records — flipping a terminal status
    back is its own write action with its own risk — it only surfaces them
    so a human can decide.

    Only ``STATUS_RETIRED``/``STATUS_NOT_EXECUTABLE`` are checked: those are
    the two statuses this dispatch's fix intercepts pre-write. A ``no_gate``
    obligation (``gate == NO_GATE_KEY``) never had a gate to review it, so it
    is excluded — there is no dispatch_id+gate result to contradict it.

    OI-1612: the lookup also passes the obligation's own ``pr_number`` (when
    the record carries one) so this diagnostic surfaces the SAME class of
    contradiction the pre-execution rescue now catches — a terminal record
    whose evidence carries a poortrun's own dispatch_id rather than the
    obligation's.
    """
    contradictions: List[Dict[str, Any]] = []
    for path, record in obligations:
        status = record.get("status")
        if status not in (STATUS_RETIRED, STATUS_NOT_EXECUTABLE):
            continue
        gate = str(record.get("gate") or "")
        if not gate or gate == NO_GATE_KEY:
            continue
        dispatch_id = str(record.get("dispatch_id") or path.stem)
        obligation_pr_number = record.get("pr_number")
        lookup = _fulfilling_result(
            result_index, dispatch_id, gate,
            pr_number=obligation_pr_number if isinstance(obligation_pr_number, int) else None,
        )
        if lookup["kind"] != "found":
            continue
        evidence_path, _evidence_record = lookup["entry"]
        reason = record.get("reason")
        contradictions.append(
            {
                "dispatch_id": dispatch_id,
                "gate": gate,
                "obligation_status": status,
                "obligation_reason": reason,
                # OI-1388 residu (T0 addendum): a MISSING reason (None) is
                # distinct from an EMPTY string and from a KNOWN reason
                # value — never collapse the three into one bucket.
                "obligation_reason_bucket": (
                    "missing" if reason is None
                    else "empty" if reason == ""
                    else "known"
                ),
                "obligation_path": str(path),
                "evidence_result_path": str(evidence_path),
            }
        )
    return contradictions


def _evidence_decision(evidence: Tuple[Path, Dict[str, Any]]) -> Dict[str, Any]:
    """Turn OI-1388-rescue evidence into a decision, split by verdict (BETA3-C2).

    ``_fulfilling_result`` guarantees ``evidence`` is a DECIDED verdict (never
    ``not_executable``, never an incomplete/unavailable/unrecognised status)
    — but decided can still mean PASS or FAIL, and the two must never be
    booked the same way. A PASS is exactly the OI-1388 defect-1 rescue:
    ``fulfill_by_evidence`` -> ``STATUS_FULFILLED``, unchanged. A FAIL must
    NOT reuse that path — ``fulfilled`` reads as "reviewed, no problem" to
    every downstream consumer that scans for it, and PR #1692's own
    glm_gate verdict on itself is the live record of exactly what that would
    launder: a rejection turned into a clean pass. ``fulfill_by_failed_evidence``
    keeps the obligation discharged (a gate DID review this) while keeping
    the negative outcome visible.
    """
    _path, record = evidence
    passed, _reason = _gate_is_pass(record)
    kind = "fulfill_by_evidence" if passed else "fulfill_by_failed_evidence"
    return {"kind": kind, "evidence": evidence}


def _pre_execution_decision(
    state_dir: Path,
    dispatch_id: str,
    gate: str,
    resolution: PrResolution,
    attempts: int,
    result_index: GateResultIndex,
) -> Dict[str, Any]:
    """Decide what happens to an obligation before any gate is actually run.

    Shared by the real run (:func:`fulfill_obligation`, which then acts on
    the decision) and the dry run (:func:`_dry_run_outcome`, which only
    reports it) so the two paths can never diverge into two decision trees
    (OI-1388 defect 2). Every input here is already read-only-derivable: PR
    resolution and the branch-existence check both happen inside
    :func:`resolve_pr_number` via read-only ``gh`` calls (safe in a dry run),
    the evidence lookup is a local file read, and the RESOLVED branch's own
    ``gh pr view`` state check (:func:`_pr_state_from_github`, OI-1508) is
    read-only too — ``state_dir`` is only needed to resolve an owner/repo
    that :func:`resolve_pr_number` did not already attach to ``resolution``
    (it only does when a PR was actually found via GitHub search; a
    ``pr_number`` sourced from the record itself or dispatch metadata
    carries no owner/repo at all).

    Returns ``{"kind": ..., ...}`` where ``kind`` is one of:
      - ``unresolvable``               — env fault, stays retryable (below
        threshold). OI-1508: also reused by the RESOLVED branch when a
        known PR's live state (open/merged/closed) could not be determined
        — the same class of fault (the environment cannot answer a
        required question) even though the PR number itself is known;
        deliberately NOT a new status vocabulary member every other
        consumer of ``STATUS_*`` would need to learn about. Carries
        ``decision["detail"]`` naming what could not be determined.
      - ``escalate``                   — env fault, past threshold: terminal
        escalation. Same OI-1508 reuse as ``unresolvable`` above.
      - ``stay_pending``               — genuine wait (branch alive / undetermined)
      - ``retire``                     — dead dispatch, no rescuing evidence.
        OI-1508: also returned by the RESOLVED branch when ``gh`` confirms
        the PR is MERGED or CLOSED (never on OPEN, and never without first
        checking for rescuing evidence) — carries
        ``decision["retire_reason"]`` (``REASON_PR_MERGED``/
        ``REASON_PR_CLOSED``) so the caller can distinguish it from the
        pre-existing "branch gone, no PR ever" retirement.
      - ``fulfill_by_evidence``        — OI-1388 defect 1: a complete-evidence
        DECIDED PASS, sha-current with the PR head, already exists for this
        dispatch_id + gate; carried as ``decision["evidence"] = (path, record)``
      - ``fulfill_by_failed_evidence`` — BETA3-C2: same rescue, but the
        existing evidence is a DECIDED FAIL — never booked as
        ``fulfill_by_evidence``, or the obligation would launder a rejection
        into a clean pass; also carries ``decision["evidence"]``
      - ``sha_unverifiable``           — OI-1571 tak 3: a complete, decided
        result exists but its sha binding to the PR head could not be
        verified. NEITHER a rescue NOR a retire/escalate — a third outcome
        that suspends judgement; carries ``decision["detail"]``
      - ``attempt_gate``               — PR resolved AND (for a RESOLVED
        resolution) confirmed OPEN, or no evidence-check applies at all:
        the caller must actually run the gate to learn the outcome (only
        the real run does this)

    ``not_executable`` and any other undecided status are never returned as
    evidence here — :func:`_fulfilling_result` rejects those itself, so a
    dispatch whose only "evidence" is a never-ran gate falls straight through
    to ``retire``/``escalate``/``unresolvable`` exactly as if no result
    existed at all. A DECIDED result about a DIFFERENT commit (OI-1571 tak 3
    ``mismatch``) is rejected the same way, but the retire/escalate decision
    carries ``decision["mismatch_detail"]`` when one was seen, so whatever
    record documents the retirement/escalation can also document why the
    rejected evidence did not count.

    OI-1508: before this fix, ``resolution.status == RESOLUTION_RESOLVED``
    returned ``attempt_gate`` unconditionally — never checking for existing
    evidence, never checking whether the PR was still open. Measured live
    against the central store: 258 obligations would re-run a gate
    (110-880s each) that in most cases had already merged or closed, and at
    least 19 of them already carried a DECIDED PASS this branch was
    silently discarding.
    """
    if resolution.status == RESOLUTION_UNRESOLVABLE:
        lookup = _fulfilling_result(result_index, dispatch_id, gate)
        if lookup["kind"] == "found":
            return _evidence_decision(lookup["entry"])
        if lookup["kind"] == "unverifiable":
            return {"kind": "sha_unverifiable", "detail": lookup["detail"]}
        if attempts >= _UNRESOLVABLE_ESCALATION_ATTEMPTS:
            return {"kind": "escalate", "mismatch_detail": lookup.get("detail")}
        return {"kind": "unresolvable"}

    if resolution.status == RESOLUTION_AWAITING:
        if resolution.branch_exists is False:
            lookup = _fulfilling_result(result_index, dispatch_id, gate)
            if lookup["kind"] == "found":
                return _evidence_decision(lookup["entry"])
            if lookup["kind"] == "unverifiable":
                return {"kind": "sha_unverifiable", "detail": lookup["detail"]}
            return {"kind": "retire", "mismatch_detail": lookup.get("detail")}
        return {"kind": "stay_pending"}

    # resolution.status == RESOLUTION_RESOLVED (OI-1508): a PR is known, but
    # that alone never used to be checked against existing evidence or the
    # PR's live state — see the docstring measurement above.
    #
    # OI-1612: this is the ONE branch that can pass pr_number into the
    # lookup — the dispatch_id-only join could structurally never find
    # pre-existing evidence here (see GateResultIndex/_fulfilling_result):
    # resolution.pr_number is already known, so the lookup also checks
    # index.by_pr[(pr_number, gate)], which is how a poortrun's own
    # differently-named dispatch_id no longer hides its evidence from this
    # obligation.
    lookup = _fulfilling_result(
        result_index, dispatch_id, gate, pr_number=resolution.pr_number,
    )
    if lookup["kind"] == "found":
        return _evidence_decision(lookup["entry"])
    if lookup["kind"] == "unverifiable":
        return {"kind": "sha_unverifiable", "detail": lookup["detail"]}

    pr_number = resolution.pr_number
    owner_repo = resolution.owner_repo or _resolve_github_owner_repo(state_dir)
    pr_state = (
        _pr_state_from_github(pr_number, owner_repo)
        if pr_number and owner_repo else None
    )
    if pr_state == "OPEN":
        return {"kind": "attempt_gate"}
    if pr_state in ("MERGED", "CLOSED"):
        retire_reason = REASON_PR_MERGED if pr_state == "MERGED" else REASON_PR_CLOSED
        return {
            "kind": "retire",
            "retire_reason": retire_reason,
            "mismatch_detail": lookup.get("detail"),
        }

    # PR state could not be determined (gh missing/timed out/unrecognised
    # response, or the owner/repo itself could not be resolved) — a THIRD
    # outcome, distinct from both "confirmed open" (attempt_gate) and
    # "confirmed merged/closed" (retire). OI-1388: ambiguous evidence must
    # never retire an obligation; an unqueryable PR state must not silently
    # gate on it either, since it might just as well already be closed.
    if pr_number and owner_repo:
        detail = (
            f"PR #{pr_number} state on {owner_repo} could not be determined "
            "(gh unreachable, timed out, or returned an unrecognised "
            "response) — cannot safely retire or gate it"
        )
    else:
        detail = (
            "PR resolved but its owner/repo could not be determined to "
            "query PR state — cannot safely retire or gate it"
        )
    if attempts >= _UNRESOLVABLE_ESCALATION_ATTEMPTS:
        return {"kind": "escalate", "detail": detail, "mismatch_detail": lookup.get("detail")}
    return {"kind": "unresolvable", "detail": detail}


_DRY_RUN_ACTION_LABELS: Dict[str, str] = {
    "unresolvable": "would_stay_unresolvable",
    "escalate": "would_escalate_not_executable",
    "fulfill_by_evidence": "would_stamp",
    "fulfill_by_failed_evidence": "would_stamp_failed",
    "sha_unverifiable": "would_stay_pending_sha_unverifiable",
    "retire": "would_retire",
    "stay_pending": "would_stay_pending",
    "attempt_gate": "would_fulfill",
}


def _dry_run_outcome(
    state_dir: Path,
    path: Path,
    record: Dict[str, Any],
    result_index: GateResultIndex,
) -> Dict[str, Any]:
    """Report the action a real run would take for one obligation — never writes.

    OI-1388 defect 2: runs the exact pre-execution decision
    :func:`fulfill_obligation` would (PR resolution + branch check via
    :func:`resolve_pr_number`, then :func:`_pre_execution_decision`) so the
    summary's retirements/rescues are the real forecast, not a uniform
    "would_fulfill" guess. ``attempt_gate`` is the one decision this cannot
    carry further without actually invoking the gate — not a read-only
    action a dry run may take — so it is reported as an attempt, not an
    outcome.
    """
    dispatch_id = str(record.get("dispatch_id") or path.stem)
    gate = str(record.get("gate") or "")
    if not gate:
        return {
            "dispatch_id": dispatch_id,
            "gate": gate,
            "action": "would_error",
            "detail": "obligation has no gate",
        }

    attempts = int(record.get("attempts") or 0) + 1
    resolution = resolve_pr_number(state_dir, record)
    decision = _pre_execution_decision(state_dir, dispatch_id, gate, resolution, attempts, result_index)
    outcome: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "gate": gate,
        "action": _DRY_RUN_ACTION_LABELS[decision["kind"]],
    }
    if decision["kind"] == "fulfill_by_evidence":
        evidence_path, _evidence_record = decision["evidence"]
        outcome["detail"] = f"would be rescued by the OI-1388 evidence discriminator ({evidence_path})"
        outcome["result_path"] = str(evidence_path)
    elif decision["kind"] == "fulfill_by_failed_evidence":
        evidence_path, _evidence_record = decision["evidence"]
        outcome["detail"] = (
            "would be rescued by the OI-1388 evidence discriminator as a FAILED "
            f"verdict — never a clean pass ({evidence_path})"
        )
        outcome["result_path"] = str(evidence_path)
    elif decision["kind"] == "sha_unverifiable":
        outcome["detail"] = decision.get("detail")
    elif decision["kind"] == "retire":
        # OI-1508: retire_reason distinguishes the RESOLVED branch's own
        # merged/closed retirement from the pre-existing "branch gone, no
        # PR ever" retirement — resolution.reason is unset for the former
        # (RESOLUTION_RESOLVED never populates it), so it must not be used
        # as the fallback text there.
        retire_reason = decision.get("retire_reason")
        if retire_reason in (REASON_PR_MERGED, REASON_PR_CLOSED):
            state_word = "merged" if retire_reason == REASON_PR_MERGED else "closed without merge"
            outcome["detail"] = (
                f"PR #{resolution.pr_number} is {state_word} and no rescuing "
                f"{gate} evidence was found"
            )
        else:
            outcome["detail"] = resolution.reason or "dispatch died without ever producing a PR; branch gone"
        if decision.get("mismatch_detail"):
            outcome["detail"] = f"{outcome['detail']} (rejected mismatched evidence: {decision['mismatch_detail']})"
    elif decision["kind"] in ("unresolvable", "escalate"):
        # OI-1508: decision["detail"] carries the RESOLVED branch's own
        # "PR state undeterminable" message when present — resolution.reason
        # is unset for RESOLUTION_RESOLVED, so it is only the fallback for
        # the pre-existing RESOLUTION_UNRESOLVABLE origin of this branch.
        outcome["detail"] = decision.get("detail") or resolution.reason
        if decision.get("mismatch_detail"):
            outcome["detail"] = f"{outcome['detail']} (rejected mismatched evidence: {decision['mismatch_detail']})"
    return outcome


def fulfill_obligation(
    state_dir: Path,
    path: Path,
    record: Dict[str, Any],
    *,
    result_index: Optional[GateResultIndex] = None,
) -> Dict[str, Any]:
    """Attempt fulfilment of one pending obligation.

    Returns a per-obligation outcome dict. Never raises: every failure mode
    is either recorded loudly (gate unreachable → not_executable records) or
    leaves the obligation pending for the freshness monitor to flag.

    ``result_index`` (see :func:`_index_gate_results`) lets :func:`run` build
    the OI-1388 evidence index once per call instead of once per obligation;
    a direct caller (tests) may omit it and one is built on demand.
    """
    state_dir = Path(state_dir)
    dispatch_id = str(record.get("dispatch_id") or path.stem)
    gate = str(record.get("gate") or "")
    outcome: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "gate": gate,
        "action": "pending",
    }
    if not gate:
        outcome["action"] = "error"
        outcome["detail"] = "obligation has no gate"
        return outcome

    if result_index is None:
        result_index = _index_gate_results(state_dir)

    attempts = int(record.get("attempts") or 0) + 1
    now = utc_now_iso()

    resolution = resolve_pr_number(state_dir, record)
    decision = _pre_execution_decision(state_dir, dispatch_id, gate, resolution, attempts, result_index)

    if decision["kind"] == "fulfill_by_evidence":
        # OI-1388 defect 1: a gate already produced a complete-evidence PASS
        # for this dispatch+gate — book it fulfilled, never retired/escalated
        # as unreviewed, regardless of why the retirement/escalation path was
        # about to fire. BETA3-C2: this branch is PASS-only now —
        # _pre_execution_decision/_evidence_decision route a DECIDED FAIL to
        # fulfill_by_failed_evidence below instead; _fulfilling_result never
        # hands either branch a not_executable or undecided-status record.
        evidence_path, evidence_record = decision["evidence"]
        updated = update_obligation(
            path,
            status=STATUS_FULFILLED,
            pr_number=evidence_record.get("pr_number") or record.get("pr_number"),
            branch=evidence_record.get("branch") or record.get("branch"),
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=now,
            result_path=str(evidence_path),
            reason="fulfilled_by_existing_evidence",
            reason_detail=(
                f"{gate} already produced a complete-evidence PASS for "
                f"dispatch {dispatch_id} ({evidence_path}) — OI-1388 defect 1: "
                "a dispatch a gate already reviewed must never be retired or "
                "escalated as unreviewed"
            ),
            fulfilled_by=gate,
            takeover_gate=None,
            evidence_result_path=str(evidence_path),
        )
        outcome["action"] = STATUS_FULFILLED
        outcome["detail"] = (
            "rescued by the OI-1388 evidence discriminator — a gate had "
            "already reviewed and approved this dispatch"
        )
        outcome["result_path"] = updated.get("result_path")
        return outcome

    if decision["kind"] == "fulfill_by_failed_evidence":
        # BETA3-C2 (2026-08-26): a gate already produced a complete-evidence
        # FAIL/ERRORED/BLOCKED verdict for this dispatch+gate. The obligation
        # IS discharged — a gate genuinely reviewed this dispatch — but it
        # must never be stamped STATUS_FULFILLED: every consumer that counts
        # "reviewed" work treats fulfilled as a clean bill, and PR #1692's
        # own glm_gate verdict on itself (pr-1692-glm_gate.json — a real
        # ``fail`` with contract_hash and report_path both populated) is the
        # live record of exactly what that would launder: a rejection
        # rewritten into a pass. STATUS_FAILED is the pre-existing, already
        # terminal vocabulary member gate_obligations.py defines for exactly
        # this shape (see TERMINAL_STATUSES and
        # producer_freshness.scan_gate_obligations's own "fulfilled /
        # not_executable / failed" doc comment) — defined but never written
        # by this runner until now.
        evidence_path, evidence_record = decision["evidence"]
        _passed, verdict_reason = _gate_is_pass(evidence_record)
        updated = update_obligation(
            path,
            status=STATUS_FAILED,
            pr_number=evidence_record.get("pr_number") or record.get("pr_number"),
            branch=evidence_record.get("branch") or record.get("branch"),
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=now,
            result_path=str(evidence_path),
            reason="failed_by_existing_evidence",
            reason_detail=(
                f"{gate} already produced a complete-evidence FAIL for "
                f"dispatch {dispatch_id} ({evidence_path}, {verdict_reason}) — "
                "BETA3-C2: the gate DID review this, so the obligation is "
                "discharged, but a rejection must never be booked as fulfilled"
            ),
            fulfilled_by=gate,
            takeover_gate=None,
            evidence_result_path=str(evidence_path),
        )
        outcome["action"] = STATUS_FAILED
        outcome["detail"] = (
            "rescued by the OI-1388 evidence discriminator as a FAILED "
            f"verdict ({verdict_reason}) — a gate had already reviewed and "
            "rejected this dispatch; never a clean pass"
        )
        outcome["result_path"] = updated.get("result_path")
        return outcome

    if decision["kind"] == "sha_unverifiable":
        # OI-1571 tak 3: a complete, decided result exists for this
        # dispatch+gate, but whether it belongs to the PR's CURRENT head
        # could not be verified (head_sha or the record's own commit_sha is
        # missing). Neither the OI-1388 rescue (that requires a confirmed
        # MATCH) nor a retire/escalate (that would silently discard evidence
        # that might still be current) applies — a third outcome that
        # suspends judgement and stays pending, loud about why, so the next
        # run gets another chance to verify it.
        update_obligation(
            path,
            status=STATUS_PENDING,
            attempts=attempts,
            last_attempt_at=now,
            reason="sha_binding_unverifiable",
            reason_detail=(
                f"{gate} has existing complete evidence for dispatch "
                f"{dispatch_id} but its sha binding to the PR head could not "
                f"be verified ({decision.get('detail')}) — suspending "
                "judgement (OI-1571 tak 3) rather than silently accepting or "
                "discarding it; will re-check on the next run"
            ),
        )
        outcome["detail"] = "existing evidence found but its sha binding is unverifiable"
        return outcome

    if decision["kind"] in ("unresolvable", "escalate"):
        # The environment is wrong (repo unattributable, gh unusable): a fault,
        # not a wait. Record it in a state distinct from ``pending`` so a
        # misconfigured obligation can never masquerade as "not yet". It stays
        # retryable — the env may be fixed — until it crosses the escalation
        # threshold, where it becomes the loud terminal not_executable.
        #
        # OI-1508: decision["detail"] carries the RESOLVED branch's own "PR
        # state undeterminable" message when this kind originated there —
        # resolution.reason is unset for RESOLUTION_RESOLVED (only
        # RESOLUTION_UNRESOLVABLE/RESOLUTION_AWAITING populate it), so it is
        # only the fallback for this block's pre-existing origin.
        reason_source = decision.get("detail") or resolution.reason
        mismatch_note = (
            f" A prior {gate} result exists for this dispatch but is about a "
            f"DIFFERENT commit ({decision['mismatch_detail']}) and was "
            "rejected as rescue evidence, never silently reused (OI-1571 tak 3)."
            if decision.get("mismatch_detail") else ""
        )
        update_obligation(
            path,
            status=STATUS_UNRESOLVABLE,
            attempts=attempts,
            last_attempt_at=now,
            reason="unresolvable_repo",
            reason_detail=f"{reason_source}{mismatch_note}",
        )
        outcome["action"] = "unresolvable"
        outcome["detail"] = reason_source
        if decision["kind"] == "escalate":
            update_obligation(
                path,
                status=STATUS_NOT_EXECUTABLE,
                attempts=attempts,
                last_attempt_at=now,
                resolved_at=now,
                reason="unresolvable_timeout",
                reason_detail=(
                    f"PR unresolved after {attempts} attempts because the "
                    f"environment is misconfigured: {reason_source}. Fix "
                    "the project attribution (VNX_PROJECT_ID / "
                    "~/.vnx/projects.json / the checkout's git origin remote) "
                    f"and reset this obligation to pending.{mismatch_note}"
                ),
            )
            outcome["action"] = "not_executable"
        return outcome

    if decision["kind"] == "retire":
        mismatch_note = (
            f" A prior {gate} result exists for this dispatch but is about a "
            f"DIFFERENT commit ({decision['mismatch_detail']}) and was "
            "rejected as rescue evidence, never silently reused (OI-1571 tak 3)."
            if decision.get("mismatch_detail") else ""
        )
        retire_reason = decision.get("retire_reason", REASON_NO_PR_BRANCH_GONE)
        if retire_reason in (REASON_PR_MERGED, REASON_PR_CLOSED):
            # OI-1508: the RESOLVED branch's own retirement — gh confirmed
            # the PR is no longer open, and the evidence lookup above this
            # branch found no rescuing verdict. Never fires on an OPEN PR
            # (that stays attempt_gate) and never without that lookup.
            state_word = "merged" if retire_reason == REASON_PR_MERGED else "closed without merge"
            update_obligation(
                path,
                status=STATUS_RETIRED,
                pr_number=resolution.pr_number,
                attempts=attempts,
                last_attempt_at=now,
                resolved_at=now,
                reason=retire_reason,
                reason_detail=(
                    f"PR #{resolution.pr_number} for dispatch {dispatch_id} is "
                    f"{state_word} and no rescuing {gate} evidence was found — "
                    f"nothing left for {gate} to guard.{mismatch_note}"
                ),
            )
            outcome["action"] = STATUS_RETIRED
            outcome["detail"] = f"PR #{resolution.pr_number} is {state_word} — retired"
            return outcome

        # OI-1388: the dispatch never produced a PR AND its head branch is
        # gone from origin — nothing will ever gate this obligation.
        # Terminal, distinct from `fulfilled` (no gate ever reviewed it).
        branch_name = f"dispatch/{dispatch_id}"
        update_obligation(
            path,
            status=STATUS_RETIRED,
            branch=branch_name,
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=now,
            reason=REASON_NO_PR_BRANCH_GONE,
            reason_detail=(
                f"dispatch {dispatch_id} never produced a PR and its head "
                f"branch {branch_name} no longer exists on "
                f"{resolution.owner_repo or 'the resolved repo'} — nothing "
                f"left for {gate} to guard.{mismatch_note}"
            ),
        )
        outcome["action"] = STATUS_RETIRED
        outcome["detail"] = "dispatch died without ever producing a PR; branch gone — retired"
        return outcome

    if decision["kind"] == "stay_pending":
        # The repo resolves and gh works, but no PR exists yet for the head
        # branch, and the branch is still there (or its existence could not be
        # determined) — a genuine wait: stays pending for the freshness
        # monitor, never closed on ambiguous evidence.
        update_obligation(
            path,
            status=STATUS_PENDING,
            attempts=attempts,
            last_attempt_at=now,
        )
        outcome["detail"] = "no PR resolvable yet — stays pending for the freshness monitor"
        return outcome

    # decision["kind"] == "attempt_gate": a PR is resolved — actually run it.
    pr_number = resolution.pr_number
    owner_repo = resolution.owner_repo or _resolve_github_owner_repo(state_dir)
    # OI-1571 tak 3: resolved ONCE for this attempt and reused for every sha
    # check below (the declared gate's own record, the takeover walk, and
    # the final terminal fallback) — never re-fetched per check, and never a
    # second implementation of "what is the PR head" (gate_executor's own
    # fresh-execution sha check uses the exact same source).
    head_sha = _get_pr_head_sha_for_gate(pr_number)

    branch = (
        str(record.get("branch") or "").strip()
        or (_branch_from_github(pr_number, owner_repo) if owner_repo else "")
        or f"dispatch/{dispatch_id}"
    )

    # Scope context for the reviewers: best-effort diff; an unresolvable
    # branch degrades to an empty changed-files list, never to a skip.
    changed_files: List[str] = []
    try:
        from review_gate_manager import _compute_changed_files  # noqa: PLC0415

        changed_files = _compute_changed_files(branch)
    except Exception as exc:  # noqa: BLE001 — degraded scope, not silence
        _LOG.info("changed-files unavailable for %s: %s", branch, exc)

    try:
        manager = _build_manager(state_dir)
        result = manager.request_and_execute(
            pr_number=pr_number,
            branch=branch,
            review_stack=[gate],
            risk_class="medium",
            changed_files=changed_files,
            mode="per_pr",
            dispatch_id=dispatch_id,
        )
        result_file = manager._result_path(gate, pr_number)
        # Mirror the recorded outcome: a gate the manager could not execute
        # leaves a loud not_executable/failed RESULT record — the obligation
        # must tell the same truth, not a cosmetic "fulfilled".
        result_status = ""
        result_reason = ""
        result_data: Optional[Dict[str, Any]] = None
        try:
            if result_file.exists():
                result_data = json.loads(result_file.read_text(encoding="utf-8"))
                result_status = str(result_data.get("status") or "")
                result_reason = str(result_data.get("reason") or "")
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.debug("result status unreadable for pr-%s-%s: %s", pr_number, gate, exc)

        # D2e: the declared gate's own record can be a permanent dead end
        # (lane_exhausted, PR #1726 measured live) while the review-gate
        # takeover chain already substituted a successor as the reader —
        # see _find_takeover_successor_evidence. Only consulted when the
        # declared gate's OWN record is not ALREADY a decided, complete
        # verdict: when it is, the existing handling below books it exactly
        # as it does today, and a successor is never even looked at (D2e:
        # "bewijs bij de gedeclareerde poort zelf wordt gevonden zoals nu").
        #
        # OI-1571 tak 3: "decided" now also means sha-CURRENT. A stale-but-
        # complete record left on disk by an EARLIER dispatch against the
        # same PR (measured live, PR #1736: an older dispatch's codex_gate
        # PASS survived untouched because this attempt's codex run never
        # overwrote it) used to satisfy the old bool check and skip the
        # takeover walk entirely — declared_kind/declared_detail are reused
        # below, after the walk, so a MISMATCH/UNKNOWN_SHA declared record
        # is never silently trusted by the unconditional fallback either.
        declared_kind, declared_detail = (
            _has_decided_evidence(result_data, head_sha) if result_data is not None
            else (_EVIDENCE_NOT_DECIDED, "no result record on disk yet")
        )
        takeover_hit = None
        if declared_kind != _EVIDENCE_USABLE:
            takeover_hit = _find_takeover_successor_evidence(manager, gate, pr_number, head_sha)

        if takeover_hit is not None:
            successor_gate, successor_path, successor_record, hops = takeover_hit
            passed, verdict_reason = _gate_is_pass(successor_record)
            terminal = STATUS_FULFILLED if passed else STATUS_FAILED
            hop_chain = " -> ".join(hops)
            reason = "fulfilled_by_takeover_evidence" if passed else "failed_by_takeover_evidence"
            reason_detail = (
                f"{gate} never produced a decided verdict of its own for PR "
                f"#{pr_number} -- the review-gate takeover chain ({hop_chain}) "
                f"already substituted {successor_gate} as the reader, and "
                f"{successor_gate} produced a complete-evidence verdict "
                f"({verdict_reason}). Booked against {gate}'s obligation with "
                f"resolved_by_gate={successor_gate!r} (D2e)."
            )
            updated = update_obligation(
                path,
                status=terminal,
                pr_number=pr_number,
                branch=branch,
                attempts=attempts,
                last_attempt_at=now,
                resolved_at=utc_now_iso(),
                request_path=str(manager._request_path(gate, pr_number)),
                result_path=str(successor_path),
                resolved_by_gate=successor_gate,
                takeover_hops=hops,
                fulfilled_by=successor_gate,
                takeover_gate=successor_gate,
                evidence_result_path=str(successor_path),
                reason=reason,
                reason_detail=reason_detail,
            )
            outcome["action"] = updated.get("status", terminal)
            outcome["request_path"] = updated.get("request_path")
            outcome["result_path"] = updated.get("result_path")
            outcome["resolved_by_gate"] = successor_gate
            outcome["has_required_failure"] = not passed
            return outcome

        # OI-1469: "provider_not_installed" means the required CLI binary was
        # missing from THIS RUNNER's own process environment (e.g. a
        # launchd/cron PATH that never sourced it) at the moment the shared
        # gate engine checked ``shutil.which`` — a fact about the runner, not
        # about the PR. Booking it in results/ would let a merge-blocking
        # evidence trail be polluted by whichever process happened to run
        # last without the binary on PATH (measured live: PR #1694). Remove
        # the record this fulfilment attempt just caused the engine to write
        # for exactly that reason — the obligation still stays pending below
        # with a named reason, so a later run from a properly configured
        # environment can still fulfil it and write a real result.
        if result_status == STATUS_NOT_EXECUTABLE and result_reason == "provider_not_installed":
            try:
                if result_file.exists():
                    result_file.unlink()
                    _LOG.warning(
                        "gate_obligation_runner: removed provider_not_installed "
                        "result for pr=%s gate=%s — provider availability is an "
                        "environment fact, not PR evidence (OI-1469)",
                        pr_number, gate,
                    )
            except OSError as exc:
                _LOG.debug("could not remove provider_not_installed result for pr-%s-%s: %s", pr_number, gate, exc)

        if result_status in _TEMPORARY_RESULT_STATUSES:
            temp_reason = "gate_run_in_progress"
            temp_detail = (
                f"{gate} for PR #{pr_number} is still running — no verdict yet, "
                "not a failure; will be re-attempted on the next run"
            )
        elif result_status == STATUS_NOT_EXECUTABLE and result_reason in _TEMPORARY_NOT_EXECUTABLE_REASONS:
            temp_reason = "gate_parked"
            if result_reason == "provider_not_installed":
                temp_cause = "the provider binary is not on PATH in this environment yet"
            else:
                temp_cause = "a config flag has it disabled"
            temp_detail = (
                f"{gate} is parked ({result_reason}: {temp_cause}), "
                "not broken — the obligation waits for it to be re-enabled/installed "
                "or for a future run, not permanently refused"
            )
        elif result_status in _GATE_RESULT_UNAVAILABLE_STATES:
            temp_reason = "gate_run_unavailable"
            temp_detail = (
                f"{gate} for PR #{pr_number} reported unavailable"
                + (f" ({result_reason})" if result_reason else "")
                + " — the provider produced no verdict at all (outage, quota, "
                "timeout, or a failed setup step such as a worktree checkout), "
                "not a judgment on the code; will be re-attempted on the next run"
            )
        elif result_status in TERMINAL_STATUSES or result_status in _GATE_RESULT_PASS_STATES:
            temp_reason = None
            temp_detail = None
        else:
            # OI-1400: a status that is neither a known terminal obligation
            # state nor a known gate-result pass must NEVER fall through to
            # "fulfilled" — that silent default is exactly how PR #966/#967
            # in a consumer project landed as vervuld with zero evidence
            # (empty contract_hash, empty report_path). Treat an unrecognised
            # status the same as the other temporary refusals above: pending,
            # loud, and bounded — never a quiet pass.
            temp_reason = "gate_status_unknown"
            temp_detail = (
                f"{gate} for PR #{pr_number} returned an unrecognised result "
                f"status ({result_status!r}) — refusing to record that as "
                "fulfilled; staying pending until a recognised status is seen "
                "or this escalates"
            )
            _LOG.warning(
                "gate_obligation_runner: unrecognised result status %r for "
                "pr=%s gate=%s (reason=%r) — NOT marking the obligation "
                "fulfilled (OI-1400)",
                result_status, pr_number, gate, result_reason,
            )

        if temp_reason is not None:
            # Same shape as the `unresolvable` PR-resolution path: stays
            # pending under a bounded retry term, then escalates loudly
            # (OI-1384) — never burns the obligation on the first temporary
            # refusal.
            update_obligation(
                path,
                status=STATUS_PENDING,
                pr_number=pr_number,
                branch=branch,
                attempts=attempts,
                last_attempt_at=now,
                request_path=str(manager._request_path(gate, pr_number)),
                result_path=str(result_file),
                reason=temp_reason,
                reason_detail=temp_detail,
            )
            outcome["action"] = "pending"
            outcome["detail"] = temp_detail
            if attempts >= _TEMPORARY_REFUSAL_ESCALATION_ATTEMPTS:
                escalation_detail = (
                    f"{gate} stayed temporarily unavailable ({temp_reason}) for "
                    f"{attempts} attempts (last status={result_status!r}, "
                    f"reason={result_reason!r}) — escalating to a loud terminal failure"
                )
                update_obligation(
                    path,
                    status=STATUS_NOT_EXECUTABLE,
                    pr_number=pr_number,
                    branch=branch,
                    attempts=attempts,
                    last_attempt_at=now,
                    resolved_at=utc_now_iso(),
                    request_path=str(manager._request_path(gate, pr_number)),
                    result_path=str(result_file),
                    reason=f"{temp_reason}_timeout",
                    reason_detail=escalation_detail,
                )
                outcome["action"] = "not_executable"
                outcome["detail"] = escalation_detail
            return outcome

        # OI-1571 tak 3: reached only when the declared gate's OWN on-disk
        # record looks decided enough to fall straight through to the
        # unconditional booking below (its status is a pass/known-terminal
        # shape) -- but "looks decided" is not "is current". A MISMATCH or
        # UNKNOWN_SHA declared_kind here means the takeover walk above ALSO
        # found nothing usable (a match would have short-circuited via
        # takeover_hit, or declared_kind itself would already be USABLE and
        # skip this branch entirely) -- so this is the dead end: neither the
        # declared gate's own evidence nor any successor's is provably about
        # the current head. Never fall through to the unconditional
        # STATUS_FULFILLED default on that basis (measured live, PR #1736: a
        # stale codex_gate PASS from an earlier dispatch against the same PR
        # booked fulfilled with zero takeover, zero rescue markers).
        if declared_kind == _EVIDENCE_MISMATCH:
            update_obligation(
                path,
                status=STATUS_PENDING,
                pr_number=pr_number,
                branch=branch,
                attempts=attempts,
                last_attempt_at=now,
                request_path=str(manager._request_path(gate, pr_number)),
                result_path=str(result_file),
                reason="stale_evidence_sha_mismatch",
                reason_detail=(
                    f"{gate}'s own result for PR #{pr_number} is decided but "
                    f"about a DIFFERENT commit than the PR head "
                    f"({declared_detail}) and no takeover successor produced "
                    "current evidence either — staying pending, never booking "
                    "a verdict about other code (OI-1571 tak 3)"
                ),
            )
            outcome["action"] = "pending"
            outcome["detail"] = declared_detail
            return outcome

        if declared_kind == _EVIDENCE_UNKNOWN_SHA:
            update_obligation(
                path,
                status=STATUS_PENDING,
                pr_number=pr_number,
                branch=branch,
                attempts=attempts,
                last_attempt_at=now,
                request_path=str(manager._request_path(gate, pr_number)),
                result_path=str(result_file),
                reason="sha_binding_unverifiable",
                reason_detail=(
                    f"{gate}'s own result for PR #{pr_number} is decided but "
                    f"its sha binding to the PR head could not be verified "
                    f"({declared_detail}) and no takeover successor produced "
                    "verifiable evidence either — suspending judgement "
                    "(OI-1571 tak 3) rather than silently accepting or "
                    "refusing it"
                ),
            )
            outcome["action"] = "pending"
            outcome["detail"] = declared_detail
            return outcome

        terminal = result_status if result_status in TERMINAL_STATUSES else STATUS_FULFILLED
        fast_fulfillment_warning = None
        if terminal in (STATUS_FULFILLED, STATUS_FAILED):
            fast_fulfillment_warning = _flag_fast_fulfillment_if_evidence_predates_attempt(
                result_file, gate, pr_number, dispatch_id,
            )
        updated = update_obligation(
            path,
            status=terminal,
            pr_number=pr_number,
            branch=branch,
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=utc_now_iso(),
            request_path=str(manager._request_path(gate, pr_number)),
            result_path=str(result_file),
            reason=None if not result.get("has_required_failure") else "required_failure",
            reason_detail=None if terminal == STATUS_FULFILLED else f"result status: {result_status}",
            fulfilled_by=gate if terminal in (STATUS_FULFILLED, STATUS_FAILED) else None,
            takeover_gate=None,
            evidence_result_path=(
                str(result_file) if terminal in (STATUS_FULFILLED, STATUS_FAILED) else None
            ),
        )
        # Mirror the status actually persisted to disk — never a hardcoded
        # "fulfilled" label, which would lie about a not_executable/failed
        # record and let a caller reading only the label count a burned
        # obligation as vervuld (OI-1400 residu, defect 2).
        outcome["action"] = updated.get("status", terminal)
        outcome["request_path"] = updated.get("request_path")
        outcome["result_path"] = updated.get("result_path")
        outcome["has_required_failure"] = bool(result.get("has_required_failure"))
        if fast_fulfillment_warning:
            outcome["fast_fulfillment_warning"] = fast_fulfillment_warning
        return outcome
    except Exception as exc:  # noqa: BLE001 — a gate that cannot run is a loud registered outcome
        result_payload = _record_loud_not_executable(
            state_dir,
            gate=gate,
            pr_number=pr_number,
            dispatch_id=dispatch_id,
            reason="runner_error",
            reason_detail=f"{type(exc).__name__}: {exc}",
        )
        update_obligation(
            path,
            status=STATUS_NOT_EXECUTABLE,
            pr_number=pr_number,
            branch=branch,
            attempts=attempts,
            last_attempt_at=now,
            resolved_at=utc_now_iso(),
            result_path=str(
                Path(state_dir) / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"
            ),
            reason="runner_error",
            reason_detail=result_payload.get("reason_detail"),
        )
        outcome["action"] = "not_executable"
        outcome["detail"] = result_payload.get("reason_detail")
        return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(
    state_dir: Path,
    *,
    write: bool = True,
    since: Optional[str] = None,
    dispatch_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Fulfil every in-scope pending obligation under ``state_dir``. Returns a summary.

    ``since`` (``YYYY-MM-DD``) and ``dispatch_prefix`` narrow which obligations
    are processed — combinable as an AND. ``obligations_seen`` always reports
    the full store (unaffected by scope); ``obligations_in_scope`` and
    ``pending_after`` are scope-filtered. With neither argument, scope is the
    full store and behavior is unchanged from before scoping existed.

    ``write=False`` (dry run) still touches the network: PR/branch
    resolution (:func:`resolve_pr_number`, called from
    :func:`_dry_run_outcome`) runs the exact same read-only ``gh`` calls the
    real run makes — it queries but never mutates anything. In an
    environment without a usable ``gh`` (missing binary, auth failure,
    timeout), resolution degrades to ``RESOLUTION_UNRESOLVABLE`` exactly as
    the real run would, and the dry run reports that obligation's action as
    ``would_stay_unresolvable`` (or, past the escalation-attempt threshold,
    ``would_escalate_not_executable``) rather than a silent gap in the
    summary.

    ``pending_after`` in a dry run is a CONSERVATIVE forecast, not an exact
    prediction of what the next real run leaves open: it counts an
    obligation whose decision is ``attempt_gate`` (reported as
    ``would_fulfill``) as still pending, because a dry run cannot actually
    invoke the gate to learn whether that attempt would resolve it. The real
    run, by contrast, only counts an obligation as pending when the gate
    invocation itself leaves it ``pending``/``unresolvable`` — a gate that
    resolves on the spot does not count. A dry run therefore never
    UNDER-reports the remaining backlog; it can over-report relative to what
    the next real run actually leaves open.
    """
    state_dir = Path(state_dir)
    outcomes: List[Dict[str, Any]] = []
    pending_after = 0
    try:
        obligations = list(iter_obligations(state_dir))
    except ValueError as exc:
        # An unreadable obligation is surfaced, not skipped — same contract
        # as the freshness monitor's source_unreadable finding.
        return {
            "state_dir": str(state_dir),
            "error": str(exc),
            "outcomes": [],
            "pending_after": -1,
        }
    scoped: List[Tuple[Path, Dict[str, Any]]] = [
        (path, record)
        for path, record in obligations
        if _in_scope(path, record, since=since, dispatch_prefix=dispatch_prefix)
    ]
    # OI-1388 defect 2: built once per run, not per obligation, and shared by
    # both the write path and the dry-run path — the same index feeds the
    # same decision tree (_pre_execution_decision) either way, so the two
    # paths can never diverge.
    result_index = _index_gate_results(state_dir)
    for path, record in scoped:
        if record.get("status", STATUS_PENDING) in TERMINAL_STATUSES:
            continue
        if not write:
            outcome = _dry_run_outcome(state_dir, path, record, result_index)
            outcomes.append(outcome)
            if outcome["action"] in (
                "would_stay_pending", "would_stay_unresolvable", "would_fulfill",
                "would_stay_pending_sha_unverifiable",
            ):
                pending_after += 1
            continue
        outcome = fulfill_obligation(state_dir, path, record, result_index=result_index)
        outcomes.append(outcome)
        if outcome["action"] in ("pending", "unresolvable"):
            pending_after += 1
    action_counts: Dict[str, int] = {}
    for outcome in outcomes:
        action_counts[outcome.get("action", "")] = action_counts.get(outcome.get("action", ""), 0) + 1
    # Read-only diagnostic over the FULL (unscoped) store — a --since/
    # --dispatch-prefix slice must not hide an already-burned record outside
    # it. Never rewrites anything (see _terminal_evidence_contradictions).
    contradictions = _terminal_evidence_contradictions(obligations, result_index)
    return {
        "state_dir": str(state_dir),
        "timestamp": utc_now_iso(),
        "obligations_seen": len(obligations),
        "obligations_in_scope": len(scoped),
        "since": since,
        "dispatch_prefix": dispatch_prefix,
        "outcomes": outcomes,
        "pending_after": pending_after,
        "unresolvable_after": sum(
            1 for o in outcomes if o.get("action") == "unresolvable"
        ),
        "action_counts": action_counts,
        "terminal_evidence_contradictions": contradictions,
        "terminal_evidence_contradiction_count": len(contradictions),
    }


class UnresolvableProjectError(RuntimeError):
    """The runner cannot attribute its store to a project (no ``--state-dir``
    and no resolvable project_id). Loud on purpose: proceeding would write
    obligations to a fabricated or project-local store (OI-1253)."""


def _default_state_dir() -> Path:
    import vnx_paths  # noqa: PLC0415

    paths = vnx_paths.ensure_env()
    project_root = Path(paths["PROJECT_ROOT"])
    project_id = vnx_paths._resolve_state_project_id(project_root)
    if project_id is None:
        raise UnresolvableProjectError(
            f"cannot resolve a project_id for project root {project_root}: "
            "the store is unattributable, so obligations cannot be written "
            "safely. A central install's git origin is not a project identity "
            "(it may point at a release-time temp checkout). Pass --state-dir "
            "(~/.vnx-data/<project_id>/state), or set VNX_PROJECT_ID / write a "
            ".vnx-project-id marker for the project whose obligations this "
            "runner fulfils."
        )
    return Path(paths["VNX_STATE_DIR"])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="VNX state dir (default: resolved via vnx_paths ensure_env)",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Dry run: report pending obligations without fulfilling them",
    )
    parser.add_argument(
        "--since", type=_valid_since_date, default=None,
        help=(
            "Only process obligations declared on/after this date (YYYY-MM-DD). "
            "Falls back to the dispatch_id date prefix when declared_at is "
            "missing/unparseable; an obligation with neither is out of scope."
        ),
    )
    parser.add_argument(
        "--dispatch-prefix", default=None,
        help="Only process obligations whose dispatch_id starts with this string",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(levelname)s: %(message)s",
    )

    try:
        state_dir = args.state_dir or _default_state_dir()
    except UnresolvableProjectError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 20
    if not Path(state_dir).is_dir():
        print(f"ERROR: state dir not found: {state_dir}", file=sys.stderr)
        return 20

    summary = run(
        state_dir,
        write=not args.no_write,
        since=args.since,
        dispatch_prefix=args.dispatch_prefix,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        if summary.get("error"):
            print(f"ERROR: {summary['error']}", file=sys.stderr)
        for outcome in summary["outcomes"]:
            line = f"{outcome['action']}: {outcome.get('dispatch_id')} gate={outcome.get('gate')}"
            if outcome.get("request_path"):
                line += f" request={outcome['request_path']}"
            if outcome.get("result_path"):
                line += f" result={outcome['result_path']}"
            if outcome.get("detail"):
                line += f" ({outcome['detail']})"
            print(line)
        print(
            f"obligations={summary['obligations_seen']} "
            f"pending_after={summary['pending_after']}"
        )
        action_counts = summary.get("action_counts") or {}
        if action_counts:
            breakdown = " ".join(
                f"{action}={count}" for action, count in sorted(action_counts.items())
            )
            print(f"action_counts: {breakdown}")
        contradictions = summary.get("terminal_evidence_contradictions") or []
        if contradictions:
            print(
                f"terminal_evidence_contradictions={len(contradictions)} "
                "(already-terminal, evidence says otherwise — NOT auto-fixed, review manually)"
            )
            for c in contradictions:
                print(
                    f"  {c['dispatch_id']} gate={c['gate']} status={c['obligation_status']} "
                    f"reason={c['obligation_reason']!r} ({c['obligation_reason_bucket']}) "
                    f"evidence={c['evidence_result_path']}"
                )

    if summary.get("error"):
        return 20
    return 11 if summary["pending_after"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
