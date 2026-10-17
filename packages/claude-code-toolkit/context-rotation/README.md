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

The script exports only the tracked files of that commit, never the working tree. It works on the branch `export/context-rotation-<version>` in the clone: the existing branch when there is one, else a new one from the commit the clone is on. That commit is the base, and everything the script writes is built from the base, never from the clone's working tree. It writes three things:

- the folder `context-rotation/`, with the source prefix stripped and files that no longer exist in the source removed (nothing outside that folder is deleted);
- the `context-rotation` entry in `.claude-plugin/marketplace.json`, derived from `context-rotation/.claude-plugin/plugin.json` (name, version, description) with the source `./context-rotation`. The file is created when it is missing. Every other entry the base has stays byte for byte;
- one row for `context-rotation/` in the `## Tools` table of the toolkit `README.md`, added once. Any other text the base has stays.

It ends with one commit of exactly those three paths, whose message carries the source commit sha. Running it again on the same commit and clone changes nothing and exits 0.

### What it refuses

The script stops before writing anything when:

- the commit is not an ancestor of `origin/main` in the source repository;
- the plugin folder at that commit contains a symbolic link or a submodule (the plugin ships regular files only);
- the target is not a git repository, or has any local change: staged, unstaged or untracked, anywhere in the clone;
- the target has a merge, rebase, cherry-pick, revert or bisect in progress;
- `context-rotation/` or anything under it, `.claude-plugin/`, `.claude-plugin/marketplace.json` or `README.md` is a symbolic link, in the working tree or on the base;
- an ignored file sits under one of the paths it writes;
- `plugin.json` and `--tag` carry different versions;
- the content of `context-rotation/` changed but the version in `plugin.json` is not higher than the one the base already has.

After the commit it runs the leak scan of `tests/test_isolation.py` over the exported folder, the marketplace file and the README. A hit on a line the base already had is marked pre-existing: the export did not bring it, but it still has to go before a publish.

### When it fails

Every exit that is not a successful export leaves the clone as it was found: the export commit removed, the three paths restored, the branch or commit the clone was on checked out again, and a branch the run created deleted. The script then compares the clone with what it recorded before the first write.

| Exit | Meaning |
|---|---|
| 0 | exported, or nothing to export |
| 1 | refused or failed; the clone is back as it was found |
| 2 | usage error |
| 3 | failed, and the clone could not be put back; the message lists what is left |

### After the export

1. Review the branch: `git -C <clone> show --stat HEAD`.
2. Push it: `git -C <clone> push -u origin export/context-rotation-<version>`.
3. Open and merge the pull request in the toolkit repository.
4. Tag the merge: `git tag context-rotation-v<version>` on the updated `main`, then `git push origin context-rotation-v<version>`.

The script is POSIX `sh` and needs `git`, `tar` and `python3`. It makes no network calls.
