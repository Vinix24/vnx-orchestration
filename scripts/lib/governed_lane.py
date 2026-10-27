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

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import gate_depth  # after the scripts/lib path guard
from gate_lane_contract import (
    VERDICT_CONTRACT,
    canonical_review_verdict,
    harness_dispatcher_timeouts,
    lane_deadline_detail,
    reviews_with_tools,
)
from gate_prompt import build_review_prompt
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
class ProviderRow:
    """One provider's row of the register.

    ``gate`` is the gate name the shared helpers hand to
    ``gate_lane_contract`` (``harness_dispatcher_timeouts``,
    ``reviews_with_tools``) and the slot/file stem ``--reprocess`` reads
    (``pr-<N>-<gate>.json``). It differs per provider, so it lives here as a
    register field rather than as an ``if provider == ...`` branch in a helper.
    """

    parser: Callable[[str], dict]
    gate: str
    origin: str


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
    ),
    "kimi": ProviderRow(
        parser=_last_json_verdict, gate="kimi_gate", origin="scripts/kimi_gate.py:153 (pre-GE-5a)",
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
