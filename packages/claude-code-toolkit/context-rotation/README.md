## Maintainers

This section is for whoever publishes a release of the plugin. The plugin is developed in its source repository and exported, as one commit, into a local clone of the public toolkit repository. Nothing is pushed or tagged by the export.

### Export

Run the export script from the source repository, on a commit that is already merged into `main`:

```
scripts/maintenance/export_toolkit_plugin.sh \
  --commit <sha> \
  --target <path to the local toolkit clone> \
  --tag context-rotation-v<version>
```

The script exports only the tracked files of that commit, never the working tree, with the exact bytes and modes the commit holds. The result is one commit on the branch `export/context-rotation-<version>` in the clone. When that branch exists the commit goes on top of it. Otherwise the commit goes on the commit the clone's checkout is on. That commit is the base.

The script never checks out, writes into, resets or cleans the clone's own working tree or index. Staged, unstaged, untracked and ignored files there stay exactly as they are, and so does the branch the clone is on. The commit is built in a temporary worktree of the script's own, outside the clone, which is removed before the script exits. No hook of the clone runs, and no filter or `core.fileMode` setting of the clone changes a byte or a mode.

The commit changes three things against the base:

- the folder `context-rotation/`, with the source prefix stripped and files that no longer exist in the source removed (nothing outside that folder is deleted);
- the `context-rotation` entry in `.claude-plugin/marketplace.json`, derived from `context-rotation/.claude-plugin/plugin.json` (name, version, description) with the source `./context-rotation`. The file is created when it is missing. Every other entry the base has stays byte for byte;
- one row for `context-rotation/` in the `## Tools` table of the toolkit `README.md`, added once. Any other text the base has stays.

The commit message carries the source commit sha. Running the script again on the same commit and clone changes nothing and exits 0.

The branch is the only ref the script writes in the clone, and it writes it last: after the leak scan, as a compare-and-swap on the value it read at the start. It writes the branch itself, never the branch a symbolic ref points at, and the commit is written without a ref. The run counts the branch as its own only once its own write succeeded. A branch that moved during the run, for instance because another export got there first, is left as that writer left it. The run then fails, and the message says what the branch holds now.

### What it refuses

The script stops, with the branch untouched, when:

- the commit is not an ancestor of `origin/main` in the source repository;
- `refs/remotes/origin/main` in the source repository is a symbolic ref. The commit is checked against `origin/main` itself, not against the branch a symbolic ref points at;
- the plugin folder at that commit contains a symbolic link or a submodule (the plugin ships regular files only);
- the target is not the root of a git repository, or has no commit yet;
- the clone's checkout has a merge, rebase, cherry-pick, revert or bisect in progress. A new export branch starts on the commit that checkout is on, and during those operations that is a commit in passing;
- the export branch `export/context-rotation-<version>` is a symbolic ref. The message names the branch it points at, and the command that removes the symbolic ref and leaves that branch as it is;
- the export branch is checked out in the clone or in any other worktree, or a worktree is rebasing or bisecting it, also under a symbolic ref that points at it. The message names the worktree;
- the base tracks a symbolic link or a submodule at `context-rotation/` or anything under it, at `.claude-plugin/`, `.claude-plugin/marketplace.json` or `README.md`, or tracks `context-rotation` as a file;
- `.claude-plugin/marketplace.json` on the base is not valid JSON with a `plugins` list;
- `plugin.json` and `--tag` carry different versions;
- the content of `context-rotation/` changed but the version in `plugin.json` is not higher than the one the base already has. Content is every file with its bytes and its mode, so a mode-only change counts;
- `TMPDIR` points inside the clone.

Before the branch is written, the script runs the leak scan of `tests/test_isolation.py` over the blobs of the commit it built: the exported folder, the marketplace file and the README. When there is nothing to export, it scans what the branch already carries. A hit on a line of the marketplace file or the README that the base already had is marked pre-existing: the export did not bring it, but it still has to go before a publish. A hit inside `context-rotation/` is never marked that way, because that content always comes from the source commit.

### When it fails

Every exit that is not a successful export leaves the clone as it was found, apart from what another writer changed in the meantime. The temporary worktree is removed. If this run wrote the branch, it is moved back to where it was, or deleted when the run created it, as a compare-and-swap on the commit the run wrote, on the branch itself. A branch this run did not write, because another writer changed it first, is left alone. The clone's checkout was never touched, so there is nothing to restore there.

| Exit | Meaning |
|---|---|
| 0 | exported, or nothing to export |
| 1 | refused for any reason under "What it refuses", a symbolic export branch included, or failed; the clone is back as it was found |
| 2 | usage error |
| 3 | failed, and the clone could not be put back; the message lists what is left and the command that removes it |

### After the export

1. Review the branch: `git -C <clone> show --stat export/context-rotation-<version>`.
2. Push it: `git -C <clone> push -u origin export/context-rotation-<version>`.
3. Open and merge the pull request in the toolkit repository.
4. Tag the merge: `git tag context-rotation-v<version>` on the updated `main`, then `git push origin context-rotation-v<version>`.

The script is POSIX `sh` and needs `git` and `python3`. It makes no network calls.
