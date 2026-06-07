# VNX

VNX runs AI coding CLI workers in tmux, isolated git worktrees, through review gates, with an append-only NDJSON receipt per dispatch.

It is a local control plane for the AI coding CLIs that already sit on your machine. One orchestrator dispatches work to ephemeral workers; each worker runs in its own git worktree; review gates decide what merges; every dispatch leaves a receipt. VNX drives `claude`, `codex`, `gemini`, `kimi`, and local `ollama` with no vendor SDK. It calls the CLIs as subprocesses and never imports a provider library.

Most agent projects build SDK-native agents. I orchestrate the binaries instead. The difference shows up in the audit trail: I can reconstruct what was dispatched, what was reviewed, what merged, and what each gate cost.

I built this for my own work, across 2,000+ hours of Claude Code and 1,450+ tests. It is open source because the architecture is portable. Source is at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

This is not a security sandbox; it isolates work with tmux sessions and git worktrees. It is not compliance certification; it produces a local, append-only, inspectable audit trail. It is optimized for human-gated coding workflows, not fully autonomous merges.

## What's new in 1.0

- **pip-installable.** `pip install vnx-orchestration`, then `vnx init`, `vnx migrate`, `vnx doctor`. No repo clone required to scaffold a governed project.
- **Provider-agnostic skill injection.** One skill folder, one structured prompt (role, assignment, resource index), identical for claude, kimi, codex, and deepseek workers. [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md).
- **Realistic benchmark suite.** Field-tests derived from production PRs, programmatic verification per task, LLM-judge fallback, cost per quality-point. Seven lanes measured on the complex tier; methodology ships with every run.
- **SSRF-safe URL policy.** Two-phase validator (lexical + DNS-resolution range-check, fail-closed) with an adversarial test suite. Ships as a governed building block in `scripts/lib/url_policy.py`; wiring into worker fetch paths is 1.0.1.
- **Headless review gates in the audit trail.** Gate results land as normalized reports plus structured result records; a required gate is not complete until both exist.
- **Receipt hash-chain verification.** `audit_chain` tooling verifies the append-only NDJSON ledger end-to-end.

## Writing

