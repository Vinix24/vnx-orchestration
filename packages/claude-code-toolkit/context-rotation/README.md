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

The script exports only the tracked files of that commit, never the working tree. It creates the branch `export/context-rotation-<version>` in the clone and writes three things on it:

- the folder `context-rotation/`, with the source prefix stripped and files that no longer exist in the source removed (nothing outside that folder is deleted);
- the `context-rotation` entry in `.claude-plugin/marketplace.json`, derived from `context-rotation/.claude-plugin/plugin.json` (name, version, description) with the source `./context-rotation`. The file is created when it is missing. Every other entry stays byte for byte;
- one row for `context-rotation/` in the `## Tools` table of the toolkit `README.md`, added once.

It ends with one commit whose message carries the source commit sha. Running it again on the same commit and clone changes nothing and exits 0.

### What it refuses

The script stops before writing anything when:

- the commit is not an ancestor of `origin/main` in the source repository;
- the target is not a git repository, or has local changes (tracked or untracked) under `context-rotation/`, `.claude-plugin/marketplace.json` or `README.md`;
- `plugin.json` and `--tag` carry different versions;
- the content of `context-rotation/` changed but the version in `plugin.json` is not higher than the one the clone already has.

After the commit it runs the leak scan of `tests/test_isolation.py` over the exported folder, the marketplace file and the README. On a hit it removes its commit again and exits non-zero.

### After the export

1. Review the branch: `git -C <clone> show --stat HEAD`.
2. Push it: `git -C <clone> push -u origin export/context-rotation-<version>`.
3. Open and merge the pull request in the toolkit repository.
4. Tag the merge: `git tag context-rotation-v<version>` on the updated `main`, then `git push origin context-rotation-v<version>`.

The script is POSIX `sh` and needs `git`, `tar` and `python3`. It makes no network calls.
