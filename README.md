# VNX — Governance-First Orchestration for AI CLI Workers

> Run Claude Code, Codex, and Gemini CLI in parallel with receipts, quality gates, provenance, and human oversight.

![VNX multi-terminal orchestration — T0 orchestrator coordinating Claude Code, Codex CLI, and Claude Opus across parallel tracks](docs/images/vnx-terminals-hero.png)

*T0 orchestrator dispatching work to parallel workers with isolated context and explicit governance.*

VNX is an open-source governance-first orchestration runtime for AI CLI workflows. One orchestrator breaks down work, interactive and headless workers execute in parallel, and everything is tracked through receipts, quality gates, and end-to-end provenance.

**No framework to import. No cloud dependency. Governance, provenance, and operator control built in.**

Current release: `v1.0.0-rc1` — architectural stabilization milestone (2026-05-09).
See [CHANGELOG.md](CHANGELOG.md) for the release summary.

**API contract is stable from this release forward.** Dispatch envelope, receipt schema, NDJSON ledger format, and 14 ADRs (`docs/governance/decisions/ADR-001` through `ADR-014`) are now backwards-compatibility-honoring. v1.0.0 final ships after Phase 5 reader cutover validation.

## The Problem

You're already using AI coding agents. But when you run multiple agents on the same project:

- They edit the same files and create merge conflicts
- Context windows fill up mid-task, losing all progress
- You can't tell which agent did what, or why something broke
- There's no way to stop an agent from merging bad code

Most multi-agent frameworks solve orchestration. VNX solves **governance** — the audit trails, quality gates, and human checkpoints that make multi-agent workflows trustworthy.

## Three Ways to Run VNX

VNX supports three modes. All share the same runtime model — receipts, provenance, and governance controls work in every mode.

### Starter Mode — Get running in 5 minutes

Single terminal, one AI provider, sequential dispatch. No tmux required.

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration && ./install.sh /path/to/your/project
cd /path/to/your/project
vnx init --starter
vnx doctor        # Validate everything
vnx status        # See your healthy state
```

Starter mode gives you scoped dispatches, structured receipts, and a full audit trail — without the multi-terminal setup. When you're ready for parallel agents, upgrade with `vnx init --operator`.

**New to VNX?** Start with the [5-Minute Quickstart](docs/QUICKSTART.md).

### Operator Mode — Full multi-agent orchestration

Four-terminal tmux grid, multiple AI providers, parallel tracks, quality gates, worktrees, and dashboard.

```bash
vnx init --operator
vnx doctor
vnx start                  # Launch the 2x2 tmux grid
vnx start claude-codex     # T1: Codex CLI, T2: Claude Code
vnx start claude-gemini    # T1: Gemini CLI, T2: Claude Code
vnx start full-multi       # T1: Codex CLI, T2: Gemini CLI
```

Press `Ctrl+G` to open the dispatch queue — see pending tasks with role, priority, and git ref.

![VNX dispatch queue showing pending tasks with role, priority, and track assignment](docs/images/vnx-dispatch-queue.png)

### Demo Mode — See it without setup

Replay real orchestration sessions with no API keys and no project setup:

```bash
vnx demo                              # Launch demo with sample state
vnx demo --replay governance-pipeline # Replay a real 6-PR session
vnx demo --dashboard                  # Dashboard with sample data
```

Demo mode uses temp directories — nothing touches your project.

### Headless Mode — Fully autonomous execution

All terminals (T0-T3) can run as `claude -p` subprocesses instead of interactive tmux sessions. In headless mode, the T0 orchestrator makes dispatch, review, and merge decisions autonomously — no human in the loop between steps.

Headless workers are triggered by file watchers (new reports arriving in the unified_reports directory), silence watchdogs (detecting stale state), and optional LLM triage. Configure which terminals go headless per-run using environment variables:

```bash
# Run T1 as headless worker
VNX_ADAPTER_T1=subprocess vnx start

