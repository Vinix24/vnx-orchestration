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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from receipt_verdict import HARD_FAILURE_STATUSES, SUCCESS_STATUSES

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
    # Coverage gate (OI-1154): below the configured seat minimum the synthesis is
    # refused, recorded here so to_report() renders it loudly, never silently.
    synthesis_refused_reason: str = ""   # non-empty => synthesis refused (coverage floor)
    degraded_synthesis: bool = False     # synthesis ran below the floor (--allow-degraded)
    # Ledger reconciliation (OI-1519): per-seat measurement records ({provider, lens,
    # stage, dispatch_id, outcome, source, frontmatter_*, ledger_*, divergent, reason}).
    # The coverage tally is reconciled against the t0 receipt ledger, never taken from
    # the exit_code a seat writes about ITSELF.
    seat_measurements: List[Dict[str, Any]] = field(default_factory=list)
    unmeasured_seats: List[Dict[str, str]] = field(default_factory=list)  # {provider, lens, dispatch_id, reason}
    ledger_available: bool = True  # False => legacy frontmatter fallback (UNVERIFIED)

    @property
    def ledger_divergences(self) -> List[Dict[str, Any]]:
        """Seats where the receipt ledger and the seat's own frontmatter DISAGREE on
        the outcome. The divergence itself is the finding — a silent correction would
        leave a panel that cannot be weighed (OI-1519)."""
        return [m for m in self.seat_measurements if m.get("divergent")]

    @property
    def coverage(self) -> str:
        """Human-readable coverage summary, e.g. "3/5 lenses present; glm-harness
        (alternative-approaches lens) failed". A dead seat must never be rendered as if
        it silently contributed to the panel (OI-810) — this is the explicit signal.
        An UNMEASURED seat (ledger has no decisive record, OI-1519) is neither present
        nor failed and is named as its own category."""
        total = len(self.fan_out)
        present = len(self.present_lenses)
        parts = [f"{present}/{total} lenses present"]
        if self.failed_seats:
            failed = ", ".join(f"{s['provider']} ({s['lens']})" for s in self.failed_seats)
            parts.append(f"{failed} failed")
        if self.unmeasured_seats:
            unmeasured = ", ".join(f"{s['provider']} ({s['lens']})" for s in self.unmeasured_seats)
            parts.append(f"{unmeasured} unmeasured (no decisive ledger record)")
        return "; ".join(parts)

    @property
    def zero_seats_delivered(self) -> bool:
        """True when NOT ONE seat produced usable content (OI-1358). ``min_seats``/
        ``allow_degraded`` govern whether the SYNTHESIS proceeds below the floor — at
        0/N delivered, a caller with no floor set (or with ``--allow-degraded``) still
        gets a synthesis run over nothing. This flag is the independent signal a caller
        checks WITHOUT parsing the report to know the run produced no real content, so
        ``scripts/panel.py`` can exit non-zero even when the gate let the run through."""
        return len(self.fan_out) > 0 and len(self.present_lenses) == 0

    def to_report(self) -> str:
        lines = [
            f"# Deliberation panel — {self.mode}",
            f"\n**Question:** {self.question}\n",
            f"**Coverage:** {self.coverage}\n",
        ]
        if not self.ledger_available:
            lines.append(
                "**Ledger:** receipt ledger UNAVAILABLE — coverage measured against the "
                "seats' own report frontmatter only (UNVERIFIED, pre-OI-1519 fallback)\n"
            )
        if self.unmeasured_seats:
            seats = ", ".join(f"{s['provider']} ({s['lens']})" for s in self.unmeasured_seats)
            lines.append(
                f"**Unmeasured seats:** {seats} — the receipt ledger has no decisive record; "
                "counted as NEITHER present nor failed\n"
            )
        if self.synthesis_refused_reason:
            lines.append(f"**Synthesis:** REFUSED — {self.synthesis_refused_reason}\n")
        if self.degraded_synthesis:
            lines.append(f"**Synthesis:** ran DEGRADED (--allow-degraded) — {self.coverage}\n")
        lines.append("## Synthesis (cited)\n")
        lines.append(
            "_(synthesis refused — see above)_"
            if self.synthesis_refused_reason
            else (self.synthesis or "_(no synthesis)_")
        )
        lines += [
            "\n---\n## Contrarian / red-team\n",
            self.contrarian or "_(none)_",
            "\n---\n## Verification pass\n",
            self.factcheck or "_(none)_",
            "\n---\n## Divergent views (fan-out)\n",
        ]
        for fo in self.fan_out:
            # Use the RECONCILED outcome recorded at measurement time (OI-1519) — the
            # tag must agree with the count. Hand-built results (no reconciliation ran)
            # fall back to the legacy text check.
            outcome = fo.get("seat_outcome")
            if outcome is None:
                outcome = SEAT_FAILED if _is_error(fo["text"]) else SEAT_PRESENT
            if outcome == SEAT_FAILED:
                failed_tag = " — **[SEAT FAILED — no report]**"
            elif outcome == SEAT_UNMEASURED:
                failed_tag = " — **[SEAT UNMEASURED — no decisive ledger record]**"
            else:
                failed_tag = ""
            lines.append(f"\n### {fo['provider']} — lens: {fo['lens']}{failed_tag}\n")
            lines.append(fo["text"] or "_(empty)_")
        divergences = self.ledger_divergences
        if divergences:
            lines += [
                "\n---\n## Ledger reconciliation — divergences\n",
                "These seats' own report frontmatter and the receipt ledger DISAGREE on the "
                "outcome. The ledger wins the count (OI-1519); the divergence itself is the "
                "finding:\n",
            ]
            for d in divergences:
                lens = f", lens: {d['lens']}" if d.get("lens") else ""
                lines.append(
                    f"- **{d['provider']}** ({d['stage']}{lens}, dispatch `{d['dispatch_id']}`): "
                    f"frontmatter said {d['frontmatter_outcome']} "
                    f"(exit_code={d['frontmatter_exit_code']}); ledger said {d['ledger_outcome']} "
                    f"(status={d['ledger_status']}, verdict.decision={d['ledger_decision']}) "
                    f"→ counted {d['outcome'].upper()}"
                )
        return "\n".join(lines)


