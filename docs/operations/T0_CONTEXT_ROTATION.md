# T0 context rotation at 500K — enforcement and handoff contract

> **Operator decision 2026-09-25.** At 500K context a T0 orchestrator MUST rotate: a new
> session takes over from the current one through a handoff, starts with the official kickoff,
> and closes the old tmux window. After 500K, work in flight may be finished, but nothing new
> is started. A `/goal` that was running continues in the new session.
>
> **Scope: T0 only.** Workers (T1-T3, dispatch worktrees, headless `claude -p`) and non-VNX
> sessions are never touched. The machine-written rotation handoff (`handoff.md` under the
> rotation dir, `vnx handoff`) is a different contract: see `CONTEXT_ROTATION.md`.

## The three pieces

| Piece | File | Role |
|---|---|---|
| Measurement | `scripts/lib/t0_context_budget.py` | context size from the transcript, rotation band |
| Guard hook | `scripts/hooks/t0_context_guard.py` | UserPromptSubmit / PreToolUse / Stop enforcement |
| Spawner | `scripts/t0_rotate_spawn.sh` | opens the successor window, starts it with the kickoff as start argument, checks the delivery, resumes the `/goal` |
| Shared state | `scripts/lib/t0_rotation_state.py` | pending marker, latch, handoff parser, receipts |
| Hook shim | `scripts/hooks/vnx_hook_shim.sh` | starts a hook entry so that a missing file never exits 2 |
| Coverage | `scripts/lib/hook_coverage.py` | the registered command, rule 1, and whether a project runs the guard itself |

The rotate skill (personal, `~/.claude/skills/rotate/SKILL.md`) runs `/build-log wrap` first and
then calls the spawner. The PreCompact reminder in the personal settings stays as it is; with a
1M window it only fires around 800K, which is why the guard exists.

## Measurement

The last `type: assistant` line in the session transcript (`transcript_path` in every hook
payload) carries `message.usage`. The context the model saw on that request is

```
input_tokens + cache_read_input_tokens + cache_creation_input_tokens
```

Sidechain entries (`isSidechain: true`) and harness-written `<synthetic>` messages are skipped.
An empty, unreadable or usage-less transcript measures as *unknown*, and unknown never blocks
anything.

**After a compaction** (OI-2072) the last usage describes the context that was compacted away:
measured 270,776 before a boundary against 89,369 after it. The backwards scan therefore stops
at a `compact_boundary` entry (not a sidechain one) that is newer than the last real assistant
entry. It returns that entry's `compactMetadata.postTokens` when it is an integer, and *unknown*
otherwise. `postTokens` is a **lower bound**: the first real request after a boundary measured
more (boundary 21,789, first request 123,227). That first assistant entry replaces it.

CLI: `python3 scripts/lib/t0_context_budget.py <transcript>` prints size, band and thresholds as
JSON, plus `source` (`usage`, `compact_boundary`, or `null` when unknown) and `lower_bound`.
A lower bound also carries a `note`.

Thresholds live in the config registry (`scripts/lib/config_registry.py`, subsystem
`t0-context-rotation`), so they follow the usual precedence (`VNX_OVERRIDE_*`, project config,
env, default):

| Key | Default | Band |
|---|---|---|
| `VNX_T0_ROTATE_WARN_TOKENS` | 400000 | warn |
| `VNX_T0_ROTATE_FORCE_TOKENS` | 500000 | force: rotation pending |
| `VNX_T0_ROTATE_HARD_TOKENS` | 600000 | hard: rotate now |

## What the guard does per band

| Band | UserPromptSubmit | PreToolUse | Stop |
|---|---|---|---|
| below warn | nothing | nothing | nothing |
| warn | adds context: size, "wind down and prepare the rotation" | nothing | nothing |
| force | adds context; refuses a prompt that starts a new `/goal` | denies starting new work (list below) | blocks the end of the turn once: finish to a boundary, start nothing, then rotate |
| hard | as force | as force | blocks once: rotate now, even with work in flight |

**Starting new work** (denied at force and above), and why each is on the list:

| Action | Why |
|---|---|
| `vnx dispatch <id>` without `--dry-run`, incl. `vnx dispatch stage` and `vnx dispatch-agent` | fires or stages a new worker |
| `dispatch_bridge.py` / a python call to `stage_spec_bundle`, `bridge_dispatch`, `deliver_via_door` | stages a new dispatch in the pending dir |
| `gh pr create` | opens a PR that needs a gate round and a merge this session will not finish |
| a new `/goal` (Skill or SlashCommand tool, or a typed `/goal` prompt) | starts a new autonomous run; a running `/goal` goes along through the handoff |

Everything else stays allowed, because finishing is allowed: `pr_merge.py`, gates, waiting on
CI, closing open items, reading, `vnx dispatch --dry-run`, and the rotation itself. The
UserPromptSubmit refusal of a typed `/goal` only works if Claude Code runs UserPromptSubmit
hooks for that native command; that was not measured.

**Loop protection.** The Stop block fires at most once per turn: on the stop that follows a
Stop-hook continuation Claude Code sets `stop_hook_active`, and the guard lets that one through.
Once the spawner started the successor it writes a latch for the session (and for the tmux
pane); a latched session is not Stop-blocked again. A latch older than 30 minutes no longer
counts: an old session still alive by then means the successor died during boot.

