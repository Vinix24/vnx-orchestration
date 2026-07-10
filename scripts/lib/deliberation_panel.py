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
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# A dispatcher runs one panelist and returns its report text.
DispatcherFn = Callable[[str, str, str, str], str]

# Default panel roster (provider, model). Each entry routes through its own governed lane.
# deepseek-harness is optional (needs its own key + hardening) — degrades gracefully if absent.
DEFAULT_ROSTER: List[Tuple[str, str]] = [
    ("codex", "gpt-5.5"),
    ("kimi", "kimi-k2-7-code"),
    ("claude", "sonnet"),
    ("glm-harness", "glm-5.2"),
    ("deepseek-harness", "deepseek-reasoner"),
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

    def to_report(self) -> str:
        lines = [
            f"# Deliberation panel — {self.mode}",
            f"\n**Question:** {self.question}\n",
            "## Synthesis (cited)\n",
            self.synthesis or "_(no synthesis)_",
            "\n---\n## Contrarian / red-team\n",
            self.contrarian or "_(none)_",
            "\n---\n## Verification pass\n",
            self.factcheck or "_(none)_",
            "\n---\n## Divergent views (fan-out)\n",
        ]
        for fo in self.fan_out:
            lines.append(f"\n### {fo['provider']} — lens: {fo['lens']}\n")
            lines.append(fo["text"] or "_(empty)_")
        return "\n".join(lines)


def _digest(fan_out: List[Dict[str, str]], limit: int = 1500) -> str:
    """Compact digest of the fan-out for the contrarian/verify/synthesis stages."""
    parts = []
    for fo in fan_out:
        text = (fo.get("text") or "").strip()
        parts.append(f"[{fo['provider']} / {fo['lens']}]\n{text[:limit]}")
    return "\n\n".join(parts)


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

    digest = _digest(result.fan_out)

    # ── Stage 2: CONTRARIAN (one red-team seat — the strongest reasoner) ──────
    contra_prompt = (
        f"You are the RED TEAM on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n"
        f"The panel said:\n{digest}\n\n"
        f"Attack the emerging consensus. Focus on: {spec.contrarian_focus}. "
        "Name what everyone MISSED, steelman the dissent, and flag any claim stated as fact "
        "without evidence. Do not be agreeable. Terse, concrete."
    )
    result.contrarian = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "deepseek-harness", "claude")),
        contra_prompt, f"panel-{mode}-contrarian",
    )

    # ── Stage 3: VERIFY (adversarial factcheck of the top claims) ────────────
    verify_prompt = (
        f"You are the VERIFY pass on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n"
        f"Panel findings:\n{digest}\n\nRed-team:\n{result.contrarian[:1500]}\n\n"
        f"Take the TOP 5 concrete claims across the above and adversarially verify each against "
        f"{spec.verify_target}. Mark each: CONFIRMED / REFUTED / UNVERIFIABLE, with the specific "
        "evidence (file:line or source). Default to REFUTED/UNVERIFIABLE when evidence is thin."
    )
    result.factcheck = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "kimi", "claude")),
        verify_prompt, f"panel-{mode}-verify",
    )

    # ── Stage 4: SYNTHESIS (one cited report) ────────────────────────────────
    synth_prompt = (
        f"You are the SYNTHESISER on a deliberation panel ({spec.description}).\n"
        f"QUESTION: {question}\n{ctx_block}\n"
        f"Divergent views:\n{digest}\n\nRed-team:\n{result.contrarian[:1500]}\n\n"
        f"Verification:\n{result.factcheck[:1500]}\n\n"
        f"Produce {spec.synth_goal}. Structure: CONSENSUS (verified), CONTESTED (surviving "
        "dissent), VERIFIED CLAIMS (ranked, with evidence), OPEN QUESTIONS. Dedupe. Cite "
        "file:line / sources. Do not invent agreement that isn't there."
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
