# VNX Framework Roadmap Proposal

**Author**: Research & Strategy — April 2026  
**Question**: What would it take for VNX to reach 1,000+ GitHub stars and become a recognized framework in the governance-first agent orchestration niche?  
**Audience**: Vincent van Deth (solo author); future contributors; anyone evaluating VNX as an open-source project bet.

---

## Vision Statement

**VNX is the governance-first multi-agent orchestration framework** — the only system where every agent action is audited, approved, and gated by default.

Not the fastest framework. Not the one with the most integrations. The one where the answer to "what did that agent do, why, and who approved it?" is always answerable — from a grep-able, append-only NDJSON ledger that is the canonical source of truth for every dispatch that ever ran.

Every other framework made agents more autonomous. VNX made humans more informed.

---

## Current State Assessment (v0.9.0, April 2026)

### What exists today

- **Headless workers**: T1/T2/T3 run as `claude -p` subprocesses — no tmux required for execution.
- **A/B tested methodology**: F40 + F42 showed headless vs. interactive produces functionally equivalent output (4% LOC delta, identical file structures, 0 human interventions in either track).
- **Autonomous loop**: T0 decision cycle fires without human intervention per cycle; pre-filter handles ~70% of decisions without LLM invocation.
- **Governance profiles**: `coding-full` and `business-light` — configurable review depth per agent domain.
- **3 agent templates**: coding worker, blog writer, LinkedIn writer.
- **210+ PRs** of production hardening across 9 months of daily use.
- **1,466+ dispatches** processed through the governance pipeline.
- **Interactive context rotation**: automatic handover with skill recovery at 65% context pressure (hooks-based, production-validated).
- **Append-only NDJSON ledger**: per-decision provenance with cost, git ref, terminal, timestamp.
- **Deterministic quality gates**: gitleaks, vulture, radon, file-size checks — LLM-invisible, cannot be reasoned past.

### What's missing for public adoption

- **Single-command install**: no `pip install vnx` or `npx vnx init`. Setup still requires cloning and manual folder configuration.
- **Headless context rotation**: interactive rotation is production-grade; headless workers have no automatic context management yet.
- **Battle-tested headless mode**: 2 weeks old as of April 2026. 2 features validated, 1 operator. Needs an extended burn-in before recommending to others.
- **Simplified onboarding**: the dispatch/receipt contract is powerful but has a learning curve. No 5-minute quickstart exists.
- **Documentation polish**: internal documentation is thorough; external-facing docs optimized for a first-time developer don't exist.
- **Community**: no Discord, no forum, no third-party tutorials. Every other framework in the competitive landscape has this.

---

## Phase 1: Foundation Hardening (4–6 weeks)

**Goal**: Make VNX installable, runnable, and understandable by an external developer in under 30 minutes.

### 1.1 Single-command setup

Either `pip install vnx` (Python entry point that scaffolds project config) or `npx vnx init` (TypeScript-friendly alternative). Target: clone-free bootstrap that creates the agent folder structure, writes minimal CLAUDE.md templates, and emits a `vnx status` that shows whether the system is ready to dispatch.

The current setup is: clone repo → create agent folders with CLAUDE.md + config.yaml → dispatch. This is achievable in 10 minutes for someone who reads the docs. It needs to be achievable in 5 minutes for someone who doesn't.

### 1.2 Remove tmux dependency for basic usage

tmux should be optional, not required. The headless path already achieves this for workers. T0 interactive operation still assumes a tmux session. Document and test a fully headless T0 path (T0 as a subprocess too, or as a simple cron-triggered script).

**Why this matters**: every developer who hits "install tmux" as step one loses 20% of potential adopters immediately.

### 1.3 Headless context rotation

Implement token tracking from `task_progress` events in stream-json output. The data is there: `usage.total_tokens` per event. Build a threshold check (e.g., 65% of estimated context limit) that triggers a graceful handover: write handover artifact, terminate subprocess, inject continuation on next cycle.

This closes the most significant gap between interactive and headless mode. It also makes VNX's context rotation story — already the most sophisticated of any framework evaluated — complete across both execution modes.

### 1.4 5-minute quickstart

A single page: install → first dispatch → observe receipt → verify gate passed. No theory, no architecture overview, no governance philosophy. Just: does it run?

Include a dummy dispatch (a simple file write) so a new user can see the full lifecycle — dispatch promotion, worker execution, receipt generation, gate result — without needing a real feature to test.

### 1.5 Headless burn-in (2-week sprint)

Run 10+ features through headless mode end-to-end before declaring it production-ready. Document failure modes found. Fix them. The A/B test validated the concept; burn-in validates the edge cases.

**Gate for Phase 2**: Do not proceed until headless mode has processed at least 10 features without operator-reported failures.

---

## Phase 2: Framework Features (6–8 weeks)

**Goal**: Make VNX competitive with CrewAI and LangGraph on the features that matter to governance-first adopters.

