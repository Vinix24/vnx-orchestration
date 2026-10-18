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
- Stays model/provider-agnostic through watcher-based observation (hooks optional).
- Adds async quality advisories (file and function size, lint, dead code, missing test changes) before final closure.
- Keeps a clear chain-of-custody from dispatch to deliverable.

## What Readers Can Verify In 5 Minutes

- Governance gates exist between proposal and execution: `vnx dispatch stage` writes a staged bundle and never fires, and a human fires it (ADR-006).
- Receipts form an auditable event trail (`dispatch`, `ack`, `completion`, `quality advisory`). A hash chain over the ledger is available per project and is off by default (ADR-023), so the ledger is append-only by design but not tamper-evident unless a project turns the chain on.
- Every dispatch funnels through one entry point (`vnx dispatch`), spawning an ephemeral per-dispatch worker rather than a fixed terminal grid — the old fixed T0/T1/T2/T3 layout is opt-in (`VNX_ADAPTER_T{n}=subprocess`), not the default.
- Quality checks run as async advisories and can force structured follow-up work.
- Provider coupling is reduced through watcher-based observation.

## Who This Is For

- Engineers running Claude, Codex, Kimi, DeepSeek or GLM lanes across ephemeral dispatch workers.
- Builders who need governance and traceability, not just “agent swarms”.
- Teams experimenting with local-first, file-based orchestration patterns.

## What Changed Recently (on main, not yet released)

Each item is summarised in one line. The canonical file owns the mechanism.

- **Review seats.** A pull request needs one reviewer per seat of the review stack, and any rejection blocks. `docs/core/00_VNX_ARCHITECTURE.md` (Review Seats), ADR-008, ADR-037.
- **Takeover chain.** A seat whose reviewer is out passes to the next gate: `codex_gate`, `kimi_gate`, `deepseek_gate`. `glm_gate` left the default chain. `docs/core/PROVIDER_LANES.md` (Takeover chain).
- **Provider switch.** `vnx providers list` shows one switch per provider family. The write verbs refuse until every launch function calls `provider_switch.require`; the enforcement line of `vnx providers list` says how many do not yet. `docs/core/PROVIDER_LANES.md` (Provider switch).
- **Plan-gate with one seat.** One seat reads the plan and a second reads it only on BLOCK; a PASS leaves a hash-chained record. `docs/core/HORIZON_LIFECYCLE.md`.
- **Open outcomes.** A dispatch is finished only when a T0 records accept or reject. `docs/core/DISPATCH_RULES.md` section 13.
- **T0 guard and rotation.** `vnx guard install` registers the context guard per Claude Code account, and a spawner hands a T0 over to a fresh one. `docs/operations/T0_CONTEXT_ROTATION.md`.
- **Context-rotation plugin.** A Claude Code plugin in `packages/claude-code-toolkit/context-rotation/` that writes a handover and restores it after `/clear`. Its README is the reference.
- **Changelog fragments.** Each PR adds one file under `changelog.d/`. `docs/core/LOCKS_AND_RELEASES.md`.

## Quick Start (Docs-First)

If this is your first read, use this order:

1. `ARCHITECTURE.md` — full system model and why it exists.
2. `ARCHITECTURAL_DECISIONS.md` — key tradeoffs and constraints.
3. `OPEN_METHOD.md` — transparency on build process and provenance.
4. `LIMITATIONS.md` — tested scope and known boundaries.
5. `ROADMAP.md` — what is next.

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
| `ARCHITECTURAL_DECISIONS.md` | Design decisions and tradeoffs |
| `OPEN_METHOD.md` | Build methodology and provenance transparency |
| `EVOLUTION_TIMELINE.md` | Condensed technical evolution |
| `ROADMAP.md` | Planned extensions and future tracks |
| `LIMITATIONS.md` | Explicit scope and constraints |
| `SCREENSHOTS.md` | Screenshot pack and capture guidelines |

## Current Status

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