# Full headless mode (all terminals)
python3 scripts/headless_trigger.py --watch-dir .vnx-data/unified_reports/
```

The same governance applies regardless of mode. Receipts, quality gates, and provenance records are generated identically whether a worker is interactive or headless. Headless does not mean ungoverned.

Typical use cases: overnight feature execution, CI/CD integration, shadow testing of new agent configurations.

### Adapter mode matrix (Operator mode)

Within Operator mode, each terminal (T0/T1/T2/T3) can independently use tmux (interactive) or subprocess (headless) adapter:

| Mode | T0 | Workers (T1/T2/T3) | When to use |
|------|----|--------------------|-------------|
| 2×2 grid | tmux | tmux | Live operator session |
| Hybrid | tmux | subprocess | Operator drives, workers background |
| Fully headless | subprocess | subprocess | CI/cron/autonomous |

```bash
# Hybrid (recommended for solo dev)
VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess vnx start

# Fully headless
VNX_ADAPTER_T0=subprocess VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess \
  python3 scripts/headless_orchestrator.py
```

See [HEADLESS_TRANSITION.md](docs/manifesto/HEADLESS_TRANSITION.md) for the architectural narrative and migration guide.

## Why CLI Subprocess — Not OAuth, Not API

Anthropic's April 2026 policy bans third-party tools from using OAuth tokens obtained through Pro/Max subscriptions. OpenClaw (340K+ stars) and similar "harness" tools were affected. **VNX was not.**

VNX exclusively spawns official `claude` CLI processes via subprocess. The binary handles authentication internally. VNX never touches OAuth tokens, never calls `api.anthropic.com`, never imports the Anthropic SDK. A [formal audit](docs/compliance/vnx_anthropic_billing_audit.pdf) confirms this with line-by-line evidence:

| Audit Question | Result |
|----------------|--------|
| Does any code call Anthropic OAuth endpoints? | **NO** |
| Does any code call `api.anthropic.com` using subscription credentials? | **NO** |
| Does it only launch `claude` CLI processes? | **YES** |
| Are there HTTP clients targeting Anthropic endpoints? | **NO** |

The cost advantage is significant: ~$200/month Max subscription versus ~$3,000/month in equivalent API tokens for the same workload. CLI subprocess trades some observability for a ~15x cost reduction — without sacrificing core functionality.

Full analysis: [Best OpenClaw Alternative? How CLI Subprocess Orchestration Survives Anthropic's OAuth Ban](https://vincentvandeth.nl/blog/best-openclaw-alternative-cli-subprocess-oauth-ban)

## How It Works

### 1. Dispatch — The orchestrator assigns tasks

T0 breaks work into scoped tasks (150-300 lines) and routes them to worker terminals. Each worker runs its own CLI with its own context window. No shared state between agents.

### 2. Execute — Each agent works in isolation

Workers execute tasks using their assigned CLI. VNX supports mixing providers freely — Claude Code, Codex CLI, Gemini CLI, or Kimi CLI. The orchestration layer doesn't care which model runs where.

### 3. Track — Every decision is recorded

Every agent action generates a structured receipt in an append-only NDJSON ledger: what was dispatched, what was produced, which files changed, git commit, duration, cost. After 1,400+ entries, patterns emerge that you can't see any other way.

```bash
vnx cost-report    # API spend per agent, per task type
```

### 4. Gate — Agents can't merge broken code

Quality gates are deterministic, not LLM-based. The agent proposes, the gate validates: file size limits, test coverage thresholds, open blocker counts. Verdicts: `APPROVE`, `HOLD`, or `ESCALATE`. The LLM never judges its own work.

![VNX quality advisory showing automated code quality checks and gate verdicts](docs/images/vnx-quality-advisory.png)

### Multi-Provider Code Review

Every PR goes through automated review from multiple AI providers before merge is allowed.

- **Codex gate** (OpenAI gpt-5.2-codex) — finds bugs, data loss risks, and security issues
- **Gemini review** (Google gemini-2.5-flash) — architecture, patterns, and trade-off analysis

Both run headlessly via CLI (`codex --dangerously-bypass-approvals-and-sandbox`, `gemini`). Each produces a structured JSON verdict: `pass`, `fail`, or `blocked`, with severity-rated findings attached.

The triple gate policy is enforced deterministically: codex pass + gemini pass + CI green → merge allowed. Gate locks are file-based, not LLM-based — the orchestrator cannot bypass them by reasoning its way around them.

```bash
vnx gate-check --pr 206          # Run all required gates
vnx gate-check --pr 206 --gate codex_gate   # Run specific gate
```

### 5. Rotate — Context fills up? No problem

Long-running tasks exhaust context windows. VNX handles this automatically:

```
Agent hits 65% context → blocked from further tool calls
  → Agent writes structured ROTATION-HANDOVER.md
    → VNX sends /clear to terminal
      → Fresh session resumes with handover + original task
