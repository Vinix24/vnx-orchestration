# Worker Permissions

> Status: current as of 2026-08-14 (scoped worker-mode is the fabric default,
> revising the 03-08 blanket-skip ratification).
> Covers the two dispatch lanes that spawn a headless/detached Claude worker:
> the tmux-spawn lane (`tmux_interactive_dispatch.py`) and the subprocess lane
> (`subprocess_adapter.py`). Module: `scripts/lib/worker_permissions.py`.

## The default: scoped worker-mode (revised 14-08)

A detached worker has no TTY to answer permission prompts, so it runs in one
of two postures: **scoped** (`--permission-mode acceptEdits` + empty ambient
MCP + role-based allow/deny list) or **blanket**
`--dangerously-skip-permissions`.

Before 14-08 the default was blanket skip, ratified 03-08 on the worktree
argument. That argument weighed one axis and missed the MCP axis: worktree
isolation bounds the filesystem, not the network, and an MCP server talks to a
service outside the checkout. Measured 2026-08-14 from a real dispatch worktree
without opt-in: six ambient MCP servers were still connected (incl. Gmail and
Google Drive). So scoped worker-mode is now the fabric default.

`worker_scoped_enabled()` (`scripts/lib/worker_permissions.py`) is the single
switch — default **on**, with two opt-outs:

- **`VNX_WORKER_BLANKET_SKIP=1`** — explicit per-dispatch opt back into the
  blanket `--dangerously-skip-permissions` posture.
- **`VNX_WORKER_SCOPED` falsy** (`0` / `false` / `no` / `off`) — the legacy
  switch, kept for backward compat, also disables scoping.

With no opt-out (the default), the worker launches with
`build_claude_scope_args(...)` — `--permission-mode acceptEdits`,
`--mcp-config '{"mcpServers":{}}' --strict-mcp-config` (unless
`requires_mcp=True`), plus `--allowedTools`/`--disallowedTools` from the role's
profile.

## `.vnx/worker_permissions.yaml` — role profiles

Project SSOT for scoped-mode profiles. Each role under `profiles:` declares:

| Key | Meaning | Enforced how |
|---|---|---|
| `allowed_tools` / `denied_tools` | Claude Code tool allow/deny list | **Hard-enforced** via `--allowedTools`/`--disallowedTools` — scoped mode is the default, so this binds unless the dispatch explicitly opts out (`VNX_WORKER_BLANKET_SKIP=1` or falsy `VNX_WORKER_SCOPED`) |
| `bash_allow_patterns` / `bash_deny_patterns` | Shell glob patterns describing expected/forbidden Bash commands | **Advisory only** — rendered into the instruction preamble via `generate_permission_preamble()`; `match_bash_deny()` exists (`scripts/lib/worker_permissions.py:320-328`) but nothing in the dispatch path calls it at Bash-tool time, so it is not a real-time gate |
| `file_write_scope` | Glob patterns for where the role may write | **Advisory only** — same preamble-only path; `match_file_write_scope()` (`scripts/lib/worker_permissions.py:331-338`) is unused outside tests |
| `mcp_servers` | Per-role allowlist of named MCP servers | **Hard-enforced** via a scoped `--mcp-config` (`resolve_role_mcp_config()`) — takes effect in scoped mode (the default), and only when `requires_mcp=False`. An empty list (the default; no shipped role currently declares one) keeps the `{"mcpServers":{}}` posture. A named server not defined in the ambient global config (`~/.claude.json`, or `VNX_GLOBAL_MCP_CONFIG_PATH` override) is skipped and logged, never fabricated |
| `terminal_assignments` | `T1`/`T2`/`T3` → expected role | Checked by `validate_dispatch_permissions()`, called from `subprocess_dispatch_internals/skill_injection.py:_inject_permission_profile` — a mismatch only **logs a warning**, it does not block the dispatch |

Current profiles (`.vnx/worker_permissions.yaml`): `backend-developer`,
`test-engineer`, `frontend-developer`, `architect`, `database-engineer`,
`intelligence-engineer`, `security-engineer`. `terminal_assignments` maps
`T1: backend-developer`, `T2: test-engineer`, `T3: frontend-developer`.

An unknown/missing role, or a profile with no `allowed_tools`, falls back to
`default_code_worker_profile()` (`Read, Write, Edit, MultiEdit, Bash, Grep,
Glob`, denying `WebSearch`/`WebFetch`) — scoped mode never strips a worker of
the tools it needs to do code work, even with no matching profile.

**Net effect:** scoped mode is the default, so the allow/deny tool list and the
empty ambient-MCP config bind on every detached dispatch unless the operator
explicitly opts out (`VNX_WORKER_BLANKET_SKIP=1` or falsy `VNX_WORKER_SCOPED`).
`.vnx/worker_permissions.yaml` therefore has runtime effect by default; it only
stops mattering for a dispatch that opts back into blanket skip.

## The fail-closed exception: `working_tree_only`

One dispatch class must never be allowed to reach `git commit`/`git push`
regardless of the permissions default: a `working_tree_only` dispatch (plan
review/plan write — no commit, no push). The commit/push deny
(`Bash(git commit)`, `Bash(git commit:*)`, `Bash(git push)`,
`Bash(git push:*)`) is only appended by `build_claude_scope_args(...,
working_tree_only=True)` — a function that only runs in scoped mode. Scoped is
the default, so a `working_tree_only` dispatch gets the deny automatically; it
only loses it if the operator explicitly opts out.

`tmux_interactive_dispatch.py` closes that gap by refusing to run the opt-out
paths, rather than silently downgrading protection:

```python
if working_tree_only and not (
    skip_permissions
    and (worker_scoped_enabled() or worker_permission_enforcement_enabled())
):
    return InteractiveDispatchResult(
        success=False,
        ...
        failure_reason=(
            "working_tree_only requires a scoped detached spawn "
            "(scoped is the default; refusing the explicit opt-out path — "
            "VNX_WORKER_BLANKET_SKIP=1 or VNX_WORKER_SCOPED=0/false/no/off "
            "— where the commit/push deny would not bind)"
        ),
    )
```

In practice: a `working_tree_only` dispatch runs scoped by default. It must
**not** set `VNX_WORKER_BLANKET_SKIP=1` or a falsy `VNX_WORKER_SCOPED`, or it
fails closed before any worker spawns.

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