# Per-report backstop applied before the per-seat distillate at digest assembly time. This
# is NOT a normal-case truncation — it only bounds a PATHOLOGICAL runaway report. Normal
# bounding is per seat, at assembly, under _SEAT_DISTILLATE_BUDGET (OI-820): each seat's
# report gets its own slice of the downstream budget, so every seat stays represented in
# the contrarian/verify/synthesis stages no matter how many seats the panel has. A prior
# single 6000-char budget over the WHOLE concatenated digest was consumed by the first
# seats' echoed frontmatter + instruction + shared context, so the last seats (and often
# the analysis itself) never reached downstream, and it degraded silently for a whole
# session (sales-copilot panel, 2026-07-18/22). Heuristic echo-stripping and a
# model-emitted sentinel were both considered and rejected — fragile, or dependent on
# model cooperation. The backstop here exists only to cap a single seat's raw report
# before the per-seat distill is applied; hitting it must never be silent (see _clip).
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


# Per-seat distillate budget for carrying the fan-out INTO a later stage's prompt (OI-820).
# Each seat report is distilled under ITS OWN budget before it is joined into the digest, so
# every seat is represented in the contrarian/verify/synthesis stages no matter how many
# seats the panel has. A single head-first cut over the WHOLE concatenated digest was the
# old shape: the first seats ate all the space and the last seats vanished entirely.
# Measured on 166 real seat reports (~14.1k chars average, up to ~50k), a 5-seat digest of
# ~70k cut to 6000 chars reached only seat 1 plus a sliver of seat 2 — 8.5% of the material,
# paid for five panelists and synthesised over one and a half.
#
# The cut itself keeps HEAD and TAIL, never the head alone: a seat report echoes its
# frontmatter, the instruction and the shared context first, and only THEN carries the
# analysis. A head-first cut over the per-seat budget leaves nothing but boilerplate when the
# echo alone exceeds the budget. Measured live (sales-copilot panel, 2026-07-18/22): an
# 11.7KB --context-file produced a ~12.8K echo inside a ~13K report, and a head-first 12000-
# char cut dropped the analysis entirely. Head-and-tail keeps the question/framing up front
# and the analysis/conclusion at the back; the echoed middle is exactly the part that may be
# dropped (see _distill).
#
# This is NOT a return to the OI-809 failure mode: _stage_prompt() puts the stage instruction
# FIRST, so a fixed-length cut anywhere downstream can only ever trim the context tail, never
# the task. The budget is therefore the second line of defence, not the only one — raising it
# is bounded risk. Hitting it is always logged loudly (see _distill), never silent.
_SEAT_DISTILLATE_BUDGET = int(os.environ.get("VNX_PANEL_SEAT_DISTILLATE_BUDGET", "12000"))