```

Zero human intervention. Zero lost work. The receipt ledger maintains a complete chain across rotations.

## What's new since v1.0.0-rc1 (May 2026)

> Strategic replan v1.2 organizes work into 6 waves. v1.0.0-rc1 itself shipped Wave 0 + 0.5 (architectural stabilization + Phase 6 P4 migration). Wave 1 (shadow read cutover) and Wave 5 P0/P1 (smart-context injection) shipped on top of the rc1 baseline and are still pending burn-in before the v1.0.0 final tag — see [CHANGELOG.md](CHANGELOG.md) "Unreleased" for the post-rc1 PR list.

### Architectural stabilization — 14 ADRs locked (v1.0.0-rc1)
Decisions previously implicit are now captured as numbered ADRs (`docs/governance/decisions/ADR-001` through `ADR-014`). Highlights: ADR-003 OAuth-only Claude routing (no SDK), ADR-005 NDJSON ledger as primary substrate, ADR-008 dual-LLM adversarial review with `contract_hash` evidence binding, ADR-011 manager+worker hierarchy with Conditional/Pilot subagents, ADR-014 autonomous chain dispatch with SHA-256 consent token. From this release forward, dispatch envelope, receipt schema, and ledger format are backwards-compatibility-honoring.

### CI gate — no Anthropic SDK imports
`scripts/check_adr_003_no_sdk_imports.py` blocks any `import anthropic` / `from anthropic` / `import claude_agent_sdk` in `scripts/`, `dashboard/`, or `tests/`. Magic-comment opt-in for explicit exceptions only. Enforces ADR-003: VNX drives Claude exclusively via `claude -p` subprocess on the operator's OAuth credential — never SDK + API key.

### Smart context injection — measured +30pp dispatch quality lift
Replaced naïve "intelligence_items" with three-state-aware context bundles. PR #455 (W5.0) adds prior-round-findings injection — when a dispatch is round N+1 of a multi-round PR, the worker auto-receives blocking + advisory findings from prior rounds. PR #456 (W5.1) adds ADR injection by file-touch — dispatches whose `dispatch_paths` overlap with files referenced in ADRs auto-receive the relevant governance text. Validated on 658 outcome-tagged dispatches: 88.9% success WITH intelligence vs 58.3% WITHOUT.

### Phase 6 P4 — central VNX state proven on production data
`scripts/migrate_to_central_vnx.py` (PR #432) imported 855k code_snippets + 505 dispatches across 4 production source DBs with **0 verifier discrepancies**. Migration 0016 schema-first rewrite per ADR-009 (PR #446) replaces hardcoded column projections with PRAGMA introspection. Composite UNIQUE rebuilds for 5 multi-tenant tables; structural test parsing `0015_complete_project_id.sql` enforces ALTER↔IMPORT_TABLES symmetry.

### Wave 1 — shadow-mode read cutover with 6 hard divergence metrics
`shadow_verifier.py` (PR #450) is an independent comparator (no shared code with the migration) that computes 6 zero-tolerance metrics: wrong-project rows, scoping/blocking-finding mismatch, top-3 divergence, count drift, lease-key collisions, p95-latency ratio. `shadow_logger.py` (PR #451) writes NDJSON divergence records under flock with timestamp-suffix rotation. PRs #452–#454 wire 4 T0 read sites + 5 IntelligenceSelector/DispatchRegister read sites + dashboard read paths through `VNX_USE_CENTRAL_DB=unset|shadow|1` flag. Canary test pack with 14+ deliberate-divergence fixtures + operator-readable rollback procedure.

### Repo hygiene — 49 strategic/business docs moved to private `claudedocs/`
Five-tier OI-1373 cleanup: 8 business agent drafts, 7 strategy files, internal design docs, 18 phase FEATURE_PLANs, and 9 stale research docs moved from public `roadmap/`+`docs/internal/` to gitignored `claudedocs/`. Pattern: filesystem `mv` + `git add -u` (NOT `git mv`) — preserves files locally on disk while removing from git tracking.

## What's new in v0.10.0 (April 2026)

### Self-healing daemons
Workers and processors now auto-respawn via wrapper supervisors. Kill `-9` a worker — `dispatcher_supervisor.sh` and `receipt_processor_supervisor.sh` bring it back with exponential backoff, clean up stale locks, and release leases through a single-owner helper. No more manual SQL `UPDATE terminal_leases SET state='idle'`.

### Cryptographic dispatch chain
Every dispatch now carries `instruction_sha256` from manifest through receipt. The chain is `dispatch_id → instruction_sha256 → manifest.json → receipt → audit event`. You can prove which prompt produced which output, end-to-end.

### Smart codex review
Codex final-gate prompt now uses strict severity rules — `error` is reserved for data loss, false PR closure, security breach, or cross-dispatch state corruption. Style, scope drift, and out-of-diff findings drop to `warning` or `info`. Result on the 2026-04-28 chain: 100% blocking → 25% blocking.

### Bounded state
Append-only logs no longer grow unbounded. `compact_state.py` runs nightly at 02:30:
- `t0_intelligence_archive.ndjson` (was 391MB) → gzipped 7-day archives
- `t0_receipts.ndjson` → cap at 10000 entries
- `open_items_digest.json` → evict entries >30d

### Real-time dashboard
`/api/register-stream` SSE endpoint streams dispatch lifecycle events live (created → promoted → started → gate_passed → completed). Dashboard kanban no longer needs polling.

### Frontend regression protection
Three new Playwright suites + tsc strict for the dashboard:
- Console + page errors per route (zero tolerance)
- Network failure scenarios (offline / 5xx / slow 3G / timeout / partial)
- Visual regression with 1% pixel-diff tolerance baselines

### Cross-provider token tracking
Codex and Gemini terminals now report token usage in receipts via `adapter.get_token_usage()`. Cost-per-feature aggregation works across all three providers.

## Install

### Prerequisites

- macOS or Linux
- tmux (operator mode only), bash, python3, git, jq, fswatch
- At least one AI CLI: [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), or [Gemini CLI](https://github.com/google-gemini/gemini-cli)

```bash
# macOS
brew install tmux jq fswatch

