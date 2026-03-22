# Feature Request: Native Worktree State Isolation

**Status**: Proposed
**Priority**: P2
**Component**: vnx core (bin/vnx, vnx_paths.sh)
**Date**: 2026-03-11

## Problem

When running VNX in a git worktree (e.g. `SEOcrawler_v2-wt-fp10`), all vnx scripts resolve `PROJECT_ROOT` back to the main repo because `cd + pwd` and `Path.resolve()` follow symlinks. This causes dispatches, receipts, and reports to land in the main repo's `.vnx-data/` instead of the worktree's own `.vnx-data/`, leading to state pollution between orchestrators.

### Current Workaround (FP10 Hotfix)

A minimal patch was applied to preserve `VNX_DATA_DIR` env override through the path resolution chain:
- `vnx_paths.sh`: preserves `VNX_DATA_DIR` when resetting stale `PROJECT_ROOT`
- `bin/vnx`: preserves `VNX_DATA_DIR` before unsetting env vars
- `bin/vnx`: sources `.vnx-data/.env_override` if present in CWD
- Worktree gets a `.vnx-data/.env_override` file that sets `VNX_DATA_DIR`

This works but requires manual setup per worktree.

## Proposed Feature: `vnx worktree-start`

A first-class `vnx worktree-start` command that automates worktree state isolation.

### User Flow

```bash
# From the worktree directory
cd /path/to/project-wt-feature
vnx worktree-start
```

### What It Should Do

1. **Detect worktree context**: Check if CWD is a git worktree (`git rev-parse --show-toplevel` vs `git rev-parse --git-common-dir`)
2. **Initialize `.vnx-data/`**: Run `vnx init` scoped to the worktree
3. **Create `.env_override`**: Auto-generate `.vnx-data/.env_override` with correct `VNX_DATA_DIR`
4. **Validate isolation**: Run a quick check that `vnx_paths.sh` resolves to worktree's `.vnx-data/`
5. **Print confirmation**: Show resolved paths for user verification

### Optional Enhancements

- **`vnx worktree-status`**: Show which `.vnx-data/` is active and whether isolation is working
- **`vnx worktree-stop`**: Clean up worktree-specific pids/locks before deletion
- **Auto-detect in `vnx start`**: If running inside a worktree without `.env_override`, warn the user and offer to create one
- **`PROJECT_ROOT` resolution without symlink-following**: Use `pwd -L` (logical) instead of `pwd -P` (physical) in path resolution to avoid symlink issues at the root cause level

### Path Resolution Hardening

Consider replacing symlink-following path resolution throughout:

```bash
# Current (follows symlinks):
_VNX_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Alternative (preserves logical path):
_VNX_PATHS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -L)"
```

And in Python:
```python
# Current (follows symlinks):
here = Path(__file__).resolve()

# Alternative (preserves logical path):
here = Path(__file__).absolute()
```

This would fix the root cause rather than working around it with env overrides. However, this needs careful testing as some scripts may depend on resolved paths for correctness.

## Existing Infrastructure

- **`vnx_worktree_setup.sh`**: Sets up T1-T3 sub-worktrees (works correctly)
- **`vnx_worktree_merge_data.sh`**: Merges worktree `.vnx-data/` back to main after feature completion (works correctly)
- **`.env_override` sourcing**: Already implemented in `bin/vnx` as part of FP10 hotfix

## Files Involved

| File | Role |
|---|---|
| `bin/vnx` | CLI entrypoint, env bootstrap |
| `scripts/lib/vnx_paths.sh` | Shared path resolver |
| `scripts/lib/vnx_paths.py` | Python path resolver (already respects env) |
| `scripts/vnx_worktree_setup.sh` | Sub-worktree setup |
| `scripts/vnx_worktree_merge_data.sh` | End-of-feature data merge |

## Acceptance Criteria

- [ ] `vnx worktree-start` creates `.vnx-data/.env_override` with correct paths
- [ ] `vnx doctor` detects and warns about worktree without isolation
- [ ] State from worktree orchestrator never appears in main repo's `.vnx-data/`
- [ ] Existing `vnx_worktree_merge_data.sh` still works for end-of-feature merging
- [ ] No breaking changes to non-worktree VNX usage
