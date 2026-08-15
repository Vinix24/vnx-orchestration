# Worker Permissions

> Status: current as of 2026-08-15 (scoped worker-mode is the fabric default,
> with the `mcp__` tool namespace denied explicitly, `dispatch_paths` narrowing
> `file_write_scope` per dispatch (OI-1196) including directory matching, and
> the hook-layer enforcement defaulted OFF — the 15-08 flip was reverted
> pending a remeasurement of the outside-rate with directory matching repaired
> — revising the 03-08 blanket-skip ratification and the 14-08 mcp-scoped-
> default flip).
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
profile. The `--disallowedTools` list also carries `mcp__*` (unless
`requires_mcp=True`) to deny the whole MCP tool namespace — see the
extension-bridge gap below.

## The extension-bridge gap: the empty `--mcp-config` is not enough

The empty `--mcp-config '{"mcpServers":{}}'` only reaches the `mcpServers`
group. An extension bridge surfaces `mcp__` tools WITHOUT appearing in
`claude mcp list`, so it is out of `--mcp-config`'s reach. Measured 2026-08-15
(dispatch `20260815-mcp-surface-probe`): a scoped worker whose argv carried
`--mcp-config '{"mcpServers":{}}' --strict-mcp-config --allowedTools ...` still
loaded four working `mcp__claude-in-chrome__*` tool schemas via ToolSearch
(`navigate`, `tabs_close_mcp`, `tabs_context_mcp`, `tabs_create_mcp`). The seven
servers from `claude mcp list` (Gmail, Google Drive, Google Calendar, Notion,
Excalidraw, Similarweb, perplexity) were correctly blocked; the extension bridge
was not. A worker in an isolated worktree could therefore reach the web with the
operator's logged-in Chrome session — the worktree bounds the filesystem,
`--strict-mcp-config` bounds the `mcpServers` configuration, and neither bounds
an extension bridge.

`--allowedTools` does not close the gap either: it is an allow-list of built-in
tools and does not restrict the `mcp__` namespace (verified: a session launched
with `--allowedTools Read,Write,Edit,MultiEdit,Bash,Grep,Glob` still saw its
ambient `mcp__` tool).

The fix is an explicit namespace deny. `build_claude_scope_args(...)` appends
`mcp__*` to `--disallowedTools` (constant `MCP_NAMESPACE_DENY` in
`scripts/lib/worker_permissions.py`). Every MCP tool is namespaced
`mcp__<server>__<tool>` regardless of whether it came from `mcpServers` or an
extension bridge, so the `mcp__*` glob covers both. The glob form was measured,
not assumed: `claude 2.1.233` accepts `mcp__*` (and `mcp__<server>` /
`mcp__<server>__*`) in `--disallowedTools`, and a live session's tool surface
drops to empty with it. Repeatable measurement:
`scripts/analysis/mcp_namespace_probe.py` (control vs scoped run of the real CLI).

As with the empty `--mcp-config`, the `mcp__*` deny is skipped when
`requires_mcp=True`, so a dispatch that declares it needs MCP keeps its `mcp__`
tools. The tmux-lane import-fault fallback in `tmux_interactive_dispatch.py`
carries the same deny so an import failure never silently reopens the namespace.

## `.vnx/worker_permissions.yaml` — role profiles

Project SSOT for scoped-mode profiles. Each role under `profiles:` declares:

| Key | Meaning | Enforced how |
|---|---|---|
| `allowed_tools` / `denied_tools` | Claude Code tool allow/deny list | **Hard-enforced** via `--allowedTools`/`--disallowedTools` — scoped mode is the default, so this binds unless the dispatch explicitly opts out (`VNX_WORKER_BLANKET_SKIP=1` or falsy `VNX_WORKER_SCOPED`) |
| `bash_allow_patterns` / `bash_deny_patterns` | Shell glob patterns describing expected/forbidden Bash commands | Rendered into the instruction preamble via `generate_permission_preamble()` (always) **and** real-time-gated via `match_bash_deny()` in the PreToolUse hook (`scripts/hooks/pretooluse_worker_scope_enforce.py`) — that hook is gated by `VNX_ENFORCE_WORKER_PERMISSIONS`, default OFF since 15-08 (opt-in: truthy `VNX_ENFORCE_WORKER_PERMISSIONS`; opt-out: `VNX_WORKER_ENFORCEMENT_SKIP=1` or falsy `VNX_ENFORCE_WORKER_PERMISSIONS`) |
| `file_write_scope` | Glob patterns for where the role may write | Real-time-gated via `match_file_write_scope()` (`scripts/lib/worker_permissions.py`) in the same hook — gated by `VNX_ENFORCE_WORKER_PERMISSIONS`, default OFF since 15-08. OI-1196 (15-08): the hook also narrows this to a dispatch's own declared `dispatch_paths` when present (never wider than the role — see below), and a declared directory path now covers its contents; previously `dispatch_paths` had no enforcement channel at all, only the `_scope_note()` prompt text |
| `mcp_servers` | Per-role allowlist of named MCP servers | **Hard-enforced** via a scoped `--mcp-config` (`resolve_role_mcp_config()`) — takes effect in scoped mode (the default), and only when `requires_mcp=False`. An empty list (the default; no shipped role currently declares one) keeps the `{"mcpServers":{}}` posture. A named server not defined in the ambient global config (`~/.claude.json`, or `VNX_GLOBAL_MCP_CONFIG_PATH` override) is skipped and logged, never fabricated |
| `terminal_assignments` | `T1`/`T2`/`T3` → expected role | Checked by `validate_dispatch_permissions()`, called from `subprocess_dispatch_internals/skill_injection.py:_inject_permission_profile` — a mismatch only **logs a warning**, it does not block the dispatch |

Current profiles (`.vnx/worker_permissions.yaml`): `backend-developer`,
`quality-engineer`, `frontend-developer`, `system-architect`,
`security-engineer`, `code-reviewer`, `research-analyst` (OI-1100 renamed
`test-engineer`→`quality-engineer` and `architect`→`system-architect`, and
dropped the never-dispatched `database-engineer`/`intelligence-engineer`
entries). `terminal_assignments` maps `T1: backend-developer`,
`T2: quality-engineer`, `T3: frontend-developer`.

An unknown/missing role, or a profile with no `allowed_tools`, falls back to
`default_code_worker_profile()` (`Read, Write, Edit, MultiEdit, Bash, Grep,
Glob`, denying `WebSearch`/`WebFetch`) — scoped mode never strips a worker of
the tools it needs to do code work, even with no matching profile.

**Net effect:** scoped mode is the default, so the allow/deny tool list and the
empty ambient-MCP config bind on every detached dispatch unless the operator
explicitly opts out (`VNX_WORKER_BLANKET_SKIP=1` or falsy `VNX_WORKER_SCOPED`).
`.vnx/worker_permissions.yaml` therefore has runtime effect by default; it only
stops mattering for a dispatch that opts back into blanket skip.

## OI-1196 — `dispatch_paths` narrows `file_write_scope`, per dispatch

A dispatch's own `--dispatch-paths` (tmux lane; plain strings, optionally
suffixed `:access` where `access` is a `PathAccess` value — `read` / `write`
/ `read_write` / `create`, default `read_write`) can narrow a role's
`file_write_scope` further, down to just the paths that dispatch declared.
It can only narrow, never widen: `scripts/lib/worker_permissions.py`'s
`match_file_write_scope()` requires the role check to pass first, then —
only when the dispatch declared paths — a second, independent check against
the write-granting subset of those paths (entries with `access=read` are
excluded: this mechanism enforces WRITE scope only, so `read` honestly means
"not a write grant", not an additional read-side restriction the fabric does
not otherwise have).

Wiring: `TmuxInteractiveDispatch._spawn_session()` JSON-encodes
`dispatch_paths` into the worker's pane as `VNX_DISPATCH_PATHS` (alongside
the existing `VNX_WORKER_ROLE`); `pretooluse_worker_scope_enforce.py` reads
it via `resolve_dispatch_write_scope()` and passes the result into
`match_file_write_scope()`. No `dispatch_paths` declared → `None` → identical
to pre-OI-1196 behavior (role scope alone). `dispatch_paths` declared but
every entry is `access=read` → an empty (not `None`) write-scope list → every
write is blocked, correctly reflecting "this dispatch does no writing".

