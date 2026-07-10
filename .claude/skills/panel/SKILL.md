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
