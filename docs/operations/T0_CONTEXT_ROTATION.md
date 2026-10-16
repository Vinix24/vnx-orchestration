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
| Spawner | `scripts/t0_rotate_spawn.sh` | opens the successor window, types the kickoff, resumes the `/goal` |
| Shared state | `scripts/lib/t0_rotation_state.py` | pending marker, latch, handoff parser, receipts |

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
anything. CLI: `python3 scripts/lib/t0_context_budget.py <transcript>` prints size, band and
thresholds as JSON.

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

**Who is guarded.** `session_stop_rotation._is_t0_session` (explicit `VNX_TERMINAL`, else
no `VNX_DISPATCH_ID`/`VNX_TMUX_SIGNAL_DIR`, else a cwd outside `.claude/terminals/T1-T3`) plus
a cwd outside `.vnx-data/worktrees/`. Anything else is a silent no-op.

**Registration.** Fabric: `.claude/settings.json`. Consumers: `templates/settings_vnx_keys.json.tmpl`
(what `vnx regen-settings --merge`, and so `vnx init` / `bootstrap_hooks`, writes) and the
`vnx init` templates `templates/init/{default,minimal}/settings.json.j2`. The PreToolUse entry
matches `Bash|Skill|SlashCommand` only.

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
| first | `t0_rotation_state.py wrap-start --project-root R --project-slug S [--bucket PATH]` | `wrap-start.json` in the project's rotation state dir: `started_at`, the bucket path, and every bucket heading `### <date> · <slug> · <title>` that exists at that moment |
| last, after the curation | `t0_rotation_state.py wrap-finish --project-root R --daily-log PATH [--handoff PATH]` | the `wrap:` block in the handoff frontmatter |

