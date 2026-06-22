#!/usr/bin/env python3
"""plan_gate_panel.py — the PM-skill plan-first gate (governed worker path).

A feature's PLAN (not its code) is reviewed by a diverse-family panel BEFORE any
implementation. This module runs that panel:

    plan doc + rubric  ->  N panelist lanes (opus / kimi / glm-5.2-harness)
                            each via provider_dispatch (governed: report -> receipt)
                       ->  parse each panelist's structured verdict
                       ->  apply the panel pass/fail rule (PM-SKILL)
                       ->  PASS | REVISE | BLOCK

The caller (``planning_cli plan-gate run``) resolves the ``OI-PLAN-<track>``
blocker on PASS, which — via ``track_reconciler`` — flips the track's
``derived_status`` away from ``blocked`` and lets ``deliverable promote`` proceed.

Panel composition (PM-SKILL "always multi-model"): Opus + Kimi + GLM-5.2-harness,
three families so real disagreement surfaces. DeepSeek (own-key) is a legal third
but stays off the default panel; Codex is reserved for security/schema/governance
plans, never a default panelist.

Every lane routes through ``provider_dispatch.py``, so the provider constraints are
enforced by construction (kimi-via-cli-only, zai-via-openrouter-only,
no-anthropic-sdk) and each panelist emits a governed report -> receipt: the gate
that gates everything is itself in the audit trail.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROVIDER_DISPATCH = HERE / "provider_dispatch.py"
TMUX_INTERACTIVE_DISPATCH = HERE / "tmux_interactive_dispatch.py"

# Claude is NOT a provider-lane provider — provider_dispatch refuses it. Claude lanes
# route via the TMUX-SPAWN lane (interactive `claude` in an ephemeral isolated worktree),
# which keeps billing on the SUBSCRIPTION (CLAUDE.md "June-15 escape"). They must NOT use
# headless `claude -p`: post-cutover that bills API credits.
_CLAUDE_PROVIDERS = {"claude"}

# Default diverse-family panel: (label, provider string, model_arg).
DEFAULT_PANEL: List[Dict[str, str]] = [
    {"label": "opus", "provider": "claude", "model_arg": "opus"},
    {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k2-7-code"},
    {"label": "glm-5.2-harness", "provider": "glm-harness", "model_arg": "glm-5.2"},
]

VERDICT_FENCE = "vnx-plan-verdict"
_VALID_VERDICTS = {"pass", "revise", "block"}

# A dispatcher takes (provider, model_arg, instruction, dispatch_id) and returns the
# panelist's report text. Injectable so the panel logic is testable without a live model.
DispatcherFn = Callable[[str, str, str, str], str]

_VERDICT_CONTRACT = (
    "When your review is done, append EXACTLY ONE fenced block and nothing after it:\n"
    f"```{VERDICT_FENCE}\n"
    "{\n"
    '  "verdict": "pass" | "revise" | "block",\n'
    '  "blocking_findings": ["short concrete issue", "..."],\n'
    '  "rationale": "one or two sentences"\n'
    "}\n"
    "```\n"
    "verdict=block: a fundamental flaw makes the plan unsafe to build as written.\n"
    "verdict=revise: real, fixable gaps remain but the approach is salvageable.\n"
    "verdict=pass: the plan is sound enough to implement.\n"
)


# The plan doc is untrusted input inlined into each panelist's instruction. Two guards:
#  - a doc must not be able to inject its own verdict fence (verdict spoofing);
#  - a doc must not blow argv past ARG_MAX when passed as --instruction.
MAX_DOC_CHARS = 60000


def _sanitize_doc(doc_text: str) -> str:
    # Neutralize any embedded verdict fence so a plan doc cannot spoof a PASS: a space
    # after the backticks breaks the exact ```vnx-plan-verdict opener parse_verdict matches.
    safe = doc_text.replace("```" + VERDICT_FENCE, "``` " + VERDICT_FENCE + " (neutralized)")
    if len(safe) > MAX_DOC_CHARS:
        safe = safe[:MAX_DOC_CHARS] + f"\n\n[... plan doc truncated at {MAX_DOC_CHARS} chars for the gate ...]"
    return safe


_RUBRIC = (
    "Judge the plan on:\n"
    "1. Problem: is the problem stated, and is it real?\n"
    "2. Approach: is it sound, or are there unaddressed failure modes?\n"
    "3. Deliverables: each scoped, independently shippable, task_class tagged?\n"
    "4. Risks: are the real risks named, each with a mitigation?\n"
    "5. Model-routing plan: a sane quality FLOOR per deliverable (not a hand-picked lane)?\n"
    "6. ADR-007: if it touches a central-DB table, does it carry a composite key over project_id?\n\n"
    "Be a skeptic. Surface concrete, fixable gaps. Do not rubber-stamp.\n"
)


def build_plan_review_instruction(doc_text: str, track_id: str) -> str:
    """Render the plan-review instruction handed to each panelist (inline-doc form).

    Used by provider lanes (kimi, glm) where the instruction is passed as a
    subprocess argument and the full inline doc is acceptable.  The claude/tmux
    lane uses ``build_plan_review_instruction_fileref`` instead so the ~50k-char
    doc body never inflates the instruction string.
    """
    doc_text = _sanitize_doc(doc_text)
    return (
        f"You are an independent plan reviewer for track {track_id}. Review the "
        "IMPLEMENTATION PLAN below. The plan only — no code exists yet.\n\n"
        + _RUBRIC
        + "\n----- PLAN UNDER REVIEW -----\n"
        f"{doc_text}\n"
        "----- END PLAN -----\n\n"
        + _VERDICT_CONTRACT
    )


def build_plan_review_instruction_fileref(
    doc_path: str, track_id: str, report_path: str
) -> str:
    """Render the plan-review instruction for the claude/tmux lane.

    The plan doc is passed by FILE REFERENCE (not inlined) so the instruction
    string stays short — avoiding the >120s bracketed-paste ingestion that trips
    the WORK_START_GATE timeout on a large doc.

    ``report_path``: the absolute path where the worker MUST write its report.
    This makes the expectation explicit so the worker does not have to guess the
    unified_reports location, and govern() can find the authored file.
    """
    return (
        f"You are an independent plan reviewer for track {track_id}.\n\n"
        f"Read the IMPLEMENTATION PLAN from this file:\n\n"
        f"  {doc_path}\n\n"
        "Review the plan only — no code exists yet.\n\n"
        + _RUBRIC
        + "\n"
        + _VERDICT_CONTRACT
        + f"\n\nREPORT FILE (MANDATORY): Write your complete review — including the "
        f"```{VERDICT_FENCE}``` block at the end — to this exact file path:\n\n"
        f"  {report_path}\n\n"
        "Do NOT write to any other path. The panel reads only that file. "
        "Your review is not recorded unless it lands there with the verdict fence intact."
    )


def parse_verdict(report_text: str) -> Dict[str, Any]:
    """Extract the LAST ``vnx-plan-verdict`` block from a panelist report.

    Fail-safe by design: anything unparseable becomes ``revise`` with
    ``parse_error=True`` so a missing/garbled verdict can never silently PASS.
    """
    empty = {"verdict": "revise", "blocking_findings": [], "rationale": "", "parse_error": True}
    if not report_text:
        return {**empty, "rationale": "empty report"}
    pattern = re.compile(r"```" + re.escape(VERDICT_FENCE) + r"\s*\n(.*?)```", re.DOTALL)
    matches = pattern.findall(report_text)
    if not matches:
        return {**empty, "rationale": "no verdict block found"}
    try:
        data = json.loads(matches[-1].strip())
    except (json.JSONDecodeError, ValueError):
        return {**empty, "rationale": "verdict block is not valid JSON"}
    if not isinstance(data, dict):
        return {**empty, "rationale": "verdict block is not a JSON object"}
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        return {**empty, "rationale": f"unknown verdict {verdict!r}"}
    findings = data.get("blocking_findings") or []
    if not isinstance(findings, list):
        findings = [str(findings)]
    return {
        "verdict": verdict,
        "blocking_findings": [str(x) for x in findings],
        "rationale": str(data.get("rationale", "")),
        "parse_error": False,
    }


@dataclass
class PanelistResult:
    label: str
    provider: str
    verdict: str = "revise"          # pass | revise | block
    blocking_findings: List[str] = field(default_factory=list)
    rationale: str = ""
    report_path: str = ""
    dispatched: bool = False         # did the dispatch + report read succeed
    parse_error: bool = False
    error: str = ""


def _decision(decision: str, block: int, revise: int, passes: int, rationale: str) -> Dict[str, Any]:
    return {
        "decision": decision,
        "block_count": block,
        "revise_count": revise,
        "pass_count": passes,
        "rationale": rationale,
    }


def apply_panel_rule(results: List[PanelistResult]) -> Dict[str, Any]:
    """The PM-SKILL pass/fail rule.

    - any infra failure (a panelist returned no verdict) -> REVISE (cannot certify
      PASS with a silent missing voice)
    - any BLOCK -> REVISE (revise the blocking sections, re-run the delta)
    - >= 2 REVISE -> REVISE (one revise round)
    - <= 1 REVISE and no BLOCK -> PASS (fold the lone dissent in as a tracked note)

    Parse errors already map to ``revise`` (see ``parse_verdict``), so a garbled
    verdict counts against PASS rather than being ignored.
    """
    if not results:
        # An empty panel must never fall through to PASS (misconfigured panel=[]).
        return _decision("REVISE", 0, 0, 0, "no panelists ran — empty panel, cannot certify")
    block = sum(1 for r in results if r.verdict == "block")
    revise = sum(1 for r in results if r.verdict == "revise")
    passes = sum(1 for r in results if r.verdict == "pass")
    # No readable verdict = no signal from that panelist: either the dispatch
    # failed (infra) or the report had no parseable verdict block. Either way it
    # must block PASS — folding an unreadable voice into PASS is fail-open.
    no_verdict = [r for r in results if not r.dispatched or r.parse_error]

    if no_verdict:
        return _decision(
            "REVISE", block, revise, passes,
            f"{len(no_verdict)} panelist(s) returned no readable verdict: "
            + ", ".join(r.label for r in no_verdict),
        )
    if block >= 1:
        return _decision(
            "REVISE", block, revise, passes,
            f"{block} BLOCK verdict(s) — revise the blocking sections, re-run the delta only",
        )
    if revise >= 2:
        return _decision(
            "REVISE", block, revise, passes,
            f"{revise} REVISE verdicts — one revise round",
        )
    # <=1 REVISE, no BLOCK. PASS only if the passing voices OUTNUMBER the dissent —
    # otherwise the "lone dissent" is not lone (a degenerate 1-/2-member panel, or a
    # 1-1 tie). This keeps the canonical 3-member result (2 pass + 1 revise -> PASS)
    # while closing the fail-open a 1-member smoke surfaced: a single REVISE must not
    # fold to PASS. (SKILL: "tie -> safety-first REVISE".)
    if passes > revise:
        dissent = [r.label for r in results if r.verdict != "pass"]
        note = f"folded dissent (tracked): {', '.join(dissent)}" if dissent else "unanimous pass"
        return _decision("PASS", block, revise, passes, note)
    return _decision(
        "REVISE", block, revise, passes,
        "no passing majority — the dissent is not outnumbered",
    )


def _read_report(base: Optional[Path], dispatch_id: str, stderr: str) -> Optional[str]:
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
) -> DispatcherFn:
    """Real dispatcher: run a panelist through its governed lane, return the report text.

    INTERIM — PR-12 consolidation target. This calls the lane scripts DIRECTLY, which is a
    side door. Once the single-entry dispatch door (`vnx dispatch` / dispatch_bridge) is
    wired and flipped, this MUST route through that one door instead. The split below is
    exactly what the door decides internally:
      claude            -> tmux-spawn lane (interactive claude, ephemeral worktree;
                           billing stays on the SUBSCRIPTION per the June-15 escape).
                           NOT provider_dispatch (refuses claude), NOT headless `claude -p`
                           (bills API credits post-cutover).
      kimi/glm/deepseek -> provider_dispatch (constraint-safe per provider).
    """
    base = Path(data_dir) if data_dir else None

    def _dispatch(provider: str, model_arg: str, instruction: str, dispatch_id: str) -> str:
        env = dict(os.environ)
        _tmp_doc_path: Optional[str] = None
        try:
            if provider in _CLAUDE_PROVIDERS:
                # BUG-2 FIX (file-ref): the instruction already has the full plan doc inlined
                # by run_panel's build_plan_review_instruction call. For the claude/tmux lane
                # we replace it with a compact file-ref instruction so the ~50k-char body never
                # inflates the bracketed-paste and does not trip the WORK_START_GATE timeout.
                #
                # We write the original inline instruction to a temp file so the worker can
                # read the plan + rubric + verdict contract from a stable on-disk path.
                # The expected report path is derived from data_dir so the file-ref instruction
                # can tell the worker exactly where to write its verdict report.
                report_path_str: str
                if base is not None:
                    report_path_str = str(base / "unified_reports" / f"{dispatch_id}.md")
                else:
                    # Fallback: derive from VNX_DATA_DIR or a tmp path
                    report_path_str = str(
                        Path(os.environ.get("VNX_DATA_DIR", tempfile.gettempdir()))
                        / "unified_reports" / f"{dispatch_id}.md"
                    )

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
                # No 50k doc body — the worker reads it from _tmp_doc_path.
                claude_instruction = build_plan_review_instruction_fileref(
                    doc_path=_tmp_doc_path,
                    track_id="<see file>",
                    report_path=report_path_str,
                )

                cmd = [
                    sys.executable, str(TMUX_INTERACTIVE_DISPATCH),
                    "--dispatch-id", dispatch_id,
                    "--model", model_arg,
                    "--role", "plan-reviewer",
                    "--instruction", claude_instruction,
                    "--deadline-seconds", str(timeout_seconds),
                    # A plan review is READ-ONLY (reads the doc file, writes a verdict report) —
                    # it needs no isolated worktree. --shared-worktree skips the expensive
                    # `git worktree add`, which on a large repo (e.g. SEOcrawler) blows the
                    # deadline and times opus out; it also grounds the review against the REAL
                    # checkout.
                    "--shared-worktree",
                    "--allow-unstaged",
                    # D2.2: a plan-review is working-tree-only — it reads the doc and
                    # writes a verdict report; it must NOT commit/push (OI-097). The
                    # flag denies git commit/push at the tool-permission layer.
                    "--working-tree-only",
                    "--reason", f"plan-gate panel {dispatch_id}",
                ]
                run_timeout = timeout_seconds + 180  # tmux warmup + teardown headroom
            else:
                claude_instruction = instruction  # provider lane: inline doc OK
                cmd = [
                    sys.executable, str(PROVIDER_DISPATCH),
                    "--provider", provider,
                    "--terminal-id", "plan-gate",
                    "--dispatch-id", dispatch_id,
                    "--model", model_arg,
                    "--role", "plan-reviewer",
                    "--instruction", instruction,
                    "--no-auto-commit",
                ]
                run_timeout = timeout_seconds
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=run_timeout, check=False, env=env,
            )
            report = _read_report(base, dispatch_id, proc.stderr)
            if report is None:
                raise RuntimeError(
                    f"no report for {dispatch_id} (rc={proc.returncode}): "
                    f"{(proc.stderr or '')[-400:]}"
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


def run_panel(
    doc_path: str | Path,
    *,
    track_id: str,
    project_id: str = "vnx-dev",
    panel: Optional[List[Dict[str, str]]] = None,
    dispatcher: Optional[DispatcherFn] = None,
    data_dir: Optional[str] = None,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    """Run the plan-first panel over ``doc_path`` and return the verdict.

    ``dispatcher`` is injectable; when omitted the governed provider_dispatch
    dispatcher is used. Returns a dict with the overall ``decision``
    (PASS|REVISE|BLOCK), the rule ``summary``, and per-panelist detail.
    """
    panel = panel or DEFAULT_PANEL
    dispatcher = dispatcher or _make_default_dispatcher(data_dir, timeout_seconds)
    doc_text = Path(doc_path).read_text(encoding="utf-8")
    instruction = build_plan_review_instruction(doc_text, track_id)

    results: List[PanelistResult] = []
    for member in panel:
        did = f"plan-gate-{track_id}-{member['label']}-{uuid.uuid4().hex[:8]}"
        try:
            report_text = dispatcher(member["provider"], member["model_arg"], instruction, did)
        except Exception as exc:  # dispatch / report-read failure -> no verdict
            results.append(PanelistResult(
                label=member["label"], provider=member["provider"],
                dispatched=False, error=str(exc), report_path=did,
            ))
            continue
        parsed = parse_verdict(report_text)
        results.append(PanelistResult(
            label=member["label"], provider=member["provider"],
            verdict=parsed["verdict"], blocking_findings=parsed["blocking_findings"],
            rationale=parsed["rationale"], parse_error=parsed["parse_error"],
            dispatched=True, report_path=did,
        ))

    summary = apply_panel_rule(results)
    return {
        "track_id": track_id,
        "project_id": project_id,
        "decision": summary["decision"],
        "summary": summary,
        "panelists": [r.__dict__ for r in results],
    }