I wrote the architecture down as I built it. The full series is on [vincentvandeth.nl](https://vincentvandeth.nl). Start here.

**Governance and trust**
- [Glass-box governance for multi-agent AI](https://vincentvandeth.nl/blog/glass-box-governance-multi-agent-ai)
- [Governance scoring: agent trust and autonomy](https://vincentvandeth.nl/blog/governance-scoring-agent-trust-autonomy)
- [Autonomous agents do not exist](https://vincentvandeth.nl/blog/autonome-ai-agents-bestaan-niet)
- [ISA 62443: AI governance and industrial safety](https://vincentvandeth.nl/blog/isa-62443-ai-governance-industrial-safety)

**Receipts, audit, traceability**
- [The NDJSON receipt ledger for AI audit trails](https://vincentvandeth.nl/blog/ndjson-receipt-ledger-ai-audit-trail)
- [Traceability architecture: the AI decision receipt](https://vincentvandeth.nl/blog/traceability-architecture-ai-decision-receipt)
- [The external watcher pattern for AI agent observation](https://vincentvandeth.nl/blog/external-watcher-pattern-ai-agent-observation)

**Orchestration architecture**
- [What is AI orchestration: terminal dispatch](https://vincentvandeth.nl/blog/wat-is-ai-orchestration-terminal-dispatch)
- [Architecture beats models in AI agent dispatches](https://vincentvandeth.nl/blog/architecture-beats-models-ai-agent-dispatches)
- [Multi-model AI orchestration from a single terminal](https://vincentvandeth.nl/blog/multi-model-ai-orchestration-single-terminal)
- [Why no subagents in AI orchestration](https://vincentvandeth.nl/blog/waarom-geen-subagents-ai-orchestration)
- [Routing is not orchestration](https://vincentvandeth.nl/blog/routing-not-orchestration-openclaw-governance)

**Cost, context, production**
- [The real cost of AI agents in production](https://vincentvandeth.nl/blog/real-cost-ai-agents-production)
- [Zero-LLM context injection with VNX intelligence](https://vincentvandeth.nl/blog/zero-llm-context-injection-vnx-intelligence)
- [Context rotation at scale](https://vincentvandeth.nl/blog/context-rotation-scale-vnx-implementation)
- [Async quality gates for AI agent workflows](https://vincentvandeth.nl/blog/async-quality-gates-ai-agent-workflows)

## What works today vs what is opt-in

I am honest about maturity because the audit trail is the whole point and an overclaim would undercut it. The following is verified against code and receipts as of 2026-05-30.

**Tier 1, in production.** Append-only NDJSON receipts with hash-chain verification tooling (`audit_chain`); per-append chain enforcement lands in 1.0.1. Multi-CLI provider hub with no vendor SDK (claude, codex, kimi, gemini, ollama). Review gates (codex and gemini) with deterministic CI as the third gate. Per-worker git worktree isolation with teardown classification (lane-specific; `VNX_ISOLATED_WORKTREE` defaults off). The interactive tmux worker lane (available and subscription-preserving; works today, is actively being hardened ahead of the June 15 OAuth rollout, and is set to become the production default as of June 15, 2026; its PREPARE/GOVERN/RECEIPT/CAPTURE structural work has shipped). The provider-constraint YAML source of truth. Zero-LLM context injection and repo map. Cost tracking per gate invocation. Governed memory PAST and CURRENT.

**Tier 2, shipped but opt-in and burning in.** Smart routing (`VNX_AUTO_ROUTE`), the elastic worker pool (`bin/vnx pool`), the track layer and roadmap autopilot (FUT-1/FUT-2 shipped and the tracks layer activated for forward-state planning), the consolidation loop (auto-dream), and governed memory FUTURE. These work mechanically, default off, and are not proven at the bar I hold Tier 1 to. I do not claim them as done.

**Tier 3, designed, not built.** Parallel multi-track execution. The wave scheduler, merge lock, and file-scope derivation are designed, not shipped. Treat it as architecture, not a feature.

Per-provider maturity differs. Worktree isolation (`VNX_ISOLATED_WORKTREE`, defaults off) is available for single-dispatch work and is not guaranteed to be race-free under parallel dispatch, which is why parallel sits in Tier 3.

## Install

```bash
pip install vnx-orchestration
vnx init                                  # scaffold a VNX project in the current dir
vnx migrate                               # apply runtime DB migrations
vnx doctor                                # environment and dependency checks
vnx dispatch-agent --agent hello-world    # works via the examples/ fallback
```

There are two binaries on purpose during the 1.0 cutover. The pip-installed `vnx` covers the essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`). Checkout-only operator commands still live behind `./bin/vnx`, including `gate-check`, `new-worktree`, and `demo`. The `demo` path runs without API keys.

## Architecture

VNX uses a T0 orchestrator and ephemeral workers. The old fixed T1-T3 mental model is no longer the core; workers spawn per dispatch and leave behind receipts, reports, and worktree state.

```
   T0 orchestrator  (plans, dispatches, reviews; does not write code)
        |
        |  one dispatch
        v
   +----------------+   +----------------+   +----------------+
   | ephemeral      |   | ephemeral      |   | ephemeral      |
   | worker         |   | worker         |   | worker         |
   | (git worktree) |   | (git worktree) |   | (git worktree) |
   +-------+--------+   +-------+--------+   +-------+--------+
           |                    |                    |
           +---------+----------+----------+---------+
                     v                     v
              review gates          worktree teardown
          (codex / gemini / CI)   (clean / pushed / dirty)
                     |
                     v
   append-only NDJSON receipts  (one per dispatch; hash-chain verify via audit_chain)
```

Claude has two explicit lanes. The reference worker lane is Claude via `claude -p` subprocess in headless mode. The interactive tmux lane runs Claude on subscription; it is available now, subscription-preserving, and set to become the default lane as of the June 15, 2026 billing change — it is still maturing into that role.

The leaseless single-shot tmux dispatch lane lives in `scripts/lib/tmux_interactive_dispatch.py`. Per-worker git worktree isolation lives in `scripts/lib/tmux_worktree.py`, including teardown classification for clean, committed or pushed, and dirty worktrees. Worktree isolation is available via `VNX_ISOLATED_WORKTREE=1` and is off by default; isolation guarantees vary by lane.

### Governed memory: past, current, future

Memory is the unsolved problem in agentic AI. Most systems bolt a vector store onto a stateless model and call it memory. I treat memory as a governed state machine with three tenses, each with its own store and its own audit guarantees.

The PAST is append-only NDJSON receipts: a forensic ledger of every dispatch, gate, and merge, with hash-chain verification tooling (`audit_chain`) over it. Per-append chain enforcement lands in 1.0.1. It is forensic, not lossy. This is in production now, with thousands of receipts behind it.

The CURRENT is `runtime_coordination.db` (SQLite WAL): real-time orchestration state, leases, tracks, and dispatch status that any terminal can read for situational awareness.

The FUTURE is the track layer and roadmap autopilot: planned features modeled as project-scoped tracks with a dependency graph, which the system can advance under human approval gates. The FUT-1 track schema, DAL, and CLI and the FUT-2 ADR-007 tenant-scoping have shipped, and the tracks layer is now activated for forward-state planning. The autopilot stays opt-in: it plans, but a human still approves the last step.

A learning layer (`quality_intelligence.db`) consolidates the past into patterns and antipatterns that get injected into future dispatch context. The consolidation loop (auto-dream) is shipped and opt-in. It is burning in, not yet on by default.

The point is not that the AI remembers. The point is that what it remembers is governed: every memory has a receipt, every plan has a gate, and a human owns the boundary.

## Architecture decisions

The decisions behind VNX are written down, not implied. There are 22 Architecture Decision Records under [docs/governance/decisions/](docs/governance/decisions/). The ones that shape the system most:

- [ADR-005](docs/governance/decisions/ADR-005-ndjson-audit-ledger-primary.md): append-only NDJSON ledger as the primary observability surface
- [ADR-006](docs/governance/decisions/ADR-006-staging-promote-human-gate.md): staging then promote, with a mandatory human approval gate
- [ADR-008](docs/governance/decisions/ADR-008-dual-llm-adversarial-review.md): dual-LLM adversarial review (codex plus gemini) bound by a contract hash
- [ADR-011](docs/governance/decisions/ADR-011-manager-worker-hierarchy.md): manager plus worker hierarchy with explicit depth, not depth-1 subagents
- [ADR-012](docs/governance/decisions/ADR-012-hybrid-interactive-headless.md): hybrid interactive and headless execution, no retire-interactive
- [ADR-014](docs/governance/decisions/ADR-014-autonomous-chain-dispatch.md): autonomous mode is pre-approved chain dispatch, never gate bypass
- [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md): one structured plain-text skill prompt for every provider lane, no per-CLI mechanisms

For how the architecture got here, [docs/manifesto/EVOLUTION_TIMELINE.md](docs/manifesto/EVOLUTION_TIMELINE.md) reconstructs the technical evolution over roughly six months, including the private incubation provenance. The public repository is the extraction, hardening, and packaging of work that started inside a private product.

## Multi-provider architecture

VNX is not a thin "supports many models" wrapper. The provider layer is governed by `scripts/lib/providers/provider_constraints.yaml`, a machine-readable source of truth for constraints such as `kimi-via-cli-only`, `no-anthropic-sdk`, and `deepseek-harness-subscription-blocked`.

The default Claude worker path runs through interactive tmux (`scripts/lib/tmux_interactive_dispatch.py`); the burst path uses subprocess `claude -p`. The receipt format and intelligence layer are uniform across all lanes today; per-lane parity on the full PREPARE/GOVERN envelope is the dispatch-unification work targeted for the 1.x release.

Kimi runs through the Kimi CLI with OAuth. VNX does not call the Moonshot SDK directly for that lane, which keeps attribution and rate-limit behavior in one place.

OpenRouter is the gateway lane for GLM-5.1 from Zhipu today; arbitrary OpenAI-compatible models via the proxy lane are planned for a later release. Local Ollama is used for the resolver layer and privacy-sensitive work, including Gemma 4 E4B, where no data leaves the machine.

The non-obvious path is DeepSeek through the Claude harness. VNX can run DeepSeek with my own DeepSeek API key plus hardening: `ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater traffic disabled, and MCP off. Operator measurement on Claude Code 2.1.150 on 2026-05-26 showed this path is meaningfully more effective on coding and tool tasks than a bare DeepSeek API call (internal measurement only, not a published benchmark), because the harness adds tool-use loops, context injection, and structured diff output that the raw API does not provide.

Skills reach every lane the same way. Rather than depend on per-CLI mechanisms (`/skill`, `--skills-dir`, runtime tools that differ per provider), VNX composes the skill content into a structured plain-text prompt — role and methodology, then the assignment, then an on-demand index of the skill's reference and script files — and applies it uniformly to claude, kimi, codex, and deepseek workers from one source-of-truth skill folder. A skill edit propagates to all providers with no per-provider sync. See [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md).

### Billing as a consequence, not a goal

VNX treats AI coding tools as interactive CLI workers, not SDK calls. A consequence falls out of that choice. Anthropic's June 15, 2026 billing change moves headless `claude -p` usage to API credits while interactive Claude Code stays on a subscription. Because interactive Claude sessions stay on the subscription rather than API credits, the interactive tmux lane is subscription-preserving. It becomes the default Claude worker lane when the June 15 billing change takes effect. This describes current public policy; vendors can change their terms.

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

Public 1.0 as of this README on 2026-05-30: the package is pip-installable, `VERSION` is `1.0.0`, and the operator binary is still required for the full command surface. Open governance and release items are tracked in [ROADMAP.md](ROADMAP.md), [FEATURE_PLAN.md](FEATURE_PLAN.md), and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

I built this for my own work. Use at your own discretion.

## Credits

Dimitri Geelen's Claude Code RFC on deterministic agent governance shaped the external governance framing: [RFC #45427](https://github.com/anthropics/claude-code/issues/45427).

My subagent observability work is currently represented by the [delivery substep observability contract](docs/core/150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md); I will link the public RFC when it is split out.

Anthropic Claude Code is the foundation. I add receipts, provider routing, tmux dispatch, and worktree isolation around it.
