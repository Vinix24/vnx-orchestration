# Permission Settings for VNX Terminals
**Status**: Active
**Last Updated**: 2026-02-05
**Owner**: T-MANAGER
**Purpose**: Documentation for Permission Settings for VNX Terminals.

## Overview
Claude Code CLI permission settings have been configured to allow direct access between all VNX terminals (T0-T3) without repeated permission prompts.

## Configuration

### Location
The main settings file is at: `.claude/settings.json`

Since all terminal directories (T0-T3) have symlinks to the main `.claude` directory, they all share the same settings.

### additionalDirectories Setting

The `additionalDirectories` setting in the permissions section grants Claude Code read access to specified directories without interactive permission prompts:

```json
{
  "permissions": {
    "additionalDirectories": [
      "terminals/T0",
      "terminals/T1",
      "terminals/T2", 
      "terminals/T3",
      "vnx-system",
      "./"
    ],
    // ... other permissions
  }
}
```

## How It Works

1. **Symlink Structure**: Each terminal has a `.claude` symlink:
   ```
   terminals/T0/.claude -> /Users/.../<project>/.claude
   terminals/T1/.claude -> /Users/.../<project>/.claude
   terminals/T2/.claude -> /Users/.../<project>/.claude
   terminals/T3/.claude -> /Users/.../<project>/.claude
   ```

2. **Shared Configuration**: All terminals use the same `.claude/settings.json`

3. **Directory Access**: The `additionalDirectories` setting allows any Claude instance started in a terminal subdirectory to access:
   - All other terminal directories (T0-T3)
   - The vnx-system directory
   - The project root directory

## Benefits

1. **No Permission Prompts**: Claude in T0 can read files from T1, T2, T3 without asking
2. **Cross-Terminal Collaboration**: Terminals can inspect each other's work
3. **VNX System Access**: All terminals can access orchestration files
4. **Uninterrupted Workflow**: No prompts for file access permissions

## Important Notes

### Official Documentation
According to Claude Code documentation, `additionalDirectories` should be placed inside the `permissions` object, not at the root level. The screenshot information was partially correct but needed the proper nesting.

### Relative Paths
Paths in `additionalDirectories` are relative to the `.claude` directory location, not the current working directory.

### Security Considerations
- This grants READ access only by default
- Write operations still require explicit permission or must be in the `allow` list
- Sensitive files can still be protected using the `deny` list

## Verification

To verify the settings are working:

1. Start Claude in any terminal (T0-T3)
2. Try reading a file from another terminal:
   ```bash
   cat ../T1/somefile.txt  # From T0
   ```
3. Should work without permission prompt

## Troubleshooting

If permission prompts still appear:

1. **Check symlinks**: Ensure `.claude` symlinks are intact
2. **Restart Claude**: Settings apply on session start
3. **Verify paths**: Paths must be relative to `.claude` directory
4. **Check syntax**: JSON must be valid with proper nesting

## Patch-Based Settings Management

VNX uses a patch-based model for `settings.json` — it manages only its own keys, preserving all project and user configuration.

### Ownership Model

| Owner | Keys |
|-------|------|
| **VNX** | `hooks`, `env.VNX_*`, baseline `permissions.allow`, baseline `permissions.deny` |
| **Project/User** | Extra `env` keys, `permissions.ask`, `additionalDirectories`, any non-VNX keys |

### Merge Semantics

- `permissions.allow`: union (deduplicated) — VNX baseline + project entries
- `permissions.deny`: union (deduplicated) — deny takes precedence over allow
- `permissions.ask` and `additionalDirectories`: preserved as-is (project-owned)
- `hooks`: replaced entirely (VNX-owned)
- `env`: VNX_* keys replaced, project keys preserved

### Commands

```bash
vnx regen-settings --merge      # Merge VNX keys into existing settings.json
vnx regen-settings --full       # Generate complete settings.json (first-time init)
vnx regen-settings --validate   # Validate settings.json structure
vnx regen-settings --dry-run    # Preview changes without writing
```

### Introspection

The `_vnx_meta` key in settings.json records which keys VNX manages:
```json
"_vnx_meta": {
  "managed_keys": ["hooks", "env.VNX_*", "permissions.allow(vnx_baseline)", "permissions.deny(vnx_baseline)"],
  "generated_at": "2026-03-23T..."
}
```

### T0 Guard Hooks

Two hooks keep a T0 session honest. Both ship through the same three templates as the rest of `hooks` (`templates/settings_vnx_keys.json.tmpl`, `templates/init/default/settings.json.j2`, `templates/init/minimal/settings.json.j2`), so `vnx regen-settings --merge` and `vnx init` deliver them.

**No subagents in T0** (`scripts/hooks/pretooluse_t0_no_subagents.py`, PreToolUse, matcher `Agent|Task`). T0 orchestrates through dispatches. A subagent started from T0 leaves no dispatch, report or receipt. The hook denies `Agent` and `Task` with "T0 gebruikt geen subagents: stage een dispatch via `vnx dispatch`".

- Project settings apply to every session in the repo, headless workers included, so the hook decides per session. A session is T0 when its `cwd` is `.claude/terminals/T0` (or below), or when its `transcript_path` sits in the Claude project directory of that launch directory (name ends in `--claude-terminals-T0`). The second signal exists because the `cwd` drifts: 3 of 143 measured T0 sessions also carry the home directory as `cwd`. Workers run in a worktree root and are not touched. Neither is any other tool.
- The deny uses `hookSpecificOutput.permissionDecision`. Measured in real T0 transcripts, that form is what Claude Code honours on PreToolUse. A flat `{"decision":"block"}` or exit code 1 lets the call through.
- The subagent tool is called `Agent` in current Claude Code and `Task` in older versions. Guarding one name guards nothing.
- There is no override marker. The older `pretooluse_block_subagent.sh` (OI-1643, `Task` only, every session, operator override) is a different mechanism and is not replaced.

**Role-loaded alarm** (`hooks/sessionstart.sh`, T0 only). The canonical role (`role-orchestrator.md`) reaches a session only through an `@role-orchestrator.md` line in the T0 `CLAUDE.md`. When that line is missing the injection opens with `ROLE NOT LOADED: deze T0 draait zonder de canonieke rol`. It is a warning: the rest of the injection is still delivered. The measurement is the reach axis of `scripts/fleet_role_drift.py` (`--reach <t0-dir>`), not a second implementation. The hook is copied into each project by `bootstrap_hooks`, so it finds the engine under `$_VNX_SCRIPTS_ROOT`, the one scripts root the whole hook resolves (hook-relative, `VNX_HOME`, then `~/.vnx-system/current`). If that root is not reachable the injection opens with `ROLE CHECK UNAVAILABLE` instead of passing silently. `vnx role sync` does not restore a missing import: it refreshes the file the import points at.

`hooks` is replaced entirely on a merge. A project that keeps its own hooks in `settings.json` loses them on `vnx regen-settings --merge`. Move them to `settings.local.json` first.

## Future Improvements

Currently, Claude Code doesn't support `additionalDirectories` in project-specific settings files (feature request #3146). When this feature is added, we could have more granular control per terminal.