- Worktree salvage (track `worktree-opruiming-structureel`, PR-2a): the new `scripts/lib/worktree_salvage.py`
  preserves a worktree's unpushed commits and tracked work before any later removal. Each salvage writes
  into a directory of its own (`<name>-<UTC>-<head[:8]>`, created with `os.mkdir`, a numeric suffix on
  collision), publishes every artifact with `os.link` so nothing overwrites a file that exists, and
  appends one line to `MANIFEST.txt`. Staged and unstaged work become separate patches, decided by
  `git status` rather than an empty diff, and `verify` returns an empty list only when every artifact
  exists, the bundle verifies and both patches apply in order. Untracked files are PR-2b's, so a tree
  with any is reported (`untracked_pending`) and never verifies clean. No caller and no removal yet.
- Worktree salvage, round 2: `verify` no longer takes `git bundle verify`'s word for a bundle. That
  command reads the header and the prerequisites and never a packed object, so a bundle truncated
  right after its header still verified clean. After it passes, the bundle must unpack inside a
  throwaway bare repository that borrows the checkout's objects through `objects/info/alternates`
  (nothing is written into the checkout) and `head` must be a commit whose carried objects
  `rev-list --objects` can walk. Any failure is named `bundle_unpack_failed: <reason>` and keeps the
  salvage from ever reading as safe to remove.
- Worktree salvage, round 3: `verify` no longer lends the scratch repository the checkout's whole
  object store through `objects/info/alternates`, which made every object the checkout had count as
  "carried by the bundle": a bundle with a valid header over a valid zero-object pack verified clean.
  The scratch repository is now given only the closure of the bundle's prerequisites, fetched from the
  checkout by SHA (`git fetch --upload-pack='git -c uploadpack.allowAnySHA1InWant=true upload-pack'`,
  because `-c` on the client does not reach the upload-pack git spawns for a local path; `--no-tags`
  keeps it to the requested closure), and a bundle without prerequisites starts from an empty one.
  Reading the header also stopped dropping every prerequisite on the floor: `_bundle_prerequisites`
  now appends the `-<sha>` line it parses instead of returning an empty list.
