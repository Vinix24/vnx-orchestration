# Multi-Agent Git Isolation: Industry Research & VNX Architecture Decision

> **Status**: This research document is partially superseded by the implementation in PR-3 (One-Command Worktree Creation) and PR-4 (Governance-Aware Finish Flow).
>
> **Current standard**: One feature worktree per feature/fix branch via `vnx new-worktree <name>`.
> Per-terminal worktrees are **deprecated** (`VNX_WORKTREES=false` is the default since VNX V8).
> The isolation model described here (isolated `.vnx-data`, intelligence snapshots, `.env_override`) has been implemented as designed.

**Date**: 2026-03-09
**Author**: Vincent van Deth (VNX Digital)
**Status**: Adopted
**Scope**: How parallel AI coding agents isolate filesystem and git state

## Problem Statement

When multiple AI agents work on the same codebase simultaneously, they conflict on:
- File writes (agent A overwrites agent B's changes)
- Git staging area (shared `git add` / `git commit` state)
- Working directory state (dirty files from one agent affect another)

Every serious multi-agent coding framework must solve this. This document surveys how the industry handles it (2025-2026) and documents VNX's architectural decision.

## Industry Survey

### Comparison Matrix

| Framework | Isolation Method | Branch Naming | Parallel Agents | Merge Strategy | Persistence |
|-----------|-----------------|---------------|-----------------|----------------|-------------|
| **Claude Code** (native) | Git worktree | `worktree-<name>` | Per agent-call | Auto-cleanup, manual merge | Ephemeral (per call) |
| **Cursor 2.0** | Git worktree | `feat-<N>-<random>` | Up to 8 | UI: "Merge" or "Full Overwrite" | Session-scoped |
| **OpenAI Codex** | Git worktree + sandbox | Per-task branch | Unlimited | Standard git merge | Session-scoped |
| **Devin / MultiDevin** | Cloud VM per worker | Manager-assigned | 1 manager + 10 workers | Manager merges successful workers | VM lifecycle |
| **SWE-agent** | Docker container | Git patch output | Trivially parallel | Patch apply | Container lifecycle |
| **OpenHands** | Docker container | No cross-agent state | Per-task | Container teardown | Ephemeral |
| **ComposioHQ** | Git worktree | `sessionPrefix-*` | 30 agents, 40 worktrees | Auto-PR per agent | Persistent |
| **ccswarm** | Git worktree | Per-specialisation | Agent pools (FE/BE/DevOps/QA) | Zero shared state (actor model) | Session-scoped |
| **Aider** | None | Direct on current branch | Single agent only | `/undo` rollback | N/A |
| **MetaGPT / ChatDev** | None (in-memory) | No git awareness | In-memory coordination | Generates complete projects | N/A |
| **VNX Orchestration** | Git worktree | `track/A`, `track/B`, `track/C` | 3 workers + 1 orchestrator | T0 creates PR, squash-merges | Persistent (tmux) |

### Detailed Analysis

#### Git Worktrees (Industry Consensus)

**Claude Code** uses worktrees per agent-call via `isolation: "worktree"`. Each subagent gets a temporary worktree that auto-cleans if no changes are made. This is ephemeral — the worktree disappears after the agent call completes. Not suitable for persistent terminal-based orchestration.

**Cursor 2.0** (released late 2025) introduced parallel background agents, each in their own worktree at `~/.cursor/worktrees/<repo>/<id>/`. Up to 8 agents run simultaneously. Branch names are auto-generated (`feat-3-a8f2d`). Users merge via UI with explicit "Full Overwrite" or "Merge" choices.

**OpenAI Codex** sandboxes each agent in a worktree with network disabled by default. Each task gets its own branch. Standard git merge since all worktrees share the `.git` object database.

**ComposioHQ agent-orchestrator** scales to 30 concurrent agents across 40 worktrees. Each agent gets its own worktree, branch, and auto-created PR. Uses `sessionPrefix` for branch naming. Auto-handles CI failures and review comments.

**ccswarm** organizes agents into specialised pools (Frontend, Backend, DevOps, QA), each in isolated worktrees. Uses an actor model with zero shared mutable state between agents.

#### Container/VM-Based Isolation

**SWE-agent** runs each task in a Docker container. Output is a git patch file, not a live branch. This makes parallelisation trivial but loses real-time git history. Uses SWE-ReX runtime for sandboxed execution.

**OpenHands** (formerly OpenDevin) gives each agent a Docker container, torn down post-session. No cross-agent interference at filesystem level. Clean but heavyweight.

**Devin / MultiDevin** provides a full cloud VM per instance. MultiDevin has 1 manager + up to 10 workers. The manager selects which workers' changes to merge into a single PR against a configurable root branch.

#### No Isolation

**Aider** commits directly to the current branch with an `(aider)` author tag. No parallel support. Relies on `/undo` for rollback. Simple but single-agent only.

**MetaGPT / ChatDev** coordinate agents in-memory. No git awareness. They generate complete projects rather than incremental changes to existing codebases.

### Key Insight: No One Has Solved Automated Merge

Every framework punts on automated conflict resolution:

1. **Human reviews PR** — most common (Claude Code, Codex, ComposioHQ)
2. **UI-based merge/overwrite** — Cursor 2.0
3. **Patch application** — SWE-agent
4. **Manager agent merges successful workers** — Devin MultiDevin
5. **Auto-retry CI on failure** — ComposioHQ agent-orchestrator

Conflict resolution remains a human responsibility across the industry.

## VNX Architecture Decision

### Chosen Approach: Persistent Worktrees with Track Branches

```
SEOcrawler_v2/              <- T0 (orchestrator, stays on main)
SEOcrawler_v2-wt-T1/        <- T1 worktree, branch: track/A
SEOcrawler_v2-wt-T2/        <- T2 worktree, branch: track/B
SEOcrawler_v2-wt-T3/        <- T3 worktree, branch: track/C
```

### Why Worktrees Over Containers

| Factor | Worktrees | Containers |
|--------|-----------|------------|
| Setup overhead | ~50MB per worktree | ~500MB+ per container |
| Shared .venv | Symlink (zero cost) | Must copy or mount |
| Git history | Real branches, shared objects | Patch files or volume mounts |
| Persistence | Survives tmux detach/reattach | Must rebuild on restart |
| IDE compatibility | Full (any editor can open) | Requires remote dev setup |
| macOS native | Yes | Docker Desktop required |

For a solopreneur on macOS with tmux-based orchestration, worktrees are the clear winner.

### Why `track/X` Over `feat/descriptive-name`

Conventional branch naming (`feat/...`, `fix/...`) solves two problems:
1. **Team communication** — "what does this branch do?"
2. **CI triggers** — pipelines that react to branch prefixes

Neither applies here:
1. AI workers get context via dispatch prompts, not branch names
2. CI triggers on PR creation, not branch prefix

The PR title carries the semantic meaning (`feat: add Mollie webhook idempotency`). Track branches are disposable work surfaces — the PR is what matters in git history.

### Why T0 as Merge Authority (Like Devin MultiDevin)

VNX's model mirrors Devin MultiDevin:
- 1 orchestrator (T0) + N workers (T1-T3)
- Orchestrator decides when to merge
- Workers never touch main

This is the safest pattern: workers produce, orchestrator governs.

### CI Strategy

| Stage | When | What runs | Cost |
|-------|------|-----------|------|
| Per-dispatch | After each task | Local tests (in-terminal) | Seconds, free |
| Per-track completion | On PR creation | Full test suite (CI) | ~2-3 runs/day |
| Post-merge | After PR merge to main | Optional integration check | ~1 run/day |

### Merge Workflow

```
1. T1 works on track/A (multiple dispatches, multiple commits)
2. T1 pushes track/A
3. T0 creates PR: track/A -> main (descriptive title)
4. CI runs on PR branch
5. T0 squash-merges PR -> 1 clean commit on main
6. T0 runs: vnx_worktree_setup.sh sync
7. All worktrees rebase on updated main
```

## Implementation Files

| Component | File | Purpose |
|-----------|------|---------|
| Worktree setup | `scripts/vnx_worktree_setup.sh` | `init-terminals`, `sync`, `list` commands |
| Terminal state | `scripts/lib/terminal_state_shadow.py` | `worktree_path` field, getters/setters |
| CLI wrapper | `scripts/terminal_state_shadow.py` | `get-worktree`, `set-worktree` subcommands |
| Dispatcher | `scripts/dispatcher_v8_minimal.sh` | cd to worktree, `Working-Directory:` header |
| Metadata extraction | `scripts/lib/dispatch_metadata.sh` | `vnx_dispatch_extract_working_directory()` |
| Receipt provenance | `scripts/append_receipt.py` | `worktree_path` in git provenance |
| Start script | `bin/vnx` | Auto-init worktrees at `vnx start` |

## References

- [Claude Code Worktrees](https://code.claude.com/docs/en/common-workflows)
- [Cursor 2.0 Parallel Agents](https://cursor.com/changelog/2-0)
- [OpenAI Codex Parallel Worktrees](https://developers.openai.com/codex/app/features/)
- [Devin MultiDevin](https://cognition.ai/blog/devin-2)
- [SWE-agent](https://github.com/SWE-agent/SWE-agent)
- [ComposioHQ agent-orchestrator](https://github.com/ComposioHQ/agent-orchestrator)
- [ccswarm](https://github.com/nwiizo/ccswarm)
- [Git Worktrees for AI Agents](https://devcenter.upsun.com/posts/git-worktrees-for-parallel-ai-coding-agents/)
