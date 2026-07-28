"""Multi-provider deliberation panel — a fabric capability for COMPLEX, multi-view questions.

`vnx panel <mode> "<question>"`. Unlike a flat fan-out, each stage builds on the previous, so
the panel actually deliberates instead of just polling:

  1. DIVERGE   — every provider gets the SAME question through a DIFFERENT mode-specific lens.
  2. CONTRARIAN — one designated seat red-teams the emerging consensus: what is everyone missing?
  3. VERIFY    — the top claims are adversarially checked (against the CODE for sweeps, against
                 SOURCES for research) — the /deep-research adversarial-verify pattern.
  4. SYNTHESIS — one cited report: consensus + surviving dissent + verified/refuted claims,
                 ranked and deduped, with file:line / source references.

Generalises `plan_gate_panel` (plan-review) to arbitrary questions and reuses its governed
review-lane dispatcher. Respects the provider constraints (kimi-via-cli-only, zai-via-
openrouter-only, deepseek-harness own-key, no-anthropic-sdk) — the dispatcher routes each
provider string through its correct lane.
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A dispatcher runs one panelist and returns its report text.
DispatcherFn = Callable[[str, str, str, str], str]

# Default panel roster (provider, model). Each entry routes through its own governed lane.
# deepseek-harness is optional (needs its own key + hardening) — degrades gracefully if absent.
DEFAULT_ROSTER: List[Tuple[str, str]] = [
    ("codex", "gpt-5.5"),
    ("kimi", "kimi-k3"),
    ("claude", "sonnet"),
    ("glm-harness", "glm-5.2"),
    ("deepseek-harness", "deepseek-v4-pro"),
]


@dataclass
class ModeSpec:
    key: str
    description: str
    lenses: List[str]          # one angle per roster slot (cycled if fewer than roster)
    contrarian_focus: str      # what the red-team seat should attack
    verify_target: str         # "the code (cite file:line)" | "the sources (try to refute)"
    synth_goal: str            # what the final report must deliver


MODES: Dict[str, ModeSpec] = {
    "sweep": ModeSpec(
        key="sweep",
        description="codebase sweep — security / correctness / dead-code / refactor",
        lenses=[
            "security vulnerabilities and unsafe patterns",
            "correctness bugs and edge cases",
            "dead / unreachable code and unused surface",
            "refactor + simplification opportunities",
            "performance and resource hotspots",
        ],
        contrarian_focus="the panel's severity ranking and any 'this is fine' conclusions — "
                         "which flagged issue is actually a non-issue, and which UNflagged area is the real risk",
        verify_target="the code — read each cited file:line and confirm it actually supports the claim",
        synth_goal="a ranked, deduped findings list (severity, file:line, one-line why), "
                   "consensus vs contested, and the single highest-leverage fix",
    ),
    "research": ModeSpec(
        key="research",
        description="market / competitive research",
        lenses=[
            "market size, segments and demand signals",
            "the competitive landscape and incumbents",
            "trends, timing and second-order effects",
            "risks, headwinds and failure modes",
            "the underserved opportunity / wedge",
        ],
        contrarian_focus="the optimistic consensus — the strongest case that this market/thesis is wrong or already lost",
        verify_target="the sources — try to REFUTE each top claim; mark unsupported assertions",
        synth_goal="a cited briefing: verified findings, the contrarian's surviving objections, "
                   "confidence per claim, and the 3 decisions this should inform",
    ),
    "architecture": ModeSpec(
        key="architecture",
        description="feature / system architecture design + tradeoffs",
        lenses=[
            "the clean design and its data model",
            "implementation feasibility and effort",
            "operational reality (failure modes, rollback, observability)",
            "alternative approaches that were not proposed",
            "long-term maintainability and coupling",
        ],
        contrarian_focus="the emerging design — where it will break under load / over time, and the simpler alternative it dismisses",
        verify_target="the codebase — confirm each feasibility/effort claim against the actual code (cite file:line)",
        synth_goal="a decision doc: recommended design, the tradeoffs, the surviving objections, "
                   "the rejected alternatives + why, and the phased rollout with rollback",
    ),
    "strategy": ModeSpec(
        key="strategy",
        description="business / product strategy",
        lenses=[
            "the opportunity and upside",
            "execution path and required resources",
            "risk, downside and what could kill it",
            "the market / customer reality",
            "sequencing and what to do first",
        ],
        contrarian_focus="the strategy's core assumption — what has to be TRUE for it to work, and why that might not hold",
        verify_target="the sources / stated facts — flag any assumption presented as fact",
        synth_goal="a one-page strategy call: the recommendation, the bet it rests on, the "
                   "surviving risks, and the first concrete move",
    ),
}


@dataclass
class DeliberationResult:
    mode: str
    question: str
    fan_out: List[Dict[str, str]] = field(default_factory=list)  # {provider, lens, text}
    contrarian: str = ""
    factcheck: str = ""
    synthesis: str = ""
    # Coverage bookkeeping (OI-810): which lenses actually produced a real report vs
    # silently degraded. Populated by run_deliberation right after the fan-out completes.
    present_lenses: List[str] = field(default_factory=list)
    failed_seats: List[Dict[str, str]] = field(default_factory=list)  # {provider, lens}

    @property
    def coverage(self) -> str:
        """Human-readable coverage summary, e.g. "3/5 lenses present; glm-harness
        (alternative-approaches lens) failed". A dead seat must never be rendered as if
        it silently contributed to the panel (OI-810) — this is the explicit signal."""
        total = len(self.fan_out)
        present = len(self.present_lenses)
        if not self.failed_seats:
            return f"{present}/{total} lenses present"
        failed = ", ".join(f"{s['provider']} ({s['lens']})" for s in self.failed_seats)
        return f"{present}/{total} lenses present; {failed} failed"

    def to_report(self) -> str:
        lines = [
            f"# Deliberation panel — {self.mode}",
            f"\n**Question:** {self.question}\n",
            f"**Coverage:** {self.coverage}\n",
            "## Synthesis (cited)\n",
            self.synthesis or "_(no synthesis)_",
            "\n---\n## Contrarian / red-team\n",
            self.contrarian or "_(none)_",
            "\n---\n## Verification pass\n",
            self.factcheck or "_(none)_",
            "\n---\n## Divergent views (fan-out)\n",
        ]
        for fo in self.fan_out:
            failed_tag = " — **[SEAT FAILED — no report]**" if _is_error(fo["text"]) else ""
            lines.append(f"\n### {fo['provider']} — lens: {fo['lens']}{failed_tag}\n")
            lines.append(fo["text"] or "_(empty)_")
        return "\n".join(lines)


# Per-report backstop fed into the sequential stages (contrarian/verify/synthesis). This is
# NOT a normal-case truncation — full seat reports (~15-25k tokens total across a 5-seat
# fan-out) fit easily in a modern context window, so the digest feeds them WHOLE. A prior
# 6000-char budget was consumed entirely by each report's echoed frontmatter + instruction +
# shared context, so downstream stages saw only boilerplate and never the analysis, and it
# degraded silently for a whole session (sales-copilot panel, 2026-07-18/22). Heuristic
# echo-stripping and a model-emitted sentinel were both considered and rejected — fragile,
# or dependent on model cooperation. This backstop exists only to bound a pathological
# runaway report; hitting it must never be silent (see _clip).
_REPORT_BACKSTOP = int(os.environ.get("VNX_PANEL_REPORT_BACKSTOP", "40000"))


def _clip(text: str, tag: str, limit: int = _REPORT_BACKSTOP) -> str:
    """Pass text through whole unless it exceeds the generous backstop. A clip is always
    logged loudly — silent degradation is the exact bug this replaces."""
    if len(text) > limit:
        logger.warning(
            "panel digest: report for %s clipped at %d chars (was %d) — raise VNX_PANEL_REPORT_BACKSTOP",
            tag, limit, len(text),
        )
        return text[:limit]
    return text


def _digest(fan_out: List[Dict[str, str]], limit: int = _REPORT_BACKSTOP) -> str:
    """Full digest of the fan-out for the contrarian/verify/synthesis stages.

    Feeds each seat's FULL report text — no normal-case truncation. ``limit`` is a
    generous per-report backstop against a pathological runaway report only; hitting it
    is always logged (see _clip), never silent.
    """
    parts = []
    for fo in fan_out:
        text = (fo.get("text") or "").strip()
        text = _clip(text, fo.get("provider", "?"), limit)
        parts.append(f"[{fo['provider']} / {fo['lens']}]\n{text}")
    return "\n\n".join(parts)


# Per-hop budget for carrying an EARLIER stage's already-substantial output INTO a LATER
# stage's prompt (OI-809). Distinct from _REPORT_BACKSTOP: that bounds one seat's raw
# report once, generously, against a pathological runaway. This bounds what gets
# RE-EMBEDDED at every downstream stage transition. Without it, the full fan-out digest
# (up to 5 seats x _REPORT_BACKSTOP each) plus the contrarian output plus the verify
# output all get re-embedded VERBATIM into the synthesis prompt — ~85% duplicated
# material, observed live as 2216- and 3751-line prompts — until a fixed-length cut
# (here or downstream in the dispatch lane) trims the prompt mid-sentence BEFORE the
# stage instruction, so the seat receives corrupted context with its task missing.
_DISTILLATE_BUDGET = int(os.environ.get("VNX_PANEL_DISTILLATE_BUDGET", "6000"))


def _distill(text: str, tag: str, limit: int = _DISTILLATE_BUDGET) -> str:
    """Bound prior-stage material before a downstream stage's prompt carries it forward.

    Applied at every stage transition (diverge->contrarian->verify->synthesis) so the
    prompt stays bounded regardless of how many hops a piece of text has already passed
    through — the cascading-verbatim growth is exactly the OI-809 bug. Trimming is always
    logged loudly, never silent (same discipline as _clip)."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    logger.warning(
        "panel distillate: %s trimmed to %d chars for the downstream stage (was %d) — "
        "raise VNX_PANEL_DISTILLATE_BUDGET if this loses signal",
        tag, limit, len(t),
    )
    return t[:limit] + f"\n…[{tag} truncated to fit the downstream-stage budget]"


