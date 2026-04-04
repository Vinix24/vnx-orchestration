# VNX — Governance-First Orchestration for AI CLI Workers

> Run Claude Code, Codex, and Gemini CLI in parallel with receipts, quality gates, provenance, and human oversight.

![VNX multi-terminal orchestration — T0 orchestrator coordinating Claude Code, Codex CLI, and Claude Opus across parallel tracks](docs/images/vnx-terminals-hero.png)

*T0 orchestrator dispatching work to parallel workers with isolated context and explicit governance.*

VNX is an open-source governance-first orchestration runtime for AI CLI workflows. One orchestrator breaks down work, interactive and headless workers execute in parallel, and everything is tracked through receipts, quality gates, and end-to-end provenance.

**No framework to import. No cloud dependency. No OAuth tokens. Governance, provenance, and operator control built in.**

Current release: `v0.5.0`
See [CHANGELOG.md](CHANGELOG.md) for the release summary.

## Full Autonomous Mode

VNX runs **full autonomous multi-agent orchestration** today — 4 tmux terminals, one orchestrator (Claude Opus) coordinating three parallel workers. Queue popup → human approval → dispatch → execution → quality gate → merge. No permission popups, no dangerous-scope interruptions.

**What's working in production (9 months):**

- **Smart dispatches with intelligence injection** — an FTS5 database stores patterns, learnings, and prevention rules from 1,400+ receipts. Before every dispatch, relevant intelligence gets injected into the worker's context. Agents start with institutional knowledge.
- **Self-learning loops** — patterns agents adopt successfully get boosted. Patterns they ignore decay. The system learns from its own behavior.
- **Multi-feature chaining** — orchestrated sequences where PR-0 feeds into PR-1, each with independent quality gates.
- **Open items tracking** — unresolved issues persist across dispatches. Nothing falls through the cracks.
- **87% token reduction** via native skill architecture instead of template compilation.

### Why CLI Subprocess — Not OAuth, Not API

Anthropic's April 2026 policy bans third-party tools from using OAuth tokens obtained through Pro/Max subscriptions. OpenClaw (340K+ stars) and similar "harness" tools were affected. **VNX was not.**

VNX exclusively spawns official `claude` CLI processes via subprocess. The binary handles authentication internally. VNX never touches OAuth tokens, never calls `api.anthropic.com`, never imports the Anthropic SDK. A [formal audit](docs/compliance/) confirms this with line-by-line evidence:

| Audit Question | Result |
|----------------|--------|
| Does any code call Anthropic OAuth endpoints? | **NO** |
| Does any code call `api.anthropic.com` using subscription credentials? | **NO** |
| Does it only launch `claude` CLI processes? | **YES** |
| Are there HTTP clients targeting Anthropic endpoints? | **NO** |

The cost advantage is significant: $200/month Max subscription versus ~$3,000/month in equivalent API tokens for the same workload. CLI subprocess trades some observability for a 15x cost reduction — without sacrificing core functionality.

