# VNX Onboarding Guide

> From zero to your first dispatched task — in starter mode or operator mode.

This guide walks you through setting up VNX and executing your first governed AI task. Choose starter mode to learn the fundamentals, then upgrade to operator mode when you need parallel agents.

---

## Which Mode Should You Start With?

| | Starter Mode | Operator Mode |
|---|---|---|
| **Best for** | First-time users, evaluation, simple projects | Production work, parallel features, team use |
| **Terminals** | Single terminal | 4-terminal tmux grid (T0-T3) |
| **AI providers** | One (Claude Code by default) | Multiple (Claude, Codex, Gemini, Kimi) |
| **Dispatch model** | Sequential (one task at a time) | Parallel (three tasks simultaneously) |
| **tmux required** | No | Yes |
| **Time to first task** | ~5 minutes | ~15 minutes |

Both modes share the same runtime: receipts, provenance, governance controls, and audit trails work identically.

---

## Part 1: Starter Mode

### Step 1: Install

```bash
# Install prerequisites (macOS)
brew install jq

# Clone VNX
git clone https://github.com/Vinix24/vnx-orchestration.git

# Install into your project
cd vnx-orchestration
./install.sh /path/to/your/project
```

### Step 2: Initialize

```bash
cd /path/to/your/project
vnx init --starter
```

This creates:
- `.vnx/` — VNX runtime (git-ignored)
- `.vnx-data/` — state directory with `mode.json` set to `starter`
- `.claude/` — Claude Code configuration and skills

### Step 3: Validate

```bash
vnx doctor
```

Doctor checks all dependencies, path resolution, and state integrity. Every check should pass. If something fails, the output tells you exactly what to fix.

### Step 4: Check Status

```bash
vnx status
```

Shows your current mode, terminal state, queue depth, and any open items. In starter mode, you'll see a single terminal.

### Step 5: Your First Dispatch

In starter mode, you work directly with the AI agent in one terminal. The dispatch flow is:

1. **Create a dispatch** — Tell the agent what to do via a structured task
2. **Agent executes** — The AI works within the scoped instructions
3. **Receipt generated** — A structured record of what was done
4. **Gate check** — Deterministic quality validation

```bash
# See available commands
vnx help

# List any staged dispatches
vnx staging-list

# Promote a dispatch to execution
vnx promote <dispatch-id>

# After completion, run a gate check
vnx gate-check --pr <PR-ID>
```

### Step 6: Explore the Audit Trail

```bash
# View receipts
cat .vnx-data/state/t0_receipts.ndjson | python3 -m json.tool

# Check API costs
vnx cost-report

# Analyze session patterns
vnx analyze-sessions
```

Every action is recorded. This is the core value proposition — you always know what happened, when, and why.

### Step 7: Try Demo Mode

See operator mode in action without setting it up:

```bash
vnx demo                              # Sample state and dispatches
vnx demo --replay governance-pipeline # Replay a real 6-PR session
vnx demo --dashboard                  # Dashboard with sample data
```

Demo mode uses temp directories — nothing touches your project.

### Upgrading to Operator Mode

When you're ready for parallel agents:

```bash
vnx init --operator
```

This re-initializes with the full 4-terminal configuration. Your existing receipts and intelligence data are preserved.

---

## Part 2: Operator Mode

### Step 1: Install Additional Dependencies

```bash
# macOS
brew install tmux fswatch

# Verify
tmux -V      # tmux 3.x+
fswatch --version
```

### Step 2: Initialize

```bash
cd /path/to/your/project
vnx init --operator
vnx doctor
```

Doctor now validates tmux, fswatch, and the full terminal grid configuration in addition to the base checks.

### Step 3: Launch the Grid

```bash
vnx start
```

This opens a 2x2 tmux grid:

```
┌────────────────────┬────────────────────┐
│    T0 (Brain)      │    T1 (Worker)     │
│    Read-Only       │    Track A         │
├────────────────────┼────────────────────┤
│    T2 (Worker)     │    T3 (Worker)     │
│    Track B         │    Track C         │
└────────────────────┴────────────────────┘
```

#### Multi-Provider Profiles

Mix AI providers across terminals:

```bash
vnx start                    # All Claude Code (default)
vnx start claude-codex       # T1: Codex CLI, T2: Claude Code
vnx start claude-gemini      # T1: Gemini CLI, T2: Claude Code
vnx start full-multi         # T1: Codex CLI, T2: Gemini CLI
```

### Step 4: Understand the Roles

**T0 — Orchestrator** (top-left)
- Plans and breaks down work
- Reviews receipts from workers
- Promotes dispatches
- Never writes code
- Runs Claude Opus for strategic reasoning

**T1 — Worker Track A** (top-right)
- Primary implementation
- Executes dispatches from T0
- Writes reports on completion