def _stage_prompt(instruction: str, context_sections: List[Tuple[str, str]], reminder: str) -> str:
    """Assemble a downstream-stage prompt with the INSTRUCTION FIRST and the (already
    distilled/bounded) prior-stage context AFTER.

    A fixed-length truncation applied anywhere downstream of this function — this
    module's own backstop or the dispatch lane's — can then only ever cut into the
    context tail, never the instruction the seat must follow. The old layout embedded
    the instruction LAST, after the full verbatim prior-stage material, so a truncation
    upstream of the seat cut the instruction away entirely and the seat received corrupt
    context with no task (OI-809). The trailing reminder is a resilience bonus, never
    load-bearing, since the real instruction already led."""
    parts = [instruction]
    for label, text in context_sections:
        parts.append(f"\n--- {label} ---\n{text}\n--- END {label} ---")
    parts.append(f"\n{reminder}")
    return "\n".join(parts)


def run_deliberation(
    mode: str,
    question: str,
    *,
    dispatcher: DispatcherFn,
    roster: Optional[List[Tuple[str, str]]] = None,
    context: str = "",
    max_workers: int = 5,
) -> DeliberationResult:
    """Run the 4-stage deliberation. ``dispatcher(provider, model, prompt, dispatch_id)`` runs
    one panelist and returns its report text (governed lane). ``context`` is optional extra
    grounding (a diff, a file list, a brief) injected into every stage."""
    spec = MODES.get(mode)
    if spec is None:
        raise ValueError(f"unknown mode {mode!r}; choose one of {sorted(MODES)}")
    roster = roster or DEFAULT_ROSTER
    ctx_block = f"\n\n## Shared context\n{context}\n" if context else ""
    result = DeliberationResult(mode=mode, question=question)

    # ── Stage 1: DIVERGE (parallel fan-out) ──────────────────────────────────
    def _one(idx: int, provider: str, model: str) -> Dict[str, str]:
        lens = spec.lenses[idx % len(spec.lenses)]
        prompt = (
            f"You are one seat on a deliberation panel ({spec.description}).\n"
            f"QUESTION: {question}\n{ctx_block}\n"
            f"YOUR LENS: {lens}.\n"
            "Analyse the question ONLY through your lens. Be concrete and cite evidence "
            "(file:line for code, a named source for research). Give your strongest findings, "
            "then one thing you are UNSURE about. Terse."
        )
        try:
            text = dispatcher(provider, model, prompt, f"panel-{mode}-diverge-{idx}-{uuid.uuid4().hex[:6]}")
        except Exception as exc:  # noqa: BLE001 — a dead provider degrades the panel, never kills it
            text = f"[dispatch error: {exc!r}]"
        return {"provider": provider, "lens": lens, "text": text or "[empty]"}

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_one, i, p, m) for i, (p, m) in enumerate(roster)]
        result.fan_out = [f.result() for f in _cf.as_completed(futures)]
    # stable order by roster
    order = {p: i for i, (p, _) in enumerate(roster)}
    result.fan_out.sort(key=lambda fo: order.get(fo["provider"], 99))

    # Coverage bookkeeping (OI-810): a failed/empty seat must never silently look like a
    # contributing lens. Exclude it from what downstream stages see, and record it so the
    # result/report say so explicitly.
    present_fan_out = [fo for fo in result.fan_out if not _is_error(fo["text"])]
    failed_fan_out = [fo for fo in result.fan_out if _is_error(fo["text"])]
    result.present_lenses = [fo["lens"] for fo in present_fan_out]
    result.failed_seats = [{"provider": fo["provider"], "lens": fo["lens"]} for fo in failed_fan_out]
    if failed_fan_out:
        logger.warning(
            "panel: %d/%d seats produced no usable report (%s) — %s",
            len(failed_fan_out), len(result.fan_out),
            ", ".join(f"{fo['provider']} ({fo['lens']})" for fo in failed_fan_out),
            result.coverage,
        )

    digest = _digest(present_fan_out)
    coverage_note = (
        f"PANEL COVERAGE: {result.coverage}. Reason only from the lenses that actually "
        "responded — a failed seat contributed NOTHING; do not assume its perspective is "
        "represented.\n"
    ) if failed_fan_out else ""

    # ── Stage 2: CONTRARIAN (one red-team seat — the strongest reasoner) ──────
    # Instruction FIRST, bounded distillate of stage 1 AFTER (OI-809) — a downstream
    # truncation can then only cut into the (already bounded) context, never the task.
    contra_instruction = (
        f"You are the RED TEAM on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n{coverage_note}"
        f"Attack the emerging consensus below. Focus on: {spec.contrarian_focus}. "
        "Name what everyone MISSED, steelman the dissent, and flag any claim stated as fact "
        "without evidence. Do not be agreeable. Terse, concrete."
    )
    contra_prompt = _stage_prompt(
        contra_instruction,
        [("The panel said (fan-out digest, distilled)", _distill(digest, "fan-out digest"))],
        reminder=f"Reminder: attack the consensus above. Focus: {spec.contrarian_focus}.",
    )
    result.contrarian = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "deepseek-harness", "claude")),
        contra_prompt, f"panel-{mode}-contrarian",
    )

    # ── Stage 3: VERIFY (adversarial factcheck of the top claims) ────────────
    verify_instruction = (
        f"You are the VERIFY pass on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n{coverage_note}"
        "Take the TOP 5 concrete claims across the panel findings and red-team output below "
        f"and adversarially verify each against {spec.verify_target}. Mark each: CONFIRMED / "
        "REFUTED / UNVERIFIABLE, with the specific evidence (file:line or source). Default to "
        "REFUTED/UNVERIFIABLE when evidence is thin."
    )
    verify_prompt = _stage_prompt(
        verify_instruction,
        [
            ("Panel findings", _distill(digest, "fan-out digest")),
            ("Red-team", _distill(result.contrarian, "contrarian")),
        ],
        reminder=f"Reminder: verify the TOP 5 claims above against {spec.verify_target}.",
    )
    result.factcheck = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "kimi", "claude")),
        verify_prompt, f"panel-{mode}-verify",
    )

    # ── Stage 4: SYNTHESIS (one cited report) ────────────────────────────────
    synth_instruction = (
        f"You are the SYNTHESISER on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n{coverage_note}"
        f"Produce {spec.synth_goal}. Structure: CONSENSUS (verified), CONTESTED (surviving "
        "dissent), VERIFIED CLAIMS (ranked, with evidence), OPEN QUESTIONS. Dedupe. Cite "
        "file:line / sources. Do not invent agreement that isn't there."
    )
    synth_prompt = _stage_prompt(
        synth_instruction,
        [
            ("Divergent views", _distill(digest, "fan-out digest")),
            ("Red-team", _distill(result.contrarian, "contrarian")),
            ("Verification", _distill(result.factcheck, "verify")),
        ],
        reminder=f"Reminder: produce {spec.synth_goal}. Do not invent agreement that isn't there.",
    )
    result.synthesis = _first_ok(
        dispatcher, _ordered_seats(roster, ("claude", "codex", "kimi")),
        synth_prompt, f"panel-{mode}-synth",
    )

    return result