Fail-open, deliberately and narrowly: a missing or malformed
`VNX_DISPATCH_PATHS` degrades to `None` (no per-dispatch narrowing) rather
than blocking. This is safe because it only removes the EXTRA narrowing —
the role's `file_write_scope` check runs unconditionally either way, so
malformed dispatch-scope data can never widen a worker past its role's
bound, only fail to apply a tightening beyond it. This mechanism inherits
the same `VNX_ENFORCE_WORKER_PERMISSIONS` gate as `file_write_scope` above —
default OFF since 15-08 (see next section).

Directory matching: a declared path is translated by
`resolve_dispatch_write_scope()` into one or more write-granting fnmatch
globs before it is matched. The directory-vs-file call is made on path form,
never on disk (`os.path.isdir` cannot be trusted for a not-yet-created
target). The deterministic rule:

- a path that already carries a glob metacharacter (`*`, `?`, `[`) is a glob —
  returned verbatim;
- a path whose final component has a file extension (`scripts/lib/foo.py`)
  names a FILE — returned verbatim, so it never opens up its neighbours
  (`foo.py.bak` or the whole directory);
- a path without an extension (`tests`, `tests/`, `scripts/commands`) is
  directory-like: the path form alone cannot tell a directory from an
  extension-less file (e.g. `VERSION`), so BOTH readings are granted — the
  exact literal AND `<path>/**`, which covers its contents at any depth. That
  never widens beyond "the path and its subtree"; it only avoids falsely
  blocking an extension-less file that shares the name.

This repairs the pre-fix behaviour where declaring `tests` could not match
`tests/test_x.py`.

The typed `DispatchSpec.dispatch_paths` surface (`scripts/lib/dispatch_spec.py`,
part of the single-entry dispatch door) has its own, independent `access`
field on each `DispatchPath`. `dispatch_spec.write_paths()` is the first code
that actually reads it (OI-1196) — it filters to the write-granting subset,
mirroring the CLI-string logic above via the shared
`WRITE_GRANTING_PATH_ACCESS` constant. It is not yet wired to the tmux lane's
`--dispatch-paths` (that bridge is `dispatch_cli.py`/`dispatch_bridge.py`,
outside this change) — today the two `dispatch_paths` concepts (typed
`DispatchPath` tuples in the door's spec, and the plain path-string CLI list
in the tmux/hook enforcement path above) are parsed independently rather than
sharing one object.

## Enforcement defaulted OFF since 15-08 (this change)

`worker_permission_enforcement_enabled()` — the gate the hook reads — defaults
OFF. It was flipped ON briefly on 15-08 (the OI-1196 wiring that first gave
`dispatch_paths` an enforcement channel), then reverted in this fix-forward
once the pre-flip blast-radius measurement showed what the flip would actually
block. The hook opens with
`if not worker_permission_enforcement_enabled(): return "allow", None`, so on a
stock environment the fine-grained boundary (role `file_write_scope`,
`bash_deny_patterns`, `dispatch_paths` narrowing) is inert.

- **`VNX_ENFORCE_WORKER_PERMISSIONS` truthy** (`1` / `true` / `yes` / `on`) —
  explicit per-dispatch opt in to the hook-layer enforcement.
- **`VNX_WORKER_ENFORCEMENT_SKIP=1`** — explicit per-dispatch opt out (same
  shape as `VNX_WORKER_BLANKET_SKIP`), for a dispatch that genuinely cannot
  run under the boundary.

The coarse launch-time posture (scoped vs blanket skip, owned by
`worker_scoped_enabled()`) is unchanged and still defaults ON since 14-08.
Only the fine-grained hook layer is OFF. The `working_tree_only`
commit/push-deny below is unaffected: it refuses the full opt-out path at
spawn time, independent of this default.

### Why it stays off, and what flips it back

