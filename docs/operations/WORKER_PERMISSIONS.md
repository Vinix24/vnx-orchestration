# Worker Permissions

> Status: current as of 2026-07-05 (#1016 blanket-skip-permissions default flip).
> Covers the two dispatch lanes that spawn a headless/detached Claude worker:
> the tmux-spawn lane (`tmux_interactive_dispatch.py`) and the subprocess lane
> (`subprocess_adapter.py`). Module: `scripts/lib/worker_permissions.py`.

## The default: blanket `--dangerously-skip-permissions` (#1016)

A detached worker has no TTY to answer permission prompts. Before #1016 the
default posture was **scoped**: `--permission-mode acceptEdits` + an empty
ambient MCP config + a role-based tool allow/deny list. That default flipped
because tmux-spawn workers already run inside an isolated per-dispatch
worktree — the scoped allow-list added prompt friction (stalling on
un-allow-listed ops: skill-dir writes, `mkdir`, `rm`) without adding real
blast-radius protection the worktree isolation didn't already provide.

`worker_scoped_enabled()` (`scripts/lib/worker_permissions.py:140-156`) is the
single switch:

```python
def worker_scoped_enabled() -> bool:
    return os.environ.get("VNX_WORKER_SCOPED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
```

- **Unset / falsey (default):** the worker launches with blanket
  `--dangerously-skip-permissions`
  (`scripts/lib/tmux_interactive_dispatch.py:273`,
  `scripts/lib/subprocess_adapter.py:75-105` `_build_worker_scope_args` →
  `_LEGACY_SKIP_FLAG`).
- **`VNX_WORKER_SCOPED=1` (opt back in):** the worker launches with
  `build_claude_scope_args(...)` instead — `--permission-mode acceptEdits`,
  `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (unless
  `requires_mcp=True`), plus `--allowedTools`/`--disallowedTools` from the
  role's profile.

## `.vnx/worker_permissions.yaml` — role profiles

Project SSOT for scoped-mode profiles. Each role under `profiles:` declares:

| Key | Meaning | Enforced how |
|---|---|---|
| `allowed_tools` / `denied_tools` | Claude Code tool allow/deny list | **Hard-enforced** via `--allowedTools`/`--disallowedTools` — only takes effect in scoped mode (`VNX_WORKER_SCOPED=1`); the blanket-skip-permissions default bypasses it entirely |
| `bash_allow_patterns` / `bash_deny_patterns` | Shell glob patterns describing expected/forbidden Bash commands | **Advisory only** — rendered into the instruction preamble via `generate_permission_preamble()`; `match_bash_deny()` exists (`scripts/lib/worker_permissions.py:320-328`) but nothing in the dispatch path calls it at Bash-tool time, so it is not a real-time gate |
| `file_write_scope` | Glob patterns for where the role may write | **Advisory only** — same preamble-only path; `match_file_write_scope()` (`scripts/lib/worker_permissions.py:331-338`) is unused outside tests |
| `mcp_servers` | Per-role allowlist of named MCP servers | **Hard-enforced** via a scoped `--mcp-config` (`resolve_role_mcp_config()`) — only takes effect in scoped mode, and only when `requires_mcp=False`. An empty list (the default; no shipped role currently declares one) keeps the pre-existing `{"mcpServers":{}}` posture. A named server not defined in the ambient global config (`~/.claude.json`, or `VNX_GLOBAL_MCP_CONFIG_PATH` override) is skipped and logged, never fabricated |
| `terminal_assignments` | `T1`/`T2`/`T3` → expected role | Checked by `validate_dispatch_permissions()`, called from `subprocess_dispatch_internals/skill_injection.py:_inject_permission_profile` — a mismatch only **logs a warning**, it does not block the dispatch |

Current profiles (`.vnx/worker_permissions.yaml`): `backend-developer`,
`test-engineer`, `frontend-developer`, `architect`, `database-engineer`,
`intelligence-engineer`, `security-engineer`. `terminal_assignments` maps
`T1: backend-developer`, `T2: test-engineer`, `T3: frontend-developer`.

An unknown/missing role, or a profile with no `allowed_tools`, falls back to
`default_code_worker_profile()` (`Read, Write, Edit, MultiEdit, Bash, Grep,
Glob`, denying `WebSearch`/`WebFetch`) — scoped mode never strips a worker of
the tools it needs to do code work, even with no matching profile.

**Net effect:** the only permission control that actually binds at the tool
layer today is the allow/deny tool list, and only when
`VNX_WORKER_SCOPED=1` is set. Under the blanket-skip-permissions default,
`.vnx/worker_permissions.yaml` has no runtime effect at all — the file only
matters once an operator opts back into scoped mode.

## The fail-closed exception: `working_tree_only`

One dispatch class must never be allowed to reach `git commit`/`git push`
regardless of the permissions default: a `working_tree_only` dispatch (plan
review/plan write — no commit, no push). The commit/push deny
(`Bash(git commit)`, `Bash(git commit:*)`, `Bash(git push)`,
`Bash(git push:*)`) is only appended by `build_claude_scope_args(...,
working_tree_only=True)` (`scripts/lib/worker_permissions.py:222-230`) — a
function that only runs in scoped mode. Since blanket skip-permissions is now
the tmux-spawn default, a `working_tree_only` dispatch running unscoped would
have no binding deny at all.

`tmux_interactive_dispatch.py` closes that gap by refusing to run, rather than
silently downgrading protection (`scripts/lib/tmux_interactive_dispatch.py:1616-1625`):

```python
if working_tree_only and not (skip_permissions and worker_scoped_enabled()):
    return InteractiveDispatchResult(
        success=False,
        ...
        failure_reason=(
            "working_tree_only requires a scoped detached spawn "
            "(skip_permissions + explicit VNX_WORKER_SCOPED=1; scoped is "
            "opt-in now that blanket skip-permissions is the default); "
            "refusing unscoped dispatch where the commit/push deny would not bind"
        ),
    )
```

In practice: a `working_tree_only` dispatch **must** set `VNX_WORKER_SCOPED=1`
for that one dispatch, or it fails closed before any worker spawns.

## The unbypassable exception: Claude Code's own dangerous-rm gate (OI-104)

Neither posture above touches this: the `claude` CLI binary has its own
built-in, unconditional safety check for `rm`/`rmdir` commands whose target
cannot be statically proven safe (a shell-variable or command-substitution
path that could be empty/unset and resolve to `/` or a top-level directory,
or a command too complex to analyze). That check always demands interactive
approval — it is enforced *inside the claude binary's permission-decision
pipeline*, before `--allowedTools`/`--disallowedTools` or even
`--dangerously-skip-permissions` are consulted, and it explicitly cannot be
satisfied by any allow-list entry. A headless worker has no TTY to answer it,
so the dispatch hangs forever.

This is what actually caused the 2026-07-14 batch regression where 4 of 6
build-workers hung on an `rm -rf` scratch-cleanup confirm **even under the
default blanket `--dangerously-skip-permissions`** — neither posture in this
doc was in play; the workers were simply telling their own Bash tool to run
`rm -rf` on a variable-expanded scratch path, and the CLI's own gate caught
it regardless of dispatch-lane permission mode. Evidence: the installed
`claude` binary's own strings carry the telemetry event name
`tengu_bash_dangerous_rm_too_complex` and the literal message "This requires
explicit approval and cannot be auto-allowed by permission rules." — matching
the empirically-observed fix (an operator manually sending "1" + Enter to a
numbered Claude Code permission choice, the signature of the CLI's own
interactive prompt, not a Unix shell `rm -i` confirmation).

**Fix:** don't ask workers to run `rm -rf`/`rmdir` with a variable-expanded
target at all. `scripts/lib/prompts/base_worker.md`'s cleanup instruction
directs workers to a **guarded** `python3 -c "..."` snippet for directory
removal instead — a `python3` invocation never routes through the CLI's
rm-specific static analyzer, so no prompt is ever raised, headless or not.
Guarded, not a bare `shutil.rmtree(...)`: the snippet resolves the target to
an absolute real path and refuses — no delete, an explicit error instead — when
the target is `/`, a top-level directory, `$HOME` or an ancestor of it, or
outside a recognized temp/scratch root (`tempfile.gettempdir()` / `$TMPDIR` /
`/tmp`). A bare, unguarded `shutil.rmtree()` would kill the interactive rm-gate
hang but reintroduce the exact failure mode that gate exists to prevent — a
wrong literal path silently, recursively deleted with no confirmation and no
error (worse if paired with `ignore_errors=True`, which the guidance
explicitly does not use). `rm -f <literal-path>` (a single named file, no
shell variable) remains fine since the gate only fires on non-provably-safe
*recursive* removal of a variable-expanded target.

## Related

- `docs/core/DISPATCH_RULES.md` §5 — lane defaults, where this default is called out
- `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` §5 — delivery invariants
- `docs/core/PROVIDER_LANES.md` — claude-tmux-spawn lane overview
- `scripts/lib/worker_permissions.py` — the module this doc describes
