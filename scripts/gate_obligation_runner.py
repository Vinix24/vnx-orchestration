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
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_UNRESOLVABLE,
    TERMINAL_STATUSES,
    iter_obligations,
    pr_number_from_pr_id,
    update_obligation,
)
from gate_status import (  # noqa: E402
    PASS_STATES as _GATE_RESULT_PASS_STATES,
    UNAVAILABLE_STATES as _GATE_RESULT_UNAVAILABLE_STATES,
)

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

# PR-resolution outcomes. The runner must tell "no PR yet" (a wait) apart from
# "cannot resolve because the environment is wrong" (a fault) IN THE RECORD,
# not only in a log line — a pending obligation that is actually misconfigured
# reads as "not yet" forever and never alarms anyone (OI-1253 fix-forward).
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_AWAITING = "awaiting"
RESOLUTION_UNRESOLVABLE = "unresolvable"


@dataclass
class PrResolution:
    """Outcome of resolving an obligation's PR number."""

    status: str
    pr_number: Optional[int] = None
    owner_repo: Optional[str] = None
    reason: Optional[str] = None


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
    return PrResolution(
        RESOLUTION_AWAITING,
        owner_repo=owner_repo,
        reason=f"no PR yet for head branch dispatch/{dispatch_id}",
    )


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------


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


def fulfill_obligation(state_dir: Path, path: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Attempt fulfilment of one pending obligation.

    Returns a per-obligation outcome dict. Never raises: every failure mode
    is either recorded loudly (gate unreachable → not_executable records) or
    leaves the obligation pending for the freshness monitor to flag.
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

    attempts = int(record.get("attempts") or 0) + 1
    now = utc_now_iso()

    resolution = resolve_pr_number(state_dir, record)

    if resolution.status == RESOLUTION_UNRESOLVABLE:
        # The environment is wrong (repo unattributable, gh unusable): a fault,
        # not a wait. Record it in a state distinct from ``pending`` so a
        # misconfigured obligation can never masquerade as "not yet". It stays
        # retryable — the env may be fixed — until it crosses the escalation
        # threshold, where it becomes the loud terminal not_executable.
        update_obligation(
            path,
            status=STATUS_UNRESOLVABLE,
            attempts=attempts,
            last_attempt_at=now,
            reason="unresolvable_repo",
            reason_detail=resolution.reason,
        )
        outcome["action"] = "unresolvable"
        outcome["detail"] = resolution.reason
        if attempts >= _UNRESOLVABLE_ESCALATION_ATTEMPTS:
            update_obligation(
                path,
                status=STATUS_NOT_EXECUTABLE,
                attempts=attempts,
                last_attempt_at=now,
                resolved_at=now,
                reason="unresolvable_timeout",
                reason_detail=(
                    f"PR unresolved after {attempts} attempts because the "
                    f"environment is misconfigured: {resolution.reason}. Fix "
                    "the project attribution (VNX_PROJECT_ID / "
                    "~/.vnx/projects.json / the checkout's git origin remote) "
                    "and reset this obligation to pending."
                ),
            )
            outcome["action"] = "not_executable"
        return outcome

    if resolution.status == RESOLUTION_AWAITING:
        # The repo resolves and gh works, but no PR exists yet for the head
        # branch. A genuine wait: stays pending for the freshness monitor.
        update_obligation(
            path,
            status=STATUS_PENDING,
            attempts=attempts,
            last_attempt_at=now,
        )
        outcome["detail"] = "no PR resolvable yet — stays pending for the freshness monitor"
        return outcome

    pr_number = resolution.pr_number
    owner_repo = resolution.owner_repo or _resolve_github_owner_repo(state_dir)

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
        try:
            if result_file.exists():
                result_data = json.loads(result_file.read_text(encoding="utf-8"))
                result_status = str(result_data.get("status") or "")
                result_reason = str(result_data.get("reason") or "")
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.debug("result status unreadable for pr-%s-%s: %s", pr_number, gate, exc)

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

        terminal = result_status if result_status in TERMINAL_STATUSES else STATUS_FULFILLED
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
        )
        # Mirror the status actually persisted to disk — never a hardcoded
        # "fulfilled" label, which would lie about a not_executable/failed
        # record and let a caller reading only the label count a burned
        # obligation as vervuld (OI-1400 residu, defect 2).
        outcome["action"] = updated.get("status", terminal)
        outcome["request_path"] = updated.get("request_path")
        outcome["result_path"] = updated.get("result_path")
        outcome["has_required_failure"] = bool(result.get("has_required_failure"))
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
    for path, record in scoped:
        if record.get("status", STATUS_PENDING) in TERMINAL_STATUSES:
            continue
        if not write:
            outcomes.append(
                {
                    "dispatch_id": record.get("dispatch_id") or path.stem,
                    "gate": record.get("gate"),
                    "action": "would_fulfill",
                }
            )
            pending_after += 1
            continue
        outcome = fulfill_obligation(state_dir, path, record)
        outcomes.append(outcome)
        if outcome["action"] in ("pending", "unresolvable"):
            pending_after += 1
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

    if summary.get("error"):
        return 20
    return 11 if summary["pending_after"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