### 2.1 Hierarchical multi-manager

Implement the architecture described in `docs/research/HIERARCHICAL_MANAGER_ARCHITECTURE.md`: multiple T0 instances coordinated by a meta-orchestrator. This enables:
- Parallel feature tracks that each have their own governance cycle.
- A/B testing at the architecture level (two T0s, two strategies, one comparative report).
- Business domain managers (content, marketing) running parallel to coding managers.

This is the feature that most directly addresses the "solo developer project" risk — a hierarchical system can be operated by a small team, not just one person.

### 2.2 Multi-channel gateways

Currently: Telegram → Claude Code is the only external trigger path. Add:
- **Slack** (most common in development teams)
- **Webhook** (generic, enables n8n, Zapier, GitHub Actions integration)
- **WhatsApp** (lower priority; relevant for solo practitioners who already use VNX via Telegram)

Each gateway needs: receive trigger → validate → promote dispatch → report back to channel. This is a thin adapter layer on top of the existing dispatch mechanism.

### 2.3 Cloud deployment guide

Document a reference deployment on AWS (EFS for shared filesystem) and GCP (Filestore or GCS FUSE). The filesystem-based architecture is already cloud-compatible — this is a configuration and validation task.

Include a `docker-compose` reference that runs T0 + workers as containers against a shared volume. This makes VNX accessible to teams who want containerized deployment without a distributed message broker.

### 2.4 Model-agnostic manager testing

Run T0 orchestration with GPT-5 and Gemini 2.5 Pro using the headless path (subprocess reduces Claude Code hook dependency). Document:
- Which decisions it gets right vs. the Opus baseline.
- Where instruction-following degrades.
- Whether the pre-filter layer compensates for model differences on deterministic decisions.

This is important for community adoption: developers invested in OpenAI or Google tooling need to know VNX is usable with their preferred provider.

### 2.5 Codex worker integration

Codex is known for strict instruction-following — a positive signal for the worker role (precise execution of dispatch contracts). Test T1/T2/T3 with Codex as the worker model. Document results. If successful, publish as a "Codex workers + VNX governance" integration guide — this is a story that will resonate with the Codex community.

---

## Phase 3: Community & Adoption (ongoing)

**Goal**: Build the external surface area that makes 1,000 stars achievable.

### 3.1 Blog series on vincentvandeth.nl

The production data from 9 months of daily VNX use is genuinely publishable. Proposed posts:

1. **"I ran 1,466 dispatches through an audited agent system. Here's what I learned."** — The quantitative story. Dispatch volume, failure rate, gate catch rate, cost per feature.
2. **"A/B testing AI coding agents: headless vs. interactive"** — The F40/F42 methodology and results. This is novel. Nobody else has published this.
3. **"Why I built a governance system instead of a productivity tool"** — The philosophy piece. Will resonate with the "AI safety" adjacent community.
4. **"Context rotation vs. context compression: two different answers to the same problem"** — Technical comparison of VNX handover vs. Mastra Observational Memory.
5. **"Autonomous agents with human gates: the case against 'agentic mode'"** — The architecture argument. Why full autonomy is the wrong goal.

### 3.2 Distribution strategy

- **Hacker News**: Show HN post when Phase 1 is complete. Frame as "I processed 1,466 agent dispatches through a governance system — here's the architecture." Technical + data-driven = HN-compatible.
- **Reddit r/MachineLearning** and **r/LocalLLaMA**: Focus on the A/B testing methodology and open-source governance architecture.
- **Twitter/X AI community**: Short threads on specific findings (context rotation implementation, pre-filter architecture, governance profiles). Tag relevant accounts (LangChain, CrewAI authors, AI engineering community).
- **GitHub Awesome Lists**: Submit VNX to `awesome-agents`, `awesome-llm`, `awesome-claude` lists after Phase 1 is complete.

### 3.3 Demo videos

- **5-minute headless execution demo**: new dispatch → worker runs headlessly → receipt generated → gate passes → merge. No commentary on architecture, just showing it works.
- **Dashboard walkthrough**: what the local dashboard shows, what an NDJSON ledger entry looks like, what a gate failure looks like.
- **Context rotation in action**: watch a context warning fire, handover artifact get written, worker continue with full context recovery. This is visually compelling and technically differentiating.

### 3.4 Agent marketplace concept

Create a `vnx-agents` repository (or directory) of community-contributed agent CLAUDE.md templates. Starting inventory:
- Code reviewer (T3 template)
- Security auditor (T3 variant)
- Technical writer
- Data analyst
- Blog writer (existing)
- LinkedIn writer (existing)

Templates lower the barrier to a working first agent significantly — a new user can fork a template rather than writing a CLAUDE.md from scratch.

### 3.5 Conference talks

