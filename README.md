# VNX

VNX runs AI coding CLI workers in tmux, isolated git worktrees, through review gates, with an append-only NDJSON receipt per dispatch.

It is a local control plane for the AI coding CLIs that already sit on your machine. One orchestrator dispatches work to ephemeral workers; each worker runs in its own git worktree; review gates decide what merges; every dispatch leaves a receipt. VNX drives `claude`, `codex`, `gemini`, `kimi`, and local `ollama` with no vendor SDK. It calls the CLIs as subprocesses and never imports a provider library.

Most agent projects build SDK-native agents. I orchestrate the binaries instead. The difference shows up in the audit trail: I can reconstruct what was dispatched, what was reviewed, what merged, and what each gate cost.

I built this for my own work, across 3,000+ hours of Claude Code and 15,000+ tests. It is open source because the architecture is portable. Source is at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

This is not a security sandbox; it isolates work with tmux sessions and git worktrees. It is not compliance certification; it produces a local, append-only, inspectable audit trail. It is optimized for human-gated coding workflows, not fully autonomous merges.

## What's new in 1.0

- **Packaged for pip.** A `pip`-installable distribution (`vnx init`, `vnx migrate`, `vnx doctor`) so a governed project can be scaffolded without a repo clone. The package builds from this tree; publishing to PyPI is the final 1.0 ship gate and has not happened yet. Until then, install from a checkout (see [Install](#install)).
- **Provider-agnostic skill injection.** One skill folder, one structured prompt (role, assignment, resource index), identical for claude, kimi, codex, and deepseek workers. [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md).
- **Realistic benchmark methodology.** A field-tests harness that measures provider lanes on production-derived tasks with programmatic verification per task, an LLM-judge fallback, and cost per quality-point. It lives in the repo under `scripts/benchmark/field-tests/` (not the pip package — the task seeds are repo-specific). A generalised "bring your own tasks" version is planned for 1.1.
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

I am honest about maturity because the audit trail is the whole point and an overclaim would undercut it. The following is verified against code and receipts as of the 1.0 launch (July 2026).

**Tier 1, in production.** Append-only NDJSON receipts with hash-chain verification tooling (`audit_chain`); per-append chain enforcement lands in 1.0.1. Multi-CLI provider hub with no vendor SDK (claude, codex, kimi, gemini, ollama). Review gates (codex and gemini) with deterministic CI as the third gate. Per-worker git worktree isolation with teardown classification (lane-specific; `VNX_ISOLATED_WORKTREE` defaults off). The interactive tmux worker lane: it is the default Claude worker lane (`scripts/commands/dispatch.sh` selects it unless a dispatch opts into the headless burst lane), runs on the subscription, and its PREPARE/GOVERN/RECEIPT/CAPTURE structural work has shipped. It is still being hardened; I do not yet claim it matches the headless lane on every surface. The headless `claude -p` burst lane is opt-in and blocked by default (`claude-headless` constraint; `VNX_OVERRIDE_CLAUDE_HEADLESS=1` to open it). The provider-constraint YAML source of truth. Zero-LLM context injection and repo map. Cost tracking per gate invocation. Governed memory PAST and CURRENT.

**Tier 2, shipped but opt-in and burning in.** Smart routing (`VNX_AUTO_ROUTE`, and `dispatch-agent --auto-route`; built, not wired into the single-entry door). The elastic worker pool (`bin/vnx pool`). The track layer and roadmap autopilot (FUT-1/FUT-2 shipped and the tracks layer activated for forward-state planning; the 1.0.1 future-state reconciliation wires the open-item → track bridge and the `derived_status` reconciler into the autopilot tick behind the `VNX_ROADMAP_AUTOPILOT=1` gate, and brings the `dispatches` table into ADR-007 composite-key tenancy). The consolidation loop (auto-dream). Governed memory FUTURE. These work mechanically, default off, and are not proven at the bar I hold Tier 1 to. The self-learning loop — outcomes proposing new success-patterns/antipatterns for a human to accept — now has a working proposal tier: it mines the governed receipt stream for recurring failures and writes prevention-rule proposals to `pending_rules.json` for a human to accept (operator-gated, G-L1). The pool does not auto-grow without that human gate, and the auto-confidence tier that would reweight patterns from outcomes is still deferred (it needs outcome-grounding that the receipts don't yet carry). I do not claim the full loop as done.

**Tier 3, designed, not built.** Parallel multi-track execution. The wave scheduler, merge lock, and file-scope derivation are designed, not shipped. Treat these as architecture, not a feature.

The single-entry dispatch door (`scripts/lib/dispatch_cli.py`) is **now the default dispatch lane** — flipped on 2026-06-24 (ADR-024, `dispatch_flags._DEFAULT_ENABLED = True`). Every dispatch routes through the one door, which normalizes GLM to the harness lane (`glm-harness`), applies the single-source routing predicate, and runs a phantom-guard that rejects evidence-free GATE-GREEN receipts. Roll back to legacy routing per terminal with `VNX_DISPATCH_LEGACY=1`. It flipped recently, so I hold it at Tier 2 (shipped, burning in) rather than Tier 1.

Per-provider maturity differs. Worktree isolation (`VNX_ISOLATED_WORKTREE`, defaults off) is available for single-dispatch work and is not guaranteed to be race-free under parallel dispatch, which is why parallel sits in Tier 3.

## Install

The package is not on PyPI yet — publishing is the final 1.0 ship gate. Install from a checkout:

```bash
git clone https://github.com/Vinix24/vnx-orchestration
cd vnx-orchestration
pip install -e .                          # editable install of the pip CLI
vnx init                                  # scaffold a VNX project in the current dir
vnx migrate                               # apply runtime DB migrations
vnx doctor                                # environment and dependency checks
vnx dispatch-agent --agent hello-world    # needs a worker CLI (see Prerequisites)
```

### Prerequisites

VNX does not run models itself — it drives existing coding CLIs as subprocesses and governs the
result. The default dispatch lane needs an **installed + authenticated `claude` CLI** on your PATH
(other lanes: `codex`, `gemini`, `kimi`), and using it incurs that provider's subscription/credit
usage. `vnx dispatch-agent` fails at spawn if no worker CLI is present — `vnx doctor` flags this with
a `tool:worker-cli` warning. (Zero-key exploration of the governance flow is not currently shipped;
the old replay demo was retired.)

There are two binaries on purpose. The pip `vnx` covers the essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`). Checkout-only operator commands live behind `./bin/vnx`, including `gate-check` and `new-worktree`. When the package publishes, `pip install vnx-orchestration` replaces the clone step.

## Architecture

VNX uses a T0 orchestrator and ephemeral workers. The old fixed T1-T3 mental model is no longer the core; workers spawn per dispatch and leave behind receipts, reports, and worktree state. (A fixed, terminal-pinned T0-T3 model still exists for the opt-in subprocess lane; the ephemeral-per-dispatch model is the default.)

The end-to-end deep dive — intent → single-entry door → bundle assembly → delivery → receipt → governance → review gates, plus the intelligence-injection contract — is in [docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md). The full component/data-flow reference is [docs/core/00_VNX_ARCHITECTURE.md](docs/core/00_VNX_ARCHITECTURE.md).

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

Claude has two explicit lanes. The default worker lane is the interactive tmux lane: it runs Claude on the subscription and is what `dispatch.sh` selects unless a dispatch opts out. The headless `claude -p` subprocess lane is the burst alternative; after the June 15, 2026 billing change it runs on API credits, so it is opt-in and blocked by default (`VNX_OVERRIDE_CLAUDE_HEADLESS=1` to open it). The tmux lane is subscription-preserving and still maturing toward full parity with the headless lane on every surface.

The leaseless single-shot tmux dispatch lane lives in `scripts/lib/tmux_interactive_dispatch.py`. Per-worker git worktree isolation lives in `scripts/lib/tmux_worktree.py`, including teardown classification for clean, committed or pushed, and dirty worktrees. Worktree isolation is available via `VNX_ISOLATED_WORKTREE=1` and is off by default; isolation guarantees vary by lane.

### Governed memory: past, current, future

Memory is the unsolved problem in agentic AI. Most systems bolt a vector store onto a stateless model and call it memory. I treat memory as a governed state machine with three tenses, each with its own store and its own audit guarantees.

The PAST is append-only NDJSON receipts: a forensic ledger of every dispatch, gate, and merge, with hash-chain verification tooling (`audit_chain`) over it. Per-append chain enforcement lands in 1.0.1. It is forensic, not lossy. This is in production now, with 14,000+ receipts in the audit trail behind it.

The CURRENT is `runtime_coordination.db` (SQLite WAL): real-time orchestration state, leases, tracks, and dispatch status that any terminal can read for situational awareness. As of 1.0.1 the `dispatches` table is ADR-007 tenant-scoped on a composite `UNIQUE(dispatch_id, project_id)`, rebuilt in place by a crash-safe migration (#859).

The FUTURE is the track layer and roadmap autopilot: planned features modeled as project-scoped tracks with a dependency graph, which the system can advance under human approval gates. The FUT-1 track schema, DAL, and CLI and the FUT-2 ADR-007 tenant-scoping have shipped, and the tracks layer is now activated for forward-state planning. The 1.0.1 future-state reconciliation keeps it honest automatically: an open-item → track bridge syncs `track_open_items` through the single-writer `tracks.py` primitives, then a reconciler derives each track's status — a track is `done` only when it has no unresolved blocking open-items, every dependency track is done, all of its dispatches are in terminal states, and any linked PR is confirmed merged. Both run inside the autopilot tick, which refuses to advance on a failed sync. The autopilot stays opt-in: it plans, but a human still approves the last step.

A learning layer (`quality_intelligence.db`) consolidates the past into patterns and antipatterns that get injected into future dispatch context. The consolidation loop (auto-dream) is shipped and opt-in. It is burning in, not yet on by default.

The point is not that the AI remembers. The point is that what it remembers is governed: every memory has a receipt, every plan has a gate, and a human owns the boundary.

## Architecture decisions

The decisions behind VNX are written down, not implied. There are 26 Architecture Decision Records under [docs/governance/decisions/](docs/governance/decisions/). The ones that shape the system most:

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

The interactive tmux lane (`scripts/lib/tmux_interactive_dispatch.py`) is the default Claude worker path and is still maturing toward full parity; the headless burst path uses subprocess `claude -p` and is opt-in (it bills API credits after the June 15, 2026 billing change). The receipt format and intelligence layer are uniform across all lanes today; per-lane parity on the full PREPARE/GOVERN envelope is the dispatch-unification work targeted for the 1.x release.

Kimi runs through the Kimi CLI with OAuth. VNX does not call the Moonshot SDK directly for that lane, which keeps attribution and rate-limit behavior in one place.

OpenRouter is the gateway lane for GLM-5.1 from Zhipu (`provider_dispatch.py --provider litellm:zai`, satisfying `zai-via-openrouter-only`). GLM also runs via a claude-CLI harness lane (`glm-harness`, the local litellm proxy in front of OpenRouter), and the single-entry door — now the default lane (ADR-024) — normalizes the plain `litellm:zai` runner to it. Arbitrary OpenAI-compatible models via a generic proxy lane are planned for a later release. Local Ollama is used for the resolver layer and privacy-sensitive work, including Gemma 4 E4B, where no data leaves the machine.

The non-obvious path is DeepSeek through the Claude harness. VNX can run DeepSeek with my own DeepSeek API key plus hardening: `ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater traffic disabled, and MCP off. Operator measurement on Claude Code 2.1.150 on 2026-05-26 showed this path is meaningfully more effective on coding and tool tasks than a bare DeepSeek API call (internal measurement only, not a published benchmark), because the harness adds tool-use loops, context injection, and structured diff output that the raw API does not provide.

Skills reach every lane the same way. Rather than depend on per-CLI mechanisms (`/skill`, `--skills-dir`, runtime tools that differ per provider), VNX composes the skill content into a structured plain-text prompt — role and methodology, then the assignment, then an on-demand index of the skill's reference and script files — and applies it uniformly to claude, kimi, codex, and deepseek workers from one source-of-truth skill folder. A skill edit propagates to all providers with no per-provider sync. See [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md).

### Billing as a consequence, not a goal

VNX treats AI coding tools as interactive CLI workers, not SDK calls. A consequence falls out of that choice. Anthropic's June 15, 2026 billing change moves headless `claude -p` usage to API credits while interactive Claude Code stays on a subscription. Because interactive Claude sessions stay on the subscription rather than API credits, the interactive tmux lane is subscription-preserving, and it is the default Claude worker lane. The headless lane stays opt-in and blocked by default for exactly this reason. This describes current public policy; vendors can change their terms.

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

1.0 released (July 2026): `VERSION` is `1.0.0`, published to PyPI (`pip install vnx-orchestration`) and tagged `v1.0.0`; the operator binary is still required for the full command surface. The single-entry dispatch door is merged and default-ON (ADR-024, 2026-06-24), and the Mission Control central-store cutover completed (2026-06-23). The 1.0.1 future-state reconciliation batch (ADR-007 composite-key `dispatches`, the open-item → track bridge, and its autopilot wiring) has also landed on `main` — see [CHANGELOG.md](CHANGELOG.md) — but `VERSION` stays `1.0.0` until that milestone is cut. Open governance and release items are tracked in [ROADMAP.md](ROADMAP.md), [FEATURE_PLAN.md](FEATURE_PLAN.md), and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

I built this for my own work. Use at your own discretion.

## Credits

Anthropic Claude Code is the foundation. I add receipts, provider routing, tmux dispatch, and worktree isolation around it.
