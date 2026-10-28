#!/usr/bin/env python3
"""governed_lane.py — the one GovernedLane adapter for the harness-lane review gates.

ADR-046 §4 names three adapter kinds: ``GovernedLane`` (``glm_gate``, ``kimi_gate``,
``deepseek_gate``), ``Deterministic`` and ``PathBinary``. This module is the
``GovernedLane``: the dispatcher wiring that used to live in
``plan_gate_panel._make_default_dispatcher``, the report reader that used to live
in ``plan_gate_panel._read_report``, and the one verdict parser the two standalone
gates used to carry as separate copies (``glm_gate._extract_verdict``,
``kimi_gate._extract_verdict``).

GE-5a is a pure extraction: every caller keeps importing the same names from
``plan_gate_panel`` (which re-exports the dispatcher and the reader under their
old names) and ``glm_gate``/``kimi_gate`` keep a module-level ``_extract_verdict``
alias, so no call site changes and no behaviour changes.

GE-5b-2 adds the other half: phases 3-6 of both gates (the unread-run outcome and
the verdict, the ``--reprocess`` identity check and the evidence pair, the record
write through the recorder, and the printed outcome) are one body here,
:func:`run_gate_tail`. The gate scripts hand it a :class:`GateRunState` — what
their own phases 1-2 produced — so every provider difference lives on the
register row, never in an ``if provider ==`` branch.

GE-5b moves phases 1-2 here too (:func:`run_gate_head`): the CLI (``--pr
--diff-file --data-dir --model --timeout --json --reprocess`` — same names,
defaults, help and exit codes), the request record written before any dispatch
(OI-1435 defect 4), and the diff/report acquisition for both a live run and
``--reprocess``. ``glm_gate.py`` and ``kimi_gate.py`` are now shims: they keep
the CLI entry point, the module-level aliases the tests read, and a
:class:`GateSeams` object bound to their OWN namespace (so every monkeypatch on
``glm_gate._get_diff`` and friends still reaches the call), then hand over.

Every seam is resolved off that object at CALL time, never snapshotted at import
— the same mechanism the phase 3-6 tail has always used. The provider
differences stay register fields: ``cli_description`` (the argparse description),
``model_pin`` (glm admits exactly one model and refuses the rest loudly; kimi
declares no pin and passes the model through), ``lane_provider``,
``dispatch_id_prefix``, ``gate`` and the optional ``report_failure_lift``.

``deepseek_gate`` has no parser of its own: it runs on gate_runner's harness-lane
path and its report is read by the provider-agnostic ``gate_report_recovery``
reader. It is therefore deliberately absent from ``GOVERNED_LANE_PROVIDERS`` —
measured on the tree, not assumed.

The dispatcher below still reaches back into ``plan_gate_panel`` for the transport
helpers it was built on (``_resolve_data_dir``, ``_seat_child_env``,
``_run_provider_lane_child``, ``_run_claude_headless_seat`` and the file-ref
instruction builders). They are resolved through :func:`_panel` at call time, not
at import time, so this module and ``plan_gate_panel`` do not import each other in
a cycle: the panel imports this adapter at module load, and the adapter imports
the panel only when a dispatcher is actually built or called. Resolving them off
the module object also keeps every existing monkeypatch seam (tests patch
``plan_gate_panel._run_provider_lane_child`` and friends) working unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import gate_depth  # after the scripts/lib path guard
import harness_run_cost  # K2b: the gate run's own cost-ledger line
import provider_costs  # K2b: the ledger of the store the gate itself resolved
from gate_lane_contract import (
    MODEL_DEFAULTS,
    VERDICT_CONTRACT,
    canonical_review_verdict,
    describe_dispatch_exception,
    harness_dispatcher_timeouts,
    lane_deadline_detail,
    reviews_with_tools,
)
from gate_artifacts import _compute_contract_hash
from gate_prompt import build_review_prompt, merge_scan_findings, scan_diff_for_instructions
from gate_report_recovery import (
    AmbiguousRecoveryCandidates,
    extract_relabeled_verdict,
    recovered_verdict_conflicts,
)
from governance_emit import _classify_lane_log_text, _read_lane_log_text
from unified_report_schema import SchemaViolation, parse_frontmatter

# A dispatcher takes (provider, model_arg, instruction, dispatch_id) and returns
# the panelist's report text. Same shape as ``plan_gate_panel.DispatcherFn`` —
# re-declared here because this module must not import the panel at import time.
DispatcherFn = Callable[[str, str, str, str], str]
# A dispatcher FACTORY takes the panel's own keyword contract and returns a
# DispatcherFn. Injected into :func:`review_dispatcher` rather than imported
# here: each gate keeps the factory bound in its own namespace, and every test
# patches it there.
DispatcherFactory = Callable[..., DispatcherFn]


def _panel():
    """The plan-gate module, imported on first use (see the module docstring)."""
    # Lazy on purpose: the panel imports this module back (see the module docstring).
    import plan_gate_panel

    return plan_gate_panel


def _last_json_verdict(text: str) -> dict:
    """Extract the worker's verdict: the LAST ```json``` block whose ``verdict`` is a
    real verdict (pass/fail/blocked).

    One rule, both providers (measured, see ``GOVERNED_LANE_PROVIDERS``): scan the
    ```json``` fences from the end and take the first body that decodes to a dict
    whose ``verdict`` is a canonical review verdict. The governed worker report
    echoes the instruction, so the FIRST ```json``` block is our own contract
    example (its ``verdict`` is the literal "pass|fail|blocked", which is not a
    valid single verdict and is skipped) — scanning backwards selects the worker's
    actual answer, never the echoed template.
    """
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and canonical_review_verdict(obj.get("verdict")):
            return obj
    return {}


@dataclass(frozen=True)
class ModelPin:
    """A gate that admits exactly ONE model, declared on the register instead of branched in code.

    ``allowed`` holds the canonical lowercase names, ``refusal`` is the operator-facing text printed
    when a run names any other (``{model}`` and ``{allowed}`` are the only placeholders).
    """

    allowed: frozenset
    refusal: str

    def canonical(self, model: str) -> "Optional[str]":
        """The canonical lowercase model, or None when this pin does not admit it."""
        normalized = (model or "").strip().lower()
        return normalized if normalized in self.allowed else None


@dataclass(frozen=True)
class ProviderRow:
    """One provider's row of the register.

    ``gate`` is the gate name the shared helpers hand to
    ``gate_lane_contract`` (``harness_dispatcher_timeouts``,
    ``reviews_with_tools``) and the slot/file stem ``--reprocess`` reads
    (``pr-<N>-<gate>.json``). It differs per provider, so it lives here as a
    register field rather than as an ``if provider == ...`` branch in a helper.

    ``lane_provider``/``dispatch_id_prefix`` are the lane name and the
    ``<prefix>-pr<N>-<epoch>`` id shape; ``cli_description`` is the argparse
    description of the gate's CLI; ``model_pin`` is the optional one-model pin
    (glm) or ``None`` for a provider whose lane admits whatever it is handed
    (kimi); ``report_failure_lift`` is the optional report-frontmatter
    failure-reason reader (glm's OI-1710 diagnosis, ``None`` for kimi) and
    ``lift_detail_label`` names it. ``cost_provider`` is the provider
    :func:`harness_run_cost.record_cli_run` books this gate's run under (K2b:
    kimi), ``None`` for a lane not written from here yet (glm, until K3). No
    provider branch in the head or the tail.
    """

    parser: Callable[[str], dict]
    gate: str
    origin: str
    lane_provider: str
    dispatch_id_prefix: str
    cli_description: str
    report_failure_lift: Optional[Callable[[str], Optional[str]]] = None
    lift_detail_label: str = "lane log"
    model_pin: Optional[ModelPin] = None
    cost_provider: Optional[str] = None


def lift_report_failure_reason(report_text: str) -> "Optional[str]":
    """OI-1710: read the canonical ``failure_reason`` the lane's own governance
    emit already stamped onto the report frontmatter (the spawn-layer error, for
    a run that died before the model could reply).

    The lane-log lift reads the raw lane log — a spawn-layer failure (proxy
    unreachable, harness refused to start) writes no lane log at all, so the
    report is the only place the real reason survives. Returns the bounded
    reason only when the frontmatter carries a real, non-empty value; returns
    None — never invents a reason — on a missing/empty field or unreadable
    frontmatter."""
    if not report_text:
        return None
    try:
        frontmatter = parse_frontmatter(report_text)
    except SchemaViolation:
        return None
    reason = frontmatter.get("failure_reason")
    return (reason or "").strip() or None


# Per-provider difference, measured at GE-5a (not assumed): there is NONE in the
# executable body. ``ast.dump()`` of the two pre-GE-5a parsers is identical once
# the docstring is stripped — glm's docstring cross-references kimi's, and that
# prose (not the rule) is what the inventory's 0.68 positional ratio measured on
# raw text. Both rows therefore carry the one rule:
#   scan ```json fences from the LAST one backwards; take the first body that
#   json-decodes to a dict whose "verdict" is a canonical review verdict
#   (pass/fail/blocked), else {}.
# Kept as one row per provider anyway: a quirk discovered for one provider
# becomes a row edit here and can never silently change its neighbour's gate.
GOVERNED_LANE_PROVIDERS: Dict[str, ProviderRow] = {
    "glm": ProviderRow(
        parser=_last_json_verdict, gate="glm_gate", origin="scripts/glm_gate.py:142 (pre-GE-5a)",
        lane_provider="glm-harness", dispatch_id_prefix="glm-gate-pr",
        cli_description="GOVERNED glm-harness review gate.",
        report_failure_lift=lift_report_failure_reason, lift_detail_label="failure reason",
        # The gate stays pinned to ONE model (glm-5.2) for a deterministic reviewer, narrower by
        # design than deprecated-glm-models' allowlist (glm-5.2/5.3/5.3-flash since 2026-09-14,
        # provider_constraints.yaml): the wider list is for lane measurements, not for this gate.
        model_pin=ModelPin(
            allowed=frozenset({MODEL_DEFAULTS["glm_gate"][1]}),
            refusal=(
                "glm_gate: REFUSING model {model!r} — only {allowed} is admitted for GLM "
                "(deprecated-glm-models: GLM-4.5/4.6/5/5.1 are blocked, operator directive 2026-08-03)"
            ),
        ),
    ),
    "kimi": ProviderRow(
        parser=_last_json_verdict, gate="kimi_gate", origin="scripts/kimi_gate.py:153 (pre-GE-5a)",
        lane_provider="kimi", dispatch_id_prefix="kimi-gate-pr",
        cli_description="Temporary GOVERNED kimi review gate.",
        # K2b: kimi's gate books its own run, from the seat's report.
        cost_provider="kimi",
    ),
}


def _row(provider: str) -> ProviderRow:
    """The register row of *provider*, or a loud refusal.

    An unknown provider RAISES instead of falling back to a neighbour's row: a
    wrong parser that answers "no verdict" would silently book a real review as
    unreadable, and a wrong gate name would read another gate's result slot.
    """
    row = GOVERNED_LANE_PROVIDERS.get((provider or "").strip().lower())
    if row is None:
        raise ValueError(
            f"governed_lane: no row registered for provider {provider!r}; "
            f"known providers: {', '.join(sorted(GOVERNED_LANE_PROVIDERS))}"
        )
    return row


def provider_row(provider: str) -> ProviderRow:
    """Public name for the register lookup — a gate script puts its row in :class:`GateRunState`."""
    return _row(provider)


def extract_verdict(text: str, provider: str) -> dict:
    """Read *text* with the register row of *provider*.

    Missing or malformed text reads as "no verdict" ({}), the answer every
    caller already maps to ``unavailable`` — never to a pass.
    """
    return _row(provider).parser(text)


def read_report(base: Optional[Path], dispatch_id: str, stderr: str) -> Optional[str]:
    """Locate a panelist's unified report. Authoritative source: the ``Report: <path>``
    line provider_dispatch prints to stderr; falls back to the deterministic path.

    Only a path whose filename is exactly ``{dispatch_id}.md`` is accepted — a foreign
    or stale ``Report:`` line must never feed this panelist a different dispatch's
    verdict (the gate's verdict-source integrity)."""
    expected = f"{dispatch_id}.md"
    for line in (stderr or "").splitlines():
        if line.startswith("Report: "):
            p = Path(line[len("Report: "):].strip())
            if p.name == expected and p.is_file():
                return p.read_text(encoding="utf-8")
    if base is not None:
        p = base / "unified_reports" / expected
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def _make_default_dispatcher(
    data_dir: Optional[str], timeout_seconds: int,
    *, role: str = "plan-reviewer", base_ref: Optional[str] = None,
    total_deadline_seconds: Optional[int] = None,
) -> DispatcherFn:
    """Real dispatcher: run a panelist through its governed lane, return the report text.

    INTERIM — PR-12 consolidation target. This dispatches WITHOUT going through the
    single-entry door (`vnx dispatch` / dispatch_bridge), which is a side door. Once the
    door is wired and flipped, this MUST route through that one door instead. The split
    below is exactly what the door decides internally:
      claude            -> headless lane, `claude -p` via ClaudeSubprocessAdapter, on
                           the SUBSCRIPTION (measured 2026-08-11; the door's default for
                           claude since A2, 2026-08-26). NOT provider_dispatch — it
                           refuses claude.
      kimi/glm/deepseek -> provider_dispatch (constraint-safe per provider).

    ``role``: OI-811 — this factory is reused by callers whose instruction is NOT a plan
    review (e.g. the deliberation panel's diverge/contrarian/verify/synthesis stages).
    Defaults to "plan-reviewer" for backward compatibility with ``run_panel``. A caller
    with a different role gets the generic (non-plan-framed) file-ref instruction on the
    claude seat, and that role is stamped on both lanes so govern() and the phantom-guard
    evaluate it correctly instead of being told it is a plan review.

    ``base_ref``: OI-1887 — threaded straight into ``provider_dispatch.py --base-ref``
    (which already threads it into ``create_dispatch_worktree``, see that module) so a
    review-gate caller reviewing a PR can isolate the provider's worktree on
    ``origin/<pr-branch>`` instead of the default ``origin/main``. None (the default)
    leaves the plan-gate and deliberation-panel callers unchanged: neither reviews a PR
    branch, so both keep isolating on main. Never applies to the claude branch below —
    that lane isolates via ``dispatch_worktree_isolation`` on its own dispatch_id and has
    no PR branch to isolate on either.

    ``total_deadline_seconds``: OI-2066 — the lane's OWN deadline, set as
    ``VNX_TOTAL_DEADLINE`` in the provider_dispatch child's environment (the drainer and
    the claude-harness adapter read it there). ``timeout_seconds`` stays the outer
    subprocess timeout; a review gate passes it with a margin on top so the lane's own
    deadline fires first and the lane writes its own report. Only the child env carries
    it — this process's ``os.environ`` is never touched. None (the default) leaves the
    plan-gate and deliberation-panel callers exactly as they were. The claude branch
    keeps running its seat with ``timeout_seconds`` as its deadline; no review gate
    routes through it.
    """
    base = _panel()._resolve_data_dir(data_dir)

    def _dispatch(provider: str, model_arg: str, instruction: str, dispatch_id: str) -> str:
        # OI-1153: pin the seat subprocess to the SAME data dir this dispatcher
        # resolved (`base`), or the lane re-resolves its own. provider_dispatch
        # honors the two-key VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1 contract (see
        # its _resolve_data_dir); without it the seat's report write path can land
        # outside the dir read_report reads back from (e.g. the legacy
        # ~/.vnx-data/unified_reports root), and the seat report is silently
        # never found. The claude branch pins the same two keys — see
        # _run_claude_headless_seat, which sets them on os.environ because on that
        # lane they must bind in this process as well as in the child.
        env = _panel()._seat_child_env(base, total_deadline_seconds)
        _tmp_doc_path: Optional[str] = None
        try:
            if provider in _panel()._CLAUDE_PROVIDERS:
                # BUG-2 FIX (file-ref): the instruction already has the full plan doc inlined
                # by run_panel's build_plan_review_instruction call. For the claude seat we
                # replace it with a compact file-ref instruction so the ~50k-char body never
                # has to travel as one prompt argument.
                #
                # We write the original inline instruction to a temp file so the worker can
                # read the plan + rubric + verdict contract from a stable on-disk path.
                # The expected report path is derived from data_dir (resolved above, never
                # None) so the file-ref instruction's write path and read_report's read path
                # agree.
                report_path_str = str(base / "unified_reports" / f"{dispatch_id}.md")

                # Write the full inline instruction (plan + rubric + contract) to a temp file.
                # The worker reads this file; the short file-ref instruction points to it.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".vnx_plan_review.md",
                    delete=False,
                    encoding="utf-8",
                    prefix=f"plan_gate_{dispatch_id}_",
                ) as fh:
                    fh.write(instruction)
                    _tmp_doc_path = fh.name

                # Short instruction: rubric + verdict contract + explicit report-path directive.
                # No 50k doc body — the worker reads it from _tmp_doc_path. A non-plan-review
                # caller (OI-811) gets the generic file-ref framing instead — never told it is
                # reviewing an IMPLEMENTATION PLAN.
                if role == "plan-reviewer":
                    claude_instruction = _panel().build_plan_review_instruction_fileref(
                        doc_path=_tmp_doc_path,
                        track_id="<see file>",
                        report_path=report_path_str,
                    )
                else:
                    claude_instruction = _panel().build_generic_fileref_instruction(
                        doc_path=_tmp_doc_path,
                        report_path=report_path_str,
                    )

                return _panel()._run_claude_headless_seat(
                    dispatch_id=dispatch_id,
                    model=model_arg,
                    instruction=claude_instruction,
                    role=role,
                    base=base,
                    timeout_seconds=timeout_seconds,
                )

            cmd = [
                sys.executable, str(_panel().PROVIDER_DISPATCH),
                "--provider", provider,
                "--terminal-id", _panel()._PANEL_TERMINAL_ID,
                "--dispatch-id", dispatch_id,
                "--model", model_arg,
                "--role", role,
                # OI-1422: every seat this dispatcher builds (plan-reviewer,
                # deliberation-panelist) is a read-only verdict/analysis worker —
                # it reads a doc and writes a report, it never touches repo files.
                # Without an explicit --task-class, provider_dispatch.py's
                # argparse default ("") falls through to VNX_TASK_CLASS, which is
                # unset here, so _build_frontmatter's own fallback stamps
                # task_class="implementation" on the receipt (provider_dispatch.py
                # _build_frontmatter). kimi_spawn.py's completion-vs-execution
                # fabrication guard (_finalize_kimi_result) keys off task_class
                # ALONE — it has no role parameter — so "implementation" leaves a
                # clean-worktree kimi seat classified as a delivery worker that
                # produced nothing, and the guard fires a false "completion
                # without execution" failure on real panel/review output.
                # "research_structured" is the fabric's own canonical task_class
                # for reviewer/architect/planner/security-engineer/data-analyst
                # work (dispatch_router.py SKILL_TO_TASK_CLASS) and is already a
                # member of phantom_guard.REVIEW_TASK_CLASSES, so this exempts the
                # seat through both guards without widening either SSOT.
                "--task-class", "research_structured",
                "--instruction", instruction,
                "--no-auto-commit",
            ]
            if base_ref:
                cmd += ["--base-ref", base_ref]
            proc = _panel()._run_provider_lane_child(
                cmd, env=env, timeout_seconds=timeout_seconds, dispatch_id=dispatch_id,
                data_dir=base)
            report = _panel()._read_report(base, dispatch_id, proc.stderr)
            if report is None:
                # provider_dispatch prints its actionable detail across both streams —
                # surfacing only stderr once masked the real cause behind an unrelated
                # last-stderr-line red herring for a full day.
                raise RuntimeError(
                    f"no report for {dispatch_id} (rc={proc.returncode}): "
                    f"stdout: {(proc.stdout or '')[-800:]} | stderr: {(proc.stderr or '')[-400:]}"
                )
            return report
        finally:
            # Always clean up the temp doc file regardless of success or failure.
            if _tmp_doc_path is not None:
                try:
                    os.unlink(_tmp_doc_path)
                except OSError:
                    pass

    return _dispatch



