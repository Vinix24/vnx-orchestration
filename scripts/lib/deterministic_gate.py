#!/usr/bin/env python3
"""deterministic_gate.py — the Deterministic adapter kind (ADR-046 §4, GE-6).

The local half of ``ci_gate`` and ``wiring_gate``: the checks that need nothing
but the checkout they run in and the data they are handed. The GitHub half of
each gate stays where it was and keeps its own ``gh`` subprocess —
``gh pr checks`` for ``ci_gate`` (``gate_executor._execute_ci_gate``) and
``gh pr diff`` for ``wiring_gate`` (``wiring_gate.check_pr_wiring``). The
adapter is called with what that read produced (``checks``, ``diff_text``), or
— for the wiring gate — derives the diff from a local checkout with ``git``
alone: no ``gh``, no network.

:data:`DETERMINISTIC_GATES` carries one row per deterministic gate: the gate
name, its local check, and the record builder that turns the check's evidence
into the outcome's record. ``gate_recorder.GATE_PROVIDERS`` names the kind; a
new deterministic gate is a row here plus a register row there.

:func:`run` returns a :class:`DeterministicRun`: the record, and the
:class:`GateOutcome` — ``None`` for a snapshot that is no outcome at all (a
``ci_gate`` run whose checks are still going, ADR-046 §1). A check that cannot
run here (no diff to read, no ``git`` to read it with) is ``not_executable``
with the reason, never ``pass``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import wiring_gate
from gate_outcome import GateOutcome

# The kind's own name in ``gate_recorder.GATE_PROVIDERS`` (ADR-046 §4).
DETERMINISTIC_KIND = "deterministic"

# `gh pr checks --json` (gh 2.76.2, measured 2026-08-20) exposes `bucket`, which
# gh itself documents (`gh pr checks --help`) as categorizing the raw `state`
# into exactly one of: pass, fail, pending, skipping, cancel. "pending" is the
# only non-terminal bucket; a bucket outside this known set is treated as
# non-terminal too, so an unrecognized future value holds the verdict at
# "running" instead of silently completing.
CI_CHECK_BUCKET_TERMINAL = frozenset({"pass", "fail", "skipping", "cancel"})
CI_CHECK_BUCKET_SKIP = frozenset({"skipping", "cancel"})


class LocalCheckNotExecutable(Exception):
    """The check cannot run here: no diff to read, or no tool to read it with.

    Raised, never returned as a pass: a check that did not look must not read
    as ``pass`` (ADR-046 §1). ``reason``/``detail`` land on the record.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class LocalContext:
    """What the gate's GitHub half already fetched, plus the run's identity.

    ``diff_text``/``checks``/``wiring_result`` are the inputs the caller's own
    read produced; ``checkout`` is the tree the local check runs against (the
    wiring analysis greps it) and the source of a locally derived diff when no
    ``diff_text`` was handed in. ``required`` is ``None`` when the row's check
    reads the gate's own posture itself.
    """

    checkout: Optional[Path] = None
    head_sha: str = ""
    base_sha: str = ""
    diff_text: str = ""
    checks: Sequence[Mapping[str, Any]] = ()
    wiring_result: Any = None
    pr_number: Optional[int] = None
    pr_id: str = ""
    branch: str = ""
    required: Optional[bool] = None
    contract_hash: str = ""
    requested_at: str = ""
    recorded_at: str = ""
    duration_seconds: float = 0.0
    report_path: str = ""
    state_dir: Optional[Path] = None


@dataclass(frozen=True)
class DeterministicRun:
    """One run of a deterministic gate: its record, and its outcome when it has one."""

    record: Dict[str, Any]
    outcome: Optional[GateOutcome] = None


def _bucket(check: Mapping[str, Any]) -> str:
    """The check's bucket, lowercased. A non-string one is not terminal (never a pass)."""
    return str(check.get("bucket") or "").lower()