Read the full analysis: [Best OpenClaw Alternative? How CLI Subprocess Orchestration Survives Anthropic's OAuth Ban](https://vincentvandeth.nl/blog/best-openclaw-alternative-cli-subprocess-oauth-ban)

## Why

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

### 5. Rotate — Context fills up? No problem

Long-running tasks exhaust context windows. VNX handles this automatically:

```
Agent hits 65% context → blocked from further tool calls
  → Agent writes structured ROTATION-HANDOVER.md
    → VNX sends /clear to terminal
      → Fresh session resumes with handover + original task
```

Zero human intervention. Zero lost work. The receipt ledger maintains a complete chain across rotations.

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

VNX mines session logs to find patterns and generate tuning suggestions. Nothing is auto-applied:

1. **Analyze** — Parse logs, detect patterns, extract model performance
2. **Brief** — Aggregate into T0-readable state file
3. **Suggest** — Generate tuning proposals (MEMORY, rules, skills)

```bash
vnx suggest review         # See what's proposed
vnx suggest accept 1,3,5   # Approve specific edits
vnx suggest apply          # Apply to target files
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

See [Who Should Use VNX](docs/audience_and_use_cases.md) for detailed use cases and audience fit.

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

Detailed comparisons: [VNX vs Claude Code](docs/comparisons/vnx_vs_claude_code.md) | [VNX vs Multi-Agent Frameworks](docs/comparisons/vnx_vs_frameworks.md)

## Roadmap

### Current: Subprocess Migration (F27–F29)

VNX is migrating from tmux-based terminal management to pure subprocess execution. This resolves the class of operational failures (stale panes, input-mode probing, `/clear` failures, capture-pane scraping) while maintaining the CLI-only billing profile.

| Feature | What | Status |
|---------|------|--------|
| **F27** | Batch refactor — resolve 57 blocker open items (file/function size violations) | Planned |
| **F28** | SubprocessAdapter — replace `tmux send-keys` with `subprocess.Popen(["claude", "-p", ...])` | Planned |
| **F29** | Dashboard agent stream — subprocess stdout → SSE → real-time browser UI | Planned |

**Key constraint:** All interaction with Claude stays through the official `claude` CLI binary. No Anthropic SDK imports, no direct API calls, no OAuth token handling. The [formal audit](docs/compliance/vnx_anthropic_billing_audit.pdf) criteria must pass after every merge.

### Completed: Dashboard & Supervisor (F22–F26)

| Feature | What | Status |
|---------|------|--------|
| **F22** | Supervisor system — health monitoring, process recovery | ✅ Done |
| **F23** | Queue popup watcher — real-time queue visualization | ✅ Done |
| **F24** | Track removal manifest — merged PR cleanup | ✅ Done |
| **F25** | Staging workflow — pre-validation before dispatch | ✅ Done |
| **F26** | Demo & distribution mode — replay without API keys | ✅ Done |

### Future direction

tmux elimination (F30) is deferred until F28+F29 are stable in production. The subprocess adapter and tmux adapter coexist behind the `RuntimeAdapter` protocol — operators can fall back to tmux if needed.

Long-term: browser-based dashboard replaces all terminal observation. Subprocess stdout piped to SSE for real-time agent visibility. The CLI subprocess pattern stays as the transport layer.

## Architecture & Docs

| Document | Description |
|----------|-------------|
| [Architecture](docs/manifesto/ARCHITECTURE.md) | Glass Box Governance design and data flow |
| [Compliance Audit](docs/compliance/vnx_anthropic_billing_audit.pdf) | Formal billing policy audit — zero OAuth, zero API calls |
| [Productization Contract](docs/productization_contract.md) | User modes, command surface, migration plan |
| [Dispatch Guide](docs/DISPATCH_GUIDE.md) | How T0 routes tasks to workers |
| [Limitations](docs/manifesto/LIMITATIONS.md) | Known constraints and failure modes |
| [Open Method](docs/manifesto/OPEN_METHOD.md) | Development philosophy |

## CI

Two offline GitHub Actions workflows (no API calls, no secrets):

- `public-ci.yml` — Install + doctor validation, gitleaks secret scan
- `vnx-ci.yml` — Core test suites + PR queue integration

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

**Most valuable contributions right now:**
- **Subprocess adapter** — help migrate from tmux to pure subprocess execution (F28)
- **Dashboard event stream** — SSE endpoint + React components for real-time agent visibility (F29)
- **Test coverage** — especially for dispatch lifecycle and quality gate edge cases
- **Provider adapters** — Gemini CLI, Codex CLI headless patterns
- **Docs clarity** — architecture diagrams, getting-started improvements

## Blog

Building VNX in public — architecture decisions, failure modes, and real data from running multi-agent workflows in production.

→ [vincentvandeth.nl/blog](https://vincentvandeth.nl/blog)

## License

MIT — see [LICENSE](LICENSE).

---

Built by [Vincent van Deth](https://vincentvandeth.nl) · Questions? [GitHub Discussions](https://github.com/Vinix24/vnx-orchestration/discussions)
