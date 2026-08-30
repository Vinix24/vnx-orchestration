---
name: panel
description: Multi-provider deliberation panel for COMPLEX, multi-view questions — architecture, strategy, market research, and codebase sweeps. Runs a 4-stage deliberation across the provider fleet (diverge → contrarian red-team → adversarial verify → cited synthesis), the same multi-perspective rigour the plan-gate already applies to plan reviews, generalised to arbitrary questions. Use when one model's answer isn't enough and you want convergence THROUGH disagreement + verification, not a single opinion.
allowed-tools: [Read, Grep, Glob, Bash]
---

# /panel — multi-provider deliberation

For hard questions where you want several strong models to genuinely deliberate, not just answer in parallel. Each stage builds on the last, so the panel converges *through* disagreement and verification instead of averaging opinions.

## When to use

- **architecture** — a feature/system design where the tradeoffs are non-obvious.
- **strategy** — a business/product call that rests on assumptions worth stress-testing.
- **research** — market/competitive questions where claims need refuting.
- **sweep** — a codebase audit (security / correctness / dead-code / refactor).

Reach for it when a single model's answer would be a guess, and you'd otherwise open five terminals yourself.

## How it works (4 stages)

1. **Diverge** — every fleet provider (codex / kimi / claude / glm-5.2 / deepseek-harness) analyses the SAME question through a DIFFERENT mode-specific lens.
2. **Contrarian** — one designated seat red-teams the emerging consensus: what did everyone miss, which "this is fine" is wrong.
3. **Verify** — the top claims are adversarially checked (against the CODE for sweeps — real `file:line`; against SOURCES for research — try to refute).
4. **Synthesis** — one cited report: consensus + surviving dissent + verified/refuted claims, ranked and deduped.

## How seat coverage is counted (OI-1519)

"N of M lenses present" is reconciled against the t0 receipt ledger
(`t0_receipts.ndjson`), never taken from the `exit_code` a seat writes about ITSELF in
its own report frontmatter. After each stage completes, every seat's dispatch-id is
looked up in the ledger via `receipt_provenance.find_receipts_by_dispatch` and lands in
one of THREE outcomes:

- **present** — the ledger holds a decisive SUCCESS record for the seat's dispatch-id
  (`status` in the ADR-035 success set `done`/`success`/`complete`/`completed`, or
  `verdict.decision=accept`).
- **failed** — the ledger holds a decisive FAILURE record (`status` in the ADR-035
  hard-failure set `failed`/`failure`/`error`/`blocked`/`timeout`/`contract_invalid`,
  or `verdict.decision=reject`; multiple receipts for one dispatch-id reconcile
  failure-beats-success — a success record never launders a recorded failure). A
  dispatcher that RAISED before the seat completed is failed on direct local evidence.
- **unmeasured** — the ledger has NO decisive record for the dispatch-id (no receipt
  at all, or only indecisive ones). This is a third BRANCH, not a third value: the
  seat counts as NEITHER present NOR failed and is named in the report
  (`**Unmeasured seats:**`, `[SEAT UNMEASURED — no decisive ledger record]`).
  Reading "unmeasured" as "probably fine" repeats exactly the bug OI-1519 repaired —
  a measurement gap is not a lens.

Where the ledger and the seat's own frontmatter DISAGREE, the ledger WINS the count and
the divergence itself is reported (`## Ledger reconciliation — divergences` in the
report, `result.ledger_divergences`) — a silent correction would produce a panel that
cannot be weighed. When no ledger exists or can be read at all (fresh checkout) the
tally falls back to the pre-OI-1519 frontmatter measurement, flagged loudly via
`ledger_available=False` in the report — never silently.

This holds for the diverge fan-out AND for the sequential stages (contrarian, verify,
synthesis), which accept a seat via `_first_ok` under the same measurement: a
ledger-failed seat is skipped; an unmeasured seat is kept only as a last-resort
fallback so a measurement gap cannot collapse a stage to `[empty]` when real content
exists.

Measured case (2026-08-29, the dispatch that opened OI-1519): seat
`panel-sweep-diverge-0-a6421f` (codex) wrote `exit_code: 0` in its own report
frontmatter while the receipt ledger records `status=timeout`,
`verdict.decision=reject` after the 600s deadline. Pre-fix it counted PRESENT (4/5
lenses reported; really 3 + a timeout). Post-fix it counts FAILED, and the divergence
is printed in the report.

## Run it

```bash
python3 scripts/panel.py <mode> "<question>" [--context-file FILE] [--timeout 900] [--out FILE]
```

Examples:

```bash
# architecture decision, grounded on a design doc
python3 scripts/panel.py architecture "Should the judge run per-receipt or batched?" --context-file docs/adr-028.md

# code sweep grounded on a diff or file list
git diff origin/main > /tmp/d.diff
python3 scripts/panel.py sweep "Review this change for security + correctness" --context-file /tmp/d.diff

# market research
python3 scripts/panel.py research "Is there an underserved MKB market for local-LLM invoice processing?"
```

The cited report lands in `unified_reports/panel-<mode>-<id>.md` and prints to stdout.

## Notes

- Governed lane: each panelist dispatches through the review lane and emits a receipt. Respects the provider constraints (kimi-via-cli-only, glm via OpenRouter/harness, deepseek-harness with its own key + hardening, no-anthropic-sdk) — the dispatcher routes each provider correctly.
- A dead/absent provider (e.g. deepseek without its key) degrades the panel gracefully; it never blocks the run.
- The panel is the fabric's general multi-view tool. It complements the **plan-gate panel** (which is scoped to plan reviews) and the **t0-orchestrator** skill (which owns orchestration decisions). Implementation: `scripts/lib/deliberation_panel.py`.