def _local_diff(ctx: LocalContext) -> str:
    """The diff derived from the local checkout: ``git`` only, no ``gh``, no network."""
    if not (ctx.checkout and ctx.base_sha and ctx.head_sha):
        raise LocalCheckNotExecutable(
            "diff_unavailable",
            "no diff text was handed to the adapter and the checkout names no base/head to derive one",
        )
    argv = [
        "git", "-C", str(ctx.checkout), "diff", "--no-color", "--no-ext-diff",
        f"{ctx.base_sha}...{ctx.head_sha}",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise LocalCheckNotExecutable("diff_unavailable", f"git diff in {ctx.checkout} failed: {exc}") from exc
    if proc.returncode != 0:
        raise LocalCheckNotExecutable(
            "diff_unavailable",
            f"git diff {ctx.base_sha}...{ctx.head_sha} exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:200]}",
        )
    return proc.stdout


def wiring_analysis(ctx: LocalContext) -> wiring_gate.WiringGateResult:
    """The wiring gate's local check: analyse a diff, grep the checkout. No ``gh``."""
    if ctx.wiring_result is not None:
        return ctx.wiring_result
    diff_text = ctx.diff_text or _local_diff(ctx)
    return wiring_gate.analyse_diff(
        diff_text, state_dir=ctx.state_dir, root=ctx.checkout, required=ctx.required,
    )


def ci_classification(ctx: LocalContext) -> Dict[str, Any]:
    """The ci gate's local check: classify the check rows the ``gh`` read returned.

    FAILURE → blocking, SKIPPED/CANCELLED → advisory, SUCCESS → passed, no
    checks at all → vacuous pass (Case D, measured). Nothing here shells out.
    """
    checks = [dict(check) for check in ctx.checks]
    passed = [check for check in checks if _bucket(check) == "pass"]
    failed = [check for check in checks if _bucket(check) == "fail"]
    skipped = [check for check in checks if _bucket(check) in CI_CHECK_BUCKET_SKIP]
    all_complete = all(_bucket(check) in CI_CHECK_BUCKET_TERMINAL for check in checks) if checks else True
    verdict = "running" if not all_complete else "fail" if failed else "pass"
    return {"checks": checks, "passed": passed, "failed": failed, "skipped": skipped, "verdict": verdict}


def format_ci_gate_report(
    *,
    pr_number: Optional[int],
    branch: str,
    checks: Sequence[Mapping[str, Any]],
    passed: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
    status: str,
    generated_at: str,
) -> str:
    """Format ci_gate results as a normalized headless report."""
    verdict_icon = {"pass": "PASS", "fail": "FAIL", "running": "RUNNING"}.get(status, status.upper())
    lines = [
        "# ci_gate — Headless Gate Report",
        "",
        f"**PR**: {pr_number or 'unknown'}",
        f"**Branch**: {branch}",
        f"**Gate**: ci_gate",
        f"**Generated**: {generated_at}",
        f"**Verdict**: {verdict_icon}",
        "",
        "---",
        "",
        "## CI Check Results",
        "",
    ]
    if not checks:
        lines.append("_(no checks found — vacuously passing)_")
    else:
        if passed:
            lines.append("### Passed")
            for c in passed:
                lines.append(f"- [OK] {c.get('name', '?')} — {c.get('state', 'SUCCESS')}")
            lines.append("")
        if failed:
            lines.append("### Failed [BLOCKING]")
            for c in failed:
                lines.append(f"- [BLOCKING] {c.get('name', '?')} — {c.get('state', 'FAILURE')}")
                lines.append(f"  - **Severity**: blocking")
            lines.append("")
        if skipped:
            lines.append("### Skipped / Cancelled")
            for c in skipped:
                lines.append(f"- [ADVISORY] {c.get('name', '?')} — {c.get('state', 'SKIPPED')}")
            lines.append("")
    lines.append(f"**Status**: {'FAIL' if failed else 'PASS' if status == 'pass' else status.upper()}")
    lines.append(f"Passed: {len(passed)} | Failed: {len(failed)} | Skipped: {len(skipped)}")
    lines.append("")
    return "\n".join(lines) + "\n"


def ci_summary(
    verdict: str,
    passed: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
) -> str:
    if verdict == "running":
        return "CI checks still running — verdict pending"
    if verdict == "fail":
        names = ", ".join(c.get("name", "?") for c in failed[:3])
        tail = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        return f"CI gate FAIL: {len(failed)} failed check(s): {names}{tail}"
    if not passed and not failed and not skipped:
        return "CI gate PASS: no checks configured (vacuous pass)"
    return f"CI gate PASS: {len(passed)} check(s) passed, {len(skipped)} advisory"


def _write_ci_report(evidence: Dict[str, Any], ctx: LocalContext) -> bool:
    """Write the report for a terminal ci verdict; ``False`` when nothing was written."""
    if not (ctx.report_path and evidence["verdict"] in ("pass", "fail")):
        return False
    content = format_ci_gate_report(
        pr_number=ctx.pr_number, branch=ctx.branch, checks=evidence["checks"],
        passed=evidence["passed"], failed=evidence["failed"], skipped=evidence["skipped"],
        status=evidence["verdict"], generated_at=ctx.recorded_at,
    )
    try:
        Path(ctx.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(ctx.report_path).write_text(content, encoding="utf-8")
    except OSError:
        # vnx-silent-except: the report is evidence for a human reader; the record and its
        # receipt are the outcome, and an unwritable report must not fail the gate run.
        return False
    return True


def ci_record(evidence: Dict[str, Any], ctx: LocalContext) -> Dict[str, Any]:
    """The ci gate's outcome record: the fields ``_execute_ci_gate`` wrote before GE-6, plus ``kind``."""
    verdict, failed, skipped = evidence["verdict"], evidence["failed"], evidence["skipped"]
    report_written = _write_ci_report(evidence, ctx)
    return {
        "gate": "ci_gate",
        "pr_id": ctx.pr_id or (str(ctx.pr_number) if ctx.pr_number is not None else ""),
        "pr_number": ctx.pr_number,
        "status": verdict,
        "kind": DETERMINISTIC_KIND,
        "summary": ci_summary(verdict, evidence["passed"], failed, skipped),
        "passed_checks": [c.get("name", "") for c in evidence["passed"]],
        "failed_checks": [c.get("name", "") for c in failed],
        "blocking_findings": [
            {
                "severity": "blocking",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' concluded with {c.get('state', 'FAILURE')}",
            }
            for c in failed
        ],
        "advisory_findings": [
            {
                "severity": "advisory",
                "title": c.get("name", "unknown"),
                "description": f"CI check '{c.get('name', '?')}' was {c.get('state', 'SKIPPED')}",
            }
            for c in skipped
        ],
        "blocking_count": len(failed),
        "advisory_count": len(skipped),
        "contract_hash": ctx.contract_hash if verdict in ("pass", "fail") else "",
        "report_path": ctx.report_path if (report_written and verdict in ("pass", "fail")) else "",
        "required_reruns": [],
        "residual_risk": "" if verdict == "pass" else f"CI gate {verdict}: {len(failed)} failed check(s)",
        "duration_seconds": ctx.duration_seconds,
        "recorded_at": ctx.recorded_at,
    }


def wiring_record(result: wiring_gate.WiringGateResult, ctx: LocalContext) -> Dict[str, Any]:
    """The wiring gate's outcome record: the fields the request writer built before GE-6, plus ``kind``."""
    blocking = result.status == "fail"
    return {
        "gate": "wiring_gate",
        "status": result.status,
        "provider": "ast_grep",
        "kind": DETERMINISTIC_KIND,
        "required": bool(ctx.required),
        "branch": ctx.branch,
        "pr_number": ctx.pr_number,
        "requested_at": ctx.requested_at,
        "completed_at": ctx.recorded_at,
        "summary": result.summary,
        "blocking_findings": [
            {
                "severity": "blocking" if blocking else "advisory",
                "title": f"Unwired {s.kind}: {s.name}",
                "description": f"{s.file}:{s.line} — zero callers outside definition file",
            }
            for s in result.unwired
        ],
        "blocking_count": len(result.unwired) if blocking else 0,
        "advisory_count": len(result.unwired) if not blocking else 0,
        "total_checked": result.total_checked,
        "skipped_symbols": result.skipped,
    }


@dataclass(frozen=True)
class DeterministicGate:
    """One register row: the gate, its local check, and the record that check's evidence becomes."""

    gate: str
    check: Callable[[LocalContext], Any]
    record: Callable[[Any, LocalContext], Dict[str, Any]]


DETERMINISTIC_GATES: Dict[str, DeterministicGate] = {
    "ci_gate": DeterministicGate("ci_gate", ci_classification, ci_record),
    "wiring_gate": DeterministicGate("wiring_gate", wiring_analysis, wiring_record),
}

# A record status that is not an outcome: the ci gate's "running" snapshot is no
# verdict at all (ADR-046 §1), so it has no GateOutcome. "advisory" is the
# wiring gate's shadow-mode pass: the gate looked and did not reject, and the
# record keeps its own status word (the four statuses do not change).
_OUTCOME_STATUS: Dict[str, str] = {"pass": "pass", "fail": "fail", "advisory": "pass"}


def _pr_ref(ctx: LocalContext) -> str:
    return ctx.pr_id or (str(ctx.pr_number) if ctx.pr_number is not None else "")


def not_executable_record(
    gate: str, reason: str, detail: str, ctx: LocalContext, *, provider: str = "local",
) -> Dict[str, Any]:
    """The record of a check that could not run: ``not_executable`` with the reason, never ``pass``."""
    return {
        "gate": gate,
        "status": "not_executable",
        "provider": provider,
        "kind": DETERMINISTIC_KIND,
        "required": bool(ctx.required),
        "branch": ctx.branch,
        "pr_number": ctx.pr_number,
        "requested_at": ctx.requested_at,
        "reason": reason,
        "reason_detail": detail,
    }


def run(gate: str, ctx: LocalContext) -> DeterministicRun:
    """Run ``gate``'s local checks and return its record and outcome (``None`` when it has none)."""
    row = DETERMINISTIC_GATES.get(gate)
    if row is None:
        raise KeyError(f"{gate} is no deterministic gate; the rows are {sorted(DETERMINISTIC_GATES)}")
    try:
        evidence = row.check(ctx)
    except LocalCheckNotExecutable as exc:
        record = not_executable_record(gate, exc.reason, exc.detail, ctx)
        return DeterministicRun(
            record=record,
            outcome=GateOutcome(
                gate=gate, status="not_executable", pr_id=_pr_ref(ctx),
                pr_number=ctx.pr_number, record=record,
            ),
        )
    record = row.record(evidence, ctx)
    status = _OUTCOME_STATUS.get(record["status"])
    outcome = None if status is None else GateOutcome(
        gate=gate, status=status, pr_id=_pr_ref(ctx), pr_number=record.get("pr_number"), record=record,
    )
    return DeterministicRun(record=record, outcome=outcome)