- **AI Engineer Summit**: "Governance-first agent orchestration — lessons from 1,466 production dispatches." Technical audience, will appreciate the architecture and data.
- **PyCon**: More accessible talk on the A/B testing methodology and what it revealed about LLM consistency.
- **DevOpsDays**: The governance angle has direct relevance to teams evaluating AI agents for production use.

---

## Realistic Star Projection

Based on comparable projects with available data:

| Project | Stars | Why |
|---------|-------|-----|
| Claude Squad | ~5,600 | Simple, good README, active community, viral HN launch |
| Agency Swarm | ~3,000 | Niche Python framework, organizational model, YouTube tutorials |
| MetaGPT | ~50,000 | Academic pedigree, ICLR oral, media coverage |
| LangGraph | ~15,000 | LangChain ecosystem, enterprise adoption, LangSmith integration |

**VNX realistic target: 1,000 stars within 6 months after Phase 1 completion**, conditional on:
- A compelling HN Show HN launch (the A/B test data + 1,466 dispatches story is strong)
- Phase 1 complete (single-command install, headless burn-in done)
- At least 2 blog posts published with real data
- README overhauled for external audiences (not internal developer notes)

1,000 stars is the threshold where organic discovery starts working (GitHub trending, Awesome lists, word-of-mouth). Below that, every star requires active distribution. Above that, some fraction comes passively.

---

## Why 1,000 Stars Is Achievable

**Unique value proposition that no competitor offers.** Governance-first is a real niche. Compliance teams, regulated-industry developers, and solo practitioners who want engineering discipline all have a problem VNX solves and no other framework does. The niche is underserved, not crowded.

**Publishable research from real production data.** The A/B testing methodology (headless vs. interactive, functionally equivalent output) is novel. The dispatch volume (1,466+) is credible. The pre-filter result (~70% of decisions without LLM invocation) is counterintuitive and interesting. This is the kind of data that gets shared.

**The headless A/B testing methodology itself is a contribution.** Even developers who don't adopt VNX will read about and reference the methodology. Secondary citation drives discovery.

**Growing demand signal.** The February 2026 data point (81% of AI agents operational, only 14.4% with full security approval) means governance pain is real and growing. VNX is positioned at the intersection of "people are starting to ask about governance" and "nobody has a good answer yet."

---

## Why It Might Not Work

**Solo developer project — bus factor of 1.** If Vincent stops working on VNX, it stops. No institutional backing, no co-founders, no succession plan. This is the single largest risk for long-term adoption. Serious adopters will see this and factor it in.

**Bash/Python codebase may deter contributors.** The organic Bash → Python growth path means the codebase is not a showcase of modern engineering practices. A developer who opens the repo and sees 60% Bash scripts may conclude this is a personal tool, not a framework to build on. This is a real perception problem even if the architecture is sound.

**Governance is not the developer zeitgeist.** The dominant developer culture in AI tooling right now is "ship fast, iterate, autonomy is a feature." VNX's value proposition — "slow down, every action should be audited" — runs against the grain. This won't kill adoption, but it will limit the ceiling. The 1,000-star target is achievable; 50,000-star trajectory is not, on this value proposition alone.

**Late entrant in a crowded market.** CrewAI has 48,000 stars and 12 million daily executions. LangGraph is backed by LangChain. Mastra has the Gatsby team and TypeScript-native appeal. VNX is entering a market where the dominant players are already well-established. The governance niche is real but the total addressable audience is smaller than "general multi-agent orchestration."

**Headless mode is new.** The feature that makes VNX competitive on setup friction is 2 weeks old. It needs to be battle-tested before it can be the centerpiece of a public launch. Launching before Phase 1 burn-in is complete risks negative first impressions that are hard to recover from.

---

## Key Metrics to Track

| Metric | Current | Phase 1 Target | Phase 2 Target |
|--------|---------|---------------|----------------|
| GitHub stars | — | 100 (HN launch) | 500 |
| Stars per month | — | 30 | 50 |
| npm/pip installs/week | — | 10 | 100 |
| Dispatches by external users | 0 | 10 | 100 |
| Issues opened by external users | 0 | 5 | 25 |
| PRs from external contributors | 0 | 1 | 5 |
| Blog post views per post | — | 1,000 | 5,000 |
| Blog post HN points (top post) | — | 50 | 200 |

The most important early signal is not stars — it is **issues opened by external users**. An external issue means someone installed VNX, ran it, hit a problem, and cared enough to report it. That is the first evidence of real adoption beyond curiosity.

---

## Decision Point

The path to 1,000 stars runs entirely through Phase 1 execution. Without a working install path, a 5-minute quickstart, and headless burn-in complete, there is no story to tell publicly. Everything else — blog posts, HN launch, conference talks, star projections — depends on that foundation being solid.

**The question is not whether VNX is technically ready. It is whether the onboarding is ready.**

Phase 1 is 4–6 weeks of focused work. The output is a version of VNX that an external developer can install, run their first dispatch through, and understand within 30 minutes. That is the precondition for everything else in this document.