# GE-5b: the twelve helpers glm_gate and kimi_gate each carried a copy of. One body each, with the
# provider (or its register row's gate name) as an argument — never an ``if provider ==`` branch.


def get_diff(pr: str, diff_file: "Optional[str]") -> "Optional[str]":
    """The diff under review: *diff_file* when given, else ``gh pr diff <pr>``; None on any failure."""
    if diff_file:
        p = Path(diff_file)
        return p.read_text(encoding="utf-8") if p.is_file() else None
    try:
        res = subprocess.run(["gh", "pr", "diff", pr], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return res.stdout if res.returncode == 0 and res.stdout.strip() else None
    except (subprocess.SubprocessError, OSError):
        return None


def build_prompt(provider: str, diff_text: str, pr: str, max_chars: int) -> str:
    """OI-1442's delimited-data prompt for *provider*'s gate; *max_chars* is resolved once by the caller (OI-1874 r3)."""
    gate = _row(provider).gate
    return build_review_prompt(gate_name=gate, pr=pr, diff_text=diff_text, verdict_contract=VERDICT_CONTRACT, max_chars=max_chars, agentic=reviews_with_tools(gate))


def review_dispatcher(provider: str, base_data_dir: Path, branch: str, coverage: dict, explicit_timeout: "Optional[int]", *, dispatcher_factory: DispatcherFactory) -> DispatcherFn:
    """OI-2066's per-run dispatcher. *dispatcher_factory* is INJECTED, never resolved here: each
    gate's tests patch ``<script>._make_default_dispatcher``, so the caller hands in that name."""
    gate = _row(provider).gate
    return dispatcher_factory(
        str(base_data_dir), role="review-gate", base_ref=(f"origin/{branch}" if branch else None),
        **harness_dispatcher_timeouts(gate, gate_depth.prompt_diff_chars(coverage), explicit=explicit_timeout),
    )


def frontmatter_run_outcome(report_text: str) -> "Tuple[Optional[bool], object, object]":
    """``(failed, exit_code, output_tokens)`` off the report frontmatter; ``exit_code`` (stamped from the
    spawn) decides, since the failure path writes report content too. ``token_usage.output`` counts only
    when ``token_usage_measured`` is true (OI-1291). None when there is no readable ``exit_code``."""
    try:
        frontmatter = parse_frontmatter(report_text)
    except SchemaViolation:
        return None, None, None
    if "exit_code" not in frontmatter:
        return None, None, None
    exit_code = frontmatter.get("exit_code")
    token_usage = frontmatter.get("token_usage")
    output_tokens = token_usage.get("output") if isinstance(token_usage, dict) else None
    measured_zero = bool(frontmatter.get("token_usage_measured")) and output_tokens == 0
    return (exit_code != 0 or measured_zero), exit_code, output_tokens


def parse_dispatch_window_start(provider: str, dispatch_id: str, *, pr: str) -> "Optional[float]":
    """Start time from a ``{provider}-gate-pr{pr}-<seconds>`` dispatch_id, or None when the id is not that
    exact shape: refuse rather than guess a recovery window (live run and ``--reprocess`` alike)."""
    prefix = f"{provider}-gate-pr{pr}-"
    ts_part = dispatch_id[len(prefix):]
    if not dispatch_id.startswith(prefix) or not ts_part.isdigit():
        return None
    return float(ts_part)


def frontmatter_duration_seconds(report_text: str) -> float:
    """Best-effort ``duration_seconds`` off the frontmatter — ``--reprocess`` only."""
    try:
        return float(parse_frontmatter(report_text).get("duration_seconds", 0.0) or 0.0)
    except (SchemaViolation, TypeError, ValueError):
        return 0.0


def reprocess_original_commit_sha(provider: str, base_data_dir: Path, pr: str, dispatch_id: str) -> "Optional[str]":
    """The commit_sha THIS dispatch's own result slot carries, or None. Results are keyed by PR, so a
    later dispatch leaves no addressable identity: ``--reprocess`` must refuse rather than guess."""
    path = base_data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-{_row(provider).gate}.json"
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("dispatch_id") != dispatch_id:
        return None
    return record.get("commit_sha") or None


def verdict_to_status(provider: str, verdict: dict, report_text: str = "", *, provider_failed_detail: "Optional[str]" = None) -> "Tuple[str, list, str]":
    """``(status, blocking_findings, residual_risk)`` for one extracted verdict. OI-1142: only a REAL
    parsed verdict may produce ``fail``. ``provider_failed_detail`` (from ``frontmatter_run_outcome``)
    replaces the parse-miss residual when the report's own frontmatter says the provider run failed."""
    v = canonical_review_verdict(verdict.get("verdict")) if verdict else ""
    findings = verdict.get("findings") or [] if verdict else []
    blocking = [f for f in findings if isinstance(f, dict) and str(f.get("severity", "")).lower() in {"error", "blocked", "blocker"}]
    residual = (verdict.get("residual_risk") if verdict else "") or ""
    if v == "pass" and not blocking:
        return "pass", [], residual
    if v in {"fail", "blocked"} or blocking:
        return "fail", blocking, residual or f"{provider} gate reported blocking findings"
    if residual:
        return "unavailable", [], residual
    if provider_failed_detail:
        return "unavailable", [], provider_failed_detail
    if (report_text or "").strip():
        return "unavailable", [], (
            f"{provider} returned a {len(report_text)}-char report, but it contained no readable ```json``` "
            f"verdict block (parse miss — {provider} did respond)")
    return "unavailable", [], f"{provider} produced no readable verdict (provider outage/quota/truncation) — not a review outcome"


# dispatch-20260823-beta2-j: reason-specific unavailable summaries — every case that used to land in
# "parse_error" now either recovers or gets its own reason.
UNAVAILABLE_SUMMARIES = {
    "recovery_empty": "recovery_empty — searched for a companion report, found none",
    "recovery_ambiguous": "recovery_ambiguous — multiple companion reports found, refusing to guess",
    "recovery_conflict": "recovery_conflict — a companion report's verdict conflicted with the primary response",
    "reprocess_no_identity_anchor": "reprocess_no_identity_anchor — no addressable original result record to verify against",
    "reprocess_stale_evidence": "reprocess_stale_evidence — PR head has moved since this dispatch ran",
    "timeout": "timeout — the lane's own deadline stopped the run before the review finished",
}


def unread_run_outcome(provider: str, dispatch_error: str, report_text: str, duration: float) -> "Optional[Tuple[str, list, str, str]]":
    """``(status, blocking, residual, reason)`` when the report is never read for a verdict, else None:
    a raised dispatch, or OI-2066's deadline-killed run (it can still hold a partial pass block)."""
    if dispatch_error:
        return "unavailable", [], f"governed {provider} dispatch failed: {dispatch_error[:400]}", "dispatch_error"
    deadline = lane_deadline_detail(report_text, duration)
    if deadline is not None:
        return "unavailable", [], f"{provider}_gate timed out: {deadline} — not a review outcome", "timeout"
    return None


def lift_lane_log_reason(dispatch_id: str, data_dir: "Optional[Path]") -> "Optional[str]":
    """OI-1452: the real crash reason off the raw lane log, classified by governance_emit's shared
    classifier — never a second marker scan. None (never an invented reason) on a missing log."""
    text = _read_lane_log_text(dispatch_id, data_dir) if data_dir is not None else None
    if not text:
        return None
    state, reason = _classify_lane_log_text(text)
    return reason if state == "lane_exhausted" else None


def status_summary(provider: str, status: str, blocking: list, reason: str = "") -> str:
    """The result record's summary line, naming the gate that booked it."""
    if status == "unavailable":
        detail = UNAVAILABLE_SUMMARIES.get(reason) or "provider outage/no verdict"
        return f"{provider} gate: UNAVAILABLE ({detail} — NOT a review fail)"
    return f"{provider} gate: {status} ({len(blocking)} blocking finding(s))"


# GE-5b-2: phases 3-6 of both review gates, one body per phase, fed by one GateRunState (phases 1-2).


@dataclass(frozen=True)
class GateSeams:
    """Every callable a gate script binds in its OWN namespace, phases 1-6. Tests monkeypatch these on
    ``glm_gate``/``kimi_gate``, so the shared body resolves them off the script module's binding at CALL
    time — the script builds this object inside ``main``/``_gate_seams``, never at import."""

    resolve_data_dir: Callable[..., Path]
    get_diff: Callable[..., "Optional[str]"]
    make_default_dispatcher: DispatcherFactory
    persist_request: Callable[..., object]
    max_diff_chars: Callable[..., int]
    get_pr_head_sha: Callable[..., str]
    get_pr_head_branch: Callable[..., str]
    record_terminal_result: Callable[..., object]
    result_file_path: Callable[..., "Optional[Path]"]
    stamp_request_identity: Callable[..., object]
    find_recovery_candidate: Callable[..., object]


@dataclass(frozen=True)
class GateRunState:
    """What a gate's phases 1-2 produced, handed to :func:`run_gate_tail` as one value."""

    provider: str
    row: ProviderRow
    args: object
    base_data_dir: Path
    pr_number: "Optional[int]"
    dispatch_id: str
    branch: str
    commit_sha: str
    requests_dir: Path
    report_path_obj: Path
    report_text: str
    dispatch_error: str
    duration: float
    window_start: "Optional[float]"
    window_end: float
    prompt: "Optional[str]"
    scan_findings: list
    execution_depth: object
    seams: GateSeams


@dataclass(frozen=True)
class GateRunInputs:
    """Phase 2's product — everything :class:`GateRunState` needs beyond the CLI/register facts."""

    report_path_obj: Path
    report_text: str
    dispatch_error: str
    duration: float
    window_start: "Optional[float]"
    window_end: float
    prompt: "Optional[str]"
    scan_findings: list
    execution_depth: object


# GE-5b: phases 1-2 of both review gates — arguments, request record, diff/report acquisition — one
# body, driven by the register row. A difference between glm and kimi is a row edit, never a branch.


def _gate_cli(row: ProviderRow) -> argparse.ArgumentParser:
    """The CLI both gates share: same seven options, same names/defaults/help text; one description per row."""
    model_env, default_model = MODEL_DEFAULTS[row.gate]
    ap = argparse.ArgumentParser(description=row.cli_description)
    ap.add_argument("--pr", required=True, help="PR number (use 0 with --diff-file for offline test)")
    ap.add_argument("--diff-file", default=None, help="read the diff from a file instead of gh")
    ap.add_argument("--data-dir", default=os.environ.get("VNX_DATA_DIR", ""),
                    help="VNX data dir; report lands in <data-dir>/unified_reports/ and the "
                         "result in <data-dir>/state/review_gates/results/")
    ap.add_argument("--model", default=os.environ.get(model_env, default_model))
    # OI-2066: no fixed default — unset, the deadline is resolved for the diff.
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="print the result record as JSON")
    ap.add_argument(
        "--reprocess", metavar="DISPATCH_ID", default=None,
        help="reprocess an already-completed dispatch from its on-disk report "
             "instead of calling the dispatcher again — no diff fetch, no model "
             "call, no new receipt. Recovers a verdict a companion report or a "
             "wrong-label fence already carries. Requires --pr for identity/"
             "staleness verification.",
    )
    return ap


