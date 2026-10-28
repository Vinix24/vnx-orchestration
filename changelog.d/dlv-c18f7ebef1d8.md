- Worktree salvage PR-2b (`scripts/lib/worktree_salvage.py`): `salvage()` now also preserves the
  work a tree holds outside the index as `<name>.untracked.tgz` plus `<name>.untracked.list`, and
  `verify()` proves both. The archive carries every untracked (`?`) and every non-throwaway ignored
  (`!`) status entry; entries below `.vnx-data/` or `.git/` stay uncovered and keep blocking removal
  as evidence, an ignored throwaway cache stays excused, and an untracked symlink into the checkout
  (`worktree_inventory.is_internal_symlink`) is covered without being carried. A directory entry —
  git reports a nested repository as one `nested/` record — is walked, so every file and symlink
  inside it, a nested `.git` included, lands in the archive; an empty directory is a `d` member of
  its own and a symlink is archived as a symlink, never followed. The list holds one NUL-terminated
  `kind size sha256 path` record per member, NUL-safe for any filename. `verify()` parses the list,
  opens the archive, proves both hold the same members with the same kind, size and sha256, and
  re-walks the tree: a listed file edited or deleted after the salvage, a member the tree gained and
  the list never carried, an archive without its list or a list without its archive are each a named
  problem. This is the half of OI-2361 the closed #2214 failed: the archive held only the directory
  record, so a removal would have deleted `nested/precious.txt` while verify passed.
- Worktree salvage PR-2b round 2 (`scripts/lib/worktree_salvage.py`): the untracked archive keeps the
  permission bits of every regular file as they are on disk (`stat.S_IMODE` of the `lstat`, setuid,
  setgid and sticky masked out), so an untracked `run.sh` at 0755 restores executable instead of
  being flattened to 0644; directories and symlinks keep their fixed modes. The list record gains a
  mode field (`kind size sha256 mode path`) and `verify()` names a member whose archive mode or
  on-disk mode differs from the recorded one, and names a list written before modes were kept
  ("list without modes") instead of passing it. An archive truncated to half its bytes is a named
  problem too: `EOFError` and `zlib.error` while reading it are translated like the other read
  failures, never raised at the caller.
