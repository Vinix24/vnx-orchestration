#!/usr/bin/env python3
"""Governance closure verification for VNX feature branches.

Includes review-contract enforcement: closure cannot be claimed unless
the contractually required review gates have produced evidence and the
deterministic findings are clean.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env
from governance_receipts import emit_governance_receipt
from review_contract import ReviewContract
from codex_final_gate import enforce_codex_gate
from gate_status import is_pass as gate_is_pass, is_terminal as gate_is_terminal


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_feature_plan(path: Path) -> Dict[str, Any]:
    content = _read_text(path)
    title_match = re.search(r"^#\s*Feature:\s*(.+)$", content, re.MULTILINE)
    status_match = re.search(r"^\*\*Status\*\*:\s*(.+)$", content, re.MULTILINE)
    deps_match = re.search(r"^## Dependency Flow\s*```text\s*(.+?)```", content, re.MULTILINE | re.DOTALL)
    pr_ids = re.findall(r"^##\s+(PR-\d+):", content, re.MULTILINE)
    return {
        "title": title_match.group(1).strip() if title_match else "",
        "status": status_match.group(1).strip() if status_match else "",
        "dependency_flow": deps_match.group(1).strip() if deps_match else "",
        "pr_ids": pr_ids,
    }


def _parse_pr_queue(path: Path) -> Dict[str, Any]:
    content = _read_text(path)
    title_match = re.search(r"^# PR Queue(?:\s*-\s*Feature:|\s*—\s*FP\d+:\s*)(.+)$", content, re.MULTILINE)
    overview_match = re.search(
        r"Total:\s*(\d+)\s+PRs\s*\|\s*Complete:\s*(\d+)\s*\|\s*Active:\s*(\d+)\s*\|\s*Queued:\s*(\d+)\s*\|\s*Blocked:\s*(\d+)",
        content,
    )
    deps_match = re.search(r"## Dependency Flow(?: \(executed\))?\s*```\s*(.+?)```", content, re.DOTALL)
    return {
        "title": title_match.group(1).strip() if title_match else "",
        "overview": tuple(int(x) for x in overview_match.groups()) if overview_match else None,
        "dependency_flow": deps_match.group(1).strip() if deps_match else "",
    }


def _find_branch_pr(branch: str) -> Optional[Dict[str, Any]]:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,mergeStateStatus,mergeCommit,statusCheckRollup,headRefName,baseRefName",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return payload[0] if payload else None


def _remote_branch_exists(branch: str, project_root: Path) -> bool:
    result = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=project_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def _merge_commit_on_main(oid: str, project_root: Path) -> bool:
    _run(["git", "fetch", "origin", "main", "--quiet"], cwd=project_root)
    result = _run(["git", "merge-base", "--is-ancestor", oid, "origin/main"], cwd=project_root)
    return result.returncode == 0


def _load_claim_file(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError:
        return {}


def _validate_test_claims(claims: Dict[str, Any], project_root: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    test_files = claims.get("test_files") or []
    test_command = (claims.get("test_command") or "").strip()
    trusted_result = claims.get("trusted_test_result")

    if not test_files:
        results.append(CheckResult("test_files", "FAIL", "no claimed test files provided"))
    else:
        missing = []
        for rel in test_files:
            path = Path(rel)
            if not path.is_absolute():
                path = project_root / rel
            if not path.exists():
                missing.append(str(rel))
        if missing:
            results.append(CheckResult("test_files", "FAIL", f"missing claimed test files: {', '.join(missing)}"))
        else:
            results.append(CheckResult("test_files", "PASS", f"{len(test_files)} claimed test file(s) exist"))

    if test_command:
        results.append(CheckResult("test_command", "PASS", "claimed test command present"))
    elif trusted_result:
        results.append(CheckResult("test_command", "PASS", "trusted test result recorded"))
    else:
        results.append(CheckResult("test_command", "FAIL", "no test command or trusted test result provided"))

    parallel_assignments = claims.get("parallel_assignments") or []
    if parallel_assignments:
        terminals = [str(entry.get("terminal")).strip() for entry in parallel_assignments if entry.get("terminal")]
        duplicates = sorted({terminal for terminal in terminals if terminals.count(terminal) > 1})
        if duplicates:
            results.append(
                CheckResult(
                    "parallelism",
                    "FAIL",
                    f"same terminal reported for parallel work: {', '.join(duplicates)}",
                )
            )
        else:
            results.append(CheckResult("parallelism", "PASS", "parallel assignments use distinct terminals"))

    commit_map = claims.get("commit_pr_map") or {}
    if commit_map:
        bad = []
        for sha in commit_map.keys():
            result = _run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=project_root)
            if result.returncode != 0:
                bad.append(sha)
        if bad:
            results.append(CheckResult("commit_mapping", "FAIL", f"unknown commit(s): {', '.join(bad)}"))
        else:
            results.append(CheckResult("commit_mapping", "PASS", "commit-to-PR mapping references known commits"))

    return results


def _find_gate_result(
    gate: str,
    pr_id: str,
    results_dir: Path,
    branch: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Search for a gate result file matching the PR and gate name.

    If ``branch`` is provided, results from a different branch are rejected.
    If a result carries a ``report_path``, that file must exist on disk or the
    result is treated as stale and skipped.
    """

    def _accept(data: Dict[str, Any]) -> bool:
        # PR-scoped AND matching: if data carries pr_id it must match the queried pr_id.
        # This prevents a result from a different PR satisfying a closure check even when
        # the filename-based lookup finds a contract for the correct PR name.
        data_pr_id = data.get("pr_id")
        if data_pr_id and data_pr_id != pr_id:
            return False
        if branch and data.get("branch") and data["branch"] != branch:
            return False
        return True

    pr_slug = pr_id.lower().replace("-", "")
    # Contract-based results: {pr_slug}-{gate}-contract.json
    contract_path = results_dir / f"{pr_slug}-{gate}-contract.json"
    if contract_path.exists():
        try:
            data = json.loads(_read_text(contract_path))
            if _accept(data):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    # Legacy pattern: pr-{number}-{gate}.json — require both pr_id AND gate to match
    for path in results_dir.glob(f"*-{gate}*.json"):
        try:
            data = json.loads(_read_text(path))
            if data.get("pr_id") == pr_id and data.get("gate") == gate:
                if _accept(data):
                    return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


