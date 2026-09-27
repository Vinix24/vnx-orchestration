# Getting Started (VNX)

**Status**: Active
**Last Updated**: 2026-09-27
**Owner**: T-MANAGER
**Purpose**: Quick orientation and links to the current VNX "source of truth" docs.

---

## Start Here

New to VNX? Follow [`docs/onboarding/ONBOARDING_GUIDE.md`](../onboarding/ONBOARDING_GUIDE.md)
end to end: pip install, first dispatch, then the repo-local operator path
(`./bin/vnx`), feature worktrees, and troubleshooting.

For a five-minute version of the pip-only path, see [`docs/QUICKSTART.md`](../QUICKSTART.md).

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

```bash
# Initialize VNX in a new project with the pip CLI
vnx init

# Health check
vnx doctor

# Project status
vnx status
```

```bash
# Launch the T0 orchestrator in a tmux session, from a repo checkout
./bin/vnx start

# Stop all processes
./bin/vnx stop

# Token cost report
./bin/vnx cost-report

# Operator recovery
./bin/vnx recover
```

`./bin/vnx start` opens a tmux session with a single T0 orchestrator pane, not
a fixed T1-T3 grid. T1/T2/T3 workers run as ephemeral per-dispatch processes
through the single-entry door (`./bin/vnx dispatch`) unless a terminal opts
into a terminal-pinned worker with `VNX_ADAPTER_T{n}=subprocess`; see
`DISPATCH_RULES.md` §8.

The full pip-vs-bash command list and the two-binary split live in one place:
[Onboarding Guide, Appendix A](../onboarding/ONBOARDING_GUIDE.md#appendix-a-two-binaries-and-the-full-pip-cli-surface).

### Key Bindings (in tmux)
- `Ctrl+G` — Open dispatch queue popup
- `Ctrl+B D` — Detach (keeps running)
- Mouse — Click to switch panes

---

## Feature Development Workflow

Feature worktrees, the gate workflow, and daily operator commands are covered
in [Onboarding Guide, Part 2](../onboarding/ONBOARDING_GUIDE.md#part-2-operator-path-repo-local-bash-cli).

### Shell Helper

For global `vnx` access from any project directory:

```bash
./bin/vnx install-shell-helper   # Adds vnx() to ~/.zshrc or ~/.bashrc
```

The helper walks up from CWD to find the project-local `.vnx/bin/vnx` or
`.claude/vnx-system/bin/vnx` (legacy layout).

---