def _digest(fan_out: List[Dict[str, str]], limit: int = _REPORT_BACKSTOP) -> str:
    """Per-seat distilled digest of the fan-out for the contrarian/verify/synthesis stages.

    Each seat report gets its OWN distillate budget (``_SEAT_DISTILLATE_BUDGET``) before it
    is joined, so every seat is represented downstream regardless of how many seats the
    panel has — never a single head-first cut over the whole concatenation, which let the
    first seats eat all the space and dropped the last seats entirely (OI-820). The per-seat
    cut keeps HEAD and TAIL (the analysis sits at the end of a seat report, after the echoed
    context), so a seat whose echo alone exceeds the budget still carries its conclusion into
    the downstream stages. ``limit`` stays the generous per-report backstop against a
    pathological runaway report (see _clip); either cut is logged loudly (see _distill),
    never silent.
    """
    parts = []
    for fo in fan_out:
        text = (fo.get("text") or "").strip()
        text = _clip(text, fo.get("provider", "?"), limit)
        seat = f"{fo.get('provider', '?')} / {fo.get('lens', '?')}"
        text = _distill(
            text, seat, _SEAT_DISTILLATE_BUDGET, env_hint="VNX_PANEL_SEAT_DISTILLATE_BUDGET"
        )
        parts.append(f"[{fo['provider']} / {fo['lens']}]\n{text}")
    return "\n\n".join(parts)


# Per-hop budget for carrying an EARLIER stage's SINGLE-document output (contrarian, verify)
# INTO a LATER stage's prompt (OI-809). Distinct from _REPORT_BACKSTOP (bounds one seat's
# raw report once, generously, against a pathological runaway) and from
# _SEAT_DISTILLATE_BUDGET (bounds each seat within the fan-out digest at assembly time,
# OI-820): this bounds what gets RE-EMBEDDED at a stage transition when the source is a
# single text, not a concatenation. Without it, the contrarian output and the verify output
# would re-embed VERBATIM into the synthesis prompt — ~85% duplicated material, observed
# live as 2216- and 3751-line prompts — until a fixed-length cut (here or downstream in the
# dispatch lane) trims the prompt mid-sentence. _stage_prompt() keeps the stage instruction
# FIRST, so such a cut can only ever hit the context tail, never the task (OI-809) — the
# budget is the second line of defence, not the only one, which is why raising it
# (6000 -> 12000) is bounded risk.
#
# A cut here also keeps HEAD and TAIL (same shape as the per-seat distill above): a
# contrarian/verify document leads with its framing and ends with its conclusion, so a
# head-first cut would keep the framing and drop the verdict — the same silent degradation
# the per-seat cut fixes, one stage later.
_DISTILLATE_BUDGET = int(os.environ.get("VNX_PANEL_DISTILLATE_BUDGET", "12000"))


