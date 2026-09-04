"""stop_conditions.py — measurable stop-conditions for an autonomous T0 chain.

The T0 orchestrator role (``.claude/terminals/T0/role-orchestrator.md``,
section "Stop conditions (wake operator)") names six cases where an
autonomous chain must halt and wake the operator (E1-E6). Until this module,
that list existed only as prose: nothing in the fabric measured any of them,
so a chain running unattended had no way to observe its own stop conditions.

This module measures FOUR of the six:

  1. ``check_main_ci_red``              — E1 (main branch CI conclusion != success)
  2. ``check_gh_auth_dead``              — E4 (gh cannot authenticate, or quota is 0)
  3. ``check_provider_exhausted``        — a provider refuses structurally (the
     live example: kimi has returned HTTP 403 access_terminated_error on its
     weekly quota since 2026-09-03 — failure_class "auth_rejected" on the
     receipt ledger)
  4. ``check_repeated_gate_failure_cause`` — E6 (N consecutive PR gate results
     blocked by the same recurring cause)

E2 (data-loss risk) and E3 (secrets in logs/PRs) are NOT measured here: both
require judging the CONTENT of a diff or a log line, not a state signal this
module can read cheaply and deterministically. Building a shallow pattern
match for either would produce exactly the false confidence this dispatch
exists to avoid — a check that looks like it covers E2/E3 but doesn't. They
stay prose-only pending a dedicated design.

Data model — a THIRD branch, not a third value of an existing branch
----------------------------------------------------------------------
Every check answers one of three things, never two:

  - ``CheckStatus.TRIGGERED``    — the condition holds; halt.
  - ``CheckStatus.CLEAR``        — measured, and the condition does not hold.
  - ``CheckStatus.UNMEASURABLE`` — could not be measured (tool missing, file
    absent, not enough data, a timeout, unparseable output, ...).

``UNMEASURABLE`` never collapses into ``CLEAR`` ("no evidence of harm" is not
"evidence of no harm") and never collapses into ``TRIGGERED`` (a halt must be
backed by a real reading, not a shrug). A caller that only checks
``status == CheckStatus.TRIGGERED`` before proceeding is correct by
construction: unmeasurable is silently *not* a green light.

Wiring note (deliberately out of scope here)
---------------------------------------------
This module is self-contained and callable/testable on its own. Calling it
from the dispatch door (``dispatch_cli.py``) is a separate, later PR — see
the module's own dispatch note. Do not import this module from
``dispatch_cli.py`` in the same change that adds it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from project_root import resolve_project_root  # noqa: E402

try:
    from failure_classification import FAILURE_CLASSES as _KNOWN_FAILURE_CLASSES
except ImportError:  # vnx-silent-except: cross-check is advisory only, module must stay importable standalone
    _KNOWN_FAILURE_CLASSES = None


# ── Data model ──────────────────────────────────────────────────────────────


class CheckStatus(str, Enum):
    TRIGGERED = "triggered"
    CLEAR = "clear"
    UNMEASURABLE = "unmeasurable"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class StopConditionResult:
    check_id: str
    status: CheckStatus
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def _triggered(check_id: str, message: str, **evidence: Any) -> StopConditionResult:
    return StopConditionResult(check_id, CheckStatus.TRIGGERED, message, evidence=evidence)


def _clear(check_id: str, message: str, **evidence: Any) -> StopConditionResult:
    return StopConditionResult(check_id, CheckStatus.CLEAR, message, evidence=evidence)


def _unmeasurable(check_id: str, message: str, **evidence: Any) -> StopConditionResult:
    return StopConditionResult(check_id, CheckStatus.UNMEASURABLE, message, evidence=evidence)


def _combine(check_id: str, sub_results: List[StopConditionResult], *, sub_key: str) -> StopConditionResult:
    """Fold N per-entity sub-results (per-provider, per-gate, ...) into one.

    ANY triggered sub-result -> TRIGGERED overall (fail loud on a single hit).
    Else, if AT LEAST ONE sub-result was actually measured (CLEAR) -> CLEAR.
    Else (every sub-result is UNMEASURABLE, or there were none) -> UNMEASURABLE.
    """
    by_id = {r.check_id: r.to_dict() for r in sub_results}
    triggered = [r for r in sub_results if r.status == CheckStatus.TRIGGERED]
    if triggered:
        return _triggered(
            check_id,
            "; ".join(r.message for r in triggered),
            **{sub_key: by_id},
        )
    if any(r.status == CheckStatus.CLEAR for r in sub_results):
        return _clear(
            check_id,
            f"{len(sub_results)} sub-check(s) gemeten, geen triggered",
            **{sub_key: by_id},
        )
    return _unmeasurable(
        check_id,
        "geen van de sub-checks kon gemeten worden" if sub_results else "geen meetbare entiteiten gevonden",
        **{sub_key: by_id},
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; None on anything not cleanly parseable.

    Mirrors merge_preflight_ci_check._parse_created_at: a missing/unparseable
    timestamp means "order unknown" to the caller, never "sorts first" or
    "sorts last" (OI-1613 precedent) — so this returns None rather than
    raising or guessing.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Path resolution (mirrors dispatch_register.py's fallback chain) ────────


def _resolve_state_dir() -> Path:
    """Resolve VNX_STATE_DIR via the canonical resolver, with the same
    fallback chain dispatch_register.py uses when it is unavailable. Never
    a hardcoded ``.vnx-data/`` literal — see project CLAUDE.md on
    central-store authority (ADR-026)."""
    try:
        from vnx_paths import resolve_paths

        return Path(resolve_paths()["VNX_STATE_DIR"])
    except Exception:  # vnx-silent-except: fallback chain below mirrors dispatch_register._register_path
        state_dir_env = os.environ.get("VNX_STATE_DIR")
        if state_dir_env:
            return Path(state_dir_env)
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            return Path(os.environ["VNX_DATA_DIR"]) / "state"
        return resolve_project_root(__file__) / ".vnx-data" / "state"


def _default_project_root() -> Optional[Path]:
    try:
        return resolve_project_root(__file__)
    except RuntimeError:
        return None


# ── Subprocess helper (mirrors merge_preflight_ci_check._capture) ──────────


def _capture(
    argv: List[str], *, timeout: int, cwd: Optional[str] = None
) -> Tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Run a command, returning (result, error_tag).

    error_tag is "missing" (binary not found), "timeout", or None. A non-zero
    exit is NOT an error_tag — the caller inspects returncode itself so it can
    distinguish "ran and said no" from "could not run at all"."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return result, None
    except FileNotFoundError:
        return None, "missing"
    except subprocess.TimeoutExpired:
        return None, "timeout"


# ── Check 1: E1 — main-branch CI conclusion ─────────────────────────────────

DEFAULT_CI_WORKFLOW_NAME = "VNX CI"
CI_WORKFLOW_NAME_ENV_VAR = "VNX_CI_WORKFLOW_NAME"
DEFAULT_MAIN_BRANCH = "main"
GH_RUN_LIST_TIMEOUT = 15


def check_main_ci_red(
    project_root: Optional[Path] = None,
    *,
    branch: str = DEFAULT_MAIN_BRANCH,
    workflow_name: Optional[str] = None,
    gh_bin: str = "gh",
) -> StopConditionResult:
    """E1: main branch broken after merge.

    Measures the VNX CI workflow conclusion of the most recent run on
    ``branch`` (default: main). A still-running latest run (in_progress /
    queued) is UNMEASURABLE — "not yet known", never read as clear or
    triggered.
    """
    check_id = "main_ci_red"
    resolved_workflow = workflow_name or os.environ.get(CI_WORKFLOW_NAME_ENV_VAR) or DEFAULT_CI_WORKFLOW_NAME
    root = Path(project_root) if project_root is not None else _default_project_root()
    if root is None:
        return _unmeasurable(check_id, "kon project-root niet resolven: main-CI-status niet meetbaar")

    if shutil.which(gh_bin) is None:
        return _unmeasurable(check_id, "gh CLI niet beschikbaar: main-CI-status niet meetbaar", workflow=resolved_workflow)

    result, err = _capture(
        [
            gh_bin, "run", "list",
            "--branch", branch,
            "--workflow", resolved_workflow,
            "--limit", "1",
            "--json", "conclusion,status,headSha,createdAt,databaseId",
        ],
        timeout=GH_RUN_LIST_TIMEOUT,
        cwd=str(root),
    )
    if err == "missing":
        return _unmeasurable(check_id, "gh CLI niet beschikbaar: main-CI-status niet meetbaar", workflow=resolved_workflow)
    if err == "timeout":
        return _unmeasurable(check_id, f"gh run list liep vast voor workflow '{resolved_workflow}'", workflow=resolved_workflow, branch=branch)
    if result is None or result.returncode != 0:
        stderr = (result.stderr if result else "").strip()
        return _unmeasurable(
            check_id,
            f"gh run list faalde voor workflow '{resolved_workflow}' op branch '{branch}'"
            + (f" ({stderr[:200]})" if stderr else ""),
            workflow=resolved_workflow,
            branch=branch,
        )

    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _unmeasurable(check_id, f"gh-uitvoer niet te parsen voor workflow '{resolved_workflow}'", workflow=resolved_workflow, branch=branch)

    if not runs:
        return _unmeasurable(check_id, f"geen VNX CI-run gevonden op branch '{branch}'", workflow=resolved_workflow, branch=branch)

    run = runs[0]
    status = run.get("status") or ""
    if status in ("in_progress", "queued"):
        return _unmeasurable(
            check_id,
            f"{resolved_workflow} draait nog op '{branch}' (status={status}): conclusie nog niet bekend",
            workflow=resolved_workflow, branch=branch, run=run,
        )

    conclusion = run.get("conclusion") or ""
    if not conclusion:
        return _unmeasurable(check_id, f"run afgerond zonder conclusion-veld op '{branch}'", workflow=resolved_workflow, branch=branch, run=run)

    if conclusion == "success":
        return _clear(check_id, f"{resolved_workflow} geslaagd op '{branch}'", workflow=resolved_workflow, branch=branch, run=run)

    return _triggered(
        check_id,
        f"{resolved_workflow} conclusion='{conclusion}' op '{branch}' (E1: main branch broken after merge)",
        workflow=resolved_workflow, branch=branch, run=run,
    )


# ── Check 2: E4 — gh auth / quota ───────────────────────────────────────────

GH_AUTH_TIMEOUT = 10
GH_RATE_LIMIT_TIMEOUT = 10


def check_gh_auth_dead(*, gh_bin: str = "gh") -> StopConditionResult:
    """E4: GitHub auth/quota fully dead (cannot proceed).

    Two sub-signals fold into one TRIGGERED: ``gh auth status`` failing
    outright, or the REST rate limit reading zero remaining. A missing gh
    binary or a subprocess timeout is UNMEASURABLE, not TRIGGERED — those are
    "the tool to check is unavailable", a different fact than "checked, and
    it is dead".
    """
    check_id = "gh_auth_dead"
    if shutil.which(gh_bin) is None:
        return _unmeasurable(check_id, "gh CLI niet geinstalleerd: auth-status niet meetbaar")

    auth, err = _capture([gh_bin, "auth", "status"], timeout=GH_AUTH_TIMEOUT)
    if err == "missing":
        return _unmeasurable(check_id, "gh CLI niet geinstalleerd: auth-status niet meetbaar")
    if err == "timeout":
        return _unmeasurable(check_id, "gh auth status liep vast (timeout): niet meetbaar")
    if auth is None:
        return _unmeasurable(check_id, "gh auth status gaf geen resultaat: niet meetbaar")
    if auth.returncode != 0:
        stderr = (auth.stderr or "").strip()
        return _triggered(
            check_id,
            f"gh is niet geauthenticeerd (E4: GitHub auth/quota fully dead)" + (f": {stderr[:200]}" if stderr else ""),
            stderr=stderr,
        )

    rate, err2 = _capture([gh_bin, "api", "rate_limit", "--jq", ".rate.remaining"], timeout=GH_RATE_LIMIT_TIMEOUT)
    if err2 == "missing":
        return _unmeasurable(check_id, "gh CLI verdween tussen auth-check en quota-check: niet meetbaar", auth_ok=True)
    if err2 == "timeout":
        return _unmeasurable(check_id, "gh api rate_limit liep vast (timeout): auth is OK, quotum onbekend", auth_ok=True)
    if rate is None or rate.returncode != 0:
        stderr = (rate.stderr if rate else "").strip()
        return _unmeasurable(
            check_id,
            "gh api rate_limit faalde: auth is OK, quotum onbekend" + (f" ({stderr[:200]})" if stderr else ""),
            auth_ok=True,
        )

    raw = (rate.stdout or "").strip()
    try:
        remaining = int(raw)
    except ValueError:
        return _unmeasurable(check_id, f"quota-uitvoer niet te parsen ('{raw}'): auth is OK, quotum onbekend", auth_ok=True, raw=raw)

    if remaining <= 0:
        return _triggered(check_id, f"GitHub API-quotum op (remaining={remaining}) (E4: GitHub auth/quota fully dead)", remaining=remaining)

    return _clear(check_id, f"gh geauthenticeerd, quotum remaining={remaining}", remaining=remaining)


# ── Shared receipt-ledger reading helpers ───────────────────────────────────

_ATTEMPT_EVENT_TYPES = frozenset({"task_complete", "subprocess_completion", "task_failed"})
_SUCCESS_STATUSES = frozenset({"success", "done", "complete"})
_FAILURE_STATUSES = frozenset({"failed", "failure"})

# The two failure_classification.py classes that mean "the provider itself
# structurally refuses" — auth/quota rejection (401/403) or balance/credit
# exhaustion — as opposed to a transient model_error, timeout, or an
# unrelated unknown failure. Grounded on the live ledger 2026-09-04: kimi's
# weekly-quota 403 ("access_terminated_error") classifies as auth_rejected.
EXHAUSTION_FAILURE_CLASSES = frozenset({"auth_rejected", "credit_exhausted"})

if _KNOWN_FAILURE_CLASSES is not None:
    assert EXHAUSTION_FAILURE_CLASSES <= _KNOWN_FAILURE_CLASSES, (
        "EXHAUSTION_FAILURE_CLASSES drifted from failure_classification.FAILURE_CLASSES"
    )


def _tail_lines(path: Path, max_lines: int) -> List[str]:
    """Return up to the last ``max_lines`` non-empty lines of ``path``,
    oldest first. Streams the file line-by-line into a bounded deque —
    O(max_lines) memory regardless of file size (t0_receipts.ndjson measured
    170MB+ on 2026-09-04): a plain read_text()/readlines() would load the
    whole ledger just to look at its tail."""
    dq: deque = deque(maxlen=max_lines)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                dq.append(line)
    return list(dq)


# ── Check 3: provider/lane exhaustion ───────────────────────────────────────

DEFAULT_RECEIPT_TAIL_LINES = 5000
DEFAULT_EXHAUSTION_THRESHOLD = 3


def check_provider_exhausted(
    receipts_path: Optional[Path] = None,
    *,
    providers: Optional[List[str]] = None,
    threshold: int = DEFAULT_EXHAUSTION_THRESHOLD,
    tail_lines: int = DEFAULT_RECEIPT_TAIL_LINES,
) -> StopConditionResult:
    """A provider/lane refuses structurally (example live on the ledger since
    2026-09-03: kimi's weekly quota, HTTP 403 access_terminated_error).

    Per provider seen in the last ``tail_lines`` receipt-ledger lines, this
    looks at the most recent ``threshold`` clean attempts (status normalized
    to success/failure; anything ambiguous — timeout, unknown, no_signal,
    contract_invalid, ... — is excluded from the timeline rather than guessed
    either way) and triggers only when ALL of them are exhaustion-class
    failures. A single success in that window clears the streak.
    """
    check_id = "provider_exhausted"
    resolved_path = Path(receipts_path) if receipts_path is not None else (_resolve_state_dir() / "t0_receipts.ndjson")

    if not resolved_path.is_file():
        return _unmeasurable(check_id, f"receipts-bestand ontbreekt: {resolved_path}")

    try:
        lines = _tail_lines(resolved_path, tail_lines)
    except OSError as exc:
        return _unmeasurable(check_id, f"receipts-bestand niet leesbaar: {exc}")

    by_provider: Dict[str, List[Dict[str, Any]]] = {}
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("event_type") not in _ATTEMPT_EVENT_TYPES:
            continue
        provider = d.get("provider")
        if not provider:
            continue
        if providers is not None and provider not in providers:
            continue
        raw_status = str(d.get("status") or "").strip().lower()
        if raw_status in _SUCCESS_STATUSES:
            norm = "success"
        elif raw_status in _FAILURE_STATUSES:
            norm = "failure"
        else:
            continue
        by_provider.setdefault(provider, []).append(
            {
                "status": norm,
                "failure_class": d.get("failure_class"),
                "timestamp": d.get("timestamp"),
                "dispatch_id": d.get("dispatch_id"),
            }
        )

    if not by_provider:
        return _unmeasurable(check_id, f"geen bruikbare provider-attempts gevonden in laatste {tail_lines} regels")

    sub_results: List[StopConditionResult] = []
    for provider, records in by_provider.items():
        sub_id = f"{check_id}:{provider}"
        if len(records) < threshold:
            sub_results.append(
                _unmeasurable(sub_id, f"minder dan {threshold} attempts voor '{provider}' in venster ({len(records)})", provider=provider, attempts=len(records))
            )
            continue
        window = records[-threshold:]
        if all(r["status"] == "failure" and r["failure_class"] in EXHAUSTION_FAILURE_CLASSES for r in window):
            sub_results.append(
                _triggered(
                    sub_id,
                    f"laatste {threshold} attempts voor '{provider}' allemaal exhaustion-failures ({window[-1]['failure_class']})",
                    provider=provider, window=window,
                )
            )
        else:
            sub_results.append(
                _clear(sub_id, f"laatste {threshold} attempts voor '{provider}' niet allemaal exhaustion-failures", provider=provider, window=window)
            )

    return _combine(check_id, sub_results, sub_key="per_provider")


# ── Check 4: E6 — repeated gate-failure cause ───────────────────────────────

_INFRA_FAIL_STATUSES = frozenset({"failed", "fail", "unavailable", "not_executable"})
DEFAULT_REPEAT_THRESHOLD = 3


def _gate_result_cause(record: Dict[str, Any]) -> Optional[str]:
    """Canonical 'cause' for a review_gates/results/*.json record, or None
    for a clean pass (or an outcome this can't classify either way).

    Two blocked shapes exist in that store:
      - an infra-level non-run (status in {failed, fail, unavailable,
        not_executable}) carrying a ``reason`` enum (e.g.
        "provider_not_installed", "gate_execution_degenerate") — the exact
        shape the role file's own E6 example (Legacy path gate tripping on a
        literal string) would produce.
      - a completed run with non-empty ``blocking_findings``.
    """
    status = record.get("status")
    if status in _INFRA_FAIL_STATUSES:
        reason = record.get("reason")
        return f"reason:{reason}" if reason else None
    if status == "completed":
        return "blocking_findings" if record.get("blocking_findings") else None
    return None


def check_repeated_gate_failure_cause(
    results_dir: Optional[Path] = None,
    *,
    n: int = DEFAULT_REPEAT_THRESHOLD,
    gates: Optional[List[str]] = None,
) -> StopConditionResult:
    """E6: three consecutive PRs blocked by the same recurring CI/gate issue.

    Reads ``review_gates/results/pr-<n>-<gate>.json`` (one file per PR+gate
    pair; a rerun overwrites it, so a single retried PR never inflates a
    streak). Per gate, orders by ``recorded_at`` (a record with a missing or
    unparseable timestamp is excluded — order-unknown, never guessed, mirrors
    OI-1613's rule) and checks whether the last ``n`` results for that gate
    all carry the same non-null cause.
    """
    check_id = "repeated_gate_failure_cause"
    resolved_dir = Path(results_dir) if results_dir is not None else (_resolve_state_dir() / "review_gates" / "results")

    if not resolved_dir.is_dir():
        return _unmeasurable(check_id, f"resultaten-map ontbreekt: {resolved_dir}")

    try:
        files = sorted(resolved_dir.glob("pr-*.json"))
    except OSError as exc:
        return _unmeasurable(check_id, f"resultaten-map niet leesbaar: {exc}")

    by_gate: Dict[str, List[Tuple[datetime, Any, Optional[str]]]] = {}
    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        gate = d.get("gate")
        if not gate:
            continue
        if gates is not None and gate not in gates:
            continue
        ts = _parse_iso(d.get("recorded_at"))
        if ts is None:
            continue
        cause = _gate_result_cause(d)
        by_gate.setdefault(gate, []).append((ts, d.get("pr_number"), cause))

    if not by_gate:
        return _unmeasurable(check_id, "geen bruikbare gate-resultaten met geldige recorded_at gevonden")

    sub_results: List[StopConditionResult] = []
    for gate, entries in by_gate.items():
        sub_id = f"{check_id}:{gate}"
        entries.sort(key=lambda e: e[0])
        if len(entries) < n:
            sub_results.append(_unmeasurable(sub_id, f"minder dan {n} geordende resultaten voor gate '{gate}' ({len(entries)})", gate=gate, count=len(entries)))
            continue
        window = entries[-n:]
        causes = [c for (_, _, c) in window]
        prs = [pr for (_, pr, _) in window]
        if causes[0] is not None and all(c == causes[0] for c in causes):
            sub_results.append(
                _triggered(
                    sub_id,
                    f"laatste {n} PR's op gate '{gate}' allemaal geblokkeerd door dezelfde oorzaak ({causes[0]}) (E6)",
                    gate=gate, cause=causes[0], prs=prs,
                )
            )
        else:
            sub_results.append(_clear(sub_id, f"laatste {n} PR's op gate '{gate}' niet allemaal dezelfde blokkade-oorzaak", gate=gate, causes=causes, prs=prs))

    return _combine(check_id, sub_results, sub_key="per_gate")


# ── Orchestration + halt.json ───────────────────────────────────────────────

HALT_FILENAME = "halt.json"

ALL_CHECK_IDS = (
    "main_ci_red",
    "gh_auth_dead",
    "provider_exhausted",
    "repeated_gate_failure_cause",
)


def run_all_checks(
    *,
    state_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    write_halt: bool = True,
) -> List[StopConditionResult]:
    """Run all four stop-condition checks. When any is TRIGGERED and
    ``write_halt`` is true, write/overwrite ``halt.json`` in ``state_dir``.

    Never raises: each check function is itself fail-closed-to-unmeasurable,
    never fail-open-to-clear.
    """
    resolved_state_dir = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    resolved_root = Path(project_root) if project_root is not None else _default_project_root()

    results = [
        check_main_ci_red(resolved_root),
        check_gh_auth_dead(),
        check_provider_exhausted(resolved_state_dir / "t0_receipts.ndjson"),
        check_repeated_gate_failure_cause(resolved_state_dir / "review_gates" / "results"),
    ]

    if write_halt and any(r.status == CheckStatus.TRIGGERED for r in results):
        write_halt_file(resolved_state_dir, results)

    return results


def write_halt_file(state_dir: Path, results: List[StopConditionResult]) -> Path:
    """Write ``halt.json`` atomically (tmp file + os.replace via atomic_io).

    This module only ever WRITES halt.json on a trigger — it never clears an
    existing one. Recovery (deciding the operator has addressed the halt and
    the chain may resume) is a deliberate, separate action, not something a
    measurement pass should do implicitly.
    """
    from atomic_io import atomic_write_json

    state_dir = Path(state_dir)
    halt_path = state_dir / HALT_FILENAME
    payload = {
        "halted_at": _utc_now_iso(),
        "triggered": [r.to_dict() for r in results if r.status == CheckStatus.TRIGGERED],
        "all_checks": [r.to_dict() for r in results],
    }
    atomic_write_json(halt_path, payload)
    return halt_path


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint: run all checks, print JSON, exit 1 iff any triggered."""
    import argparse

    parser = argparse.ArgumentParser(description="Measure T0 autonomous-chain stop-conditions (E1/E4/provider-exhaustion/E6).")
    parser.add_argument("--state-dir", default=None, help="override VNX_STATE_DIR resolution")
    parser.add_argument("--no-write-halt", action="store_true", help="measure only, never write halt.json")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else None
    results = run_all_checks(state_dir=state_dir, write_halt=not args.no_write_halt)
    print(json.dumps([r.to_dict() for r in results], indent=2, default=str))
    return 1 if any(r.status == CheckStatus.TRIGGERED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
