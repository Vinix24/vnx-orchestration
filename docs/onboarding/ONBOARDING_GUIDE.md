# VNX Onboarding Guide

> From a fresh pip install to your first dispatched task, then onward to the
> repo-local operator workflow.

This guide separates the two supported command surfaces. Plain `vnx` is the
pip-installed Python CLI for user-facing essentials. Repo-local automation,
tmux orchestration, worktrees, gates, recovery, and cost reports run through
`./bin/vnx` from a cloned `vnx-orchestration` repository.

---

## Part 1: Starter Path (Pip CLI)

### Step 1: Install

```bash
pip install vnx-orchestration
vnx version
```

For the repo-local operator surface (Part 2), clone the repository and run
`pip install -e .` from the checkout instead.

**Prerequisites.** VNX drives existing coding CLIs as subprocesses — it does not run models itself.
The default dispatch lane needs an **installed + authenticated `claude` CLI** on your PATH (other
lanes: `codex`, `gemini`, `kimi`), and using it incurs that provider's subscription/credit usage.
`vnx dispatch-agent` (Step 4) fails at spawn without one; `vnx doctor` flags this as a
`tool:worker-cli` warning.

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

The full pip command set is listed once, in [Appendix A](#appendix-a-two-binaries-and-the-full-pip-cli-surface) below.

---

## Part 2: Operator Path (Repo-Local Bash CLI)

Use this path when you need the full VNX operator surface: tmux sessions,
queue promotion, gate checks, worktrees, recovery, and cost reports.

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

### Step 3: Launch T0

```bash
./bin/vnx start
```

This opens a tmux session with a single T0 orchestrator pane, not a fixed T1-T3
grid. T1/T2/T3 workers are dispatched as ephemeral per-dispatch processes
through the single-entry door (`./bin/vnx dispatch`, the `claude_headless` lane
for claude/Opus/Sonnet), not as pre-opened panes. A terminal can opt into a
terminal-pinned worker instead with `VNX_ADAPTER_T{n}=subprocess`. See
`docs/core/DISPATCH_RULES.md` §8 for the lane mechanics.

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

### Step 7: Session Intelligence

```bash
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

## Appendix A: Two binaries and the full pip CLI surface

VNX ships TWO `vnx` entry-points with different scopes. This is the single
source for the pip surface; other docs link here instead of repeating the
list.

- **`vnx`** (the pip-installed Python CLI, `vnx_cli/main.py`). Full command
  set: `init`, `doctor`, `fabric-audit`, `status`, `subsystems`,
  `dispatch-agent`, `track`, `pool`, `role`, `update`, `release`, `migrate`,
  `attest`, `horizon` (alias `objective`), `deliverable`, `handoff`,
  `learning`, `dream`, `gate-check`, `pr-ready`, `worktree-release`, `version`.
  Run `vnx --help` for the authoritative, versioned list.
- **`./bin/vnx`** (the bash CLI in a cloned repo checkout). Operator +
  automation surface: `start`, `stop`, `new-worktree`, `finish-worktree`,
  `merge-preflight`, `staging-list`, `promote`, `gate-check`, `pr-ready`,
  `cost-report`, `recover`, `ps`, `analyze-sessions`, `regen-settings`,
  `install-shell-helper`, and more. Run from the repo root; `./bin/vnx --help`
  lists the full set.

`gate-check`, `pr-ready`, and `worktree-release` exist on **both** binaries:
same underlying machinery (`scripts/pre_merge_gate.py`, `scripts/pr_ready.py`,
`scripts/lib/worktree_release.py`), exposed on the pip CLI too (OI-1135,
OI-1389) so a consumer repo without a `bin/` directory (Mission Control,
SEOcrawler_v2, sales-copilot) can still gate and release.

This split is intentional: the pip surface is stable + minimal; the bash surface is rich + repo-local.