def _parse_gate_args(row: ProviderRow, argv: "Optional[list]") -> "Optional[argparse.Namespace]":
    """Parse argv and admit the model; None means the row's own refusal was printed (exit 1).

    A row with no ``model_pin`` (kimi) dispatches whatever model it was handed, unchanged — that is
    the lane's own business, not the gate's. A pinned row (glm) refuses LOUDLY at its own entry
    point, before the dispatcher is ever built, instead of failing deep inside the provider lane
    where a blocked model would be indistinguishable from an outage.
    """
    args = _gate_cli(row).parse_args(argv)
    if row.model_pin is None:
        return args
    canonical = row.model_pin.canonical(args.model)
    if canonical is None:
        print(row.model_pin.refusal.format(model=args.model, allowed=sorted(row.model_pin.allowed)),
              file=sys.stderr)
        return None
    args.model = canonical
    return args


def _write_gate_request(
    row: ProviderRow, args: argparse.Namespace, *, requests_dir: Path, report_path: str,
    pr_number: "Optional[int]", dispatch_id: str, branch: str, commit_sha: str, test_run: bool,
    seams: GateSeams,
) -> "Optional[int]":
    """OI-1435 defect 4: the request record exists BEFORE the gate ever dispatches.

    Same on-disk shape (``pr-<N>-<gate>.json`` under ``state/review_gates/requests/``) as
    ci_gate/codex_gate's own request writers, so a generic reader of that directory sees no
    difference. Returns exit code 1 when the record cannot be written — a gate that cannot prove it
    was requested must not go on to burn a dispatch (measured on PR #1672).
    """
    payload = {
        "gate": row.gate,
        "status": "requested",
        "provider": row.lane_provider,
        "branch": branch,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "report_path": report_path,
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dispatch_id": dispatch_id,
        "test_run": test_run,
    }
    try:
        requests_dir.mkdir(parents=True, exist_ok=True)
        seams.persist_request(requests_dir, row.gate, payload, pr_number=pr_number, pr_id="")
    except OSError as exc:
        print(f"{row.gate}: FAILED to write request record: {exc}", file=sys.stderr)
        return 1
    return None


