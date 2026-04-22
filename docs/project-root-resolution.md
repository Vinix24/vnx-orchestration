# Project-Root Resolution Guide

**Issue:** [#225 — Cross-project state pollution via leaked env vars](https://github.com/Vinix24/vnx-orchestration/issues/225)

## Problem

When running VNX scripts across multiple projects or git worktrees, shell environment variables like `VNX_CANONICAL_ROOT` and `VNX_DATA_DIR` can leak from one project context into another. This causes scripts to read from or write to the wrong `.vnx-data/` directory — silently corrupting state without any error.

Example failure scenario:

```bash
# Terminal opened in project-A sets:
export VNX_CANONICAL_ROOT=/Users/me/project-a

# Then cd into project-B and run a VNX script:
cd /Users/me/project-b
python3 scripts/dispatcher.py   # resolves to project-A's data dir — wrong!
```

## Solution

All VNX scripts resolve project root via git, not env vars. The git-based approach is worktree-aware: each worktree returns its own root, preventing cross-project pollution.

Helper libraries ship in two flavors:

| Language | File |
|----------|------|
| Python   | `scripts/lib/project_root.py` |
| Bash     | `scripts/lib/vnx_resolve_root.sh` |

## Resolution Order

Both libraries follow the same resolution order:

1. **`git rev-parse` from caller's physical location** — symlink-resolved directory of the calling script. Most reliable; always correct for scripts run from within the repo.
2. **`git rev-parse` from current working directory** — fallback when caller path is unknown (e.g. interactive use).
3. **`VNX_CANONICAL_ROOT` env var** — last resort, with `DeprecationWarning`. Will be removed in v0.10.0.
4. **Error / return 1** — raises `RuntimeError` (Python) or returns exit code 1 (bash).

## Python API

```python
from scripts.lib.project_root import resolve_data_dir, resolve_state_dir

# Preferred: pass __file__ so resolution starts from the calling script's directory.
data_dir = resolve_data_dir(caller_file=__file__)
state_dir = resolve_state_dir(caller_file=__file__)

# Also available:
from scripts.lib.project_root import resolve_project_root, resolve_dispatch_dir

project_root = resolve_project_root(caller_file=__file__)
dispatch_dir = resolve_dispatch_dir(caller_file=__file__)
```

All functions accept `caller_file: str | None`. Passing `__file__` is strongly recommended — it anchors resolution to the script's own git repo, avoiding CWD surprises.

### Return values

| Function | Returns |
|----------|---------|
| `resolve_project_root` | `Path` — git toplevel |
| `resolve_data_dir` | `Path` — `$PROJECT_ROOT/.vnx-data` |
| `resolve_state_dir` | `Path` — `$PROJECT_ROOT/.vnx-data/state` |
| `resolve_dispatch_dir` | `Path` — `$PROJECT_ROOT/.vnx-data/dispatches` |

## Bash API

```bash
# At the top of your script:
source "$(dirname "${BASH_SOURCE[0]}")/lib/vnx_resolve_root.sh"

# Resolve project root (pass caller file for accuracy):
vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"

# Then derive data/state/dispatch dirs:
vnx_resolve_data_dir
vnx_resolve_state_dir
vnx_resolve_dispatch_dir

# Now use the exported vars:
echo "$VNX_PROJECT_ROOT"
echo "$VNX_DATA_DIR"
echo "$VNX_STATE_DIR"
echo "$VNX_DISPATCH_DIR"
```

For scripts inside `scripts/lib/`, the source path is relative:

```bash
source "$(dirname "${BASH_SOURCE[0]}")/vnx_resolve_root.sh"
```

### Exported variables

| Variable | Value |
|----------|-------|
| `VNX_PROJECT_ROOT` | git toplevel |
| `VNX_DATA_DIR` | `$VNX_PROJECT_ROOT/.vnx-data` |
| `VNX_STATE_DIR` | `$VNX_DATA_DIR/state` |
| `VNX_DISPATCH_DIR` | `$VNX_DATA_DIR/dispatches` |

## Escape Hatch: `VNX_DATA_DIR_EXPLICIT=1`

For CI pipelines or tests that must use a custom data directory, set both:

```bash
export VNX_DATA_DIR=/tmp/my-test-data
export VNX_DATA_DIR_EXPLICIT=1
```

When `VNX_DATA_DIR_EXPLICIT=1`, the value of `VNX_DATA_DIR` is honored directly — no git resolution occurs. This is the only safe way to override the data directory without triggering a `DeprecationWarning`.

**Without** the explicit flag, `VNX_DATA_DIR` is ignored and a warning is emitted:

```
DeprecationWarning: VNX_DATA_DIR env-var set (/tmp/stale) but
VNX_DATA_DIR_EXPLICIT=1 is required for it to be honored.
```

## Migration: Replacing Old Patterns

### Python

```python
# Before (unsafe — env var may be from another project):
import os
data_dir = os.environ.get("VNX_DATA_DIR", "/fallback/.vnx-data")

# After (git-based, always correct):
from scripts.lib.project_root import resolve_data_dir
data_dir = resolve_data_dir(caller_file=__file__)
```

### Bash

```bash
# Before (unsafe):
DATA_DIR="${VNX_DATA_DIR:-$VNX_CANONICAL_ROOT/.vnx-data}"

# After (git-based):
source "$(dirname "${BASH_SOURCE[0]}")/lib/vnx_resolve_root.sh"
vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"
vnx_resolve_data_dir
# Use $VNX_DATA_DIR
```

## Troubleshooting

**`RuntimeError: Cannot resolve project root`**

The script is being run outside a git repository and `VNX_CANONICAL_ROOT` is not set. Ensure you're running from within the cloned repo, or set `VNX_DATA_DIR_EXPLICIT=1` + `VNX_DATA_DIR` for CI use.

**`DeprecationWarning: VNX_CANONICAL_ROOT env-var used`**

Your shell has `VNX_CANONICAL_ROOT` set from an older session or profile. Unset it and rely on git resolution:

```bash
unset VNX_CANONICAL_ROOT
```

**`DeprecationWarning: VNX_DATA_DIR env-var set but VNX_DATA_DIR_EXPLICIT=1 is required`**

You have `VNX_DATA_DIR` set but without the guard flag. Either unset `VNX_DATA_DIR` or add `VNX_DATA_DIR_EXPLICIT=1` if the override is intentional.

**Worktree resolves to wrong root**

Pass `caller_file=__file__` (Python) or `"${BASH_SOURCE[0]}"` (bash). Without a caller file, resolution falls back to CWD — which may point to the wrong worktree if you changed directories.

## Examples

### Running a script from project root

```bash
cd /Users/me/my-project
python3 scripts/dispatcher.py          # resolves to /Users/me/my-project
```

### Running a script from a worktree

```bash
cd /Users/me/my-project-wt            # git worktree of my-project
python3 scripts/dispatcher.py          # resolves to /Users/me/my-project-wt
```

Both cases produce the correct `.vnx-data/` path for their respective tree.

### CI with custom data dir

```yaml
- run: |
    export VNX_DATA_DIR=/tmp/ci-data
    export VNX_DATA_DIR_EXPLICIT=1
    python3 scripts/health_check.py
```
