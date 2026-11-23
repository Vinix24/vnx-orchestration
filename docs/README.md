# VNX Documentation

`DOCS_INDEX.md` is the index of every active doc and the one place to start.
[docs/DOCS_INDEX.md](DOCS_INDEX.md)

## Writing

I wrote the architecture down as I built it. The full series is on [vincentvandeth.nl](https://vincentvandeth.nl). Start here.

**Governance and trust**
- [Glass-box governance for multi-agent AI](https://vincentvandeth.nl/blog/glass-box-governance-multi-agent-ai)
- [Governance scoring: agent trust and autonomy](https://vincentvandeth.nl/blog/governance-scoring-agent-trust-autonomy)
- [Autonomous agents do not exist](https://vincentvandeth.nl/blog/autonome-ai-agents-bestaan-niet) (Dutch)
- [ISA 62443: AI governance and industrial safety](https://vincentvandeth.nl/blog/isa-62443-ai-governance-industrial-safety)

**Receipts, audit, traceability**
- [The NDJSON receipt ledger for AI audit trails](https://vincentvandeth.nl/blog/ndjson-receipt-ledger-ai-audit-trail)
- [Traceability architecture: the AI decision receipt](https://vincentvandeth.nl/blog/traceability-architecture-ai-decision-receipt)
- [The external watcher pattern for AI agent observation](https://vincentvandeth.nl/blog/external-watcher-pattern-ai-agent-observation)

**Orchestration architecture**
- [What is AI orchestration: terminal dispatch](https://vincentvandeth.nl/blog/wat-is-ai-orchestration-terminal-dispatch) (Dutch)
- [Architecture beats models in AI agent dispatches](https://vincentvandeth.nl/blog/architecture-beats-models-ai-agent-dispatches)
- [Multi-model AI orchestration from a single terminal](https://vincentvandeth.nl/blog/multi-model-ai-orchestration-single-terminal)
- [Why no subagents in AI orchestration](https://vincentvandeth.nl/blog/waarom-geen-subagents-ai-orchestration) (Dutch)
- [Routing is not orchestration](https://vincentvandeth.nl/blog/routing-not-orchestration-openclaw-governance)

**Cost, context, production**
- [The real cost of AI agents in production](https://vincentvandeth.nl/blog/real-cost-ai-agents-production)
- [Zero-LLM context injection with VNX intelligence](https://vincentvandeth.nl/blog/zero-llm-context-injection-vnx-intelligence)
- [Context rotation at scale](https://vincentvandeth.nl/blog/context-rotation-scale-vnx-implementation)
- [Async quality gates for AI agent workflows](https://vincentvandeth.nl/blog/async-quality-gates-ai-agent-workflows)

## Context rotation for any Claude Code session

`packages/claude-code-toolkit/context-rotation/` is a Claude Code plugin, separate from the VNX runtime and released in the public toolkit repository ([Vinix24/claude-code-toolkit](https://github.com/Vinix24/claude-code-toolkit)). It measures how full the context is, asks Claude for a handover note at set points, checks that note, and gives it to the fresh session after `/clear`. Install, settings and the hook layout are in the [plugin README](../packages/claude-code-toolkit/context-rotation/README.md). The T0 rotation VNX enforces for its own orchestrator is a different mechanism: [T0_CONTEXT_ROTATION.md](operations/T0_CONTEXT_ROTATION.md).

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.