def _reprocess_inputs(
    provider: str, row: ProviderRow, args: argparse.Namespace, base_data_dir: Path, dispatch_id: str,
    branch: str, seams: GateSeams,
) -> "Tuple[Optional[GateRunInputs], Optional[int]]":
    """``--reprocess``: read the report the ORIGINAL run wrote, never a diff and never a model call.

    Everything after (verdict extraction, recovery search, record) is the unchanged tail, so a
    recovered verdict was produced by the gate itself, on the run's own commit, in the run's own
    contract shape — not prose-to-verdict promotion of a new source.
    """
    report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
    if not report_path_obj.is_file():
        print(f"{row.gate}: --reprocess: no report on disk for dispatch_id={dispatch_id!r} "
              f"at {report_path_obj}", file=sys.stderr)
        return None, 1
    report_text = report_path_obj.read_text(encoding="utf-8")
    window_start = parse_dispatch_window_start(provider, dispatch_id, pr=args.pr)
    if window_start is None:
        print(f"{row.gate}: --reprocess: dispatch_id={dispatch_id!r} does not match the "
              f"{row.dispatch_id_prefix}{args.pr}-<timestamp> shape this gate constructs — "
              "refusing to guess a recovery time window", file=sys.stderr)
        return None, 1
    return GateRunInputs(
        report_path_obj=report_path_obj, report_text=report_text, dispatch_error="",
        duration=frontmatter_duration_seconds(report_text),
        window_start=window_start, window_end=report_path_obj.stat().st_mtime,
        prompt=None,  # never fetched in reprocess mode — see the contract_hash in the tail
        # OI-1442: nothing to scan. Scanning the CURRENT diff would attach findings from one commit
        # to a verdict formalized against another. OI-1618: no diff, so no depth measured — the
        # "not measured" ExecutionDepth is never re-evaluated against a floor this run never had.
        scan_findings=[], execution_depth=gate_depth.ExecutionDepth(),
    ), None


