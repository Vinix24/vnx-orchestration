# VNX Manifesto

VNX is a governance-first architecture for multi-agent AI workflows.

Core idea: orchestration is not enough. If you cannot audit decisions, enforce gates, and reconstruct what happened after a failure, you cannot trust multi-agent execution at scale.

This folder is the public narrative: architecture, method, limits, and roadmap.

## In Action

![VNX orchestration in action: T0 manages parallel worker tracks](assets/screenshots/s1-orchestration-in-action.png)

_Parallel execution with explicit manager decisions (T0), not uncontrolled agent fan-out._

![Quality advisory warning incident with follow-up decision](assets/screenshots/s5-quality-advisory.png)

_Completion is evidence-based: warnings are surfaced and tracked before closure._

## What VNX Solves

- Turns agent activity into an auditable receipt ledger (`NDJSON`), not opaque chat state.
- Enforces human approval gates between dispatch proposal and execution.
- Observes providers through watchers, not hooks (hooks optional). Provider lanes are claude, codex, kimi, deepseek and glm; a live T0 on a non-Claude harness is not proven.
- Adds async quality advisories (file and function size, lint, dead code, missing test changes) before final closure.
- Keeps a clear chain-of-custody from dispatch to deliverable.

## What Readers Can Verify In 5 Minutes

- Governance gates exist between proposal and execution: `vnx dispatch stage` writes a staged bundle and never fires, and a human fires it (ADR-006).
- Receipts form an auditable event trail (`dispatch`, `ack`, `completion`, `quality advisory`). A hash chain over the ledger is available per project and is off by default (ADR-023), so the ledger is append-only by design but not tamper-evident unless a project turns the chain on.
- Every dispatch funnels through one entry point (`vnx dispatch`), spawning an ephemeral per-dispatch worker rather than a fixed terminal grid. The terminal-pinned layout stays only as the rollback route (`docs/manifesto/LIMITATIONS.md`).
- Quality checks run as async advisories and can force structured follow-up work.
- Provider coupling is reduced through watcher-based observation.
- Learning and dreaming only propose; the decision is a human one (ADR-065, ADR-072).

## Who This Is For

- Engineers running Claude, Codex, Kimi, DeepSeek or GLM lanes across ephemeral dispatch workers.
- Builders who need governance and traceability, not just “agent swarms”.
- Teams experimenting with local-first, file-based orchestration patterns.

## What Is On Main, Not Yet Released

`VERSION` is 1.8.0. Review seats, the takeover chain, the provider switch, the plan-gate with one seat, open outcomes, T0 guard and rotation are in it. After it, these landed on `origin/main` (c41411fea, 2026-11-12). The canonical file owns the mechanism.

- **Merge door permit.** A sixth preflight, `door`, publishes `vnx-gate/door` for one exact head and pins the merge to it. ADR-061.
- **One permission policy.** One digest over the role boundary (`scripts/lib/permission_policy.py`). The binding to every dispatch is the decision of ADR-057.
- **Learning proposes.** A fixed failure-class table and a proposal queue, shadow by default, nothing activated. ADR-065.
- **Operator steering.** Classes of my own corrections and a proposal store with a decision record. ADR-072.
- **Dream liveness probe.** A read-only report on whether a night of consolidation produced output. ADR-066.
- **Intelligence effect measurement.** A hash-assigned A/B window, off by default. ADR-069.

Learning and dreaming propose. A human decides.

## Quick Start (Docs-First)

If this is your first read, use this order:

1. `ARCHITECTURE.md` — full system model and why it exists.
2. `ARCHITECTURAL_DECISIONS.md` — key tradeoffs and constraints.
3. `OPEN_METHOD.md` — transparency on build process and provenance.
4. `LIMITATIONS.md` — tested scope and known boundaries.
5. `ROADMAP.md`: what is done, what is on main, what is 2.0.

## Clone

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration/docs/manifesto
```

## Visual Proof Pack

Screenshots are managed in one place:

- `SCREENSHOTS.md` — required captures, naming, and placement.
- `assets/screenshots/` — screenshot files.
- `assets/diagrams/` — diagram exports.

## Document Index

| File | Description |
|------|-------------|
| `ARCHITECTURE.md` | Glass Box Governance architecture (full narrative) |
| `ARCHITECTURE_DIAGRAM.mermaid` | Mermaid source for architecture visual |
| `ARCHITECTURAL_DECISIONS.md` | Design decisions, tradeoffs and the index of all ADRs |
| `OPEN_METHOD.md` | Build methodology and provenance transparency |
| `EVOLUTION_TIMELINE.md` | Condensed technical evolution |
| `ROADMAP.md` | Done, on main unreleased, and 2.0 |
| `LIMITATIONS.md` | Explicit scope and constraints |
| `SCREENSHOTS.md` | Screenshot pack and capture guidelines |

## Current Status

- Verified against `origin/main` at c41411fea on 2026-11-12.
- Validated in local workflows with ephemeral per-dispatch workers.
- Governance primitives are in place (receipts, review seats, gates, advisories).
- What is released and what is only on main: `CHANGELOG.md` (`[Unreleased]` is main, not yet released).
- Public history starts at repo separation from a private incubator project.

For implementation detail, see the main repository scripts and docs outside this manifesto folder.

## Implementation Lives Here

- Runtime scripts and orchestration logic: `scripts/`
- Main CLI entrypoint: `bin/vnx`
- Terminal role files and skills: `.claude/terminals/` and `.claude/skills/`
- Local runtime state (queues, receipts, snapshots, logs): `.vnx-data/`
- Dashboard assets and serving logic: `dashboard/`

## Contributing / Collaboration

- Current maintainer focus: architecture, governance model, and practical reliability in local workflows.
- High-value contributions: reliability improvements, cross-provider adapters, test coverage, and docs quality.
- Especially welcome: Rust/Go-oriented contributors for a future production-grade engine.
- Collaboration style: small, reviewable changes with clear evidence (tests, logs, and behavior proof).