**T2 — Worker Track B** (bottom-left)
- Testing, integration, validation
- Independent from T1's work

**T3 — Worker Track C** (bottom-right)
- Code review, security analysis, deep investigation
- Runs Claude Opus for analytical depth

### Step 5: Navigate the Grid

```bash
# From any terminal
vnx jump T0                  # Focus T0
vnx jump T1                  # Focus T1
vnx jump --attention         # Focus the terminal needing human input

# tmux shortcuts
Ctrl+G                       # Open dispatch queue popup
Ctrl+B D                     # Detach (session keeps running)
Ctrl+B [arrow]               # Navigate between panes
```

### Step 6: The Dispatch Workflow

1. **Describe work to T0**: Tell it what you need built
2. **T0 creates dispatches**: Scoped tasks with tracks, priorities, gates
3. **Review in queue**: Press `Ctrl+G` to see pending dispatches
4. **Approve**: Press `A` to accept, dispatcher routes to the right terminal
5. **Monitor**: `vnx status` shows what each terminal is doing
6. **Receive results**: Receipt processor delivers structured results to T0
7. **Gate check**: `vnx gate-check` validates quality deterministically
8. **Iterate**: T0 decides next steps based on results

### Step 7: Feature Worktrees

For feature work, isolate from `main`:

```bash
# Create worktree
vnx worktree create my-feature --ref main
cd ../your-project-wt-my-feature/

# Work in isolation
vnx start

# Pre-merge check
vnx merge-preflight my-feature

# Finish and clean up
vnx finish-worktree my-feature --delete-branch
```

Worktrees get their own `.vnx-data/`, so session state is fully isolated.

### Step 8: Session Intelligence

After running several sessions, VNX mines patterns:

```bash
vnx analyze-sessions           # Parse logs, detect patterns
vnx suggest review             # See tuning proposals
vnx suggest accept 1,3,5       # Approve specific suggestions
vnx suggest apply              # Apply to target files
```

Intelligence reveals which models perform best on which task types, where context rotations happen most, and where governance overhead can be reduced.

---

## Common Operations Reference

### Daily Workflow

```bash
vnx start                     # Launch grid
vnx status                    # Check state
# ... work with T0 ...
vnx cost-report               # End-of-session costs
vnx stop                      # Shut down
```

### Recovery

```bash
vnx recover                   # Fix stuck state, stale locks, orphan processes
vnx doctor                    # Validate everything is healthy
```

### Monitoring

```bash
vnx status                    # Overview: terminals, queue, items
vnx ps                        # Process health with PIDs
vnx cost-report               # API spend per agent/task
```

### Queue Management

```bash
vnx staging-list              # Pending dispatches
vnx promote <id>              # Promote to queue
Ctrl+G                        # Visual queue popup
```

---

## Troubleshooting

### "Command requires operator mode"

You're in starter mode. Either:
- Use `vnx init --operator` to upgrade
- Use the starter-mode equivalent (check `vnx help`)

### `vnx doctor` reports failures

Doctor output is actionable — each failure includes what to install or fix. Common issues:
- Missing `tmux` → `brew install tmux` (operator mode only)
- Missing `jq` → `brew install jq`
- Missing `fswatch` → `brew install fswatch` (operator mode only)
- Stale state → `vnx recover`

### Terminal not responding

```bash
vnx recover                   # Clears locks, restarts stuck processes
vnx ps                        # Check process health
```

### Context window full

VNX handles this automatically via context rotation. If manual intervention is needed:
- The agent writes a handover document
- `/clear` resets the session
- Fresh session resumes with handover context

### Lost receipts

```bash
vnx recover                   # Reprocesses orphaned reports
```

The receipt processor re-scans `.vnx-data/unified_reports/` for unprocessed reports.

---

## Next Steps

- **Example flows**: See [docs/examples/](../examples/) for realistic walkthroughs
  - [Coding orchestration](../examples/example_coding_orchestration.md) — feature development with parallel agents
  - [Headless research](../examples/example_headless_research.md) — structured analysis without interactive tmux
  - [Content orchestration](../examples/example_content_orchestration.md) — documentation and non-coding tasks
- **Architecture**: [docs/manifesto/ARCHITECTURE.md](../manifesto/ARCHITECTURE.md)
- **Dispatch guide**: [docs/DISPATCH_GUIDE.md](../DISPATCH_GUIDE.md)
- **Limitations**: [docs/manifesto/LIMITATIONS.md](../manifesto/LIMITATIONS.md)
- **Comparisons**: [VNX vs Claude Code](../comparisons/vnx_vs_claude_code.md) | [VNX vs Frameworks](../comparisons/vnx_vs_frameworks.md)