def _live_inputs(
    provider: str, row: ProviderRow, args: argparse.Namespace, base_data_dir: Path, dispatch_id: str,
    branch: str, seams: GateSeams,
) -> "Tuple[Optional[GateRunInputs], Optional[int]]":
    """A live run: the diff, the single-shot investigation depth, the governed dispatch and its report.

    ``role="review-gate"`` (dispatch-20260823-beta2-j): the factory's default "plan-reviewer" carries
    agents/plan-reviewer/CLAUDE.md, written for a plan-gate PANEL SEAT, and gives the model three
    conflicting instructions against this gate's own contract (a different fence label, a different
    verdict vocabulary, a different destination). agents/review-gate/CLAUDE.md is this gate's own
    role: a role-level split, not a dispatch_id string check. ``base_ref`` (OI-1887) isolates the
    governed dispatch on the PR's own branch instead of origin/main — measured 27-09 on PR #1950,
    where a run without it fetched the branch itself in the orchestrator's own checkout.
    """
    diff = seams.get_diff(args.pr, args.diff_file)
    if not diff:
        print(f"{row.gate}: no diff for PR {args.pr}", file=sys.stderr)
        return None, 1
    # OI-1874 r3: resolved ONCE — the same cap feeds gate_depth.diff_coverage (below) and the prompt
    # (build_prompt), so a config change between two calls can never make the recorded
    # diff_truncated disagree with what the prompt actually truncated. OI-1618/OI-1851: the diff IS
    # the investigation for a single-shot lane, so diff_chars (post-strip length, the degeneracy
    # floor), the cap and the cut files are the depth.
    diff_cap = seams.max_diff_chars(row.gate)
    coverage = gate_depth.diff_coverage(diff, diff_cap)
    execution_depth = gate_depth.single_shot_depth(
        coverage["diff_chars"], coverage["diff_truncated"],
        diff_limit=coverage["diff_limit"],
        truncated_files=tuple(coverage["truncated_files"]),
    )
    dispatcher = review_dispatcher(
        provider, base_data_dir, branch, coverage, args.timeout,
        dispatcher_factory=seams.make_default_dispatcher,
    )
    prompt = build_prompt(provider, diff, args.pr, diff_cap)
    # OI-1442: the deterministic half. The prompt asks the model to report instruction-shaped text
    # in the diff as a finding; this scan does not depend on it having done so. Both land in the record.
    scan_findings = scan_diff_for_instructions(diff)
    start = time.monotonic()
    report_text = ""
    dispatch_error = ""
    try:
        report_text = dispatcher(row.lane_provider, args.model, prompt, dispatch_id)
    except Exception as exc:  # The dispatcher's failure is booked as `unavailable`, never swallowed.
        # OI-1142: do NOT bail without a record — a required gate that cannot fire must surface as
        # ``unavailable`` in the results dir, not vanish. OI-2066: str(exc) of a TimeoutExpired is the
        # whole argv, the diff included — describe it without argv or prompt, bounded.
        dispatch_error = describe_dispatch_exception(exc, prompt=prompt)
        print(f"{row.gate}: governed {provider} dispatch failed: {dispatch_error}", file=sys.stderr)
    duration = time.monotonic() - start
    report_path_obj = base_data_dir / "unified_reports" / f"{dispatch_id}.md"
    # (a) OI-recovery-venster: reuse dispatch_id's OWN embedded int(time.time()) floor instead of a
    # fresh sub-second time.time() read. Measured 26-08 as a real CI flake
    # (test_search_recovers_verdict_from_companion[kimi], same commit 7a4d4a93: pass then fail):
    # Linux stamps file mtimes from a COARSE clock, so a file written a fraction of a millisecond
    # after a fine-clock reading can land one tick EARLIER. floor(t) sits a full second below the
    # construction instant — far larger than the skew — and lowering window_start can only ADMIT
    # more files, never exclude one the higher bound would have included. Cannot be None here: the
    # id was just constructed in exactly this shape (the None path only exists for --reprocess).
    window_start = parse_dispatch_window_start(provider, dispatch_id, pr=args.pr)
    window_end = report_path_obj.stat().st_mtime if report_path_obj.is_file() else time.time()
    return GateRunInputs(
        report_path_obj=report_path_obj, report_text=report_text, dispatch_error=dispatch_error,
        duration=duration, window_start=window_start, window_end=window_end, prompt=prompt,
        scan_findings=scan_findings, execution_depth=execution_depth,
    ), None