**Who is guarded.** Three rules, all required: see [Who is guarded](#who-is-guarded). Anything
else is a silent no-op.

**Registration.** Fabric: `.claude/settings.json`. Consumers: `templates/settings_vnx_keys.json.tmpl`
(what `vnx regen-settings --merge`, and so `vnx init` / `bootstrap_hooks`, writes) and the
`vnx init` templates `templates/init/{default,minimal}/settings.json.j2`. The PreToolUse entry
matches `Bash|Skill|SlashCommand` only.

## Who is guarded

A session is guarded only when all three rules hold (`t0_context_guard.is_guarded_session`).

1. **A VNX project with a T0.** The root of the payload `cwd` has a `.vnx-project-id` whose
   first line is not empty, and a `.claude/terminals/T0/CLAUDE.md`. The root is the nearest
   directory, from the real path of the cwd up, that holds a `.git` entry (directory or file):
   the folder `git rev-parse --show-toplevel` names, found without starting git. The cwd is not
   under `.vnx-data/worktrees/`. A folder with only a project id, such as
   `~/Desktop/BUSINESS/clients`, is not guarded.
2. **The T0 by convention.** `session_stop_rotation._is_t0_session` says T0: an explicit
   `VNX_TERMINAL` (or `VNX_TERMINAL_ID`) wins, else no `VNX_DISPATCH_ID`/`VNX_TMUX_SIGNAL_DIR`,
   else a cwd outside `.claude/terminals/T1-T3`. So an attended session at the project root or
   in the T0 folder is the orchestrator. **Opt-out:** start a side session there with
   `VNX_TERMINAL=<name>` (anything but `T0`) and the guard leaves it alone.
3. **Attended.** Not `CLAUDE_CODE_ENTRYPOINT=sdk-*` and not `VNX_WORKER_SCOPED=1`.

Rule 3 rests on this measurement of 2026-10-16 (Claude Code 2.1.301). The `claude -p` row is
the environment of a probe hook inside that session; the other two rows were read in the
session's own shell.

| Session | `CLAUDE_CODE_ENTRYPOINT` | `VNX_WORKER_SCOPED` | `CLAUDE_CODE_SESSION_ATTENDED` | `TMUX_PANE` |
|---|---|---|---|---|
| interactive T0 | `cli` | unset | `1` | set |
| `claude -p` (probe hook) | `sdk-cli` | unset | `0` | set, inherited |
| headless dispatch worker | `sdk-cli` | unset | `0` | set |

`CLAUDE_CODE_CHILD_SESSION=1` in all three, so it tells them apart no more than the pane does.
The plan-gate panel sets `VNX_WORKER_SCOPED=1` on its seats.

The shim (`scripts/hooks/vnx_hook_shim.sh`) checks rule 1 itself, from the process cwd, before
Python starts. Outside a guarded project it exits 0 without output.

**Which code runs.** The fabric repo runs its own `<root>/scripts/hooks/<entry>`, but only when
two things hold: its `.vnx-project-id` says `vnx-dev`, and its real path is the real path of the
first `vnx-dev` entry in the user-owned registry `~/.vnx/projects.json`. A repository writes its
own `.vnx-project-id`, so the id alone never selects repo code. Otherwise a cloned repo that
calls itself `vnx-dev` would have its own file run by a user-level hook, without the trust
prompt Claude Code shows for project hooks. A missing or unreadable registry means the released
code. The shim reads the registry with `python3 -I -S`: the cwd is the repo, and isolated mode
keeps a module the repo plants there from being imported. That read ends itself after 2 s.

Everyone else runs the released code, and the scope decides which release:

| Call | Entry it runs |
|---|---|
| user scope (`--scope user`, the user-level registration) | the entry beside the shim it was started as: the release its registered command names |
| project scope (the default) | `${VNX_HOME:-$HOME/.vnx-system/current}/scripts/hooks/<entry>` |

A user-scope call ignores an inherited `VNX_HOME` (fix-forward on #2075, OI-2117). Nobody the
user-level registration trusts chose that value: `bin/vnx` and `scripts/lib/vnx_paths.sh`
export it as the location of whichever copy launched the session, a version directory or a
dispatch worktree, and a project's settings `env` block can set it. Either would choose the code
a machine-wide hook starts, possibly a release whose guard does not know the user scope and runs
a second time. `hook_coverage.shim_entry_file` is the Python twin (`user_shim=` for a user-scope
call), and a test holds the two to the same answer for both scopes.

**The registered command.** `hook_coverage.registered_command` puts the VNX home into shell
source, so it accepts only the documented forms: an absolute path of plain path characters, or
one that starts with `$HOME`, `${HOME}`, `$CLAUDE_PROJECT_DIR`, `${CLAUDE_PROJECT_DIR}` or the
template placeholder `{{VNX_HOME}}`. Anything else, a command substitution included, is
refused. The command maps every non-zero exit to 1 (the plan's line maps only 2): bash exits
127 for a shim removed between the test and the start, and every case must exit 0 or 1. The
home a user-scope command names is also the release whose guard it runs, whatever `VNX_HOME`
says (see the table above).

**User scope.** Started with `VNX_HOOK_SCOPE=user`, the guard stays silent for an event that
the project's own settings already cover (`hook_coverage.coverage`, per PreToolUse tool), so it
never runs twice. A project-scope call never skips. Covered means a project registration that
decides; anything `hook_coverage` cannot establish does not cover, because a double run is the
smaller fault than no guard at all. A registration decides when all of this holds:

- **It is a foreground command hook.** Not `async` or `asyncRewake` (it runs in the background
  and cannot block), not `once`, no `if` condition, no exec-form `args`, no other `shell`.
- **It runs the guard at project scope.** Directly: the guard is the script of a `python3` call
  (after `exec`, `command`, `env` and flags without a value) or stands executable in command
  position, also inside `if`/`then`, `( )`, a `;`/`&&`/`||` list or `sh -c '...'`. `test -f`,
  `grep` or `cat` on the guard path decide nothing. A call that is piped, backgrounded, negated
  or redirects anything but stderr does not deliver its decision. A command that mentions
  `VNX_HOOK_SCOPE` other than `VNX_HOOK_SCOPE=project`, or runs `.`, `source` or `eval`, does
  not cover. Through the shim: the inline form or a plain `sh <shim> [--scope project] <entry>`,
  with arguments the shim accepts.
- **The paths resolve without a shell and exist.** Variables expand only where the shell
  expands them: a `$HOME` in single quotes or behind a backslash stays literal (that call exits
  127, or 2 under dash), and a bare one the shell would split does not resolve. A direct path
  expands from the environment and from what the command itself assigns before the call
  (`ROOT=$(git rev-parse --show-toplevel ...)` reads as the project dir); an unknown variable
  does not resolve. A shim path expands only `$HOME` and `$CLAUDE_PROJECT_DIR`. For a shim
  registration the entry it would start must exist too.
- **Claude Code runs the project's hooks.** Measured in the 2.1.301 binary: trust is recorded
  per account in the global config file (`$CLAUDE_CONFIG_DIR/.claude.json` or
  `~/.claude.json`, a non-empty legacy `<config home>/.config.json` first) as
  `projects[<folder>].hasTrustDialogAccepted: true`, for the session folder or a parent up to
  its git root. In an interactive session without trust 2.1.301 skips every hook, user-level
  ones included. A registration in a folder without recorded trust reports `missing`; a trust
  file that cannot be read reports `unknown`.

A registration that cannot decide reports `missing` (or `unknown`), so the user-level guard
decides instead of no guard at all.

**Time.** Claude Code gives the hook 10 s. Neither the shim nor the guard starts git: rule 1
walks to the nearest `.git`, and the rotation state is resolved from rule 1's root. The only
subprocess before the entry is the shim's registry read in the fabric repo, which ends itself
after 2 s. The ledger append on a band transition is outside this bound. With neither
`VNX_T0_ROTATION_RECEIPTS_FILE` nor `VNX_STATE_DIR` in the environment, `append_receipt`
resolves its state dir through `vnx_paths`: measured on 2026-10-16, five `git` calls without a
timeout, against the checkout the guard's code lives in rather than the project.

## User-level install and check

`vnx guard install` and `vnx guard check` exist in the pip CLI and in `bin/vnx`. Both run
`scripts/vnx_guard.py`; the logic is `scripts/lib/guard_install.py` and
`scripts/lib/guard_check.py`.

**Install.** `vnx guard install [--dry-run] [--config-dir DIR]...` finds the accounts: `~/.claude`
and every `~/.claude-*` directory holding a `settings.json` or a `.claude.json`. `--config-dir`
replaces discovery. It refuses when `~/.vnx-system/current` lacks the shim or the guard, and on a
platform without an atomic exchange (see below), `--dry-run` included. In each account it writes
three entries with `hook_coverage.registered_command(..., "user")` and
`"timeout": 10` (seconds):

| Event | Matcher |
|---|---|
| UserPromptSubmit | `""` |
| PreToolUse | `Bash\|Skill\|SlashCommand` |
| Stop | `""` |

An entry is the install's own when its command names the shim path, the guard entry and
`--scope user`. A second run finds three entries in the exact form and changes nothing. An entry
of ours in another form is replaced, and so is every further entry of ours in an event: an event
ends with exactly one, also when a correct entry sat next to an older registration (for example
`"timeout": 5000`). Every other hook stays. `--dry-run` lists the accounts,
prints a diff per file and writes nothing.

**The write.** Per file: a fresh read, new content that must parse as JSON and keep every
top-level key, a timestamped backup (`settings.json.vnx-backup-<UTC>`), a `tempfile.mkstemp`
scratch file in the same directory, and a comparison of size, mtime and hash just before the
replace. A changed target starts the command over once and removes the backup of the abandoned
attempt; a second change refuses and leaves no backup. A symlinked `settings.json` is written
through, so it stays a symlink. Invalid JSON and a document that is not an object are refused
and never written. So is a `hooks` tree that is not the documented shape anywhere: an event that
is not a list, a group that is not an object, a group `hooks` that is not a list, a `matcher`
that is not a string, a hook that is not an object, a `command` that is not a string. The
refusal names the path to the bad node, for example `hooks.Stop[0].hooks is not a list`. The
rules are the ones `hook_coverage` loads a project's settings with; a test keeps them equal.

**Concurrent writes.** Claude Code writes this file itself (permission grants) and holds no lock
the command can join. The replace is therefore an atomic exchange of the scratch file and the
target, not `os.replace`: `renamex_np(..., RENAME_SWAP)` on macOS, `renameat2(...,
RENAME_EXCHANGE)` on Linux, through `ctypes` (`scripts/lib/atomic_swap.py`). Measured on
2026-10-16 on APFS (macOS 27.0.1): the two inodes trade places, and a missing side fails with
ENOENT and changes nothing. After the exchange the scratch name holds exactly the file that was
at `settings.json` at that instant. It is compared with the backup. Equal, it is removed.
Different, it is kept as `settings.json.vnx-displaced-<id>`, the run reports the top-level keys
that differ and exits 1. Then the file is read back: the three entries and every top-level key
of the backup must be there.

What this guarantees: a write that landed before the replace, by rename or in place, is never
lost. It is either in the file the install read, or in the kept displaced file.

What it does not guarantee: a writer that read the file before the replace and writes after it
removes the three entries again. Nothing of its own is lost, and the read-back reports it when it
lands first, `vnx guard check` afterwards. A writer that holds the old file open and writes into
it after the comparison writes into the moved-aside copy, which is gone when it still matched
the backup. Run the install while no session is answering a permission prompt.

A platform without the call (anything but macOS 10.12+ and Linux with glibc 2.28+) refuses the
install with that reason, and so does a filesystem that answers the exchange with ENOTSUP,
EINVAL or ENOSYS: then nothing is written and no backup is left. There is no fallback to
`os.replace`, because that would drop such a write without a copy.

**Check.** `vnx guard check [--config-dir DIR]... [--scan-root DIR]...` is read-only and exits 0
only when everything below holds:

- per account: the three entries are in the exact form and there is one per event (an event with
  more than one entry of ours is `duplicate` and named in the findings), `vnx_hook_shim.sh` and
  `t0_context_guard.py` exist in `current`, and the report names the root the registry trusts to
  run repo code, which is the first `vnx-dev` entry of `~/.vnx/projects.json` (shim step 2). When
  that path does not exist on disk it says so: every project then runs `current`. An account
  whose `settings.json` cannot be read, is not a JSON object or holds a malformed `hooks` tree is
  `unknown` with the reason (for the tree, the path to the bad node), and so is every project row
  of that account that the project itself does not cover;
- project inventory: the registry plus every `.vnx-project-id` under each `--scan-root` (depth 3,
  `.vnx-data` skipped). A scan hit the registry does not list is marked;
- a project without `.claude/terminals/T0/CLAUDE.md` prints `not guarded (no T0)` and is never a
  finding;
- a project with one is first held against the rest of the shim's rule 1,
  `hook_coverage.t0_project_root`: a `.vnx-project-id` with a non-empty first line, a `.git` entry
  at or above the folder, and a place outside `.vnx-data/worktrees/`. A project that fails it
  prints `not guarded (<reason>)` and is a finding, because the installed command exits there
  without running the guard;
- a project that passes is answered per event through `hook_coverage.coverage`, PreToolUse once per
  tool (`Bash`, `Skill`, `SlashCommand`): `project (direct|shim)` when the project's own settings
  run the guard, `user` when nothing in the project covers and this account has the entry,
  `missing` when it has not, `unknown` when a settings file of the project cannot be loaded.
  `hook_coverage.coverage` runs with the environment of the account it reports, because what
  Claude Code records per project (the trust decision) lives per account in its global config:
  `$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set, else `$HOME/.claude.json`. So
  `~/.claude` is judged without `CLAUDE_CONFIG_DIR` (it reads `~/.claude.json`, not
  `~/.claude/.claude.json`) and every other account with the variable set to its own folder,
  whatever the environment `check` itself runs in.

`vnx doctor` runs the same report without scan roots as check `guard:user-level`: PASS when no
Claude account directory exists or everything is covered, WARN otherwise. It also warns, naming
`vnx guard install`, when no account has the three entries.

`vnx init` (when it writes `settings.json`) and `vnx regen-settings` (`--full` and `--merge`,
not `--dry-run`) print one line when no account on the machine has the user-level guard, using
the install's own account discovery and entry check. Nothing is installed automatically.

**Rollback.** Switching `current` to a version without the shim makes the written command exit 1
with `vnx: guard shim missing: <path>` on every event, under bash and under dash, and `check`
reports the shim missing. Sessions keep working and the guard is off. Restore it by rolling
forward, or put the guard back per project with the template entries of that version, then run
`vnx guard check` again.

**Templates.** `templates/settings_vnx_keys.json.tmpl` and `templates/init/{default,minimal}/settings.json.j2`
register no guard entry, and every `timeout` is in seconds. The 5, 3 and 8 seconds were measured
on 2026-10-16 (10 runs per hook on a fixture project, at most 0.354 s for the slowest hook), and
a test keeps each one at or above three times that maximum, rounded up. `pretooluse_t0_no_subagents.py` is
NOT started through the shim. It recognises a T0 session by its cwd or by the launch directory in
its transcript path, because a T0's cwd drifts (3 of 143 real T0 sessions also carried the
operator's home as cwd). The shim's rule 1 tests the cwd and exits 0 outside a project with a T0
folder, which is exactly the moment the transcript test exists for. So the templates register
the inline command `scripts/lib/t0_subagent_block.py` renders: it tests that the entry exists in
the install the command names, runs it with `python3` and maps every non-zero exit to 1. A
missing entry exits 1 with `vnx: hook entry missing: <path>`; no case exits 2, which would block
every Agent call in every session. A test pins each template's command to that function's output.
The VNX home that `vnx regen-settings` (`vnx_settings_merge.load_template`) and `vnx init`
substitute is rendered through the same function at render time, which applies the
`registered_command` allowlist, so a path with a quote, a backtick or a `$(` is refused with an
error that names the value.

## Handoff contract

The spawner reads `daily-log/handoff.md` (default; `--handoff` overrides). `/build-log wrap`
must write:

1. **Next steps, numbered**, under a heading that starts with `## Next steps` or
   `## Volgende stappen`. The first numbered item (continuation lines included) is what the
   successor is told to pick up, literally. A handoff without a numbered step is refused.
2. **`## Actief /goal`**, only when a `/goal` was running:

   ```markdown
   ## Actief /goal
   Directive: <the /goal directive, with or without the leading /goal>
   Resterend:
   - <remaining task>
   - <remaining task>
   ```

   `**Directive**:` and `**Resterend**: a; b` (semicolon-separated on one line) are accepted
   too, as is a bare `/goal ...` line as directive. A section that says `geen`, is empty, or
   has no directive counts as no goal.
3. **The wrap proof**, a `wrap:` block in the frontmatter. The wrap never types it itself: a
   helper writes it, with the system clock (operator decisions 2026-10-16 a and b).

The handoff must be fresh: older than 15 minutes (`--max-age-minutes`) and the spawner refuses.
Handing over a stale handoff is the worst case, because the successor would resume the wrong
state with full confidence.

### The wrap proof

A hand-written handoff used to pass: the spawner only checked that the file existed, was
fresh and had a first step. Now `/build-log wrap` runs two helper steps around its own work:

| Step | Command | What it records |
|---|---|---|
| first | `t0_rotation_state.py wrap-start --project-root R --project-slug S [--bucket PATH]` | `wrap-start.json` in the project's rotation state dir: a random `start_id`, `started_at`, the bucket path, and every bucket heading `### <date> · <slug> · <title>` that exists at that moment |
| last, after the curation | `t0_rotation_state.py wrap-finish --project-root R --daily-log PATH [--handoff PATH]` | the `wrap:` block in the handoff frontmatter |

`wrap-finish` counts as nuggets the headings of the slug from `wrap-start` that appeared since
then, so the shared month bucket only counts this project's new entries. It keeps every other
frontmatter key and the body byte for byte, and gives a handoff without frontmatter one. An
earlier `wrap:` entry goes as a whole: its key line and every line up to the next top-level key,
blank lines and comments inside it included (OI-2116; round 2 stopped at the first blank line and
left the rest of the entry behind without its key). The rewritten frontmatter is parsed again,
strictly, before anything is written. A result that would not parse is refused with exit 3 and a
message that names the line; the handoff and the start record stay as they were. Both helpers
write atomically (a per-writer `mkstemp` name in the destination's directory, then
`os.replace`). A relative `--daily-log` or `--handoff` resolves against the project root. A wrap
without `--bucket` records `nuggets: 0`, no headings and `bucket: null`. Before it writes,
`wrap-finish` runs the same content checks as the spawner and exits 6, with nothing written,
when they would refuse, so the wrap can still repair the cause.

**One wrap-start proves one wrap.** A `wrap-finish` whose block passes that check writes
`consumed_at` (its `wrapped_at`) into `wrap-start.json`. A second `wrap-finish` without a new
`wrap-start` refuses with exit 3 and "run wrap-start first", and writes nothing, so a fresh
`wrapped_at` can never be stamped on the daily log and nuggets of an older wrap. A finish that
fails (a missing daily log: exit 3; a block the check refuses: exit 6) leaves the start record
usable, so the wrap repairs the cause and finishes again. The read, the block, the check and the
two writes hold one lock on the start record (`atomic_io.slot_lock`, the sibling
`.wrap-start.json.lock`), and `wrap-start` takes the same lock: of two finishes on one start,
exactly one passes.

**Consume, then publish (OI-2116).** The two writes of a finish are one step for the spawner.
`wrap-finish` first consumes the start record, then publishes the block, and the block carries
the record's `start_id`. `compose` accepts a block only when the record with that `start_id`
carries `consumed_at` equal to the block's `wrapped_at`. The crash points:

| Crash or failure | State left behind | What happens next |
|---|---|---|
| the consumption write fails | nothing changed | exit 3, "nothing published"; `wrap-finish` again |
| after the consumption, before the block | a consumed start, no block | exit 3 (or a dead process); `compose` refuses, a second `wrap-finish` says "run wrap-start first": run the wrap again from `wrap-start` |
| after both writes | a consumed start and its block | a valid proof |

Round 2 published first and consumed second. When the consumption write failed there, the
spawner accepted the proof while the start stayed reusable, and a later finish could stamp a
fresh `wrapped_at` on the same evidence. `compose` reads the start record and never writes it.

```yaml
wrap:
  start_id: "9f2c4e1a07b3d815"
  started_at: 2026-10-16T09:31:02Z
  wrapped_at: 2026-10-16T09:40:12Z
  nuggets: 2
  nugget_headings: ["### 2026-10-16 · vnx-orchestration · ...", "### 2026-10-16 · vnx-orchestration · ..."]
  daily_log: "daily-log/2026-10-16.md"
  bucket: "/Users/<user>/Desktop/BUSINESS/Content/build-log-bucket/2026-10.md"
```

`compose` (called by the spawner with `--max-age-minutes` and `--project-root`) reads the block
with a stdlib parser (the skills run the helper with whatever `python3` is on `PATH`, which need
not have PyYAML) and refuses with exit 6 at the first check that fails. It reads the handoff
frontmatter strictly: one level of YAML block-mapping structure, so an orphaned indented line, a
list mixed with keys, or a plain value holding `: ` is refused. The daily log is read leniently,
so one odd line in it never hides its `handoff: true`. The message names the check and ends
with "run /build-log wrap":

| Check | Holds when |
|---|---|
| `wrap_block` | the frontmatter parses and has a `wrap:` block |
| `wrap_times` | `started_at` and `wrapped_at` are ISO-8601 times with a zone |
| `wrap_order` | `started_at <= wrapped_at` |
| `wrapped_at_future` | `wrapped_at` lies at most 60 s in the future |
| `wrapped_at_age` | `wrapped_at` is not older than `--max-age-minutes` (default 15) |
| `daily_log` | the `daily_log` file exists (relative to the project root) |
| `daily_log_handoff` | its frontmatter carries `handoff: true` |
| `daily_log_window` | its mtime lies in `[started_at, wrapped_at + 60 s]` |
| `nuggets_count` | `nuggets` equals the number of `nugget_headings` |
| `nugget_in_bucket` | every listed heading exists in `bucket` |
| `wrap_start_binding` | the start record in the project's rotation state dir has the block's `start_id` and was consumed by this block (`consumed_at` equals `wrapped_at`) |

**The window.** It follows the wrap's real order (nuggets, handoff, daily log, curation), so the
operator's wait in the curation step is inside it and a wrap of twenty minutes passes. It binds
the proof to this wrap: a daily log from an earlier session that day falls outside it. The
curation step itself is not proven.

### Emergency path

`t0_rotate_spawn.sh --skip-wrap-reason "<text>" [--transcript PATH]` (operator decision
2026-10-16 c) is for a rotation that cannot wait for a wrap.

- It skips the wrap proof and the handoff age check, never the first-step check. An empty
  reason is exit 2, and so is `--transcript` without `--skip-wrap-reason`.
- The predecessor's transcript is `--transcript`, else the `transcript_path` in the pending
  marker the guard wrote for this pane (only the marker of `--session-id` when that is given).
  With neither, or when that file is not a readable JSONL file (a regular file whose first
  non-empty line is a JSON object), the spawner refuses with exit 6 before any window opens.
- A relative transcript path (from `--transcript` or from the marker) is made absolute first,
  against the physical folder the spawner was called from (`pwd -P`), because the successor
  starts in another folder. The JSONL check, the receipt, the prompt and the digest command all
  carry that absolute path. An absolute path stays byte for byte. A relative path whose folder
  is gone is exit 6, before any window.
- Ledger first (ADR-005): `record-skip` books a `t0_context_rotation_wrap_skipped` receipt
  with the reason and the transcript before the first window opens. A ledger that refuses it
  refuses the spawn, exit 6: a skip never goes unrecorded.
- The prompt starts with the catch-up step: run `/build-log wrap --from-transcript <path>`
  before anything else, and read the transcript only through
  `t0_rotation_state.py transcript-digest --transcript <path>`. Closing the old window does not
  wait for that step: the delivery job closes it on a confirmed delivery (after the `/goal`
  follow-up when there is one). The wrap reads the predecessor's transcript file, which closing
  its window does not touch.
- `transcript-digest --transcript PATH [--max-bytes 200000]` prints the user and assistant text
  after the last `compact_boundary`: no tool calls, no tool results, no thinking, no images or
  attachments, no harness-written `isMeta` or `<synthetic>` messages, no sidechains. The newest
  entries win the byte budget (UTF-8), and the output runs oldest first. The entry that no
  longer fits whole keeps its header and the end of its text.
- `t0_rotation_state.py skips [--since DAYS]` lists the skip receipts of the last 7 days
  (default), one line each, then a count. The kickoff shows them, so the emergency path cannot
  quietly become the normal path.

`compose` re-run into an existing run dir follows the handoff it reads: it rewrites
`first_step.txt` and writes `goal.txt`, or removes it when the handoff has no `## Actief /goal`.

## What the spawner does

1. In a new window next to the old one, with the old window's name:
   `claude --model opus`, plus `--remote-control "<name>"` when `CLAUDE_CODE_BRIDGE_SESSION_ID`
   is set (`--rc` / `--no-rc` override), prefixed with `CLAUDE_CONFIG_DIR=...` when that is set
   (tmux does not carry a bare export into a new pane). After that comes
   `--session-id <uuid>` (generated by the spawner) and the first message as the positional
   prompt argument: `/kickoff rotatie <run_id>`. The input box is never typed into for the first
   message (OI-2102: a redraw while Remote Control connected wiped what was typed, so only the
   tail arrived, measured four times).

   **Start folder.** The successor starts in `<root>/.claude/terminals/T0` when that folder has
   a `CLAUDE.md` AND its `.claude` resolves (physical path) to `<root>/.claude`. Then the launch
   spawner first types a plain `cd -- <T0 folder>` in the new window and reads the pane's
   folder, and only after that types the `claude ...` launch line with the first message.
   Another folder means the new window is closed before claude exists, exit 5: nothing ran in
   the successor, no delivery check, no latch, no receipt. In every other case the successor starts in the project root as before, with
   one log line. A broken T0 folder adds a WARNING with a repair that destroys nothing, which the
   spawner prints and never runs: a link to another target gets
   `ln -sfn <root>/.claude <T0>/.claude`; a plain directory gets
   `mv <T0>/.claude <T0>/.claude.replaced-<date> && ln -s <root>/.claude <T0>/.claude` (only with
   no session running in that folder). A missing `.vnx-data` link in a healthy T0 folder is one
   log line, not a reason to start elsewhere. When the old session runs in another folder than
   the start folder, one log line names both.

   **Switch.** `VNX_T0_ROTATE_START_DIR` is `t0` (default) or `root`. The tmux global environment
   is read first, the process environment second; `root` forces the project-root start.
   ```
   tmux set-environment -g VNX_T0_ROTATE_START_DIR root     # project root
   tmux set-environment -gu VNX_T0_ROTATE_START_DIR         # back to the default
   ```
   Any other value is refused with exit 2 before a window is opened.
2. The footer (`auto mode`) is the launch check. Nothing is typed after it by the spawner. The full instruction
   (first step, the catch-up head of a skip run, the goal sentence) is `prompt.txt` in the run
   dir, which the kickoff skill reads via `t0_rotation_state.py run-dir <run_id>`; it carries no
   instruction to close the old window.
3. A `state_mutation` receipt (`t0_context_rotation_started`), then the latch (ledger first, see
   "Receipts and the state files").
4. The last stdout line, then one job on the tmux server (see below). The line comes first, so a
   fast close of the old window cannot cut the spawner's output off.
5. Inside that job, after a confirmed delivery and with an active goal:
   `/goal <directive>, hervat na context-rotatie; resterend: <tasks>` (shortened to stay within
   the 4000-character `/goal` limit).

Enter is always its own `send-keys` call. Every line is collapsed to one line, since a newline
inside `send-keys -l` would submit early.

**Delivery check, the `/goal` follow-up and closing the old window.** The spawner hands
`delivery_job.sh` (a script file in the run dir, never an inline command: `run-shell` expands
`#{...}` formats in its argument with the caller's window) to `tmux run-shell -b`, after it
printed its last stdout line. One job does everything, in this order
(`scripts/t0_rotation_delivery_job.py`; the transcript logic is in
`scripts/lib/t0_rotation_delivery.py`, which never touches tmux):

1. It waits for `<config dir>/projects/*/<uuid>.jsonl` (`VNX_T0_ROTATE_DELIVERY_TIMEOUT`, default
   120 s), takes the first real user record (not meta, not hook or local-command output, not a
   tool result) and compares it to the first message. A start argument can land in two forms
   and the check accepts both: the expanded slash-command form
   (`<command-message>kickoff</command-message>`, `<command-name>/kickoff</command-name>`,
   `<command-args>rotatie <run_id></command-args>`, measured on claude 2.1.304 transcripts) and
   the plain text `/kickoff rotatie <run_id>`. **The first live rotation confirms which one
   claude writes for a positional start argument**; until then both count as delivered.
2. Receipt first (ADR-005): it books `t0_context_rotation_delivery` and requires that the ledger
   took it (the `event` command exits non-zero when its append failed). Only then `delivery.json`
   is written (`outcome` `confirmed` | `mismatch` | `timeout`, `expected`, the first 200
   characters found, `transcript`, atomically) and a window is touched. A refused append, or one
   that does not finish in 60 s, leaves both windows open, writes no `delivery.json`, sends no
   `/goal` and says so in both windows with `tmux display-message`.
3. `confirmed`: when `goal.txt` exists the `/goal` follow-up runs first and is waited for, and
   only then the old window is killed, while the new window still exists. The follow-up reports
   its own outcome; the old window closes after it either way.
4. `mismatch` / `timeout`: nothing is typed, nothing is closed, display-message names the
   outcome in both windows. The operator decides.

The last output line carries `close=on_delivery_confirmed`: the rotate skill needs no safety net
of its own.

**Why one job on the tmux server.** The old window is killed while the spawner, which runs as a
tool call inside it, may still be running. The job is handed to `tmux run-shell -b`, which runs
on the tmux server and survives the kill. The `/goal` follow-up is a step of that job, never a
second job beside it: a confirmed delivery cannot close the window before the follow-up ran, and
a failed delivery cannot be followed by a `/goal`. The follow-up waits
until the first turn is over (busy marker `esc to interrupt` seen and then gone twice in a row
with the footer present), types the `/goal` line, and waits for `/goal active` on screen.

**How the input box takes `/goal`: not measured live.** This dispatch was not allowed to start
a real claude or touch a real tmux session, so the sequence is designed, not observed:

- The line is typed literally (`send-keys -l`), then Enter separately.
- No Escape: in the Claude Code input box a double Escape clears the input or opens rewind.
- If `/goal active` does not show within 20 s: when the typed text is still visible in the
  pane, the first Enter was taken by the autocomplete and one more Enter is sent; otherwise the
  line is typed again. Then one more check.
- Still not active: a tmux `display-message` in the new window plus a natural-language message
  to the successor with the exact `/goal` line to type, and a receipt with
  `outcome=unconfirmed`. The markers are configurable (`VNX_T0_ROTATE_FOOTER_PATTERN`,
  `VNX_T0_ROTATE_BUSY_PATTERN`, `VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN`); set the real footer text
  there after the first live rotation if it differs.

The `/goal` follow-up is not exposed to the startup race: it types after the first turn, when the
TUI has finished starting and Remote Control is connected, and it checks the screen.

The last stdout line is
`new_window=@N old_window=@N goal_followup=0|1 run_dir=PATH close=on_delivery_confirmed start_dir=PATH`;
`start_dir` is shell-quoted (`%q`) and is the last field.

Every run leaves `prompt.txt`, `first_step.txt`, `continuation.json` (`run_id`, `first_step`,
`goal`, `handoff` as an absolute path), `goal.txt`, `goal_followup.log`, `delivery_job.sh`, `delivery_job.log` and
`delivery.json` (later `kickoff_done.json`) in
`~/.vnx-data/<project>/state/t0_rotation/rotation-<timestamp>-<pid>/`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | successor started (and the goal follow-up handed to the tmux server, if any) |
| 2 | usage error, an invalid `VNX_T0_ROTATE_START_DIR` (valid: `t0`, `root`), an empty `--skip-wrap-reason`, or `--transcript` without it |
| 3 | not in tmux, project root unresolvable, handoff missing or stale |
| 4 | handoff has no numbered next step |
| 5 | the successor never showed its footer, or its window is in another folder than the T0 folder (checked before claude starts: nothing ran in it); the old window keeps running |
| 6 | no valid wrap proof (the message names the check and ends with "run /build-log wrap"); in a skip run: no readable JSONL transcript, a relative transcript path whose folder is gone, or the ledger refused the skip receipt. No window was opened |

## Receipts

All `state_mutation`, `source: t0_context_rotation`:

| Trigger | When |
|---|---|
| `t0_context_rotation_pending` | a T0 session first crosses force, and again when it crosses hard |
| `t0_context_rotation_wrap_skipped` | a `--skip-wrap-reason` run, before any window opens: `reason`, `transcript`, `handoff`, `run_dir` (also `file`), `old_window`, `pane`, `session_id` |
| `t0_context_rotation_started` | the successor window is up and was started with the kickoff as start argument |
| `t0_context_rotation_delivery` | the delivery check ended: `outcome` = `confirmed`, `mismatch` or `timeout`, `old_window_close`. Booked before `delivery.json` and before any window is closed |
| `t0_context_rotation_kickoff_done` | `t0_rotation_state.py kickoff-done`, booked before `kickoff_done.json` (a refused append exits 1 and writes no marker): the successor finished its kickoff (`--catch-up-done` first checks the handoff carries a wrap newer than the skip receipt, exit 3 if not) |
| `t0_context_rotation_goal_followup` | the `/goal` follow-up ended: `outcome` = `confirmed`, `unconfirmed` or `turn_timeout` |

### Receipts and the state files (ADR-005)

A transition is a `state_mutation` receipt in `t0_receipts.ndjson`, appended through
`append_receipt_payload`. That file is the first canonical ledger in the Decision of
`docs/governance/decisions/ADR-005-ndjson-audit-ledger-primary.md`, and the path is the one
`scripts/lib/state_mutation.py`, `roadmap_manager.py` and `pr_merge.py` already use for
state-file writes. The receipt is written BEFORE the state file it describes changes. The ledger
wins: a crash between the two can duplicate an event on the retry and cannot lose one.

- **Pending marker** (`pending-pane-<pane>.json`): written only on a transition, the first
  crossing for a session or a change of band (force to hard). A repeat call in the same band
  leaves the file as it was: no rewrite, no new timestamp, no receipt. When the ledger refuses
  the receipt the marker is not written, and the next hook call decides again and retries.
  The read, the decision and the write run as one critical section under `atomic_io.slot_lock`,
  because parallel tool calls each start their own hook process (OI-1486).
- **Latch**: receipt first, then the latch. The latch is written even when the ledger refuses,
  because the successor is already up: withholding it would only make the old session nag, and
  nothing can retry that transition. The refusal is reported on stderr.
- **Wrap skip**: receipt first, then the spawn. Here a refusal does stop it: nothing is up yet,
  and an unrecorded skip is exactly what the receipt exists to prevent (risk T9). The spawner
  exits 6 and opens no window. `skips` reads these receipts back from the same ledger
  (`VNX_T0_ROTATION_RECEIPTS_FILE`, else `append_receipt`'s default).
- **Delivery and kickoff-done**: receipt first, then the file (`delivery.json`,
  `kickoff_done.json`), and a refusing ledger stops what follows. The delivery job closes no
  window and sends no `/goal` without an acknowledged append; `kickoff-done` writes no marker
  and exits 1. `t0_rotation_state.py event` exits 1 when its append failed.
- **Why not `.vnx-data/events/`:** `events/T{n}.ndjson` is the per-dispatch ring buffer of
  subprocess-routed terminals, truncated after every dispatch (ADR-005, Reasoning 5, and
  CLAUDE.md "Event Streams"). T0 is tmux-routed and produces no such stream, and a rotation
  is a session event, not a dispatch event.

## The retired worker monitor

`hooks/vnx_context_monitor.sh` read `context_window_T{1,2,3}.json`, which nothing writes any
more. Its dead branch is gone rather than rewired to the new measurement, because rotation is
T0-only by operator decision. The file remains as a silent no-op because SEOcrawler_v2 still
pins it; a deleted hook target would print a hook error on every tool call there.
