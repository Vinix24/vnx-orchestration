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
&nbsp;![Audit trail](https://img.shields.io/badge/audit%20trail-15k%2B%20receipts-2ea043)
&nbsp;![Vendor SDK](https://img.shields.io/badge/vendor%20SDK-none-24292f)

**[Docs](docs/) · [Architecture](docs/core/00_VNX_ARCHITECTURE.md) · [State Fabric](docs/core/STATE_FABRIC.md) · [ADRs](docs/governance/decisions/) · [Writing](https://vincentvandeth.nl) · [Changelog](CHANGELOG.md)**

</div>

---

VNX runs AI coding CLI workers in tmux, isolated git worktrees, through review gates, with an append-only NDJSON receipt per dispatch.

It is a local control plane for the AI coding CLIs that already sit on your machine. One orchestrator dispatches work to ephemeral workers; each worker runs in its own git worktree; review gates decide what merges; every dispatch leaves a receipt. VNX drives `claude`, `codex`, `gemini`, `kimi`, and local `ollama` with no vendor SDK. It calls the CLIs as subprocesses and never imports a provider library.

Most agent projects build SDK-native agents. I orchestrate the binaries instead. The difference shows up in the audit trail: I can reconstruct what was dispatched, what was reviewed, what merged, and what each gate cost.

I built this for my own work, across 3,000+ hours of Claude Code and 15,000+ tests. It is open source because the architecture is portable. Source is at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

This is not a security sandbox; it isolates work with tmux sessions and git worktrees. It is not compliance certification; it produces a local, append-only, inspectable audit trail. It is optimized for human-gated coding workflows, not fully autonomous merges.

## What's new in 1.1

- **Horizon planning module.** `vnx horizon` is the named command surface for the future-state layer: `list / show / add / sync / drift / reconcile / close / reopen / plan-gate`. Tracks project from `ROADMAP.yaml`, and a git-grounded reconcile closes them against merged PRs. See the [State Fabric](docs/core/STATE_FABRIC.md).
- **Signed attestation enforcement.** SSH-key-signed, content-keyed, diff-bound attest records with a server-side verify gate and a signed, budgeted, audited override (a recorded deviation, never silent). [ADR-027](docs/governance/decisions/ADR-027-signed-attestation-enforcement.md).
- **Track-linkage + git-grounded backward closure.** `track_id` is validated at the dispatch door and auto-propagated to `track.pr_ref` on merge; a reconcile loop verifies PR merge state via `gh` and closes done tracks under a system actor. No manual bookkeeping.
- **`vnx fabric-audit`.** A Phase-0 store-hygiene check over split-brain stores, per-project ledgers, and receipt hash-chain integrity. [ADR-028](docs/governance/decisions/ADR-028-orchestration-target-architecture.md).
- **Operator-gated self-learning proposal tier.** The receipt stream is mined for recurring failures into prevention-rule proposals for a human to accept. Nothing auto-activates.

Full detail, including the folded-in 1.0.1 future-state batch, is in the [CHANGELOG](CHANGELOG.md). The 1.0 launch surface — pip packaging, provider-agnostic skill injection ([ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md)), the benchmark field-tests harness, the SSRF-safe URL policy, headless review gates, and receipt hash-chain verification — shipped in [1.0.0](CHANGELOG.md).

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

The audit trail is the whole point, so I am honest about maturity. Verified against code and receipts as of 1.1 (July 2026).

**Tier 1 — in production.** Append-only NDJSON receipts with hash-chain verification (`audit_chain`); per-append enforcement is designed as epoch-rotation ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)) and rolling out. Multi-CLI provider hub, no vendor SDK. Review gates (codex + gemini) with deterministic CI as the third gate. Per-worker git worktree isolation with teardown classification (`VNX_ISOLATED_WORKTREE` defaults off). Default interactive tmux worker lane on the subscription (headless `claude -p` is opt-in, blocked by default). Zero-LLM context injection and repo map. Cost tracking per gate. Governed memory (past + current).

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

There are two binaries on purpose. The pip `vnx` covers the essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`). Checkout-only operator commands live behind `./bin/vnx`, including `gate-check` and `new-worktree` — for those, clone the repository and run `pip install -e .` from the checkout.

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

The PAST is append-only NDJSON receipts: a forensic ledger of every dispatch, gate, and merge, with hash-chain verification tooling (`audit_chain`) over it. Per-append chain enforcement is designed as epoch-rotation ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)) and rolling out. It is forensic, not lossy. This is in production now, with 15,000+ receipts in the audit trail behind it.

The CURRENT is `runtime_coordination.db` (SQLite WAL): real-time orchestration state, leases, tracks, and dispatch status that any terminal can read for situational awareness. As of 1.1.0 the `dispatches` table is ADR-007 tenant-scoped on a composite `UNIQUE(dispatch_id, project_id)`, rebuilt in place by a crash-safe migration (#859).

The FUTURE is the track layer and roadmap autopilot: planned features modeled as project-scoped tracks with a dependency graph, which the system can advance under human approval gates. The FUT-1 track schema, DAL, and CLI and the FUT-2 ADR-007 tenant-scoping have shipped, and the tracks layer is now activated for forward-state planning. The 1.1.0 future-state reconciliation keeps it honest automatically: an open-item → track bridge syncs `track_open_items` through the single-writer `tracks.py` primitives, then a reconciler derives each track's status — a track is `done` only when it has no unresolved blocking open-items, every dependency track is done, all of its dispatches are in terminal states, and any linked PR is confirmed merged. Both run inside the autopilot tick, which refuses to advance on a failed sync. The autopilot stays opt-in: it plans, but a human still approves the last step.

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

VNX is not a thin "supports many models" wrapper. The provider layer is governed by [`provider_constraints.yaml`](scripts/lib/providers/provider_constraints.yaml), a machine-readable source of truth for constraints such as `kimi-via-cli-only`, `no-anthropic-sdk`, and `deepseek-harness-subscription-blocked`.

| Provider | How VNX drives it | Billing / constraint |
|---|---|---|
| **claude** | interactive tmux CLI (default) · headless `claude -p` (opt-in) | subscription (tmux) / API credits (headless, blocked by default) |
| **codex** | CLI subprocess | provider sub/credits · review gate + worker |
| **gemini** | CLI subprocess | provider sub/credits · review gate + worker |
| **kimi** | Kimi CLI over OAuth | `kimi-via-cli-only`, no Moonshot SDK |
| **GLM-5.2** (Zhipu) | OpenRouter (`litellm:zai`) or the `glm-harness` proxy | `zai-via-openrouter-only` |
| **any OpenRouter / OpenAI-compatible model** | claude-CLI harness or the local litellm proxy lane | routed generically via harness/proxy |
| **DeepSeek v4** | Claude harness + your own DeepSeek key, hardened | `deepseek-harness-subscription-blocked` (own key OK) |
| **ollama** (local, e.g. Gemma 4 E4B) | local runtime, no network | free/local · resolver + privacy-sensitive work |

No vendor SDK: VNX calls each CLI as a subprocess and never imports a provider library. One source-of-truth skill folder composes into a structured plain-text prompt — role, assignment, then an on-demand index of reference and script files — applied uniformly to every lane, so a single skill edit propagates to all providers with no per-CLI sync ([ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md)).

**The non-obvious lane: DeepSeek v4 through the Claude harness.** With my own DeepSeek key plus hardening (`ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater off, MCP off), DeepSeek v4 runs inside the Claude harness. Operator measurement on Claude Code 2.1.150 (2026-05-26) showed this beats a bare DeepSeek API call on coding and tool tasks (internal measurement, not a published benchmark) — the harness adds tool-use loops, context injection, and structured diff output the raw API lacks.