def run_gate_head(
    provider: str, argv: "Optional[list]", *, seams: GateSeams,
) -> "Tuple[Optional[int], Optional[GateRunState]]":
    """Phases 1-2 of a review gate: ``(exit_code, None)`` when it stops, ``(None, state)`` otherwise.

    The script module owns the entry point and the seam bindings; this body owns every decision
    between argv and the tail. A provider difference is read off the register row, so this function
    carries no ``if provider == ...`` branch.
    """
    row = _row(provider)
    args = _parse_gate_args(row, argv)
    if args is None:
        return 1, None
    base_data_dir = seams.resolve_data_dir(args.data_dir or None)
    pr_number = int(args.pr) if str(args.pr).isdigit() else None
    dispatch_id = args.reprocess if args.reprocess else f"{row.dispatch_id_prefix}{args.pr}-{int(time.time())}"
    # Offline runs read the diff from a file and are NOT tied to a live GitHub PR — the branch and
    # commit_sha are resolved once here and reused for the request record and the result record.
    test_run = bool(args.diff_file)
    branch = "" if test_run else seams.get_pr_head_branch(pr_number)
    commit_sha = "" if test_run else seams.get_pr_head_sha(pr_number)
    requests_dir = base_data_dir / "state" / "review_gates" / "requests"
    rc = _write_gate_request(
        row, args, requests_dir=requests_dir,
        report_path=str(base_data_dir / "unified_reports" / f"{dispatch_id}.md"),
        pr_number=pr_number, dispatch_id=dispatch_id, branch=branch, commit_sha=commit_sha,
        test_run=test_run, seams=seams,
    )
    if rc is not None:
        return rc, None
    build = _reprocess_inputs if args.reprocess else _live_inputs
    inputs, rc = build(provider, row, args, base_data_dir, dispatch_id, branch, seams)
    if inputs is None:
        return rc, None
    return None, GateRunState(
        provider=provider, row=row, args=args, base_data_dir=base_data_dir, pr_number=pr_number,
        dispatch_id=dispatch_id, branch=branch, commit_sha=commit_sha, requests_dir=requests_dir,
        report_path_obj=inputs.report_path_obj, report_text=inputs.report_text,
        dispatch_error=inputs.dispatch_error, duration=inputs.duration,
        window_start=inputs.window_start, window_end=inputs.window_end, prompt=inputs.prompt,
        scan_findings=inputs.scan_findings, execution_depth=inputs.execution_depth, seams=seams,
    )