`wrap-finish` counts as nuggets the headings of the slug from `wrap-start` that appeared since
then, so the shared month bucket only counts this project's new entries. It keeps every other
frontmatter key and the body byte for byte, replaces an earlier `wrap:` block, and gives a
handoff without frontmatter one. Both helpers write atomically (a per-writer `mkstemp` name in
the destination's directory, then `os.replace`). A relative `--daily-log` or `--handoff`
resolves against the project root. A wrap without `--bucket` records `nuggets: 0`, no headings
and `bucket: null`. After writing, `wrap-finish` runs the same check as the spawner and exits 6
when that check would refuse, so the wrap can still repair it.

**One wrap-start proves one wrap.** A `wrap-finish` whose block passes that check writes
`consumed_at` (its `wrapped_at`) into `wrap-start.json`. A second `wrap-finish` without a new
`wrap-start` refuses with exit 3 and "run wrap-start first", and writes nothing, so a fresh
`wrapped_at` can never be stamped on the daily log and nuggets of an older wrap. A finish that
fails (a missing daily log: exit 3; a block the check refuses: exit 6) leaves the start record
usable, so the wrap repairs the cause and finishes again. The read, the block, the check and the
consumption hold one lock on the start record (`atomic_io.slot_lock`, the sibling
`.wrap-start.json.lock`), and `wrap-start` takes the same lock: of two finishes on one start,
exactly one passes. `compose` only reads the handoff block and the daily log.

```yaml
wrap:
  started_at: 2026-10-16T09:31:02Z
  wrapped_at: 2026-10-16T09:40:12Z
  nuggets: 2
  nugget_headings: ["### 2026-10-16 · vnx-orchestration · ...", "### 2026-10-16 · vnx-orchestration · ..."]
  daily_log: "daily-log/2026-10-16.md"
  bucket: "/Users/<user>/Desktop/BUSINESS/Content/build-log-bucket/2026-10.md"
```

`compose` (called by the spawner with `--max-age-minutes` and `--project-root`) reads the block
with a stdlib parser, no PyYAML, and refuses with exit 6 at the first check that fails. The
message names the check and ends with "run /build-log wrap":

| Check | Holds when |
|---|---|
| `wrap_block` | the frontmatter has a `wrap:` block |
| `wrap_times` | `started_at` and `wrapped_at` are ISO-8601 times with a zone |
| `wrap_order` | `started_at <= wrapped_at` |
| `wrapped_at_future` | `wrapped_at` lies at most 60 s in the future |
| `wrapped_at_age` | `wrapped_at` is not older than `--max-age-minutes` (default 15) |
| `daily_log` | the `daily_log` file exists (relative to the project root) |
| `daily_log_handoff` | its frontmatter carries `handoff: true` |
| `daily_log_window` | its mtime lies in `[started_at, wrapped_at + 60 s]` |
| `nuggets_count` | `nuggets` equals the number of `nugget_headings` |
| `nugget_in_bucket` | every listed heading exists in `bucket` |

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
  `t0_rotation_state.py transcript-digest --transcript <path>`. Closing the old window comes
  after that step.
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

## What the spawner types

1. In a new window next to the old one, with the old window's name:
   `claude --model opus`, plus `--remote-control "<name>"` when `CLAUDE_CODE_BRIDGE_SESSION_ID`
   is set (`--rc` / `--no-rc` override), prefixed with `CLAUDE_CONFIG_DIR=...` when that is set
   (tmux does not carry a bare export into a new pane).

   **Start folder.** The successor starts in `<root>/.claude/terminals/T0` when that folder has
   a `CLAUDE.md` AND its `.claude` resolves (physical path) to `<root>/.claude`. Then the launch
   line is `cd -- <T0 folder> && claude ...`, and after the footer shows the spawner reads the
   new pane's folder: another folder means the new window is closed, exit 5, nothing typed, no
   latch, no receipt. In every other case the successor starts in the project root as before, with
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
2. After the footer (`auto mode`) shows, one line of natural language: close the old window
   (`tmux kill-window -t <old>`), run the kickoff skill on the handoff, then take up step 1:
   `<first next step>`. Natural language, because a leading `/` opens the slash autocomplete.
3. A `state_mutation` receipt (`t0_context_rotation_started`), then the latch (ledger first, see
   "Receipts and the state files").
4. With an active goal: `/goal <directive>, hervat na context-rotatie; resterend: <tasks>`
   (shortened to stay within the 4000-character `/goal` limit).

Enter is always its own `send-keys` call. Every line is collapsed to one line, since a newline
inside `send-keys -l` would submit early.

**Why the `/goal` step runs on the tmux server.** The successor kills the old window in its
first turn, and with it the spawner, which runs as a tool call inside that window. The follow-up
is handed to `tmux run-shell -b`, which runs on the tmux server and survives the kill. It waits
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

The last stdout line is `new_window=@N old_window=@N goal_followup=0|1 run_dir=PATH start_dir=PATH`;
`start_dir` is shell-quoted (`%q`) and is the last field.

Every run leaves `prompt.txt`, `first_step.txt`, `goal.txt` and `goal_followup.log` in
`~/.vnx-data/<project>/state/t0_rotation/rotation-<timestamp>-<pid>/`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | successor started (and the goal follow-up handed to the tmux server, if any) |
| 2 | usage error, an invalid `VNX_T0_ROTATE_START_DIR` (valid: `t0`, `root`), an empty `--skip-wrap-reason`, or `--transcript` without it |
| 3 | not in tmux, project root unresolvable, handoff missing or stale |
| 4 | handoff has no numbered next step |
| 5 | the successor never showed its footer, or started in another folder than the T0 folder; nothing was typed, the old window keeps running |
| 6 | no valid wrap proof (the message names the check and ends with "run /build-log wrap"); in a skip run: no readable JSONL transcript, a relative transcript path whose folder is gone, or the ledger refused the skip receipt. No window was opened |

## Receipts

All `state_mutation`, `source: t0_context_rotation`:

| Trigger | When |
|---|---|
| `t0_context_rotation_pending` | a T0 session first crosses force, and again when it crosses hard |
| `t0_context_rotation_wrap_skipped` | a `--skip-wrap-reason` run, before any window opens: `reason`, `transcript`, `handoff`, `run_dir` (also `file`), `old_window`, `pane`, `session_id` |
| `t0_context_rotation_started` | the successor window is up and the kickoff was typed |
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
- **Why not `.vnx-data/events/`:** `events/T{n}.ndjson` is the per-dispatch ring buffer of
  subprocess-routed terminals, truncated after every dispatch (ADR-005, Reasoning 5, and
  CLAUDE.md "Event Streams"). T0 is tmux-routed and produces no such stream, and a rotation
  is a session event, not a dispatch event.

## The retired worker monitor

`hooks/vnx_context_monitor.sh` read `context_window_T{1,2,3}.json`, which nothing writes any
more. Its dead branch is gone rather than rewired to the new measurement, because rotation is
T0-only by operator decision. The file remains as a silent no-op because SEOcrawler_v2 still
pins it; a deleted hook target would print a hook error on every tool call there.