`scripts/analysis/worker_scope_enforcement_measure.py` replays the hook's own
matchers over every dispatch spec in
`~/.vnx-data/vnx-dev/dispatches/pending/` that links to a landed commit via its
`Dispatch-ID:` provenance line. Measured 2026-08-15 over 712 specs; 348 linked
to a landed commit, 364 had no linkable commit:

- **149 of 348 (43%) would be blocked by the flip.**
- **139 of 348 (40%) wrote outside their ROLE `file_write_scope`** — the
  role-scope-only number, ignoring `dispatch_paths` entirely. It matters most
  because the role scope is the layer that becomes hard when the flip is on.
  110 of those 139 declared no `dispatch_paths` at all, so the role scope is
  their entire boundary (e.g. `backend-developer` — `scripts/**`, `tests/**`,
  `dashboard/**` — writing `vnx_cli/`, `docs/`, `schemas/`, `templates/`,
  `hooks/`, `configs/`, `bin/`, `CHANGELOG.md`, `VERSION`, `pyproject.toml`,
  `.github/`, `.vnx/`).
- **Dispatch-scope compliance** (the 58 dispatches that declared write-granting
  `dispatch_paths`): 45 of 58 (78%) wrote outside their declared paths under
  the pre-fix literal matcher; 31 of 58 (53%) under the repaired directory
  matcher. The fix rescues 14 dispatches that declared a bare directory and
  wrote inside it, and the 2 release-bump dispatches that declared `VERSION`
  are no longer over-restricted (extension-less files keep their exact
  literal).

Two structural causes sit under the role-scope number, both pre-existing in
the shipped code and both only visible once the boundary binds:

1. **Role `file_write_scope` is narrower than what build-workers actually
   touch.** `backend-developer` omits `vnx_cli/`, `docs/`, `schemas/`,
   `templates/`, `hooks/`, `configs/`, `bin/`, `pyproject.toml`, `.github/`
   and `.vnx/` — all locations that recur in the blocked examples. Fixing that
   is a role-YAML decision (`.vnx/worker_permissions.yaml`), measured here but
   not changed in this dispatch.
2. **`dispatch_paths` entries were exact `fnmatch` matches, not directory
   prefixes.** This dispatch repairs exactly that (directory matching above).

The flip-back condition is the remeasurement the revert bought time for: once
the role `file_write_scope` is widened or ratified as-is AND the dispatch-scope
outside-rate is re-run with directory matching repaired (split into real
violations versus matcher artifacts), the default can flip back ON without
blocking legitimate build-worker writes.

## The fail-closed exception: `working_tree_only`

One dispatch class must never be allowed to reach `git commit`/`git push`
regardless of the permissions default: a `working_tree_only` dispatch (plan
review/plan write — no commit, no push). The commit/push deny
(`Bash(git commit)`, `Bash(git commit:*)`, `Bash(git push)`,
`Bash(git push:*)`) is only appended by `build_claude_scope_args(...,
working_tree_only=True)` — a function that only runs in scoped mode. Scoped is
the default (the launch posture — `worker_scoped_enabled()` — defaults ON; the
fine-grained enforcement predicate defaults OFF), so a `working_tree_only`
dispatch gets the deny automatically; it only loses it if the operator opts
out of BOTH layers.

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
            "(both the scoped posture and ADR-012 enforcement default ON; "
            "refusing the full opt-out path — VNX_WORKER_BLANKET_SKIP=1 / "
            "falsy VNX_WORKER_SCOPED AND VNX_WORKER_ENFORCEMENT_SKIP=1 / "
            "falsy VNX_ENFORCE_WORKER_PERMISSIONS — where the commit/push "
            "deny would not bind)"
        ),
    )
```

In practice: a `working_tree_only` dispatch runs scoped by default. It must
**not** opt out of BOTH layers — `VNX_WORKER_BLANKET_SKIP=1` (or falsy
`VNX_WORKER_SCOPED`) AND `VNX_WORKER_ENFORCEMENT_SKIP=1` (or falsy
`VNX_ENFORCE_WORKER_PERMISSIONS`) — or it fails closed before any worker
spawns. Either layer alone still forces the scoped spawn (the two predicates
are OR-ed in the precondition), so the deny still binds.

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