@dataclass(frozen=True)
class VerdictFacts:
    """Phase 3's outcome: the record fields derived from one run's report."""

    verdict: dict
    status: str
    blocking: list
    residual: str
    reason: str
    recovered_path: "Optional[Path]" = None
    lifted_reason: "Optional[str]" = None


def _facts_for(state: GateRunState, verdict: dict, text: str, reason: str) -> VerdictFacts:
    status, blocking, residual = verdict_to_status(state.provider, verdict, text)
    return VerdictFacts(verdict, status, blocking, residual, reason)


def _unread_or_verdict(state: GateRunState) -> VerdictFacts:
    """Phase 3a: a raised dispatch or a deadline-killed report is never read for a verdict."""
    unread = unread_run_outcome(state.provider, state.dispatch_error, state.report_text or "", state.duration)
    if unread is not None:
        status, blocking, residual, reason = unread
        return VerdictFacts({}, status, blocking, residual, reason)
    return _verdict_from_report(state)


def _verdict_from_report(state: GateRunState) -> VerdictFacts:
    """Phase 3b: this gate's own fence, then the relabeled fence, then the report frontmatter."""
    text = state.report_text or ""
    verdict = extract_verdict(text, state.provider)
    if verdict:
        return _facts_for(state, verdict, text, "verdict")
    if not text.strip():
        return _facts_for(state, {}, text, "no_verdict")
    relabeled = extract_relabeled_verdict(text)
    if relabeled:
        return _facts_for(state, relabeled, text, "relabeled_verdict")
    run_failed, exit_code, output_tokens = frontmatter_run_outcome(text)
    if run_failed is True:
        return _run_failed_facts(state, exit_code, output_tokens)
    return _parse_miss_facts(state)


def _run_failed_facts(state: GateRunState, exit_code: object, output_tokens: object) -> VerdictFacts:
    """Phase 3c: the report's own frontmatter stamps the provider run as failed — an outage, not a review
    outcome. The cause is lifted off the lane log, then the row's own report reader; never invented."""
    text = state.report_text or ""
    detail = (
        f"{state.provider}'s own report frontmatter stamps this run as failed "
        f"(exit_code={exit_code!r}, token_usage.output={output_tokens!r}) — "
        "provider-side outage, not a review outcome"
    )
    lifted = lift_lane_log_reason(state.dispatch_id, state.base_data_dir)
    if lifted is None and state.row.report_failure_lift is not None:
        lifted = state.row.report_failure_lift(text)
    if lifted:
        detail += f" — {state.row.lift_detail_label}: {lifted}"
    status, blocking, residual = verdict_to_status(
        state.provider, {}, text, provider_failed_detail=detail,
    )
    return VerdictFacts({}, status, blocking, residual, "dispatch_error", lifted_reason=lifted)


def _parse_miss_facts(state: GateRunState) -> VerdictFacts:
    """Phase 3d: no readable verdict — the bounded companion-report search, refusing rather than guessing."""
    text = state.report_text or ""
    try:
        candidate = state.seams.find_recovery_candidate(
            state.base_data_dir / "unified_reports",
            pr_id=str(state.args.pr),
            exclude_name=f"{state.dispatch_id}.md",
            window_start=state.window_start,
            window_end=state.window_end,
        )
    except AmbiguousRecoveryCandidates as exc:
        return VerdictFacts({}, "unavailable", [], (
            f"{state.provider} returned a {len(text)}-char report with no readable verdict block; "
            f"{len(exc.candidates)} companion reports were found for PR {state.args.pr} in the run's "
            f"time window — refusing to guess which is real: {', '.join(str(p) for p in exc.candidates)}"
        ), "recovery_ambiguous")
    if candidate is None:
        return VerdictFacts({}, "unavailable", [], (
            f"{state.provider} returned a {len(text)}-char report with no readable ```json``` verdict "
            f"block; searched unified_reports/ for a companion report for PR {state.args.pr} in the "
            "run's time window and found none (parse miss — no recoverable evidence)"
        ), "recovery_empty")
    conflict = recovered_verdict_conflicts(text, candidate.verdict)
    if conflict:
        return VerdictFacts({}, "unavailable", [], (
            f"a companion report ({candidate.path}) carried a parseable verdict, but it conflicts "
            f"with the primary response: {conflict} — abstaining rather than overriding the primary "
            "run with a second source"
        ), "recovery_conflict")
    status, blocking, recovered_residual = verdict_to_status(state.provider, candidate.verdict)
    residual = (
        f"{recovered_residual + ' — ' if recovered_residual else ''}"
        f"verdict recovered from companion report {candidate.path} "
        "(same dispatch run, found via bounded search; the harness-captured primary report lacked the fence)"
    )
    return VerdictFacts(candidate.verdict, status, blocking, residual, "recovered_verdict", recovered_path=candidate.path)


def _reprocess_identity_facts(state: GateRunState, facts: VerdictFacts) -> VerdictFacts:
    """Phase 4a: formalized only against the commit_sha the PR carried WHEN this dispatch ran, never a guess."""
    if not (state.args.reprocess and facts.status in {"pass", "fail"}):
        return facts
    original_sha = reprocess_original_commit_sha(
        state.provider, state.base_data_dir, state.args.pr, state.dispatch_id,
    )
    record_name = f"pr-{state.args.pr}-{state.row.gate}.json"
    if original_sha is None:
        return replace(facts, status="unavailable", blocking=[], reason="reprocess_no_identity_anchor", residual=(
            "--reprocess found no addressable original result record for "
            f"dispatch_id={state.dispatch_id!r} ({record_name} is missing, or now reflects a LATER "
            "dispatch for this PR — results are keyed by PR number, not dispatch_id) — cannot verify "
            "the commit_sha this run evaluated against, refusing rather than guessing"
        ))
    current_sha = state.seams.get_pr_head_sha(int(state.args.pr)) if str(state.args.pr).isdigit() else ""
    if not current_sha or current_sha != original_sha:
        return replace(facts, status="unavailable", blocking=[], reason="reprocess_stale_evidence", residual=(
            f"--reprocess: PR {state.args.pr} head has moved since this dispatch ran "
            f"(evaluated against commit_sha={original_sha!r}, current head is {current_sha!r}) — "
            "the recovered evidence is stale, refusing to formalize it"
        ))
    return facts


