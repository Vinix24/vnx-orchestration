# VNX in 5 Minutes

This quickstart uses the pip-installed Python CLI. Plain `vnx` commands below
are limited to the stable pip surface: `init`, `migrate`, `doctor`, `status`,
`dispatch-agent`, `track`, `pool`, `dream`, `version`, and `update`.

## Step 1: Install

The package is not on PyPI yet — publishing is the final 1.0 ship gate. Until
then, install from a checkout:

```bash
git clone https://github.com/Vinix24/vnx-orchestration
cd vnx-orchestration
pip install -e .
vnx version
```

For repo-local operator commands such as gates, worktrees, and tmux sessions,
clone the repository and run `./bin/vnx` from the repo root. See Appendix A.

## Step 2: Initialize

```bash
mkdir -p my-vnx-project
cd my-vnx-project
vnx init
vnx doctor
```

`vnx init` creates the tracked project scaffold (`.vnx/`, `.vnx-project-id`,
and `agents/`) and a resolved runtime state directory.

## Step 3: Run the Hello-World Demo

The `hello-world` example agent ships with VNX. No files to create:

```bash
vnx dispatch-agent --agent hello-world
```

`dispatch-agent` finds the packaged `hello-world` example automatically and uses
its built-in default instruction. Pass `--instruction "..."` to override.

## Step 4: Dispatch with a Custom Instruction

```bash
vnx dispatch-agent --agent hello-world --instruction "Write a greeting for a new VNX user"
```

The pip CLI validates `examples/hello-world/CLAUDE.md`, creates a dispatch ID,
and routes the instruction through the packaged dispatch engine.

## Step 5: Check Status

```bash
vnx status
```

Use `vnx status --json` when you need machine-readable project state.

## Step 6: Operator Gate Check

Quality gates are repo-local operator commands. From a cloned
`vnx-orchestration` repo root, run:

```bash
./bin/vnx gate-check --pr 1
```

Operator commands run via the bash entrypoint `bin/vnx`. See Appendix A.

## What's Next?

- Create your own agent: [Agent Creation Guide](guides/AGENT_CREATION_GUIDE.md)
- Full documentation: [README](../README.md)
- Architecture: [docs/manifesto/ARCHITECTURE.md](manifesto/ARCHITECTURE.md)

## Appendix A: Two binaries

VNX ships TWO `vnx` entry-points with different scopes:
- **`vnx`** (pip-installed Python CLI at `vnx_cli/main.py`): user-facing essentials (`init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`, `version`, `update`).
- **`./bin/vnx`** (bash CLI in the repo): operator + automation surface (`gate-check`, `new-worktree`, `finish-worktree`, `merge-preflight`, `start`, `recover`, `cost-report`). Run from the repo root.

This split is intentional: the pip surface is stable + minimal; the bash surface is rich + repo-local.