### Billing as a consequence, not a goal

VNX treats AI coding tools as interactive CLI workers, not SDK calls. A consequence falls out of that choice. Anthropic's June 15, 2026 billing change moves headless `claude -p` usage to API credits while interactive Claude Code stays on a subscription. Because interactive Claude sessions stay on the subscription rather than API credits, the interactive tmux lane is subscription-preserving, and it is the default Claude worker lane. The headless lane stays opt-in and blocked by default for exactly this reason. This describes current public policy; vendors can change their terms.

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

1.2 (July 2026): `VERSION` is `1.2.0` — the second minor since the 1.0.0 PyPI launch (2026-07-02, `pip install vnx-orchestration`, tagged `v1.0.0`). 1.2 adds the central-store authority root-cause fix, ADR-028 decision phases 1–4 (agent-folder fusion + shadow/fast-path/binding judge, all default-off), the `/panel` multi-provider deliberation skill, and a batch of governance hardening (evidence-bound merge gate, signed delegation mandate, ADR-029 hash-chain epoch-rotation) — see [CHANGELOG.md](CHANGELOG.md). 1.1 added the Horizon planning module (`vnx horizon`), signed attestation enforcement (ADR-027), track-linkage + git-grounded backward closure, `vnx fabric-audit`, and an operator-gated self-learning proposal tier. The `v1.2.0` tag and PyPI publish follow this cut. The single-entry dispatch door is merged and default-ON (ADR-024, 2026-06-24), the Mission Control central-store cutover completed (2026-06-23), and the operator binary is still required for the full command surface. Open governance and release items are tracked in [ROADMAP.md](ROADMAP.md), [FEATURE_PLAN.md](FEATURE_PLAN.md), and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

I built this for my own work. Use at your own discretion.

## Credits

Anthropic Claude Code is the foundation. I add receipts, provider routing, tmux dispatch, and worktree isolation around it.
