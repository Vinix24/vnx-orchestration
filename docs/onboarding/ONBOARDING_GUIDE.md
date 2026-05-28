# VNX Onboarding Guide

> From a fresh pip install to your first dispatched task, then onward to the
> repo-local operator workflow.

This guide separates the two supported command surfaces. Plain `vnx` is the
pip-installed Python CLI for user-facing essentials. Repo-local automation,
tmux orchestration, worktrees, gates, recovery, demos, and cost reports run
through `./bin/vnx` from a cloned `vnx-orchestration` repository.

---

## Part 1: Starter Path (Pip CLI)

### Step 1: Install

```bash
python3 -m pip install vnx-orchestration
vnx version
```

### Step 2: Initialize a Project

```bash
mkdir -p my-vnx-project
cd my-vnx-project
vnx init
vnx doctor
vnx status
```

`vnx init` creates `.vnx/`, `.vnx-project-id`, an `agents/` scaffold, and a
resolved runtime state directory.

### Step 3: Create the Hello-World Agent

`vnx dispatch-agent` accepts either `agents/<name>/CLAUDE.md` or
`examples/<name>/CLAUDE.md`.

```bash
mkdir -p examples/hello-world
cat > examples/hello-world/CLAUDE.md <<'EOF'
# Hello World Agent

Write a friendly, professional greeting for a new VNX user.

Create a file called greeting.md in the current directory with:
- A welcoming header
- 2-3 sentences about VNX
- Today's date
- A sign-off
EOF
```

### Step 4: Dispatch

```bash
vnx dispatch-agent --agent hello-world --instruction "Write a greeting for a new VNX user"
vnx status
```

This is the literal pip happy path: install, initialize, define an example
agent, and dispatch it through the stable Python CLI.

### Step 5: Learn the Pip Surface

```bash
vnx --help
vnx doctor --strict
vnx status --json
vnx pool --help
vnx update --dry-run
```

The pip CLI command set is intentionally small: `init`, `doctor`, `status`,
`dispatch-agent`, `pool`, `version`, and `update`.

---

## Part 2: Operator Path (Repo-Local Bash CLI)

Use this path when you need the full VNX operator surface: tmux sessions,
queue promotion, gate checks, worktrees, recovery, demos, and cost reports.

### Step 1: Clone and Install Operator Prerequisites

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration
brew install jq tmux fswatch
```

### Step 2: Initialize Operator Mode

```bash
./bin/vnx init --operator
./bin/vnx doctor
```

### Step 3: Launch the Grid

```bash
./bin/vnx start
```

This opens the 2x2 T0-T3 tmux grid used by the operator workflow.

### Step 4: Queue and Gate Workflow

```bash
./bin/vnx staging-list
./bin/vnx promote <dispatch-id>
./bin/vnx gate-check --pr <PR-ID>
```

Press `Ctrl+G` inside tmux for the visual dispatch queue popup.

### Step 5: Feature Worktrees

```bash
./bin/vnx new-worktree my-feature --base main
cd ../vnx-orchestration-wt-my-feature
./bin/vnx start
./bin/vnx merge-preflight my-feature
./bin/vnx finish-worktree my-feature --delete-branch
```

Worktrees get isolated runtime state so feature sessions do not overwrite the
main session.

### Step 6: Daily Operator Commands

```bash
./bin/vnx status
./bin/vnx ps
./bin/vnx cost-report
./bin/vnx recover
./bin/vnx stop
```

### Step 7: Demos and Session Intelligence

```bash
./bin/vnx demo
./bin/vnx demo --replay governance-pipeline
./bin/vnx demo --dashboard
./bin/vnx analyze-sessions
./bin/vnx suggest review
./bin/vnx suggest accept 1,3,5
./bin/vnx suggest apply
```

---

## Troubleshooting

### Pip CLI command not found

```bash
python3 -m pip install --upgrade vnx-orchestration
vnx version
```

### Operator command not found

Make sure you are in the cloned `vnx-orchestration` repository root and use the
bash entrypoint:

```bash
./bin/vnx --help
```

### `vnx doctor` reports failures

Doctor output is actionable. Common fixes:
- Missing `tmux`: `brew install tmux` (operator mode only)
- Missing `jq`: `brew install jq`
- Missing `fswatch`: `brew install fswatch` (operator mode only)

### Stale Operator State

```bash
./bin/vnx recover
./bin/vnx ps
```

---

## Next Steps

- **Example flows**: See [docs/examples/](../examples/) for realistic walkthroughs
- **Architecture**: [docs/manifesto/ARCHITECTURE.md](../manifesto/ARCHITECTURE.md)
- **Dispatch guide**: [docs/DISPATCH_GUIDE.md](../DISPATCH_GUIDE.md)
- **Limitations**: [docs/manifesto/LIMITATIONS.md](../manifesto/LIMITATIONS.md)
- **Comparisons**: [VNX vs Claude Code](../comparisons/vnx_vs_claude_code.md) | [VNX vs Frameworks](../comparisons/vnx_vs_frameworks.md)

## Appendix A: Two binaries

VNX ships TWO `vnx` entry-points with different scopes:
- **`vnx`** (pip-installed Python CLI at `vnx_cli/main.py`): user-facing essentials (`init`, `doctor`, `status`, `dispatch-agent`, `pool`, `version`, `update`).
- **`./bin/vnx`** (bash CLI in the repo): operator + automation surface (`gate-check`, `new-worktree`, `finish-worktree`, `merge-preflight`, `demo`, `start`, `recover`, `cost-report`). Run from the repo root.

This split is intentional: the pip surface is stable + minimal; the bash surface is rich + repo-local.