# Request-side states for the optional Claude GitHub review gate that record
# an explicit, intentional absence (writer: gate_request_handler).  These are
# canonicalized in claude_github_receipt.INTENTIONALLY_ABSENT_STATES /
# EVIDENCE_STATES; duplicated here to avoid an import cycle since
# closure_verifier already pins sys.path on its own.
_INTENTIONALLY_ABSENT_REQUEST_STATES = frozenset({"not_configured", "configured_dry_run"})
_EVIDENCE_REQUEST_STATES = frozenset({"requested", "completed"})


def _find_gate_request_payload(
    gate: str,
    pr_id: str,
    results_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Locate a gate **request** payload as a fallback when no result exists.

    For optional gates (notably ``claude_github_optional``), the explicit-absence
    state (``not_configured``, ``configured_dry_run``) is recorded by
    gate_request_handler in the requests directory only — no separate result
    file is written.  This helper finds that payload and normalises legacy
    ``status``-only writers into the same shape the closure verifier expects
    (with ``state``/``contributed_evidence``/``was_intentionally_absent``).
    """
    requests_dir = results_dir.parent / "requests"
    if not requests_dir.exists():
        return None

    pr_slug = pr_id.lower().replace("-", "")
    candidates: List[Path] = []
    contract_path = requests_dir / f"{pr_slug}-{gate}-contract.json"
    if contract_path.exists():
        candidates.append(contract_path)
    candidates.extend(
        path for path in requests_dir.glob(f"*-{gate}*.json")
        if path not in candidates
    )

    for path in candidates:
        try:
            data = json.loads(_read_text(path))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("pr_id") and data["pr_id"] != pr_id:
            continue
        if data.get("gate") and data["gate"] != gate:
            continue

        # Normalise legacy `status`-only payloads (writer: _request_claude_github)
        # so downstream checks can read `state` / contributed_evidence /
        # was_intentionally_absent uniformly.
        normalised = dict(data)
        if "state" not in normalised and "status" in normalised:
            normalised["state"] = normalised["status"]
        state = normalised.get("state")
        if isinstance(state, str):
            normalised.setdefault(
                "was_intentionally_absent",
                state in _INTENTIONALLY_ABSENT_REQUEST_STATES,
            )
            normalised.setdefault(
                "contributed_evidence",
                state in _EVIDENCE_REQUEST_STATES,
            )
        normalised["__source"] = "request"
        return normalised
    return None


def _validate_review_evidence(
    contract: ReviewContract,
    results_dir: Path,
    branch: Optional[str] = None,
) -> List[CheckResult]:
    """Validate review contract presence and gate results against the review stack.

    Checks:
    1. Review contract has required fields (pr_id, review_stack)
    2. Each gate in the review stack has a result
    3. Required gates (codex for high-risk) have passing verdicts
    4. Optional gates have explicit state (contributed or intentionally absent)
    5. Content hash consistency between contract and gate receipts
    6. No unresolved error-severity deterministic findings

    When ``branch`` is provided, gate results from a different branch are
    rejected as stale evidence — preventing a result from a prior branch with
    the same ``pr_id`` from satisfying current closure.
    """
    checks: List[CheckResult] = []

    if not contract.pr_id:
        checks.append(CheckResult("review_contract", "FAIL", "review contract missing pr_id"))
        return checks

    if not contract.review_stack:
        checks.append(CheckResult("review_contract", "FAIL", "review contract has empty review_stack"))
        return checks

    checks.append(CheckResult(
        "review_contract",
        "PASS",
        f"review contract present for {contract.pr_id} "
        f"(stack: {', '.join(contract.review_stack)})",
    ))

    for gate in contract.review_stack:
        result = _find_gate_result(gate, contract.pr_id, results_dir, branch=branch)

        if gate == "claude_github_optional":
            # Fallback: explicit-absence state for the optional gate is recorded
            # in the requests directory by gate_request_handler — not as a
            # separate result file — so a missing result is not by itself
            # ambiguous.  Look there before declaring no evidence.
            if result is None:
                result = _find_gate_request_payload(gate, contract.pr_id, results_dir)
            if result is None:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "FAIL",
                    f"no evidence for optional gate {gate} — state must be explicit",
                ))
            elif result.get("was_intentionally_absent") or result.get("contributed_evidence"):
                state = result.get("state", "unknown")
                source = result.get("__source", "result")
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "PASS",
                    f"{gate} state explicit: {state} (source: {source})",
                ))
            else:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "FAIL",
                    f"{gate} state is ambiguous — neither contributed evidence nor intentionally absent",
                ))

        elif gate == "codex_gate":
            enforcement = enforce_codex_gate(contract)
            if enforcement.required:
                if result is None:
                    checks.append(CheckResult(
                        f"gate_{gate}",
                        "FAIL",
                        f"codex gate required ({', '.join(enforcement.reasons)}) but no result found",
                    ))
                else:
                    passed, reason = gate_is_pass(result)
                    if passed:
                        checks.append(CheckResult(
                            f"gate_{gate}",
                            "PASS",
                            "codex gate passed",
                        ))
                    else:
                        checks.append(CheckResult(
                            f"gate_{gate}",
                            "FAIL",
                            f"codex gate not passing — {reason}",
                        ))
            else:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "PASS",
                    "codex gate not required by risk policy",
                ))

        elif gate == "gemini_review":
            if result is None:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "FAIL",
                    f"no gate result for required reviewer {gate}",
                ))
            else:
                passed, reason = gate_is_pass(result)
                advisory_count = result.get("advisory_count", 0)
                if passed:
                    checks.append(CheckResult(
                        f"gate_{gate}",
                        "PASS",
                        f"gemini review passed ({advisory_count} advisory, 0 blocking)",
                    ))
                else:
                    checks.append(CheckResult(
                        f"gate_{gate}",
                        "FAIL",
                        f"gemini review not passing — {reason}",
                    ))

        else:
            if result is None:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "FAIL",
                    f"no gate result for unknown gate {gate}",
                ))
            else:
                checks.append(CheckResult(
                    f"gate_{gate}",
                    "PASS",
                    f"gate {gate} result present",
                ))

    # Content hash consistency
    for gate in contract.review_stack:
        result = _find_gate_result(gate, contract.pr_id, results_dir, branch=branch)
        if result and contract.content_hash:
            result_hash = result.get("contract_hash") or result.get("content_hash") or ""
            if result_hash and result_hash != contract.content_hash:
                checks.append(CheckResult(
                    f"hash_{gate}",
                    "FAIL",
                    f"{gate} receipt hash mismatch — evidence is stale "
                    f"(contract={contract.content_hash[:8]}.. receipt={result_hash[:8]}..)",
                ))

    # Report path validation — per headless review evidence contract, every
    # pass/fail gate result must carry a report_path pointing to a real file
    # under $VNX_DATA_DIR/unified_reports/.
    for gate in contract.review_stack:
        result = _find_gate_result(gate, contract.pr_id, results_dir, branch=branch)
        if result is not None and gate_is_terminal(result):
            report_path = result.get("report_path", "")
            if not report_path:
                checks.append(CheckResult(
                    f"report_{gate}",
                    "FAIL",
                    f"{gate} gate result is missing required report_path field",
                ))
            elif not Path(report_path).exists():
                checks.append(CheckResult(
                    f"report_{gate}",
                    "FAIL",
                    f"{gate} report_path does not exist: {report_path}",
                ))
            else:
                checks.append(CheckResult(
                    f"report_{gate}",
                    "PASS",
                    f"{gate} normalized report exists at {report_path}",
                ))

    # Deterministic findings — error-severity blocks closure
    error_findings = [f for f in contract.deterministic_findings if f.severity == "error"]
    if error_findings:
        checks.append(CheckResult(
            "deterministic_findings",
            "FAIL",
            f"{len(error_findings)} unresolved error-severity deterministic finding(s)",
        ))
    else:
        total = len(contract.deterministic_findings)
        checks.append(CheckResult(
            "deterministic_findings",
            "PASS",
            f"{total} deterministic finding(s), 0 errors",
        ))

    return checks


def _check_stale_staging(paths: Dict[str, str], active_pr_ids: Iterable[str]) -> CheckResult:
    staging_dir = Path(paths["VNX_DISPATCH_DIR"]) / "staging"
    if not staging_dir.exists():
        return CheckResult("stale_staging", "PASS", "no staging directory")

    active_set = set(active_pr_ids)
    stale: List[str] = []
    for dispatch in staging_dir.glob("*.md"):
        content = _read_text(dispatch)
        match = re.search(r"^PR-ID:\s*(.+)$", content, re.MULTILINE)
        pr_id = match.group(1).strip() if match else ""
        if pr_id and pr_id not in active_set:
            stale.append(dispatch.name)

    if stale:
        return CheckResult("stale_staging", "FAIL", f"stale staging dispatches present: {', '.join(sorted(stale))}")
    return CheckResult("stale_staging", "PASS", "no stale staging dispatches")


def verify_pr_closure(
    *,
    pr_id: str,
    project_root: Path,
    feature_plan: Path,
    dispatch_dir: Path,
    receipts_file: Path,
    state_dir: Path,
    review_contract: Optional[ReviewContract] = None,
    gate_results_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    require_github_pr: bool = False,
) -> Dict[str, Any]:
    """Verify closure for a single PR without requiring whole-feature completion.

    This runs queue reconciliation to derive the PR's current state,
    validates gate evidence for that PR, and detects contradictions
    between structured gate results and normalized reports.

    When ``branch`` is provided and ``require_github_pr`` is True, also verifies
    that a real GitHub PR exists for the branch and that required CI checks are
    green (pre-merge mode).  Local-only closure cannot pass merge readiness when
    GitHub PR / CI linkage is required.
    """
    from queue_reconciler import QueueReconciler

    checks: List[CheckResult] = []
    paths = ensure_env()

    # 1. Reconcile queue to get fresh PR state
    projection_file = state_dir / "pr_queue_state.json"
    reconciler = QueueReconciler(
        dispatch_dir=dispatch_dir,
        receipts_file=receipts_file,
        feature_plan=feature_plan,
        projection_file=projection_file if projection_file.is_file() else None,
    )
    result = reconciler.reconcile()

    pr_reconciled = next((p for p in result.prs if p.pr_id == pr_id), None)
    if pr_reconciled is None:
        checks.append(CheckResult(
            "pr_exists_in_plan", "FAIL",
            f"{pr_id} not found in FEATURE_PLAN.md",
        ))
        return {
            "verdict": "fail",
            "mode": "per_pr",
            "pr_id": pr_id,
            "checks": [c.__dict__ for c in checks],
            "reconciled_state": None,
            "review_evidence": None,
        }

    # 2. PR must be completed per reconciliation
    if pr_reconciled.state == "completed":
        receipt_note = ""
        if not pr_reconciled.provenance.get("receipt_confirmed"):
            receipt_note = " (unconfirmed — no terminal receipt)"
        checks.append(CheckResult(
            "pr_completed", "PASS",
            f"{pr_id} is completed via {pr_reconciled.provenance.get('source', '?')}{receipt_note}",
        ))
    else:
        checks.append(CheckResult(
            "pr_completed", "FAIL",
            f"{pr_id} state is {pr_reconciled.state}, not completed",
        ))

    # 3. Check for blocking drift affecting this PR
    pr_drift = [w for w in result.drift_warnings if w.pr_id == pr_id and w.severity == "blocking"]
    if pr_drift:
        checks.append(CheckResult(
            "queue_drift", "FAIL",
            f"{pr_id} has blocking queue drift: {pr_drift[0].message}",
        ))
    else:
        checks.append(CheckResult(
            "queue_drift", "PASS",
            f"no blocking drift for {pr_id}",
        ))

    # 4. Review contract and gate evidence
    review_evidence_summary: Optional[Dict[str, Any]] = None
    if review_contract is not None:
        # Per-PR closure must reject a contract for a different PR — combining
        # PR-A's queue state with PR-B's review evidence can falsely green.
        if review_contract.pr_id and review_contract.pr_id != pr_id:
            checks.append(CheckResult(
                "review_contract_pr_match",
                "FAIL",
                f"review contract pr_id {review_contract.pr_id!r} does not match closure pr_id {pr_id!r}",
            ))
        else:
            effective_results_dir = gate_results_dir
            if effective_results_dir is None:
                effective_results_dir = Path(paths["VNX_STATE_DIR"]) / "review_gates" / "results"
            checks.extend(
                _validate_review_evidence(review_contract, effective_results_dir, branch=branch)
            )

            # 5. Gate result vs report contradiction detection
            checks.extend(
                _detect_gate_report_contradictions(
                    review_contract, effective_results_dir, branch=branch
                )
            )

            review_evidence_summary = {
                "contract_pr_id": review_contract.pr_id,
                "contract_hash": review_contract.content_hash,
                "review_stack": review_contract.review_stack,
                "risk_class": review_contract.risk_class,
                "deterministic_finding_count": len(review_contract.deterministic_findings),
                "error_finding_count": len([f for f in review_contract.deterministic_findings if f.severity == "error"]),
            }
    else:
        checks.append(CheckResult(
            "review_contract", "FAIL",
            "no review contract provided — per-PR closure requires contract-backed evidence",
        ))

    # 5. GitHub PR and CI checks (when branch is provided and explicitly required)
    # Local-only closure cannot pass merge readiness without a real GitHub PR + green checks.
    # Per-PR mode must mirror the full-feature pre_merge guarantees: state == OPEN and
    # mergeStateStatus == CLEAN, otherwise a draft, blocked, or merged-but-stale PR with
    # green CI could be reported as merge-ready.
    github_pr: Optional[Dict[str, Any]] = None
    if branch and require_github_pr:
        github_pr = _find_branch_pr(branch)
        checks.append(CheckResult(
            "github_pr_exists",
            "PASS" if github_pr else "FAIL",
            f"GitHub PR {'found' if github_pr else 'missing'} for branch {branch}",
        ))
        if github_pr:
            state = str(github_pr.get("state") or "").upper()
            merge_state = str(github_pr.get("mergeStateStatus") or "").upper()
            mergeable = state == "OPEN" and merge_state == "CLEAN"
            checks.append(CheckResult(
                "github_pr_mergeable",
                "PASS" if mergeable else "FAIL",
                f"PR state={state or 'unknown'} mergeStateStatus={merge_state or 'unknown'}"
                + ("" if mergeable else " — PR must be OPEN and CLEAN for merge readiness"),
            ))
            rollup = github_pr.get("statusCheckRollup") or []
            all_green = bool(rollup) and all(
                item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
                for item in rollup
                if item.get("__typename") == "CheckRun"
            )
            checks.append(CheckResult(
                "github_checks",
                "PASS" if all_green else "FAIL",
                "all required GitHub checks green" if all_green
                else "GitHub checks incomplete or failing — merge readiness blocked",
            ))

    verdict = "pass" if all(c.status == "PASS" for c in checks) else "fail"
    return {
        "verdict": verdict,
        "mode": "per_pr",
        "pr_id": pr_id,
        "branch": branch,
        "reconciled_state": {
            "pr_id": pr_reconciled.pr_id,
            "state": pr_reconciled.state,
            "provenance": pr_reconciled.provenance,
        },
        "checks": [c.__dict__ for c in checks],
        "review_evidence": review_evidence_summary,
        "github_pr": github_pr,
    }


def _detect_gate_report_contradictions(
    contract: ReviewContract,
    results_dir: Path,
    branch: Optional[str] = None,
) -> List[CheckResult]:
    """Detect contradictions between structured gate result JSON and normalized report content.

    A contradiction exists when:
    - Gate result JSON says pass but report contains blocking findings
    - Gate result JSON says fail but report claims all clear
    - Gate result blocking_count disagrees with actual blocking findings in report

    When ``branch`` is provided, gate results from a different branch are
    rejected as stale evidence so contradiction detection always runs against
    the current branch's payloads.
    """
    checks: List[CheckResult] = []

    for gate in contract.review_stack:
        result = _find_gate_result(gate, contract.pr_id, results_dir, branch=branch)
        if result is None:
            continue

        report_path_str = result.get("report_path", "")
        if not report_path_str:
            continue

        report_path = Path(report_path_str)
        if not report_path.exists():
            continue

        try:
            report_content = report_path.read_text(encoding="utf-8")
        except OSError:
            continue

        passed, _reason = gate_is_pass(result)
        gate_blocking = result.get("blocking_count", 0)
        if not isinstance(gate_blocking, int):
            gate_blocking = len(result.get("blocking_findings") or [])

        # Count blocking-severity indicators in report
        report_blocking_indicators = _count_report_blocking_indicators(report_content)

        # Contradiction 1: gate says pass but report has blocking indicators
        if passed and report_blocking_indicators > 0:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "FAIL",
                f"{gate}: gate result says pass but report contains "
                f"{report_blocking_indicators} blocking indicator(s) — evidence mismatch",
            ))
        # Contradiction 2: gate says fail/blocking but report has none
        elif (not passed) and gate_blocking > 0 and report_blocking_indicators == 0:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "FAIL",
                f"{gate}: gate result says fail with {gate_blocking} blocking finding(s) "
                f"but report contains no blocking indicators — evidence mismatch",
            ))
        else:
            checks.append(CheckResult(
                f"contradiction_{gate}",
                "PASS",
                f"{gate}: gate result and report content are consistent",
            ))

    return checks


def _count_report_blocking_indicators(content: str) -> int:
    """Count blocking-severity indicators in a normalized report.

    Looks for patterns that indicate blocking findings in headless review reports.
    """
    import re
    count = 0
    # Standard blocking patterns in normalized reports
    blocking_patterns = [
        r"\[BLOCKING\]",
        r"\*\*Severity\*\*:\s*blocking",
        r"severity:\s*blocking",
        r"BLOCK(?:ER|ING)\s*:",
        r"Status:\s*FAIL",
    ]
    for pattern in blocking_patterns:
        count += len(re.findall(pattern, content, re.IGNORECASE))
    return count


def verify_closure(
    *,
    project_root: Path,
    feature_plan: Path,
    pr_queue: Path,
    branch: str,
    mode: str,
    claim_file: Optional[Path] = None,
    review_contract: Optional[ReviewContract] = None,
    gate_results_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    feature = _parse_feature_plan(feature_plan)
    queue = _parse_pr_queue(pr_queue)
    claims = _load_claim_file(claim_file)
    paths = ensure_env()

    checks: List[CheckResult] = []

    checks.append(
        CheckResult(
            "feature_plan_status",
            "PASS" if feature["status"].lower() == "complete" else "FAIL",
            f"FEATURE_PLAN status is {feature['status'] or 'missing'}",
        )
    )

    queue_match = queue["overview"] is not None and queue["overview"][0] == queue["overview"][1]
    checks.append(
        CheckResult(
            "pr_queue_complete",
            "PASS" if queue_match else "FAIL",
            "PR queue is fully complete" if queue_match else "PR queue totals are not fully complete",
        )
    )

    checks.append(
        CheckResult(
            "metadata_sync",
            "PASS" if feature["title"] and feature["title"] == queue["title"] and feature["dependency_flow"] == queue["dependency_flow"] else "FAIL",
            "FEATURE_PLAN and PR_QUEUE titles/dependency flow match"
            if feature["title"] and feature["title"] == queue["title"] and feature["dependency_flow"] == queue["dependency_flow"]
            else "FEATURE_PLAN and PR_QUEUE drift detected",
        )
    )

    branch_exists = _remote_branch_exists(branch, project_root)
    checks.append(
        CheckResult(
            "branch_pushed",
            "PASS" if branch_exists else "FAIL",
            f"remote branch {'found' if branch_exists else 'missing'}: {branch}",
        )
    )

    pr = _find_branch_pr(branch)
    checks.append(
        CheckResult(
            "pr_exists",
            "PASS" if pr else "FAIL",
            f"PR {'found' if pr else 'missing'} for branch {branch}",
        )
    )

    if pr:
        if mode == "pre_merge":
            clean = str(pr.get("mergeStateStatus") or "").upper() == "CLEAN" and str(pr.get("state") or "").upper() == "OPEN"
            checks.append(
                CheckResult(
                    "merge_state",
                    "PASS" if clean else "FAIL",
                    f"PR state={pr.get('state')} mergeStateStatus={pr.get('mergeStateStatus')}",
                )
            )
            rollup = pr.get("statusCheckRollup") or []
            all_green = bool(rollup) and all(
                item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS"
                for item in rollup
                if item.get("__typename") == "CheckRun"
            )
            checks.append(
                CheckResult(
                    "github_checks",
                    "PASS" if all_green else "FAIL",
                    "all required GitHub checks green" if all_green else "GitHub checks incomplete or failing",
                )
            )
        else:
            merged = str(pr.get("state") or "").upper() == "MERGED"
            checks.append(
                CheckResult(
                    "pr_merged",
                    "PASS" if merged else "FAIL",
                    f"PR state={pr.get('state')}",
                )
            )
            merge_commit = ((pr.get("mergeCommit") or {}) if isinstance(pr.get("mergeCommit"), dict) else {}) or {}
            oid = merge_commit.get("oid")
            on_main = bool(oid) and _merge_commit_on_main(oid, project_root)
            checks.append(
                CheckResult(
                    "merge_commit_on_main",
                    "PASS" if on_main else "FAIL",
                    f"merge commit {'present on origin/main' if on_main else 'missing from origin/main'}",
                )
            )

    checks.extend(_validate_test_claims(claims, project_root))
    checks.append(_check_stale_staging(paths, feature["pr_ids"]))

    # Review contract and gate evidence enforcement
    review_evidence_summary: Optional[Dict[str, Any]] = None
    if review_contract is not None:
        effective_results_dir = gate_results_dir
        if effective_results_dir is None:
            effective_results_dir = Path(paths["VNX_STATE_DIR"]) / "review_gates" / "results"
        checks.extend(
            _validate_review_evidence(review_contract, effective_results_dir, branch=branch)
        )
        # Gate result vs report contradiction detection — feature-level closure
        # must catch a [BLOCKING] report shipped alongside a passing gate JSON.
        checks.extend(
            _detect_gate_report_contradictions(
                review_contract, effective_results_dir, branch=branch
            )
        )
        review_evidence_summary = {
            "contract_pr_id": review_contract.pr_id,
            "contract_hash": review_contract.content_hash,
            "review_stack": review_contract.review_stack,
            "risk_class": review_contract.risk_class,
            "deterministic_finding_count": len(review_contract.deterministic_findings),
            "error_finding_count": len([f for f in review_contract.deterministic_findings if f.severity == "error"]),
        }
    else:
        checks.append(CheckResult(
            "review_contract",
            "FAIL",
            "no review contract provided — closure requires contract-backed evidence",
        ))

    verdict = "pass" if all(check.status == "PASS" for check in checks) else "fail"
    payload = {
        "verdict": verdict,
        "mode": mode,
        "branch": branch,
        "feature_title": feature["title"],
        "checks": [check.__dict__ for check in checks],
        "pr": pr,
        "claim_file": str(claim_file) if claim_file else None,
        "review_evidence": review_evidence_summary,
    }
    return payload


def _default_claim_file(paths: Dict[str, str]) -> Path:
    return Path(paths["VNX_STATE_DIR"]) / "closure_claim.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="VNX closure verifier")
    parser.add_argument("--feature-plan", default="FEATURE_PLAN.md")
    parser.add_argument("--pr-queue", default="PR_QUEUE.md")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--mode", choices=("pre_merge", "post_merge"), default="pre_merge")
    parser.add_argument("--claim-file", default=None)
    parser.add_argument("--review-contract", default=None, help="Path to review contract JSON")
    parser.add_argument("--gate-results-dir", default=None, help="Directory containing gate result JSONs")
    parser.add_argument("--pr-id", default=None, help="Per-PR closure mode: verify a single PR without requiring whole-feature completion")
    parser.add_argument(
        "--require-github-pr",
        action="store_true",
        help="In --pr-id mode, also require a real GitHub PR for --branch with OPEN/CLEAN state and green CI",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-receipt", action="store_true")
    args = parser.parse_args(argv)

    paths = ensure_env()
    project_root = Path(paths["PROJECT_ROOT"]).resolve()

    contract: Optional[ReviewContract] = None
    if args.review_contract:
        contract_path = Path(args.review_contract)
        if contract_path.exists():
            contract = ReviewContract.from_json(contract_path.read_text(encoding="utf-8"))

    gate_results_dir: Optional[Path] = None
    if args.gate_results_dir:
        gate_results_dir = Path(args.gate_results_dir)

    # Per-PR closure mode
    if args.pr_id:
        feature_plan_path = (
            project_root / args.feature_plan
            if not Path(args.feature_plan).is_absolute()
            else Path(args.feature_plan)
        )
        # Infer the current branch when --branch is omitted so the caller does
        # not have to repeat themselves and so require_github_pr is reachable
        # with just --pr-id + --mode pre_merge.
        branch = args.branch or _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root
        ).stdout.strip() or None
        # In pre_merge mode, --require-github-pr enforces a real GitHub PR with
        # OPEN/CLEAN state and green CI; in post_merge mode the caller can still
        # opt in but it is only meaningful pre-merge.
        require_github_pr = bool(args.require_github_pr) and args.mode == "pre_merge"
        result = verify_pr_closure(
            pr_id=args.pr_id,
            project_root=project_root,
            feature_plan=feature_plan_path,
            dispatch_dir=Path(paths["VNX_DISPATCH_DIR"]),
            receipts_file=Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson",
            state_dir=Path(paths["VNX_STATE_DIR"]),
            review_contract=contract,
            gate_results_dir=gate_results_dir,
            branch=branch,
            require_github_pr=require_github_pr,
        )
    else:
        branch = args.branch or _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root).stdout.strip()
        claim_file = Path(args.claim_file) if args.claim_file else _default_claim_file(paths)

        result = verify_closure(
            project_root=project_root,
            feature_plan=(project_root / args.feature_plan if not Path(args.feature_plan).is_absolute() else Path(args.feature_plan)),
            pr_queue=(project_root / args.pr_queue if not Path(args.pr_queue).is_absolute() else Path(args.pr_queue)),
            branch=branch,
            mode=args.mode,
            claim_file=claim_file if claim_file.exists() else None,
            review_contract=contract,
            gate_results_dir=gate_results_dir,
        )

        if args.emit_receipt:
            emit_governance_receipt(
                "closure_verification_result",
                status="success" if result["verdict"] == "pass" else "blocked",
                branch=branch,
                verification_mode=args.mode,
                feature_title=result.get("feature_title"),
                verifier="closure_verifier.py",
                checks=result["checks"],
                review_evidence=result.get("review_evidence"),
            )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        mode_label = f"Per-PR ({args.pr_id})" if args.pr_id else "Closure"
        print(f"{mode_label} verifier: {result['verdict'].upper()}")
        for check in result["checks"]:
            print(f"- [{check['status']}] {check['name']}: {check['detail']}")

    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
