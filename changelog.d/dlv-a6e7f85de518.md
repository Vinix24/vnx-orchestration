- Worktree salvage (WT-2a1b, OI-2417 points 2 and 4): `verify()` still refuses every tree state the
  salvage did not preserve, except the caches a worker's tree fills on its own. An IGNORED status
  entry is now covered when one of its path components is in `THROWAWAY_IGNORED_DIRS`
  (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`) and no `.vnx-data/`
  or `.git/` component precedes it. Both shapes count — the directory entry (`pkg/__pycache__/`) and
  the file entry inside it (`pkg/__pycache__/m.cpython-312.pyc`). Nothing else widens: a tracked,
  staged, untracked or unmerged entry blocks whatever its path, an ignored `.vnx-data/` entry blocks
  (its own note file or a cache directory inside it), and so does an ignored `build/`. No caller, CLI
  flag or removal is added; submodule bundling (OI-2411) is still open.
- Worktree salvage (WT-2a1b, round 2): `verify()` now matches `THROWAWAY_IGNORED_DIRS` only against
  DIRECTORY components of an ignored status entry — every component but the last, plus the last only
  when git marks it a directory with a trailing `/`. An ignored regular FILE that merely shares a cache
  directory name (`node_modules`, `pkg/__pycache__`) stays uncovered and is named, because no bundle
  carries an ignored file and approving its loss would lose unpreserved data.