def _ordered_seats(
    roster: List[Tuple[str, str]], prefer: Tuple[str, ...]
) -> List[Tuple[str, str]]:
    """Preferred seats present in the roster (in preference order), then the rest — so a
    stage can fall back to the next provider when one fails."""
    have = {p: m for p, m in roster}
    seats = [(p, have[p]) for p in prefer if p in have]
    seats += [(p, m) for p, m in roster if p not in prefer]
    return seats or list(roster)


def _pick(roster: List[Tuple[str, str]], prefer: Tuple[str, ...]) -> Tuple[str, str]:
    """First preferred provider present in the roster, else the first roster seat."""
    return _ordered_seats(roster, prefer)[0]


def _is_error(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or t.startswith("[dispatch error") or t == "[empty]"


def _first_ok(
    dispatcher: DispatcherFn,
    seats: List[Tuple[str, str]],
    prompt: str,
    did_prefix: str,
) -> str:
    """Try each seat in order until one returns a real (non-error) report. This keeps the
    critical sequential stages (contrarian / verify / synthesis) from collapsing the whole
    panel when the first-choice provider is down (sales-copilot T0, 2026-07-10)."""
    last = "[empty]"
    for provider, model in seats:
        try:
            out = dispatcher(provider, model, prompt, f"{did_prefix}-{provider}-{uuid.uuid4().hex[:6]}")
        except Exception as exc:  # noqa: BLE001
            last = f"[dispatch error {provider}: {exc!r}]"
            continue
        if not _is_error(out):
            return out
        last = out or "[empty]"
    return last


__all__ = ["MODES", "DEFAULT_ROSTER", "ModeSpec", "DeliberationResult", "run_deliberation"]
