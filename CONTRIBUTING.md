# Contributing to VNX

## Development Workflow

1. **Create a feature worktree**:
   ```bash
   vnx new-worktree my-feature --branch feature/my-feature
   cd ../your-project-wt-my-feature
   ```

2. **Start VNX session**:
   ```bash
   vnx start
   ```

3. **Work within dispatches** -- T0 creates dispatches, workers execute scoped tasks.

4. **Settings management** -- if you need to update VNX settings:
   ```bash
   vnx regen-settings --merge  # Patches VNX-owned keys only
   ```

5. **Pre-merge verification**:
   ```bash
   vnx merge-preflight my-feature   # Check governance state
   vnx gate-check --pr PR-X         # Run deterministic gate checks
   ```

6. **Close the worktree**:
   ```bash
   vnx finish-worktree my-feature --delete-branch
   ```

## Code Standards

- All shell changes must pass `bash -n`
- PRs should be 150-300 lines
- Every change goes through a dispatch
- `.vnx-data/` is runtime state -- never commit it

## Layout

`.vnx/` is the primary layout. Legacy `.claude/vnx-system/` is compatibility-only.
