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

# Full diverse-family assurance panel: (label, provider string, model_arg).
# One panelist per provider family (Anthropic / Moonshot / Zhipu / DeepSeek / OpenAI) so a
# plan is reviewed from five independent vantage points before any code is written.
# NOTE: a panelist that flakes (a down proxy, an uninstalled CLI, or an unparseable verdict) is
# RETRIED once (VNX_PANEL_RETRY, see run_panel) and, if it still yields no readable verdict,
# ABSTAINS as a non-scoring lane instead of vetoing — so a single down lane no longer forces a
# REVISE (apply_panel_rule's liveness quorum). Keep every provider here runnable anyway; the
# retry only rescues transient flakes. glm-harness requires the local litellm proxy on :4141.
DEFAULT_PANEL: List[Dict[str, str]] = [
    {"label": "opus", "provider": "claude", "model_arg": "opus"},
    {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k2-7-code"},
    {"label": "glm-5.2-harness", "provider": "glm-harness", "model_arg": "glm-5.2"},
    {"label": "deepseek", "provider": "deepseek-harness", "model_arg": "deepseek-v4-pro"},
    {"label": "codex", "provider": "codex", "model_arg": "gpt-5.5"},
    # gemini is intentionally omitted until the `gemini` CLI is installed: an unrunnable
    # panelist emits no verdict, which the fail-safe rule turns into an unconditional REVISE.
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


_CONTRACT_FENCE_RE = re.compile(r"```" + re.escape(VERDICT_FENCE) + r"\s*\n(.*?)```", re.DOTALL)
# Any code fence (```json ...```, ``` ...```, ```JSON5 ...```) — the degraded-recovery channel
# for a panelist that wrapped its verdict in the wrong fence (the codex/glm verdict-JSON flake).
_CODE_FENCE_RE = re.compile(r"```[A-Za-z0-9_.+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def _loads_json(candidate: str) -> Any:
    """``json.loads`` with ONE conservative repair pass (strip trailing commas before a
    closing brace/bracket — the single most common LLM JSON tic). Returns the parsed value
    or ``None`` when it still will not parse. The repair only removes a dangling comma; it can
    never quote an unquoted key or invent a value, so genuine garbage (``{verdict: pass,,,}``)
    stays garbage and still abstains."""
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    if repaired != candidate:
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _find_balanced_objects(text: str) -> List[str]:
    """Every balanced ``{...}`` substring in ``text``, in order of appearance.

    String- and escape-aware, so braces inside JSON string values do not skew the depth
    count. Used to recover a verdict object embedded in prose or wrapped in a non-contract
    code fence — the ``glm``/``codex`` case where the model emits the right JSON but not the
    exact ``vnx-plan-verdict`` fence."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, j = 0, False, False, i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[i:j + 1])
                    break
            j += 1
        i = j + 1 if j < n else n
    return out


def _verdict_obj_in(chunk: str) -> Optional[Dict[str, Any]]:
    """The verdict OBJECT (a dict carrying a ``verdict`` key) inside ``chunk``, whether the
    chunk is clean JSON, JSON wrapped in prose, or JSON in a nested code fence. ``None`` if
    the chunk carries no such object. Validity of the verdict VALUE is checked by the caller."""
    chunk = chunk.strip()
    if not chunk:
        return None
    data = _loads_json(chunk)
    if isinstance(data, dict) and "verdict" in data:
        return data
    for block in reversed(_CODE_FENCE_RE.findall(chunk)):
        data = _loads_json(block.strip())
        if isinstance(data, dict) and "verdict" in data:
            return data
    for cand in reversed(_find_balanced_objects(chunk)):
        data = _loads_json(cand)
        if isinstance(data, dict) and "verdict" in data:
            return data
    return None


def _locate_verdict_object(report_text: str) -> Optional[Dict[str, Any]]:
    """Locate the panelist's verdict object across three channels, most-trusted first.

    1. CONTRACT — the ``vnx-plan-verdict`` fence. When present it is AUTHORITATIVE and its
       LAST occurrence is the only one consulted (a doc's own fence is neutralized upstream by
       ``_sanitize_doc``, so this channel cannot be spoofed). If that fence carries no JSON
       object at all we abstain rather than fall through — the same fail-safe as before.
    2. CODE-FENCE — only when NO contract fence exists: a verdict wrapped in a ```json
       block (the codex/glm flake). Last valid object wins.
    3. PROSE — last resort: a balanced JSON object with a ``verdict`` key sitting in prose.

    Returns a dict that has a ``verdict`` key (its VALUE may still be invalid — the caller
    validates), or ``None`` when no verdict object can be recovered anywhere."""
    contract = _CONTRACT_FENCE_RE.findall(report_text)
    if contract:
        return _verdict_obj_in(contract[-1])
    for block in reversed(_CODE_FENCE_RE.findall(report_text)):
        obj = _verdict_obj_in(block)
        if obj is not None:
            return obj
    for cand in reversed(_find_balanced_objects(report_text)):
        data = _loads_json(cand)
        if isinstance(data, dict) and "verdict" in data:
            return data
    return None


def parse_verdict(report_text: str) -> Dict[str, Any]:
    """Extract the panelist's ``vnx-plan-verdict`` from a report.

    Robust by design (2026-07-11): the contract fence is honoured first, but a verdict wrapped
    in a ```json fence or embedded in prose is still recovered instead of abstaining — the
    codex/glm verdict-JSON flake that dropped those seats to non-scoring. See
    ``_locate_verdict_object`` for the channel precedence.

    Fail-safe is preserved: a genuinely empty or garbage output (no recoverable verdict object,
    or an object whose ``verdict`` value is not pass/revise/block) becomes ``revise`` with
    ``parse_error=True`` so a missing/garbled verdict can never silently PASS."""
    empty = {"verdict": "revise", "blocking_findings": [], "rationale": "", "parse_error": True}
    if not report_text or not report_text.strip():
        return {**empty, "rationale": "empty report"}
    data = _locate_verdict_object(report_text)
    if data is None:
        return {**empty, "rationale": "no verdict block found"}
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
    """The PM-SKILL pass/fail rule, with NON-SCORING lanes + a liveness quorum.

    A lane with no readable verdict (undispatched, or a report whose verdict block did not
    parse) is NON-SCORING: it ABSTAINS rather than vetoing. Rationale (2026-06-24): a
    structurally-broken or input-degraded lane (e.g. a model that won't emit the requested
    fence on a large doc) must not veto a substantive PASS from the readable lanes forever —
    that is a liveness hole, not safety. The non-scoring lanes are named in the rationale so
    the abstention is transparent, never silent.

    Over the SCORING (readable) lanes only:
    - quorum: require >= 2 readable verdicts to certify (a single voice can't fold to PASS).
    - any BLOCK -> REVISE.
    - >= 2 REVISE -> REVISE.
    - <= 1 REVISE and no BLOCK, with passes OUTnumbering the dissent -> PASS (the lone dissent
      folds as a tracked note); a tie is safety-first REVISE.
    """
    if not results:
        # An empty panel must never fall through to PASS (misconfigured panel=[]).
        return _decision("REVISE", 0, 0, 0, "no panelists ran — empty panel, cannot certify")
    # NON-SCORING: undispatched or parse_error lanes abstain (do not count toward the verdict).
    scoring = [r for r in results if r.dispatched and not r.parse_error]
    non_scoring = [r for r in results if not (r.dispatched and not r.parse_error)]
    ns_note = (
        f"; non-scoring (abstained): {', '.join(r.label for r in non_scoring)}"
        if non_scoring else ""
    )
    block = sum(1 for r in scoring if r.verdict == "block")
    revise = sum(1 for r in scoring if r.verdict == "revise")
    passes = sum(1 for r in scoring if r.verdict == "pass")

    # Liveness quorum: a multi-member panel must keep >= 2 readable voices to certify, so a
    # degraded 3-panel with only one readable lane can't pass on a single voice. A DELIBERATE
    # 1-member panel (a smoke) needs only its one voice. quorum = min(2, panel size).
    required = min(2, len(results))
    if len(scoring) < required:
        return _decision(
            "REVISE", block, revise, passes,
            f"only {len(scoring)} readable verdict(s) of {len(results)} — below quorum "
            f"({required}); cannot certify{ns_note}",
        )
    if block >= 1:
        return _decision(
            "REVISE", block, revise, passes,
            f"{block} BLOCK verdict(s) — revise the blocking sections, re-run the delta only{ns_note}",
        )
    if revise >= 2:
        return _decision(
            "REVISE", block, revise, passes,
            f"{revise} REVISE verdicts — one revise round{ns_note}",
        )
    if passes > revise:
        dissent = [r.label for r in scoring if r.verdict != "pass"]
        note = f"folded dissent (tracked): {', '.join(dissent)}" if dissent else "unanimous pass (scoring)"
        return _decision("PASS", block, revise, passes, note + ns_note)
    return _decision(
        "REVISE", block, revise, passes,
        f"no passing majority — the dissent is not outnumbered{ns_note}",
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


def _resolve_data_dir() -> str:
    """Resolve a real VNX data_dir for the claude/tmux lane's report round-trip.

    The claude lane writes its verdict report to ``<data_dir>/unified_reports/<id>.md`` but,
    unlike ``provider_dispatch``, prints NO ``Report:`` stderr line, so ``_read_report`` can
    only find it through the ``base`` fallback. When ``base`` is None the opus seat's report is
    written but never read back -> NO-VERDICT (rc1) — the #1102-class failure that drops opus
    from the panel. Resolving a real data_dir here keeps the write-path and read-path in
    agreement for EVERY caller (mirrors panel.py's ``_resolve_reports_dir().parent``, #1102)."""
    try:
        from vnx_paths import resolve_state_dir  # noqa: PLC0415
        return str(resolve_state_dir().parent)
    except Exception:
        return str(Path(os.environ.get("VNX_DATA_DIR") or (HERE.parent.parent / ".vnx-data")))


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
    # A REAL base is required for the claude/tmux lane: it writes its report to
    # <base>/unified_reports/<id>.md and _read_report reads it back from exactly there (the tmux
    # lane prints no `Report:` line to key off). With base=None the opus seat's report is written
    # but never found -> NO-VERDICT (rc1). Resolve a real data_dir so no caller can pass None and
    # silently drop the claude seat — the #1102-class fix, now guaranteed at the module level.
    base = Path(data_dir) if data_dir else Path(_resolve_data_dir())

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
                # The expected report path is derived from `base` (a real data_dir, resolved
                # above) so the write-path here and _read_report's read-path always agree.
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


def _panel_retry_count() -> int:
    """Extra attempts for a flaked panelist (``VNX_PANEL_RETRY``, default 1, clamped >= 0).

    A panelist whose verdict is unparseable (parse_error) or that failed to dispatch is a
    transient-flake suspect (the codex verdict-JSON flake, the glm parse flake). It is retried
    up to this many times before it falls through to the abstain/non-scoring path — recovering
    the flake without letting one down lane force a REVISE. A malformed value falls back to 1.
    """
    raw = os.environ.get("VNX_PANEL_RETRY", "").strip()
    if not raw:
        return 1
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


def _dispatch_one(
    dispatcher: DispatcherFn, member: Dict[str, str], instruction: str, dispatch_id: str,
) -> PanelistResult:
    """Dispatch ONE panelist once and parse its verdict into a ``PanelistResult``.

    Best-effort and non-raising: a dispatch / report-read failure returns a non-scoring result
    (``dispatched=False``, ``error`` set); a returned report whose verdict block does not parse
    returns ``parse_error=True``. Both outcomes are RETRYABLE by ``run_panel`` before they fall
    through to the abstain path — a retry that itself errors just degrades to the same abstain.
    """
    try:
        report_text = dispatcher(member["provider"], member["model_arg"], instruction, dispatch_id)
    except Exception as exc:  # dispatch / report-read failure -> no verdict
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            dispatched=False, error=str(exc), report_path=dispatch_id,
        )
    parsed = parse_verdict(report_text)
    return PanelistResult(
        label=member["label"], provider=member["provider"],
        verdict=parsed["verdict"], blocking_findings=parsed["blocking_findings"],
        rationale=parsed["rationale"], parse_error=parsed["parse_error"],
        dispatched=True, report_path=dispatch_id,
    )


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

    retries = _panel_retry_count()

    results: List[PanelistResult] = []
    for member in panel:
        # One dispatch, plus up to `retries` retries when the lane flakes (a dispatch failure
        # or an unparseable verdict). The first SCORING verdict wins and short-circuits; if a
        # retry also flakes we keep its (still non-scoring) result, which then abstains via
        # apply_panel_rule. Never more than `retries` extra attempts — each is a fresh governed
        # dispatch id so the retry lands its own report -> receipt.
        result: PanelistResult
        for _ in range(retries + 1):
            did = f"plan-gate-{track_id}-{member['label']}-{uuid.uuid4().hex[:8]}"
            result = _dispatch_one(dispatcher, member, instruction, did)
            if result.dispatched and not result.parse_error:
                break  # readable verdict — no retry needed
        results.append(result)

    summary = apply_panel_rule(results)
    return {
        "track_id": track_id,
        "project_id": project_id,
        "decision": summary["decision"],
        "summary": summary,
        "panelists": [r.__dict__ for r in results],
    }
