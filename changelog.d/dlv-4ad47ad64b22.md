- Worktree inventory (worktree-opruiming-structureel PR-1): a new read-only
  `scripts/lib/worktree_inventory.py` records every worktree git knows about — dispatch,
  gate, pool, bench and external — with its lock state, dirty-path count, commits no
  `origin/*` ref reaches and age in days. `cleanup_reviewed_worktrees.py --all` prints it
  (table, or `--json`) and evaluates, locks and releases nothing; the default run is
  byte-identical to before.
