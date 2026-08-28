#!/usr/bin/env python3
"""pr_readiness.py — what does this PR still need before it can merge?

The question a human answered by hand fourteen times in one night: which of
the fourteen branch-protection contexts are present, which review gates the
obligation demands, whether each gate record sits on the CURRENT head, whether
its report exists, and whether record and report contradict each other. Each
pass took several ``gh`` calls plus a throwaway Python block.

None of the underlying answers are new. This module deliberately reuses the
implementations that already decide them, rather than growing a second
opinion beside each one:

  contexts        ``ci_contexts.evaluate_commit`` (required-vs-actual, with
                  "not created yet" separated from "never created")
  declared gates  ``gate_obligations.declared_gates_for_pr`` (the door's own
                  obligation record, the same join ``pr_merge`` uses)
  gate evidence   ``closure_verifier.check_review_gate_for_merge`` (record on
                  this head, terminal, contract_hash + report_path non-empty,
                  report on disk, verdict passes, report does not contradict
                  the verdict)

What is new is only the aggregation, the cost of closing each gap, and the
refusal to round anything unmeasured up to green.

Fail-loud is the whole point. Every section that cannot be measured yields
:data:`VERDICT_UNMEASURABLE`, never ``READY``. "I could not look" and "I
looked and it is fine" are the two answers this report exists to keep apart —
counting only what is present is exactly how #1691 read as nine green passes
while five required contexts had never been created.
"""

from __future__ import annotations

import json
import subprocess
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _p in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import ci_contexts  # noqa: E402
from dispatch_spec import Gate  # noqa: E402

VERDICT_READY = "READY"
VERDICT_NOT_READY = "NOT READY"
VERDICT_UNMEASURABLE = "UNMEASURABLE"

#: PR fields the report needs. ``mergeable``/``mergeStateStatus`` are reported
#: verbatim, including GitHub's "UNKNOWN" — that value means the background
#: mergeability job has not finished, not that the PR is fine.
_PR_FIELDS = (
    "number,state,isDraft,title,headRefName,headRefOid,baseRefName,"
    "mergeable,mergeStateStatus"
)


@dataclass(frozen=True)
class GateCost:
    """What closing one gate gap actually costs, in a runnable form."""

    command: str
    lane: str
    usd: Optional[str] = None
    note: str = ""

    def describe(self, pr_number: int, head_sha: str) -> str:
        price = self.usd or "no USD cost"
        line = f"{self.command.format(pr=pr_number)} — {self.lane}, {price}"
        if self.note:
            line += f" ({self.note})"
        return line


#: Cost per gate, keyed on the CLOSED ``Gate`` enum. Every member has an entry
#: and ``test_pr_readiness.py`` pins that: a gate added to the enum without a
#: cost entry would otherwise be reported as "run it" with no idea what running
#: it takes — which is the half of the question that made the manual count slow.
#:
#: The USD band on the OpenRouter lanes is the operator's measured range, not a
#: list price: a gate run costs 1.3–2.8 USD, not the 0.4 an earlier estimate
#: assumed.
GATE_COST: Dict[str, GateCost] = {
    Gate.GLM_GATE.value: GateCost(
        command="python3 scripts/glm_gate.py --pr {pr}",
        lane="glm-harness → litellm :4141 → OpenRouter",
        usd="~1.3–2.8 USD",
        note="source ~/.config/vnx/provider-usage.env in the same subshell; glm-5.2 only",
    ),
    Gate.KIMI_GATE.value: GateCost(
        command="python3 scripts/kimi_gate.py --pr {pr}",
        lane="kimi CLI (OAuth)",
        usd="subscription",
        note="kimi-via-cli-only: never the Moonshot API",
    ),
    Gate.CODEX_GATE.value: GateCost(
        command="python3 scripts/review_gate_manager.py request-and-execute --gate codex_gate --pr {pr}",
        lane="codex CLI",
        usd="subscription",
        note="rate-limited; operator policy A1 is to wait for the reset, not to fall back",
    ),
    Gate.GEMINI_REVIEW.value: GateCost(
        command="python3 scripts/review_gate_manager.py request-and-execute --gate gemini_review --pr {pr}",
        lane="gemini CLI",
        usd="subscription",
        note="no standalone runner script in this repo — goes through the manager",
    ),
    Gate.CI_GATE.value: GateCost(
        command="python3 scripts/review_gate_manager.py request-and-execute --gate ci_gate --pr {pr}",
        lane="gh CLI, inline (no provider)",
        usd="free",
        note="reads the checks that already ran; it does not start CI",
    ),
    Gate.WIRING_GATE.value: GateCost(
        command="python3 scripts/review_gate_manager.py request-and-execute --gate wiring_gate --pr {pr}",
        lane="local, deterministic",
        usd="free",
    ),
    Gate.CLAUDE_GITHUB_OPTIONAL.value: GateCost(
        command="python3 scripts/review_gate_manager.py request-and-execute --gate claude_github_optional --pr {pr}",
        lane="GitHub app",
        usd="subscription",
        note="optional gate: an intentional absence is a legitimate end state",
    ),
}