def _distill(
    text: str,
    tag: str,
    limit: int = _DISTILLATE_BUDGET,
    env_hint: str = "VNX_PANEL_DISTILLATE_BUDGET",
) -> str:
    """Bound prior-stage material before a downstream stage's prompt carries it forward.

    Applied at single-text stage transitions (contrarian->verify, contrarian/verify->
    synthesis) so the prompt stays bounded regardless of how many hops a piece of text has
    already passed through — the cascading-verbatim growth is exactly the OI-809 bug. The
    fan-out digest is instead bounded per seat at assembly time by _digest() under
    _SEAT_DISTILLATE_BUDGET (OI-820). ``env_hint`` names the budget's env var in the log so
    an operator raises the right one.

    A cut keeps the HEAD and the TAIL, never the head alone. A panel report opens with the
    question and framing, then echoes the shared context, and only THEN carries the analysis
    and conclusion — so a head-first cut over the budget leaves only boilerplate when the
    echo alone exceeds it. Measured live (sales-copilot panel, 2026-07-18/22): an 11.7KB
    --context-file produced a ~12.8K echo inside a ~13K report, and a head-first 12000-char
    cut dropped the analysis entirely. The tail gets at least half the budget so the analysis
    and conclusion always survive; the middle (where the echoed context sits) is what is
    dropped, with a marker naming how many chars were omitted. Trimming is always logged
    loudly, never silent (same discipline as _clip)."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    omitted = len(t) - limit
    logger.warning(
        "panel distillate: %s trimmed to %d chars for the downstream stage (was %d; "
        "%d middle chars omitted) — raise %s if this loses signal",
        tag, limit, len(t), omitted, env_hint,
    )
    head_budget = limit // 2
    tail_budget = limit - head_budget
    head = t[:head_budget]
    tail = t[-tail_budget:] if tail_budget else ""
    marker = f"\n…[{tag}: {omitted:,} middle chars omitted]\n"
    return f"{head}{marker}{tail}"


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
    min_seats: Optional[int] = None,
    allow_degraded: bool = False,
    receipts_path: Optional["Path | str"] = None,
) -> DeliberationResult:
    """Run the 4-stage deliberation. ``dispatcher(provider, model, prompt, dispatch_id)`` runs
    one panelist and returns its report text (governed lane). ``context`` is optional extra
    grounding (a diff, a file list, a brief) injected into every stage.

    ``min_seats`` (OI-1154) is the coverage floor for the SYNTHESIS stage: when fewer than
    ``min_seats`` DELIVERED seats (the same present/total count ``DeliberationResult.coverage``
    reports — never a second, drifting tally) produced a usable report, the synthesis is
    REFUSED and ``result.synthesis_refused_reason`` carries the delivered/expected counts.
    ``None`` (the default) disables the floor entirely — callers that already bound coverage
    their own way stay unaffected. ``allow_degraded`` is the operator escape: with it True the
    synthesis proceeds below the floor, and ``result.degraded_synthesis`` marks that choice so
    the report renders it, never silently.

    ``receipts_path`` (OI-1519) points at the t0 receipt ledger (``t0_receipts.ndjson``)
    each seat's dispatch-id is reconciled against. ``None`` resolves the canonical state
    dir's ledger. When no ledger exists or can be read (fresh checkout), the tally falls
    back to the pre-OI-1519 frontmatter measurement — flagged on the result
    (``ledger_available=False``) and in the report, never silently."""
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
        # OI-1519: the dispatch-id is CARRIED through — reconciliation against the
        # receipt ledger is impossible when the id is built here and thrown away.
        did = f"panel-{mode}-diverge-{idx}-{uuid.uuid4().hex[:6]}"
        raised = False
        try:
            text = dispatcher(provider, model, prompt, did)
        except Exception as exc:  # noqa: BLE001 — a dead provider degrades the panel, never kills it
            text = f"[dispatch error: {exc!r}]"
            raised = True
        return {"provider": provider, "lens": lens, "text": text or "[empty]",
                "dispatch_id": did, "_raised": raised}

    with _cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_one, i, p, m) for i, (p, m) in enumerate(roster)]
        result.fan_out = [f.result() for f in _cf.as_completed(futures)]
    # stable order by roster
    order = {p: i for i, (p, _) in enumerate(roster)}
    result.fan_out.sort(key=lambda fo: order.get(fo["provider"], 99))

    # Coverage bookkeeping, reconciled against the receipt LEDGER (OI-1519) — the
    # ledger is loaded AFTER the fan-out completes so the seats' receipts exist by
    # the time we measure (the governed lane writes the receipt before the dispatcher
    # returns). A seat's own frontmatter exit_code is the seat testifying about
    # itself; the ledger is the independent record and WINS when the two disagree
    # (the disagreement is reported, never silently corrected). A seat the ledger
    # cannot give a decisive record for is UNMEASURED — its own branch, counted as
    # neither present nor failed. When no ledger exists at all (fresh checkout) the
    # tally falls back to the legacy frontmatter measurement so the panel keeps
    # working — flagged via result.ledger_available, never silently.
    ledger = _ReceiptLedger.load(receipts_path)
    result.ledger_available = ledger is not None
    fan_out_measurements = []
    for fo in result.fan_out:
        measurement = _measure_seat(
            fo["provider"], fo["lens"], fo.get("dispatch_id", ""), fo["text"],
            stage="diverge", ledger=ledger, raised=fo.pop("_raised", False),
        )
        fo["seat_outcome"] = measurement["outcome"]
        fan_out_measurements.append(measurement)
    result.seat_measurements.extend(fan_out_measurements)
    present_fan_out = [fo for fo in result.fan_out if fo["seat_outcome"] == SEAT_PRESENT]
    failed_fan_out = [fo for fo in result.fan_out if fo["seat_outcome"] == SEAT_FAILED]
    unmeasured_fan_out = [fo for fo in result.fan_out if fo["seat_outcome"] == SEAT_UNMEASURED]
    result.present_lenses = [fo["lens"] for fo in present_fan_out]
    result.failed_seats = [
        {"provider": fo["provider"], "lens": fo["lens"], "dispatch_id": fo.get("dispatch_id", "")}
        for fo in failed_fan_out
    ]
    result.unmeasured_seats = [
        {"provider": m["provider"], "lens": m["lens"], "dispatch_id": m["dispatch_id"],
         "reason": m["reason"]}
        for m in fan_out_measurements if m["outcome"] == SEAT_UNMEASURED
    ]
    if failed_fan_out:
        logger.warning(
            "panel: %d/%d seats produced no usable report (%s) — %s",
            len(failed_fan_out), len(result.fan_out),
            ", ".join(f"{fo['provider']} ({fo['lens']})" for fo in failed_fan_out),
            result.coverage,
        )
    if unmeasured_fan_out:
        logger.warning(
            "panel: %d/%d seats are UNMEASURED — the receipt ledger has no decisive "
            "record (%s); counted as neither present nor failed — %s",
            len(unmeasured_fan_out), len(result.fan_out),
            ", ".join(f"{fo['provider']} ({fo['lens']})" for fo in unmeasured_fan_out),
            result.coverage,
        )
    if result.ledger_divergences:
        logger.warning(
            "panel: %d seat(s) where the ledger DISAGREES with the seat's own frontmatter "
            "(ledger wins): %s",
            len(result.ledger_divergences),
            ", ".join(f"{d['provider']} ({d['stage']})" for d in result.ledger_divergences),
        )

    # ── Coverage floor for the synthesis (OI-1154) ──────────────────────────
    # Decide on the SAME present/total count `coverage` reports (OI-1150), never a
    # second tally. Below `min_seats` delivered seats the synthesis is refused
    # LOUDLY with both counts in the message; `allow_degraded` is the conscious
    # operator escape, and the choice is stamped on the result so the report
    # renders it (degraded_synthesis) instead of it vanishing.
    present = len(result.present_lenses)
    total = len(result.fan_out)
    if min_seats is not None and present < min_seats:
        if allow_degraded:
            result.degraded_synthesis = True
            logger.warning(
                "panel: proceeding with DEGRADED synthesis (--allow-degraded) at "
                "%d/%d lenses delivered (minimum %d)",
                present, total, min_seats,
            )
        else:
            result.synthesis_refused_reason = (
                f"refusing synthesis: {present}/{total} lenses delivered (minimum {min_seats})"
            )
            logger.error(
                "panel: synthesis refused — %d/%d lenses delivered (minimum %d); "
                "re-run with --allow-degraded to proceed with degraded coverage",
                present, total, min_seats,
            )
            return result

    digest = _digest(present_fan_out)
    coverage_note = (
        f"PANEL COVERAGE: {result.coverage}. Reason only from the lenses that actually "
        "responded — a failed seat contributed NOTHING, and an unmeasured seat could not "
        "be confirmed against the receipt ledger and is excluded; do not assume either "
        "perspective is represented.\n"
    ) if failed_fan_out or unmeasured_fan_out else ""

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
        [("The panel said (fan-out digest)", digest)],
        reminder=f"Reminder: attack the consensus above. Focus: {spec.contrarian_focus}.",
    )
    result.contrarian = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "deepseek-harness", "claude")),
        contra_prompt, f"panel-{mode}-contrarian",
        ledger=ledger, measure_sink=result.seat_measurements, stage="contrarian",
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
            ("Panel findings", digest),
            ("Red-team", _distill(result.contrarian, "contrarian")),
        ],
        reminder=f"Reminder: verify the TOP 5 claims above against {spec.verify_target}.",
    )
    result.factcheck = _first_ok(
        dispatcher, _ordered_seats(roster, ("codex", "kimi", "claude")),
        verify_prompt, f"panel-{mode}-verify",
        ledger=ledger, measure_sink=result.seat_measurements, stage="verify",
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
            ("Divergent views", digest),
            ("Red-team", _distill(result.contrarian, "contrarian")),
            ("Verification", _distill(result.factcheck, "verify")),
        ],
        reminder=f"Reminder: produce {spec.synth_goal}. Do not invent agreement that isn't there.",
    )
    result.synthesis = _first_ok(
        dispatcher, _ordered_seats(roster, ("claude", "codex", "kimi")),
        synth_prompt, f"panel-{mode}-synth",
        ledger=ledger, measure_sink=result.seat_measurements, stage="synthesis",
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


def _seat_exit_code(text: str) -> Optional[int]:
    """Read a seat's real outcome from the ``exit_code`` in its unified-report frontmatter
    (OI-1358), when present. Returns ``None`` when there is nothing to read: no frontmatter
    block, malformed YAML, or an ``exit_code`` that isn't an int — the caller must then fall
    back to the sentinel-text check, since the outcome cannot be read off this text.

    Why frontmatter carries the outcome and sentinel text doesn't: a seat that fail-closed
    REFUSES does so BEFORE inference and never writes its own report, so the lane wrapper
    writes the unified report itself — WITH frontmatter, carrying the real ``exit_code``.
    ``emit_unified_report`` is idempotent, so a seat that ran inference and wrote its OWN
    report (worker-authored) leaves that report untouched — WITHOUT frontmatter. Measured
    live: a refused glm-harness seat's report on disk
    (``panel-sweep-diverge-3-e4bafa.md``) carries ``exit_code: 1`` in frontmatter, with a
    2.6-14KB non-empty body (the generic instruction-echo wrapper) that matches none of the
    sentinel markers below — this is exactly how 4/5 dead seats in a real 0/5 panel run
    were miscounted as present before this fix.

    ``token_usage`` is deliberately NEVER used as a discriminator here: for 14 of 47
    measured failed seats (all kimi), post-run token capture is itself unavailable
    (``token_usage_measured: false``) even when the seat produced real, verified analysis
    (e.g. ``panel-research-diverge-1-fadf0e.partial.md``, 6.1KB of real panel content
    alongside ``token_usage.output: 0``). Gating presence on output-token count would trade
    the false-positive failure mode this fix repairs for a false-negative one on exactly the
    seats that did the work — ``exit_code`` alone is the reliable signal.
    """
    try:
        from unified_report_schema import parse_frontmatter, SchemaViolation  # noqa: PLC0415
    except ImportError:
        return None
    try:
        frontmatter = parse_frontmatter(text or "")
    except SchemaViolation:
        return None
    exit_code = frontmatter.get("exit_code")
    return exit_code if isinstance(exit_code, int) else None


def _is_error(text: str) -> bool:
    """A seat counts as failed when its OUTCOME says so, not the shape of its text
    (OI-1358). Prefers the real ``exit_code`` carried in the seat's own unified-report
    frontmatter (see ``_seat_exit_code``): a dispatch that REFUSES but still emits a
    non-empty report body (the generic wrapper) used to slip through as "present" because
    it matched none of the three sentinel strings below. Falls back to the sentinel-text
    check ONLY when no frontmatter is present (no report on disk, or a worker-authored
    report without frontmatter) — this is the pre-existing behaviour, not a regression: a
    report with no frontmatter carries no readable outcome, so it stays exactly as
    permissive as it was before this fix.

    NOTE (OI-1519): this is the LEGACY measure — the seat testifying about itself. The
    reconciled coverage tally goes through ``_measure_seat``, which lets the receipt
    ledger overrule this answer (and reports the divergence). ``_is_error`` remains the
    fallback when no ledger exists."""
    exit_code = _seat_exit_code(text)
    if exit_code is not None:
        return exit_code != 0
    t = (text or "").strip()
    return (not t) or t.startswith("[dispatch error") or t == "[empty]"


# ── Ledger reconciliation (OI-1519) ──────────────────────────────────────────
# A seat's coverage outcome is reconciled against the t0 receipt ledger
# (t0_receipts.ndjson), never taken from the ``exit_code`` the seat writes about
# ITSELF in its own report frontmatter. Measured live on dispatch
# ``panel-sweep-diverge-0-a6421f`` (2026-08-30): ledger ``status=timeout`` /
# ``verdict.decision=reject`` while the report frontmatter says ``exit_code: 0`` —
# the frontmatter-only tally counted that timed-out seat as a PRESENT lens.
#
# The vocabulary is NOT hand-rolled here: it reuses the fabric's canonical,
# measured sets from ``receipt_verdict`` (ADR-035 §3.1) — HARD_FAILURE_STATUSES
# (failed/failure/error/blocked/timeout/contract_invalid) and SUCCESS_STATUSES
# (done/success/complete/completed) — the same rule table that produced the
# ``verdict.decision`` values in the ledger itself. ``status`` is the primary
# signal, ``verdict.decision`` the secondary (consulted only when the status is
# indecisive, e.g. "unknown"). Measured on the live ledger (28,592 receipts,
# 2026-08-30): every frequent status literal lands in exactly one of the three
# branches (success / hard-failure / indecisive), and verdict.decision carries
# accept(15) / reject(3015) / investigate(7027).

SEAT_PRESENT = "present"
SEAT_FAILED = "failed"
SEAT_UNMEASURED = "unmeasured"


def _default_receipts_path() -> Optional[Path]:
    """Default t0 receipt ledger location, via the canonical state-dir resolver (the
    same resolution family the governed dispatch lane writes receipts under). Returns
    None when the resolver itself is unavailable — the caller then measures in legacy
    mode. Callers and tests that need a specific ledger pass ``receipts_path``
    explicitly; ambient env-vars are deliberately NOT read here so tests stay
    hermetic."""
    try:
        from vnx_paths import resolve_state_dir
        return Path(resolve_state_dir()) / "t0_receipts.ndjson"
    except Exception as exc:  # resolver failure = no known ledger → legacy mode
        logger.debug("panel: could not resolve the default receipt ledger path: %r", exc)
        return None


class _ReceiptLedger:
    """Per-dispatch-id lookup against the t0 receipt ledger (OI-1519).

    ``lookup`` returns:
      - a non-empty list  → the ledger KNOWS this dispatch-id (decisive or not),
      - an empty list     → the ledger is readable but has NO record of it
                            (an UNMEASURED seat — its own branch, never silently
                            counted present),
      - None              → the ledger could not be read right now (degrade to the
                            legacy frontmatter measurement for this seat).
    Results are cached per dispatch-id. Lookups go through
    ``receipt_provenance.find_receipts_by_dispatch`` — the shared, tested NDJSON
    reader — instead of parsing the ledger here.
    """

    def __init__(self, path: Path):
        self.path = path
        self._cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}

    @classmethod
    def load(cls, receipts_path: Optional["Path | str"]) -> Optional["_ReceiptLedger"]:
        """Open the ledger once, after the seats completed (their receipts exist by
        then). Returns None — legacy fallback mode — when no path resolves, the file
        does not exist (fresh checkout), or it cannot be read."""
        path = Path(receipts_path) if receipts_path is not None else _default_receipts_path()
        if path is None:
            return None
        try:
            if not path.is_file():
                return None
            with path.open("r", encoding="utf-8"):
                pass  # readability probe only
        except OSError as exc:
            logger.debug("panel: receipt ledger %s unreadable: %r", path, exc)
            return None
        return cls(path)

    def lookup(self, dispatch_id: str) -> Optional[List[Dict[str, Any]]]:
        if not dispatch_id:
            return []
        if dispatch_id in self._cache:
            return self._cache[dispatch_id]
        receipts: Optional[List[Dict[str, Any]]]
        try:
            from receipt_provenance import find_receipts_by_dispatch
            receipts = find_receipts_by_dispatch(self.path, dispatch_id)
        except (ImportError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "panel: receipt ledger lookup failed for %s: %r — legacy fallback for this seat",
                dispatch_id, exc,
            )
            receipts = None
        self._cache[dispatch_id] = receipts
        return receipts


def _receipt_outcome(receipt: Dict[str, Any]) -> Optional[bool]:
    """One receipt's answer: True = success, False = failure, None = indecisive.

    ``status`` is the primary signal (canonical ADR-035 sets); ``verdict.decision``
    the secondary, consulted only when the status is indecisive."""
    status = str(receipt.get("status") or "").strip().lower()
    if status in HARD_FAILURE_STATUSES:
        return False
    if status in SUCCESS_STATUSES:
        return True
    verdict = receipt.get("verdict")
    decision = ""
    if isinstance(verdict, dict):
        decision = str(verdict.get("decision") or "").strip().lower()
    if decision == "reject":
        return False
    if decision == "accept":
        return True
    return None


def _ledger_outcome(receipts: List[Dict[str, Any]]) -> Optional[bool]:
    """Reconcile possibly-multiple receipts for one dispatch-id (measured:
    ``panel-architecture-diverge-1-094e47`` carries both ``success`` and
    ``unknown``). Fail-closed on conflict: ANY failure record fails the seat — a
    success record never launders a recorded failure. None = the ledger knows the
    id but every receipt is indecisive."""
    outcomes = [o for o in (_receipt_outcome(r) for r in receipts) if o is not None]
    if not outcomes:
        return None
    if any(o is False for o in outcomes):
        return False
    return True


def _receipt_decision_str(receipt: Dict[str, Any]) -> str:
    verdict = receipt.get("verdict")
    if isinstance(verdict, dict):
        decision = str(verdict.get("decision") or "").strip().lower()
        if decision:
            return decision
    return "<none>"


def _measure_seat(
    provider: str,
    lens: str,
    dispatch_id: str,
    text: str,
    *,
    stage: str,
    ledger: Optional[_ReceiptLedger],
    raised: bool = False,
) -> Dict[str, Any]:
    """Reconcile one seat's outcome against the receipt ledger (OI-1519).

    Three branches, never two:
      1. the ledger knows this dispatch-id and says FAILURE → ``failed``
      2. the ledger knows this dispatch-id and says SUCCESS → ``present``
      3. the ledger has NO decisive record (unknown dispatch-id, indecisive
         receipts) → ``unmeasured`` — its own branch, counted as neither present
         nor failed, never a silent fail-open to present.

    Two degradations are explicit, not silent: a dispatcher that RAISED is failed on
    direct local evidence (no receipt will ever exist for it); a missing/unreadable
    ledger falls back to the legacy frontmatter measurement (``source="legacy"``) so
    the panel keeps working in a fresh checkout — flagged on the result via
    ``ledger_available=False``.

    When the ledger is decisive and DISAGREES with the seat's own frontmatter, the
    ledger wins and ``divergent`` is set — the divergence itself is the finding and
    must be reported, not silently corrected.
    """
    legacy_failed = _is_error(text)
    record: Dict[str, Any] = {
        "provider": provider,
        "lens": lens,
        "stage": stage,
        "dispatch_id": dispatch_id,
        "outcome": "",
        "source": "",
        "frontmatter_exit_code": _seat_exit_code(text),
        "frontmatter_outcome": SEAT_FAILED if legacy_failed else SEAT_PRESENT,
        "ledger_status": "",
        "ledger_decision": "",
        "ledger_outcome": "",
        "divergent": False,
        "reason": "",
    }
    if raised:
        record.update(
            outcome=SEAT_FAILED, source="local",
            reason="dispatcher raised before the seat completed — local evidence",
        )
        return record
    receipts = ledger.lookup(dispatch_id) if ledger is not None else None
    if receipts is None:
        record.update(
            outcome=record["frontmatter_outcome"], source="legacy",
            reason="receipt ledger unavailable/unreadable — frontmatter fallback (UNVERIFIED)",
        )
        return record
    if receipts:
        record["ledger_status"] = "+".join(sorted({
            str(r.get("status") or "").strip().lower() or "<none>" for r in receipts
        }))
        record["ledger_decision"] = "+".join(sorted({_receipt_decision_str(r) for r in receipts}))
    outcome = _ledger_outcome(receipts)
    if outcome is None:
        record.update(
            outcome=SEAT_UNMEASURED, source="ledger",
            reason=("ledger receipts are indecisive (no success/failure signal)"
                    if receipts else "ledger has no receipt for this dispatch-id"),
        )
        return record
    record.update(
        outcome=SEAT_PRESENT if outcome else SEAT_FAILED,
        source="ledger",
        ledger_outcome=SEAT_PRESENT if outcome else SEAT_FAILED,
        reason=("ledger records success for this dispatch-id" if outcome
                else "ledger records a failure for this dispatch-id"),
    )
    record["divergent"] = record["outcome"] != record["frontmatter_outcome"]
    return record


def _first_ok(
    dispatcher: DispatcherFn,
    seats: List[Tuple[str, str]],
    prompt: str,
    did_prefix: str,
    *,
    ledger: Optional[_ReceiptLedger] = None,
    measure_sink: Optional[List[Dict[str, Any]]] = None,
    stage: str = "",
) -> str:
    """Try each seat in order until one returns a real (non-error) report. This keeps the
    critical sequential stages (contrarian / verify / synthesis) from collapsing the whole
    panel when the first-choice provider is down (sales-copilot T0, 2026-07-10).

    OI-1519: the accept decision is reconciled against the receipt ledger with the
    SAME ``_measure_seat`` the fan-out uses — these stages deliver the final verdict,
    so the identical frontmatter-lie bug may not survive here. A ledger-failed seat is
    skipped; an UNMEASURED seat (ledger has no decisive record, e.g. a receipt-lag
    window) is kept as a last-resort fallback so a measurement gap cannot cascade
    through the whole roster and collapse the stage to ``[empty]`` when real content
    exists — and every measurement is recorded on the result via ``measure_sink`` so
    the gap stays visible."""
    last = "[empty]"
    unmeasured_fallback: Optional[str] = None
    for provider, model in seats:
        did = f"{did_prefix}-{provider}-{uuid.uuid4().hex[:6]}"
        raised = False
        try:
            out = dispatcher(provider, model, prompt, did)
        except Exception as exc:  # noqa: BLE001
            out = f"[dispatch error {provider}: {exc!r}]"
            raised = True
        measurement = _measure_seat(
            provider, "", did, out, stage=stage or did_prefix, ledger=ledger, raised=raised,
        )
        if measure_sink is not None:
            measure_sink.append(measurement)
        if measurement["outcome"] == SEAT_PRESENT:
            return out
        if measurement["outcome"] == SEAT_UNMEASURED and unmeasured_fallback is None and out:
            unmeasured_fallback = out
        last = out or "[empty]"
    return unmeasured_fallback if unmeasured_fallback is not None else last


__all__ = ["MODES", "DEFAULT_ROSTER", "ModeSpec", "DeliberationResult", "run_deliberation"]