# Clone and install into your project
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration
./install.sh /path/to/your/project

# Initialize
cd /path/to/your/project
vnx init                # Interactive: choose starter or operator
vnx doctor              # Validate everything
```

Starter mode needs only bash, python3, git, and jq. Operator mode additionally requires tmux and fswatch.

## Commands

Commands are tiered by mode. Running an operator-only command in starter mode returns a clear error with upgrade instructions.

### Universal (all modes)

| Command | What it does |
|---------|-------------|
| `vnx init` | Initialize VNX project (with mode selection) |
| `vnx doctor` | Validate setup and dependencies |
| `vnx status` | Show current state and mode |
| `vnx recover` | Recover from failures |
| `vnx help` | Show available commands for current mode |
| `vnx update` | Pull latest VNX version |

### Starter + Operator

| Command | What it does |
|---------|-------------|
| `vnx staging-list` | List pending dispatches |
| `vnx promote` | Promote a dispatch |
| `vnx gate-check` | Run quality gate check |
| `vnx cost-report` | API spend per agent and task |
| `vnx analyze-sessions` | Populate session analytics |
| `vnx suggest review` | View AI-generated tuning suggestions |
| `vnx suggest accept <ids>` | Approve specific suggestions |
| `vnx suggest apply` | Apply approved tuning edits |
| `vnx bootstrap-skills` | Install skill templates |
| `vnx bootstrap-terminals` | Configure terminal grid |

### Operator Only

| Command | What it does |
|---------|-------------|
| `vnx start [profile]` | Launch the 2x2 tmux grid |
| `vnx stop` | Stop tmux session |
| `vnx jump <T0\|T1\|T2\|T3>` | Switch tmux focus to terminal |
| `vnx jump --attention` | Focus the terminal needing human attention |
| `vnx worktree create <name>` | Isolated feature branch worktree |
| `vnx worktree list` | List active worktrees |
| `vnx merge-preflight` | Pre-merge governance check |
| `vnx smoke` | Run pipeline smoke test |

### Demo Only

| Command | What it does |
|---------|-------------|
| `vnx demo` | Launch demo with sample state |
| `vnx demo --replay <scenario>` | Replay a recorded orchestration flow |
| `vnx demo --dashboard` | Dashboard with sample data |

## Git Worktrees (Operator Mode)

Isolate feature work from `main`. Each worktree gets its own branch — all agents work in the worktree, `main` stays clean.

```bash
vnx worktree create fp04              # Branch from HEAD
vnx worktree create fp04 --ref staging  # Branch from staging
cd ../project-wt-fp04/                # All agents work here
vnx worktree remove fp04             # Clean up after merge
```

Receipts track `in_worktree: true/false` and commit provenance (`CLEAN`, `DIRTY_LOW`, `DIRTY_HIGH`).

## Session Intelligence

VNX continuously mines its own NDJSON ledger + dispatch metadata to learn what works and inject it into future dispatches. Three layers:

1. **Pattern extraction** — `intelligence_extractor.py` writes success patterns + anti-patterns to SQLite (`quality_intelligence.db`) with `valid_from` / `valid_until` lifecycle, `source_dispatch_ids` provenance, and per-pattern confidence scores updated from outcome feedback.
2. **Three-state-aware context bundle** — `IntelligenceSelector.select()` now combines intelligence_items, prior-round review findings (Wave 5 P0), ADR governance text by file-touch (Wave 5 P1), and on-roadmap code anchors / schemas / operator memory (Wave 5 P2-P4 in flight) into a bounded per-dispatch context pack.
3. **Tuning suggestions** — `vnx suggest review` surfaces auto-generated proposals (MEMORY edits, rule/skill changes); nothing auto-applies. `vnx suggest accept <ids>` then `vnx suggest apply`.

Measured impact: **88.9% dispatch success WITH intelligence vs 58.3% WITHOUT** over 658 outcome-tagged dispatches (+30 percentage-point lift). PR-cascade prevention validated against PR #432's 9-round chain as canonical proof-of-concept.

```bash
vnx suggest review         # See what's proposed
vnx suggest accept 1,3,5   # Approve specific edits
vnx suggest apply          # Apply to target files
```

## Mission Control Dashboard

Real-time operator dashboard at `localhost:3100` (Next.js frontend) backed by a Python API server at `localhost:4173`. No cloud dependency — the dashboard reads directly from `.vnx-data/` filesystem state and the runtime SQLite DBs.

### Live streams (SSE)
- **`/api/register-stream`** — dispatch lifecycle events stream (`created → promoted → started → gate_passed → completed`). Kanban no longer polls.
- **`/api/agent-stream`** — per-terminal NDJSON event tail (`init`, `thinking`, `text`, `tool_use`, `tool_result`) — watch a headless worker reason in real time.

### Operator pages
- **Terminals** — per-terminal status cards: which agent is doing what, lease state, context pressure, last heartbeat.
- **Kanban** — pending / active / completed dispatch board with full provenance chain.
- **Dispatches** (+ detail view) — every dispatch's manifest, instruction_sha256, receipt, and gate evidence in one place.
- **Reports** — auto-assembled worker reports browsable by domain (coding / business) and headless gate output (codex_gate, gemini_review markdown).
- **Open Items** — blocker / warn / info ledger filterable by PR, severity, and dispatch source.
- **Intelligence** — pattern catalog with confidence trends, injection ledger, dispatch-effectiveness bucket chart (with vs without intelligence).
- **Improvements** — proposals view: see what `vnx suggest` would change, accept/reject inline.
- **Governance** — ADR catalog, CI gate status, contract-hash binding for the active review stack.
- **Conversations** — session transcript viewer for any historical dispatch.

### Token + cost
- **Tokens** + **Models** + **Usage** pages — cross-provider token counts (Claude / Codex / Gemini), per-feature spend aggregation, model-mix breakdown.

```bash
vnx dashboard          # Launch at localhost:3100
vnx dashboard --api    # API server only (localhost:4173)
```

## Project Structure

```
your-project/
├── .vnx/              # VNX runtime (git-ignored)
│   ├── bin/           # CLI + core scripts
│   ├── hooks/         # PreToolUse, PostToolUse hooks
│   ├── ledger/        # Receipt processor
│   └── skills/        # Skill templates
├── .vnx-data/         # State (git-ignored)
│   ├── state/         # t0_receipts.ndjson, terminal_state.json
│   ├── dispatches/    # staging/ → queue/ → active/ → completed/
│   └── mode.json      # Current mode (starter/operator)
├── dashboard/         # Operator dashboard (git-tracked)
│   ├── index.html     # Vanilla HTML/JS UI (no build step)
│   └── serve_dashboard.py # Python HTTP server (port 4173)
└── .claude/           # Claude Code config + skills
```

All state lives on the filesystem. No database, no cloud dependency.

## Who Is VNX For?

**Solo developers** managing 2-4 AI agents who need to know what each agent did, when, and why. Start with starter mode, graduate to operator mode when you need parallel tracks.

**Small engineering teams** (2-5 people) coordinating AI-assisted feature work across branches and worktrees with traceable provenance.

**Compliance-aware organizations** that need audit trails for AI-generated code — every change traces back to a dispatch, a human approval, and a quality gate verdict.

VNX is **not** a consumer AI chat wrapper, a CI/CD replacement, or a no-code tool. It's an orchestration system for developers who want governance over their AI workflows.

See [VNX vs Claude Code](docs/comparisons/vnx_vs_claude_code.md) and [VNX vs Multi-Agent Frameworks](docs/comparisons/vnx_vs_frameworks.md) for positioning and audience-fit tradeoffs.

## How VNX Compares

| | VNX | Raw Claude Code | OpenClaw / CrewAI / LangGraph |
|---|-----|----------------|-------------------------------|
| **Multi-agent coordination** | Built-in (T0-T3 grid) | Manual (multiple terminals) | Framework-level orchestration |
| **Auth method** | CLI subprocess (unaffected by OAuth ban) | CLI OAuth (subscription) | Direct API keys |
| **Audit trail** | Append-only NDJSON ledger | Chat logs only | Varies; often requires custom logging |
| **Quality gates** | Deterministic, non-LLM | None built-in | Framework-dependent |
| **Human approval** | Mandatory on every dispatch | Per-tool approval | Configurable but not default |
| **Context rotation** | Automatic handover | Manual /clear | Not typically handled |
| **LLM-agnostic** | Yes (Claude, Codex, Gemini, Kimi) | Claude only | Varies by framework |
| **Setup complexity** | `git clone` + `vnx init` | `npm install` | pip install + code integration |
| **Automated multi-provider review** | Built-in (Codex + Gemini triple gate) | None built-in | Not typically included |

Detailed comparisons: [VNX vs Claude Code](docs/comparisons/vnx_vs_claude_code.md) | [VNX vs Multi-Agent Frameworks](docs/comparisons/vnx_vs_frameworks.md)

## Roadmap

Active development. Priorities shift based on real usage patterns.

### Recently landed (Wave 0 + 0.5 + Wave 1 + Wave 5 P0/P1, May 2026)

Strategic replan v1.2 reframed development around 6 waves. Shipped between v1.0.0-rc1 cut and now:

- **Wave 0** — 14 ADRs locked (003-014), CI gate `ADR-003: No Anthropic SDK Imports`, 49 strategic docs moved to private `claudedocs/` (5-tier OI-1373 cleanup), v1.0.0-rc1 release notes (#439–#449)
- **Wave 0.5** — Phase 6 P4 data migration to central VNX state proven on real production data (855k code_snippets, 505 dispatches, 0 verifier discrepancies); migration 0016 schema-first per ADR-009 (#432, #446)
- **Wave 1 — shadow-mode read cutover (#450–#454)** —
  - W1.1 `shadow_verifier.py` independent comparator with 6 hard divergence-detection metrics (zero-tolerance for scoping/blocking findings)
  - W1.2 `shadow_logger.py` NDJSON writer + report CLI + flock-rotation
  - W1.3 T0 state-builder shadow wiring (4 read sites)
  - W1.4 `IntelligenceSelector` + `DispatchRegister` shadow wiring (5 read sites)
  - W1.5 Dashboard shadow wiring + canary divergence test pack (14+ fixtures) + operator-readable rollback docs
- **Wave 5 P0 — prior-round-findings injection (#455)** — when a dispatch is round N+1 of a multi-round PR, the worker auto-receives blocking + advisory findings from prior rounds. Validated against PR #432's 9-round cascade as the canonical proof-of-concept.
- **Wave 5 P1 — ADR injection by file-touch (#456)** — dispatches whose `dispatch_paths` overlap with files referenced in ADRs auto-receive the relevant ADR sections as governance context.

### Next

Strategic replan v1.2 sequence:

- **Wave 1 cutover validation (in progress)** — pilot burn-in on mc + sales projects with `VNX_USE_CENTRAL_DB=shadow`. Required: 7 consecutive days clean on all 6 hard metrics before flipping `shadow → 1` per project per table.
- **Wave 5 P2/P3/P4 (in progress)** — file:line code-anchor injection, operator-memory injection, schema-introspection injection (extends the +30pp smart-context lift across the remaining context bundle classes).
- **Wave 2 — Phase 6 cleanup** — retire shadow-mode flag once all consumers cut over, remove dual-write code paths, rotate-and-archive per-project DBs.
- **Wave 5.5 (conditional)** — cryptographic audit-integrity layer (signed checkpoints, hash-chained NDJSON) — gating decision pending operator review.
- **Wave 6** — workers=N tactical: subagent-pilot 2-gate split per ADR-011 v2 (Gate 1 provenance/redaction, Gate 2 performance baseline median ≥30% wall-clock saved).
- **Wave 7+** — option space: multi-operator/federation, performance/scale, integration breadth (LiteLLM bridge to non-Claude providers), domain expansion. Non-binding; refresh post-Wave 6.

### Known gaps / deferred

- **Headless context rotation** (OI-1073) — subprocess workers currently use single-shot dispatch; active token-stream tracking, auto-rotation, handover writing, and continuation prompt injection are deferred. Interactive terminals retain native Claude Code rotation.
- **MCP server** — expose VNX state to external Claude sessions; not yet built.
- **v1.0.0 final tag** — operator decision after Wave 1 cutover validates on pilot projects. Currently `v1.0.0-rc1` is the public release candidate.

See [CHANGELOG.md](CHANGELOG.md) for what shipped recently.

## Architecture & Docs

| Document | Description |
|----------|-------------|
| [Architecture](docs/manifesto/ARCHITECTURE.md) | Glass Box Governance design and data flow |
| [Compliance Audit](docs/compliance/vnx_anthropic_billing_audit.pdf) | Formal billing-policy audit — zero OAuth tokens, zero API calls |
| [Productization Contract](docs/contracts/PRODUCTIZATION_CONTRACT.md) | User modes, command surface, migration plan |
| [Dispatch Guide](docs/DISPATCH_GUIDE.md) | How T0 routes tasks to workers |
| [Limitations](docs/manifesto/LIMITATIONS.md) | Known constraints and failure modes |
| [Open Method](docs/manifesto/OPEN_METHOD.md) | Development philosophy |

## CI

Two offline GitHub Actions workflows (no API calls, no secrets):

- `public-ci.yml` — Install + doctor validation, gitleaks secret scan
- `vnx-ci.yml` — Core test suites + PR queue integration

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

**Most valuable contributions:** test coverage, failure-mode hardening, provider adapters, docs clarity.

## Blog

Building VNX in public — architecture decisions, failure modes, and real data from running multi-agent workflows in production.

→ [vincentvandeth.nl/blog](https://vincentvandeth.nl/blog)

## License

MIT — see [LICENSE](LICENSE).

---

Built by [Vincent van Deth](https://vincentvandeth.nl) · Questions? [GitHub Discussions](https://github.com/Vinix24/vnx-orchestration/discussions)