@dataclass(frozen=True)
class GateEvidence:
    """One declared gate, and whether its evidence holds for THIS head."""

    gate: str
    verdict: str  # "GO" | "NO-GO" | "UNMEASURABLE"
    message: str
    record_sha: Optional[str] = None
    report_path: Optional[str] = None
    report_exists: Optional[bool] = None
    #: False when no obligation declared this gate but a result record for it
    #: exists anyway — a gate run by hand rather than through the door.
    declared: bool = True

    @property
    def satisfied(self) -> bool:
        return self.verdict == "GO"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate": self.gate,
            "verdict": self.verdict,
            "message": self.message,
            "record_sha": self.record_sha,
            "report_path": self.report_path,
            "report_exists": self.report_exists,
            "declared": self.declared,
        }


@dataclass
class Readiness:
    """The whole answer for one PR, renderable and serialisable."""

    pr_number: int
    facts: Dict[str, Any] = field(default_factory=dict)
    contexts: List[Any] = field(default_factory=list)
    contexts_error: Optional[str] = None
    gates: List[GateEvidence] = field(default_factory=list)
    gates_error: Optional[str] = None
    declared_gates: List[str] = field(default_factory=list)
    observed_gates: List[str] = field(default_factory=list)

    @property
    def head_sha(self) -> str:
        return str(self.facts.get("headRefOid") or "")

    @property
    def unmeasurable_reasons(self) -> List[str]:
        reasons = []
        if self.contexts_error:
            reasons.append(f"required contexts: {self.contexts_error}")
        elif not self.contexts:
            # An empty required-context set is a misconfiguration, not a pass.
            # The renderer already said so; without this the verdict said READY
            # and the command exited 0 on a set nobody measured — the report
            # contradicting itself, which is the exact defect class this
            # command exists to surface on other people's evidence.
            reasons.append(
                "required contexts: branch protection lists none, so nothing was verified"
            )
        if self.gates_error:
            reasons.append(f"review gates: {self.gates_error}")
        reasons += [
            f"{c.context}: {c.detail}"
            for c in self.contexts
            if c.state == ci_contexts.STATE_UNVERIFIED
        ]
        reasons += [
            f"{g.gate}: {g.message}" for g in self.gates if g.verdict == VERDICT_UNMEASURABLE
        ]
        return reasons

    @property
    def blockers(self) -> List[str]:
        out = []
        blocking = [c for c in self.contexts if c.blocking]
        settled = [c for c in blocking if not c.transient]
        in_flight = [c for c in blocking if c.transient]
        if settled:
            out.append(f"{len(settled)} required context(s) not satisfied")
        if in_flight:
            out.append(f"{len(in_flight)} required context(s) still in flight")
        unsatisfied = [g for g in self.gates if g.verdict == "NO-GO"]
        if unsatisfied:
            out.append(f"{len(unsatisfied)} review gate(s) without evidence on this head")
        if not self.declared_gates and not self.gates_error:
            if self.observed_gates:
                out.append(
                    "no review-gate obligation declared — "
                    f"{len(self.observed_gates)} gate result(s) exist off the door"
                )
            else:
                out.append("no review-gate obligation declared for this PR")
        state = str(self.facts.get("state") or "").upper()
        if state and state != "OPEN":
            out.append(f"PR state is {state}")
        if self.facts.get("isDraft"):
            out.append("PR is a draft")
        return out

    @property
    def verdict(self) -> str:
        if self.unmeasurable_reasons:
            return VERDICT_UNMEASURABLE
        return VERDICT_NOT_READY if self.blockers else VERDICT_READY

    def costs(self) -> List[str]:
        """What it takes to close each open gap, most actionable first."""
        out = []
        never = [c for c in self.contexts if c.state == ci_contexts.STATE_NEVER_CREATED]
        no_run = [c for c in self.contexts if c.state == ci_contexts.STATE_NO_RUN]
        for group, action in ((never, "re-run"), (no_run, "start")):
            for c in group:
                out.append(
                    f"{action} {c.workflow_name or 'the producing workflow'} on {self.head_sha[:12]} "
                    f"— gh run rerun (no flags, keeps the sha) — free, for {c.context}"
                )
        failed = [c for c in self.contexts if c.state == ci_contexts.STATE_FAILED]
        for c in failed:
            out.append(f"fix and re-push — {c.context} {c.detail}")
        if not self.declared_gates and not self.gates_error and not self.observed_gates:
            # An undeclared obligation has a cost too, and leaving it off the
            # list was the one place this report said "nothing outstanding"
            # about a PR it had just called NOT READY.
            choices = " or ".join(
                f"{name} ({GATE_COST[name].usd})"
                for name in (Gate.GLM_GATE.value, Gate.CODEX_GATE.value)
            )
            out.append(
                f"declare and run a review gate for #{self.pr_number} — {choices}; "
                "the door writes the obligation, `vnx dispatch` is the entry"
            )
        for gate in self.gates:
            if gate.satisfied:
                continue
            cost = GATE_COST.get(gate.gate)
            if cost is None:
                out.append(
                    f"{gate.gate}: no cost entry — this gate is not in GATE_COST, so what it "
                    "takes to run is UNKNOWN, not free"
                )
                continue
            out.append(cost.describe(self.pr_number, self.head_sha))
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "verdict": self.verdict,
            "head_sha": self.head_sha,
            "facts": self.facts,
            "contexts": [c.to_dict() for c in self.contexts],
            "contexts_error": self.contexts_error,
            "contexts_summary": ci_contexts.summarise(self.contexts) if self.contexts else None,
            "declared_gates": self.declared_gates,
            "observed_gates": self.observed_gates,
            "gates": [g.to_dict() for g in self.gates],
            "gates_error": self.gates_error,
            "blockers": self.blockers,
            "unmeasurable": self.unmeasurable_reasons,
            "costs": self.costs(),
        }