def _evidence_pair(state: GateRunState, facts: VerdictFacts) -> "Tuple[str, str]":
    """Phase 4b: OI-1178's evidence pair — hash + report_path for a verdict, empty hash otherwise."""
    if facts.status not in {"pass", "fail"}:
        return "", str(state.report_path_obj)
    report_path = str(facts.recovered_path) if facts.recovered_path is not None else str(state.report_path_obj)
    if state.prompt is not None:
        return _compute_contract_hash({"prompt": state.prompt}, state.row.gate), report_path
    branch = state.seams.get_pr_head_branch(int(state.args.pr)) if str(state.args.pr).isdigit() else ""
    return _compute_contract_hash({"branch": branch}, state.row.gate), report_path


def _build_record(state: GateRunState, facts: VerdictFacts, contract_hash: str, report_path: str) -> dict:
    """Phase 4c: the result record — one shape for both gates, names taken from the register row."""
    return {
        "gate": state.row.gate,
        "pr_id": str(state.args.pr),
        "pr_number": state.pr_number,
        "test_run": bool(state.args.diff_file),
        "status": facts.status,
        "reason": facts.reason,
        "duration_seconds": round(state.duration, 3),
        "summary": status_summary(state.provider, facts.status, facts.blocking, facts.reason),
        "contract_hash": contract_hash,
        "report_path": report_path,
        "report_path_informational": str(state.report_path_obj),
        "provider": state.row.lane_provider,
        "model": state.args.model,
        "dispatch_id": state.dispatch_id,
        "blocking_findings": facts.blocking,
        "advisory_findings": merge_scan_findings(
            [f for f in (facts.verdict.get("findings") or []) if f not in facts.blocking],
            state.scan_findings,
        ),
        "required_reruns": [],
        "residual_risk": facts.residual,
        "failure_reason": facts.lifted_reason or "",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "evidence_source": "reprocessed" if state.args.reprocess else "live",
    }


def _stamp_and_persist(state: GateRunState, record: dict) -> "Optional[int]":
    """Phase 5: stamp identity from the FRESH request record (B8), write through the recorder; returns a
    failure exit code or None (the recorder may downgrade ``record`` in place, which phase 6 reads)."""
    request_file = state.seams.result_file_path(
        state.requests_dir, state.row.gate, pr_number=state.pr_number, pr_id="",
    )
    disk_request_payload: "Optional[dict]" = None
    if request_file is not None and request_file.exists():
        try:
            loaded = json.loads(request_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            disk_request_payload = loaded
    if disk_request_payload is None:
        disk_request_payload = {"gate": record["gate"], "pr_id": record["pr_id"]}
    disk_request_payload["branch"] = state.branch
    disk_request_payload["commit_sha"] = state.commit_sha
    state.seams.stamp_request_identity(record, disk_request_payload)
    results_dir = state.base_data_dir / "state" / "review_gates" / "results"
    out = results_dir / f"pr-{state.args.pr}-{state.row.gate}.json"
    try:
        state.seams.record_terminal_result(
            gate=state.row.gate, pr_id=record["pr_id"], result_path=out, payload=record,
            execution_depth=state.execution_depth,
        )
    except (OSError, ValueError) as exc:
        # A gate that cannot persist its own evidence must fail LOUDLY, never exit 0 with nothing on disk.
        print(f"{state.row.gate}: FAILED to write result record to {out}: {exc}", file=sys.stderr)
        return 1
    print(f"{state.row.gate}: wrote {out}", file=sys.stderr)
    return None


def _emit_outcome(state: GateRunState, record: dict) -> int:
    """Phase 6: print the record (or verdict), return the CLI's exit code: 0 pass, 2 fail/blocked, 1 else."""
    status = record.get("status")
    reason = record.get("reason", "")
    residual = record.get("residual_risk", "")
    blocking = record.get("blocking_findings", [])
    if state.args.json:
        print(json.dumps(record, indent=2))
    elif status == "unavailable":
        print(f"VERDICT: UNAVAILABLE  ({reason})")
        print(f"  {residual}")
    elif status == "partial_review":
        print(f"VERDICT: PARTIAL_REVIEW  ({reason})")
        print(f"  {record.get('reason_detail', '')}")
    else:
        print(f"VERDICT: {str(status).upper()}  ({len(blocking)} blocking)")
        for finding in blocking:
            print(f"  · [{finding.get('severity')}] {finding.get('message')}")
    if status == "pass":
        return 0
    return 2 if status == "fail" else 1


def _book_gate_cost(state: GateRunState) -> None:
    """Book this gate run's own cost-ledger line (K2b) for a lane that reports its own usage.

    kimi's gate ends here; the seat's report is the only usage report the lane
    gives, and one not stamped ``token_usage_measured: true`` books
    ``unavailable:kimi_cli_no_usage``, never a zero. Booking is fail-open
    (``record_cli_run`` logs the refusal) and this function never raises: a
    verdict is never the ledger's to fail. A ``--reprocess`` run calls no model
    and a row with no ``cost_provider`` (glm, until K3) books nothing either.
    """
    provider = state.row.cost_provider
    if provider is None or getattr(state.args, "reprocess", None):
        return
    ended = datetime.fromtimestamp(state.window_end, tz=timezone.utc)
    started = datetime.fromtimestamp(state.window_start, tz=timezone.utc) if state.window_start else ended
    run = harness_run_cost.cli_run(
        provider=provider, model=getattr(state.args, "model", "") or "", started=started, ended=ended,
        report_text=state.report_text or "",
    )
    harness_run_cost.record_cli_run(
        run, project_id=harness_run_cost.ledger_project_id(state.base_data_dir / "state"),
        producer=state.row.gate, ref=state.dispatch_id,
        # The store THIS gate resolved, not whatever the ambient env names: an
        # offline run (--data-dir) books into the store whose report it read.
        ledger_path=provider_costs.costs_path_in(state.base_data_dir),
    )


def run_gate_tail(state: GateRunState) -> int:
    """Phases 3-6 of a review gate: verdict, reprocess identity, evidence pair, record, output."""
    _book_gate_cost(state)
    facts = _reprocess_identity_facts(state, _unread_or_verdict(state))
    contract_hash, report_path = _evidence_pair(state, facts)
    record = _build_record(state, facts, contract_hash, report_path)
    rc = _stamp_and_persist(state, record)
    if rc is not None:
        return rc
    return _emit_outcome(state, record)
