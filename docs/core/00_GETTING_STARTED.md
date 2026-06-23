# Getting Started (VNX)

**Status**: Active
**Last Updated**: 2026-06-22
**Owner**: T-MANAGER
**Purpose**: Quick orientation and links to the current VNX "source of truth" docs.

---

## Current System Snapshot

- Architecture: `00_VNX_ARCHITECTURE.md`
- Dispatch workflow: `../DISPATCH_GUIDE.md`
- Dispatch rules + lanes: `DISPATCH_RULES.md`, `PROVIDER_LANES.md`
- Monitoring/ops: `../operations/CONTROL_CENTRE.md`
- Receipt pipeline: `../operations/RECEIPT_PIPELINE.md`
- Runtime rollback: `../operations/RUNTIME_CORE_ROLLBACK.md`
- Product modes: `../contracts/PRODUCTIZATION_CONTRACT.md`

For full navigation, start at `../DOCS_INDEX.md`.

---

## VNX CLI Quick Reference

Plain `vnx` is the pip-installed Python CLI. It exposes the stable essentials:
`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`,
`dream`, `version`, and `update`. Operator commands run through `./bin/vnx`
from the repository root.

```bash
# Initialize VNX in a new project with the pip CLI
vnx init

# Health check
vnx doctor

# Project status
vnx status

# Update the pip-installed CLI
vnx update --dry-run
```

```bash
# Launch orchestration (tmux session with T0-T3) from a repo checkout
./bin/vnx start

# Stop all processes
./bin/vnx stop

# Token cost report
./bin/vnx cost-report

# Operator recovery
./bin/vnx recover
```

### Key Bindings (in tmux)
- `Ctrl+G` — Open dispatch queue popup
- `Ctrl+B D` — Detach (keeps running)
- Mouse — Click to switch panes

### Demo Setup
Use `./bin/vnx demo` from a cloned `vnx-orchestration` repo root for the current
demo workflow.

---

## Feature Development Workflow

The primary workflow for new features uses feature worktrees:

### 1. Create a Feature Worktree

```bash
./bin/vnx new-worktree my-feature --branch feature/my-feature --base main
```

This creates a git worktree, initializes isolated `.vnx-data`, bootstraps skills/terminals/hooks, merges settings, and validates with `./bin/vnx doctor`.

### 2. Work in the Worktree

```bash
cd ../your-project-wt-my-feature
./bin/vnx start
```

### 3. Monitor Session State

```bash
./bin/vnx status    # Session overview: terminals, queue, open items
./bin/vnx ps        # Process health with PID metadata
```

### 4. Pre-Merge Check

```bash
./bin/vnx merge-preflight my-feature
```

Returns GO or NO-GO based on: git cleanliness, open items, PR queue status, active processes, and gate-check results.

### 5. Close the Worktree

```bash
./bin/vnx finish-worktree my-feature --delete-branch
```

Runs merge-preflight, stops worktree processes, merges intelligence back to main, removes worktree.

### Settings Management

VNX settings are patch-managed -- VNX updates only its owned keys:

```bash
./bin/vnx regen-settings --merge   # Update VNX keys, preserve project config
```

### Shell Helper

For global `vnx` access from any project directory:

```bash
./bin/vnx install-shell-helper   # Adds vnx() to ~/.zshrc or ~/.bashrc
```

The helper walks up from CWD to find the project-local `.vnx/bin/vnx` or `.claude/vnx-system/bin/vnx`.

> **Deprecated**: Per-terminal worktrees are deprecated. Use `./bin/vnx new-worktree` for all new development.

## Appendix A: Two binaries

VNX ships TWO `vnx` entry-points with different scopes:
- **`vnx`** (pip-installed Python CLI at `vnx_cli/main.py`): user-facing essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`, `version`, `update`).
- **`./bin/vnx`** (bash CLI in the repo): operator + automation surface (`gate-check`, `new-worktree`, `finish-worktree`, `merge-preflight`, `demo`, `start`, `recover`, `cost-report`). Run from the repo root.

This split is intentional: the pip surface is stable + minimal; the bash surface is rich + repo-local.

---