class PRReadinessError(RuntimeError):
    """The PR itself could not be read — nothing downstream is meaningful."""


def fetch_pr_facts(pr_number: int, project_root: Path, timeout: int = 20) -> Dict[str, Any]:
    """``gh pr view`` for the fields the report needs, or raise."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", _PR_FIELDS],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PRReadinessError(f"gh CLI not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PRReadinessError(f"gh pr view #{pr_number} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise PRReadinessError(
            f"gh pr view #{pr_number} failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:300]}"
        )
    try:
        facts = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PRReadinessError(f"gh pr view #{pr_number} returned unparseable JSON: {exc}") from exc
    if not isinstance(facts, dict) or not facts.get("headRefOid"):
        raise PRReadinessError(f"gh pr view #{pr_number} returned no head sha — cannot bind evidence")
    return facts


def observed_gates_for_pr(results_dir: Path, pr_number: int) -> List[str]:
    """Gates that produced a result record for this PR, declared or not.

    The obligation store is the door's record of what a PR OWES. It is empty
    for a PR whose gate was run by hand rather than dispatched through the
    door — measured on this fleet, #1701 merged with no obligation at all.
    Reading only the obligations therefore misses gate evidence that is
    sitting on disk, and reports "no gate verdict" about a PR that has one.

    Offline ``test_run`` records are excluded here for the same reason
    ``closure_verifier`` excludes them: a synthetic pr_id must never align
    with a real PR.

    An unreadable or malformed result file is skipped rather than raised on:
    unlike an obligation, a result file is not a claim that something is
    owed, so a corrupt one cannot hide an obligation. The declared list —
    which CAN hide one — still fails loud.
    """
    gates: List[str] = []
    if not results_dir.is_dir():
        return gates
    for path in sorted(results_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("pr_id") or "") != str(pr_number):
            continue
        if record.get("test_run") in (True, "1", "true", "yes"):
            continue
        gate = (record.get("gate") or "").strip()
        if gate and gate not in gates:
            gates.append(gate)
    return gates


def collect_gate_evidence(
    pr_number: int,
    declared_gates: Sequence[str],
    state_dir: Path,
    *,
    branch: str,
    head_sha: str,
    declared: Optional[Sequence[str]] = None,
) -> List[GateEvidence]:
    """Evaluate each declared gate against ``head_sha``.

    Delegates the verdict to ``closure_verifier.check_review_gate_for_merge`` —
    the same five invariants the merge door enforces, never a weaker second
    copy. On top of that it records WHERE the newest record for this gate sits
    when it is not on this head, because "the gate ran, two commits ago" and
    "the gate never ran" cost completely different things to fix and the merge
    door's binary verdict cannot tell them apart.
    """
    from closure_verifier import _find_gate_result, check_review_gate_for_merge

    results_dir = Path(state_dir) / "review_gates" / "results"
    declared_set = set(declared_gates if declared is None else declared)
    evidence: List[GateEvidence] = []
    for gate in declared_gates:
        try:
            verdict = check_review_gate_for_merge(
                str(pr_number), gate, results_dir, branch=branch, head_sha=head_sha,
            )
        except (OSError, ValueError) as exc:
            evidence.append(
                GateEvidence(
                    gate=gate,
                    verdict=VERDICT_UNMEASURABLE,
                    message=f"gate evidence could not be read: {type(exc).__name__}: {exc}",
                    declared=gate in declared_set,
                )
            )
            continue

        record_sha = None
        report_path = None
        report_exists = None
        # Only interesting when the head-bound lookup found nothing: name the
        # commit the newest record DOES sit on, so the reader sees "stale" and
        # "absent" as the different problems they are.
        anywhere = _find_gate_result(gate, str(pr_number), results_dir)
        if anywhere:
            record_sha = anywhere.get("commit_sha") or None
            report_path = anywhere.get("report_path") or None
            if report_path:
                report_exists = Path(report_path).exists()
        evidence.append(
            GateEvidence(
                gate=gate,
                verdict=verdict.get("verdict", VERDICT_UNMEASURABLE),
                message=verdict.get("message", ""),
                record_sha=record_sha,
                report_path=report_path,
                report_exists=report_exists,
                declared=gate in declared_set,
            )
        )
    return evidence


def assess(
    pr_number: int,
    project_root: Path,
    state_dir: Path,
    *,
    protected_branch: str = "main",
    timeout: int = 20,
) -> Readiness:
    """Build the full readiness answer for one PR.

    Raises :class:`PRReadinessError` only when the PR itself is unreadable —
    without a head sha there is nothing to bind evidence to. Every other
    failure is captured into the report as an unmeasurable section, because a
    report that dies on one unreadable input tells the reader less than one
    that says exactly which part it could not see.
    """
    facts = fetch_pr_facts(pr_number, project_root, timeout)
    report = Readiness(pr_number=pr_number, facts=facts)
    head_sha = str(facts.get("headRefOid") or "")
    branch = str(facts.get("headRefName") or "")

    try:
        report.contexts = ci_contexts.evaluate_commit(
            project_root, head_sha, branch=protected_branch, timeout=timeout,
        )
    except ci_contexts.CIContextsError as exc:
        report.contexts_error = str(exc)

    try:
        from gate_obligations import declared_gates_for_pr

        report.declared_gates = list(dict.fromkeys(declared_gates_for_pr(state_dir, pr_number)))
    except (ValueError, OSError) as exc:
        report.gates_error = f"{type(exc).__name__}: {exc}"
        return report

    results_dir = Path(state_dir) / "review_gates" / "results"
    observed = observed_gates_for_pr(results_dir, pr_number)
    report.observed_gates = [g for g in observed if g not in report.declared_gates]
    report.gates = collect_gate_evidence(
        pr_number,
        report.declared_gates + report.observed_gates,
        state_dir,
        branch=branch,
        head_sha=head_sha,
        declared=report.declared_gates,
    )
    return report
