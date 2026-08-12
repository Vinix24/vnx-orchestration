<div align="center">

# 🧾 VNX Orchestration

### Governance-first runtime for AI coding agents

**Glass-box governance · Local NDJSON receipts · No vendor SDK**

[![PyPI version](https://img.shields.io/pypi/v/vnx-orchestration?color=1f6feb&label=pypi)](https://pypi.org/project/vnx-orchestration/)
&nbsp;![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
&nbsp;[![License: MIT](https://img.shields.io/github/license/Vinix24/vnx-orchestration?color=2ea043)](LICENSE)
&nbsp;[![CI](https://img.shields.io/github/actions/workflow/status/Vinix24/vnx-orchestration/public-ci.yml?branch=main&label=CI)](https://github.com/Vinix24/vnx-orchestration/actions)

[![Stars](https://img.shields.io/github/stars/Vinix24/vnx-orchestration?logo=github)](https://github.com/Vinix24/vnx-orchestration/stargazers)
&nbsp;![Forks](https://img.shields.io/github/forks/Vinix24/vnx-orchestration?logo=github)
&nbsp;![Audit trail](https://img.shields.io/badge/audit%20trail-27k%2B%20receipts-2ea043)
&nbsp;![Vendor SDK](https://img.shields.io/badge/vendor%20SDK-none-24292f)

**[Docs](docs/) · [Architecture](docs/core/00_VNX_ARCHITECTURE.md) · [State Fabric](docs/core/STATE_FABRIC.md) · [ADRs](docs/governance/decisions/) · [Writing](https://vincentvandeth.nl) · [Changelog](CHANGELOG.md)**

</div>

---

VNX runs AI coding CLI workers in tmux, isolated git worktrees, through review gates, with an append-only NDJSON receipt per dispatch.

It is a local control plane for the AI coding CLIs that already sit on your machine. One orchestrator dispatches work to ephemeral workers; each worker runs in its own git worktree; review gates decide what merges; every dispatch leaves a receipt. VNX drives `claude`, `codex`, `gemini`, `kimi`, and local `ollama` with no vendor SDK. It calls the CLIs as subprocesses and never imports a provider library.

Most agent projects build SDK-native agents. I orchestrate the binaries instead. The difference shows up in the audit trail: I can reconstruct what was dispatched, what was reviewed, what merged, and what each gate cost.

I built this for my own work, across 3,000+ hours of Claude Code and 18,816 test functions across 1,008 test files. It is open source because the architecture is portable. Source is at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

This is not a security sandbox; it isolates work with tmux sessions and git worktrees. It is not compliance certification; it produces a local, append-only, inspectable audit trail. It is optimized for human-gated coding workflows, not fully autonomous merges.

## What's new in 1.4

Six patch releases (1.4.0 through 1.4.5, July 31 to August 7, 2026), all documented in the [CHANGELOG](CHANGELOG.md). The headlines:

- **Worker-provider free choice shipped end-to-end** (1.4.0): a `ModelPin` floor-vs-default contract replaces the old hard pin, so a dispatch can choose its provider instead of inheriting a fleet default.
- **Fleet-wide plan-gate outage fixed** (1.4.1, #1280): a central install resolved the plan-gate's data directory from the module's own (read-only, pinned) install path instead of the project's central store, so every plan-gate on the machine died with `PermissionError` across all five provider lanes. Now resolved from the central store for the active `project_id`.
- **Reconcile chain and open-item cleanup** (1.4.2): open items went from 773 to 53 across eight triage rounds, and the auto-close reconciler's 31-hour silent outage on a bare `gh` lookup is fixed.
- **CI measured 2% of the suite** (1.4.3): Profile A ran 18 of 933 test files; it now runs the full suite, and two tests that could write into the live production store are fail-closed.
- **ReceiptV2 schema + measured token capture** (1.4.4): real `token_usage` harvested from claude-harness transcripts and the kimi session log, replacing modeled estimates.
- **Release-publish guard + `vnx horizon link-pr`/`set-lane-hint` on the pip CLI** (1.4.5).

Since 1.4.5, three defaults have flipped by direct operator decision, all in this repository's history: the headless `claude -p` lane opened by default (#1455), per-dispatch git-worktree isolation became unconditional for the provider/subprocess lanes (#1449), and `vnx gate-check` landed on the pip CLI (#1462) — see "What works today vs what is opt-in" and "Your first dispatch" below for what that means in practice.

Full history for every minor and patch back to 0.1.0 is in the [CHANGELOG](CHANGELOG.md).

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

The audit trail is the whole point, so I am honest about maturity. Verified against code and receipts on 2026-08-12 (version 1.4.5, unreleased fixes on top).

**Tier 1 — in production.** Append-only NDJSON receipts with hash-chain verification (`audit_chain`); per-append enforcement is designed as epoch-rotation ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)) and rolling out. Multi-CLI provider hub, no vendor SDK. Review gates (codex + gemini) with deterministic CI as the third gate. Per-worker git worktree isolation, default-on since #1449 — unconditional for the provider and subprocess lanes, on by default for the tmux lane (`--no-isolated-worktree` opts out), with teardown classification for clean, committed, or dirty state. Default interactive tmux worker lane on the subscription; headless `claude -p` opened by default since #1455 (both lanes bill the subscription, not API credits — see "Billing" below). Zero-LLM context injection and repo map. Cost tracking per gate. Governed memory (past + current).

**Tier 2 — shipped, opt-in, burning in.** Smart routing (`VNX_AUTO_ROUTE`), elastic worker pool (`bin/vnx pool`), track layer + roadmap autopilot (`VNX_ROADMAP_AUTOPILOT=1`), auto-dream consolidation, and an operator-gated self-learning proposal tier that mines the receipt stream for recurring failures into `pending_rules.json` for a human to accept (G-L1; nothing auto-activates). These default off and are not yet proven at the Tier 1 bar. The single-entry dispatch door (`dispatch_cli.py`) is the exception: default-ON since 2026-06-24 (ADR-024), normalizing GLM to the harness lane and running a phantom-guard that rejects evidence-free GATE-GREEN receipts — recent enough that I still hold it here. Roll back per terminal with `VNX_DISPATCH_LEGACY=1`.

**Tier 3 — designed, not built.** Parallel multi-track execution, wave scheduler, merge lock, file-scope derivation. Architecture, not a feature — worktree isolation is not yet guaranteed race-free under parallel dispatch.

## Install

```bash
pip install vnx-orchestration
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

There are two binaries on purpose. The pip `vnx` covers the essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`), and as of `#1462` also `gate-check` — the same deterministic pre-merge GO/HOLD check the fabric's own CI runs, now usable without a checkout. Checkout-only operator commands still live behind `./bin/vnx` — `new-worktree` and the rest of the operator surface (worktree lifecycle, snapshot/restore, staging) — for those, clone the repository and run `pip install -e .` from the checkout.

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

Claude has two lanes. The default worker lane is the interactive tmux lane, which runs on the subscription and is what `dispatch.sh` selects unless a dispatch opts out. The headless `claude -p` subprocess lane is the burst alternative. It was opt-in and blocked by default until an operator directive opened it on 2026-08-11 (#1455, `lane_safety.headless_block.enabled: false` in [`routing_policy.yaml`](scripts/lib/providers/routing_policy.yaml)). Both lanes bill the same Claude subscription, not API credits. A `cost=$0.0000` receipt confirms either lane; billing only changes if the environment carries its own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`, which routes outside both lanes entirely. Headless is usable but not yet governed: it ignores `isolation=worktree`, and the report gate has been seen to miss a report with all four mandatory headings absent (see [DISPATCH_RULES.md](docs/core/DISPATCH_RULES.md)). `VNX_OVERRIDE_CLAUDE_HEADLESS=1` re-lifts the block if it is ever re-enabled.

The leaseless single-shot tmux dispatch lane lives in `scripts/lib/tmux_interactive_dispatch.py`. Per-worker git worktree isolation lives in `scripts/lib/tmux_worktree.py`, including teardown classification for clean, committed or pushed, and dirty worktrees. Isolation is default-on since #1449 (`--no-isolated-worktree` opts out on the tmux lane; unconditional on the provider and subprocess lanes); isolation guarantees still vary by lane, and the headless lane ignores it (see above).

### Governed memory: past, current, future

Memory is the unsolved problem in agentic AI. Most systems bolt a vector store onto a stateless model and call it memory. I treat memory as a governed state machine with three tenses, each with its own store and its own audit guarantees.

The PAST is append-only NDJSON receipts: a forensic ledger of every dispatch, gate, and merge, with hash-chain verification tooling (`audit_chain`) over it. Per-append chain enforcement is designed as epoch-rotation ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)) and rolling out. It is forensic, not lossy. This is in production now, with 15,000+ receipts in the audit trail behind it.

The CURRENT is `runtime_coordination.db` (SQLite WAL): real-time orchestration state, leases, tracks, and dispatch status that any terminal can read for situational awareness. As of 1.1.0 the `dispatches` table is ADR-007 tenant-scoped on a composite `UNIQUE(dispatch_id, project_id)`, rebuilt in place by a crash-safe migration (#859).

The FUTURE is the track layer and roadmap autopilot: planned features modeled as project-scoped tracks with a dependency graph, which the system can advance under human approval gates. The FUT-1 track schema, DAL, and CLI and the FUT-2 ADR-007 tenant-scoping have shipped, and the tracks layer is now activated for forward-state planning. The 1.1.0 future-state reconciliation keeps it honest automatically: an open-item → track bridge syncs `track_open_items` through the single-writer `tracks.py` primitives, then a reconciler derives each track's status — a track is `done` only when it has no unresolved blocking open-items, every dependency track is done, all of its dispatches are in terminal states, and any linked PR is confirmed merged. Both run inside the autopilot tick, which refuses to advance on a failed sync. The autopilot stays opt-in: it plans, but a human still approves the last step.

A learning layer (`quality_intelligence.db`) consolidates the past into patterns and antipatterns that get injected into future dispatch context. The consolidation loop (auto-dream) is shipped and opt-in. It is burning in, not yet on by default.

The point is not that the AI remembers. The point is that what it remembers is governed: every memory has a receipt, every plan has a gate, and a human owns the boundary.

## Architecture decisions

The decisions behind VNX are written down, not implied. There are 34 Architecture Decision Records under [docs/governance/decisions/](docs/governance/decisions/). The ones that shape the system most:

- [ADR-005](docs/governance/decisions/ADR-005-ndjson-audit-ledger-primary.md): append-only NDJSON ledger as the primary observability surface
- [ADR-006](docs/governance/decisions/ADR-006-staging-promote-human-gate.md): staging then promote, with a mandatory human approval gate
- [ADR-008](docs/governance/decisions/ADR-008-dual-llm-adversarial-review.md): dual-LLM adversarial review (codex plus gemini) bound by a contract hash
- [ADR-011](docs/governance/decisions/ADR-011-manager-worker-hierarchy.md): manager plus worker hierarchy with explicit depth, not depth-1 subagents
- [ADR-012](docs/governance/decisions/ADR-012-hybrid-interactive-headless.md): hybrid interactive and headless execution, no retire-interactive
- [ADR-014](docs/governance/decisions/ADR-014-autonomous-chain-dispatch.md): autonomous mode is pre-approved chain dispatch, never gate bypass
- [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md): one structured plain-text skill prompt for every provider lane, no per-CLI mechanisms
- [ADR-030](docs/governance/decisions/ADR-030-plan-first-gate-enforcement.md): the plan-first gate enforced at the dispatch door and the merge gate, advisory-first — plan before work, structurally

For how the architecture got here, [docs/manifesto/EVOLUTION_TIMELINE.md](docs/manifesto/EVOLUTION_TIMELINE.md) reconstructs the technical evolution over roughly six months, including the private incubation provenance. The public repository is the extraction, hardening, and packaging of work that started inside a private product.

## Multi-provider architecture

VNX is not a thin "supports many models" wrapper. The provider layer is governed by [`provider_constraints.yaml`](scripts/lib/providers/provider_constraints.yaml), a machine-readable source of truth for constraints such as `kimi-via-cli-only`, `no-anthropic-sdk`, and `deepseek-harness-subscription-blocked`.

| Provider | How VNX drives it | Billing / constraint |
|---|---|---|
| **claude** | interactive tmux CLI (default) · headless `claude -p` (open by default since #1455) | subscription, both lanes (own `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` routes outside both) |
| **codex** | CLI subprocess | provider sub/credits · review gate + worker |
| **gemini** | CLI subprocess | provider sub/credits · review gate + worker |
| **kimi** | Kimi CLI over OAuth | `kimi-via-cli-only`, no Moonshot SDK |
| **GLM-5.2** (Zhipu) | OpenRouter (`litellm:zai`) or the `glm-harness` proxy | `zai-via-openrouter-only` |
| **any OpenRouter / OpenAI-compatible model** | claude-CLI harness or the local litellm proxy lane | routed generically via harness/proxy |
| **DeepSeek v4** | Claude harness + your own DeepSeek key, hardened | `deepseek-harness-subscription-blocked` (own key OK) |
| **ollama** (local, e.g. Gemma 4 E4B) | local runtime, no network | free/local · resolver + privacy-sensitive work |

No vendor SDK: VNX calls each CLI as a subprocess and never imports a provider library. One source-of-truth skill folder composes into a structured plain-text prompt — role, assignment, then an on-demand index of reference and script files — applied uniformly to every lane, so a single skill edit propagates to all providers with no per-CLI sync ([ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md)).

**The non-obvious lane: DeepSeek v4 through the Claude harness.** With my own DeepSeek key plus hardening (`ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater off, MCP off), DeepSeek v4 runs inside the Claude harness. Operator measurement on Claude Code 2.1.150 (2026-05-26) showed this beats a bare DeepSeek API call on coding and tool tasks (internal measurement, not a published benchmark) — the harness adds tool-use loops, context injection, and structured diff output the raw API lacks.

### Billing follows auth, not the lane

VNX treats AI coding tools as interactive CLI workers, not SDK calls, and that choice raised a real billing question: does the headless lane cost more than the interactive one. I measured it instead of assuming. Both the interactive tmux lane and the headless `claude -p` lane run the same CLI under the same OAuth session, and both post `cost=$0.0000` receipts, confirmed 2026-08-11 via auth state (no `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` in the environment, keychain `subscriptionType: max`). The billing boundary is the auth method, not which lane dispatched the work. Set your own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` and that changes, since both lanes then bill through that key instead of the subscription.

An earlier version of this README assumed a June 15, 2026 Anthropic billing change that would push headless usage to API credits by default. That change was never carried out. The headless lane was opened by direct operator decision (#1455) once subscription billing was confirmed by measurement, not by reading vendor announcements. This describes what this repository measures today; Anthropic's billing terms are the vendor's to change, and I have verified nothing beyond what a `cost=$0.0000` receipt tells me.

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

1.4.6 (August 12, 2026): `VERSION` is `1.4.6`, tagged today (`v1.4.6`) and rolled out to the central version store. This patch release hardens the dispatch lanes and worker receipt semantics: the claude headless lane is open by default (#1455), tmux concurrency is raised to 5, worktree isolation is default-on and salvages untracked non-gitignored files on teardown, the heartbeat silence threshold moved from 600s to 1800s with failure-shaped kills, `gate-check` is exposed on the pip CLI (#1462), and the pre-merge gate can no longer report GO on unverified checks. Full entry, and every release back to 0.1.0, in [CHANGELOG.md](CHANGELOG.md). Open governance and release items are tracked in [ROADMAP.md](ROADMAP.md), [FEATURE_PLAN.md](FEATURE_PLAN.md), and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

I built this for my own work. Use at your own discretion.

## Credits

Anthropic Claude Code is the foundation. I add receipts, provider routing, tmux dispatch, and worktree isolation around it.
