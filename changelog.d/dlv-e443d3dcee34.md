- Worktree salvage (track `worktree-opruiming-structureel`, PR-2a): the new `scripts/lib/worktree_salvage.py`
  preserves a worktree's unpushed commits and tracked work before any later removal. Each salvage writes
  into a directory of its own (`<name>-<UTC>-<head[:8]>`, created with `os.mkdir`, a numeric suffix on
  collision), publishes every artifact with `os.link` so nothing overwrites a file that exists, and
  appends one line to `MANIFEST.txt`. Staged and unstaged work become separate patches, decided by
  `git status` rather than an empty diff, and `verify` returns an empty list only when every artifact
  exists, the bundle verifies and both patches apply in order. Untracked files are PR-2b's, so a tree
  with any is reported (`untracked_pending`) and never verifies clean. No caller and no removal yet.
