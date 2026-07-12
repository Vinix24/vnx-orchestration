# T0 Context Rotation — Operator Guide

> **Status: DEFAULT OFF.** Native Claude Code compaction (microcompaction +
> auto-compaction at ~83% of the effective window) is the baseline and stays
> the baseline. This control-plane is an *optional* add-on for a
> **T0-initiated, non-destructive** handoff+respawn cycle on top of it — it
> never replaces or disables compaction.
>
> Design authority: `claudedocs/plans/t0-context-rotation-revival.md` (rev 3),
> ruled on by `claudedocs/2026-07-11-panel-t0-context-rotation-verdict.md`.
> Not to be confused with the older **worker** (T1/T2/T3) rotation system
> (`hooks/vnx_rotate.sh`, `docs/core/technical/CONTEXT_ROTATION_SYSTEM.md`),
> which explicitly excludes T0 and uses a completely different mechanism
> (`/clear` inside the SAME session, driven by a `PreToolUse` hook). This
> document covers ONLY the T0 control-plane
> (`scripts/lib/context_rotation.py`).

---

## Why this exists, and why it is not the worker system

The worker rotation system fires `/clear` inside the same tmux pane — that
works because a worker's job is a single bounded dispatch with a known
skill/dispatch-id to resume. T0 is different: it is a long-running,
open-ended orchestrator session with no single dispatch to hand back to, and
a shell `Stop` hook **cannot launch a fresh interactive Claude session** —
there is no hook-driven way to spawn T0's replacement. So the T0 respawn must
be **T0-initiated**: the *running* T0 decides to rotate (at a governance
boundary) and spawns its own successor, waiting for that successor to
confirm it is alive before the original exits. If the successor never
confirms, the rotation **aborts** and the original T0 keeps running — there
is no window where zero T0 sessions exist.

---

## The moving parts

| Piece | File | Role |
|---|---|---|
| `RotationPolicy` | `scripts/lib/context_rotation.py` | yaml + env policy surface |
| `decide_rotation()` | same | pure decision function (no I/O) |
| `checkpoint()` | same | the integration point T0 calls at a governance boundary |
| `write_t0_handoff()` | same | writes the `handoff.md` contract |
| `respawn()` | same | non-destructive `tmux new-session` + bounded readiness wait |
| `handoff_reader.py` | `scripts/lib/handoff_reader.py` | parses `handoff.md` into a briefing |
| `vnx handoff show` / `mark-ready` | `vnx_cli/commands/handoff.py` | CLI the successor runs to resume + ack |
| `session_stop_rotation.py` | `scripts/hooks/session_stop_rotation.py` | Stop-hook **safety net only** — never spawns T0 |

---

## Policy surface

`RotationPolicy.load()` reads `configs/context_rotation.yaml`, then applies
env-var overrides (env always wins when the var is set):

| Field | yaml key | env override | Default |
|---|---|---|---|
| enabled | `enabled` | `VNX_T0_ROTATION` (exactly `"1"` to enable) | `false` |
| trigger | `trigger` | `VNX_T0_ROTATION_TRIGGER` | `governance_boundary` (only mode implemented) |
| min_boundaries_between_rotations | `min_boundaries_between_rotations` | `VNX_T0_ROTATION_MIN_BOUNDARIES` | `3` |
| pct_ceiling | `pct_ceiling` | `VNX_T0_ROTATION_PCT_CEILING` | `null` (off) |
| respawn | `respawn` | `VNX_T0_ROTATION_RESPAWN` | `off` (`tmux_new_session` to actually spawn) |
| handoff_template | `handoff_template` | — | `default` |

**Two independent switches, both required for a real respawn:**
`enabled=true` gates the whole control-plane (handoff writing, debounce
bookkeeping, receipts). `respawn=tmux_new_session` additionally gates
whether a decided rotation actually spawns a successor — with `respawn=off`
(the shipped default even when `enabled` is flipped on), `checkpoint()` still
writes the handoff/marker and advances debounce state on a decided rotation,
which is useful for dry-running the pipeline without ever touching tmux.

There is deliberately **no live context-percentage trigger** for interactive
T0 (verified in round 1 of the design panel — no reliable signal exists).
`pct_ceiling` is a *backstop*, not a trigger: when set, and the caller
supplies `context_pct` at/above it, a rotation is allowed even before the
boundary counter clears — but only ever at a genuine governance boundary
(never mid-action).

---

## The decision: `decide_rotation()`

Pure function, no I/O. Gate order:

1. `policy.enabled` — false short-circuits everything.
2. `mid_action` — a rotation is never decided mid-action.
3. `at_governance_boundary` — must be true (the only implemented trigger).
4. Durable **boundary-count** debounce: `boundaries_since_last_rotation >=
   min_boundaries_between_rotations`, OR the `pct_ceiling` backstop fires.

Note this is a **counter**, not a cooldown timer — `min_boundaries_between_
rotations` counts governance boundaries crossed, not elapsed wall-clock time.

---

## The integration point: `checkpoint()`

```python
from context_rotation import checkpoint

outcome = checkpoint(
    at_governance_boundary=True,   # T0 calls this only at a real boundary
    project_id=project_id,         # required — ADR-007, no silent default
    context_pct=None,              # optional observability/backstop signal
)
```

What it does, per call:

1. Loads (or accepts an injected) `RotationPolicy`. If disabled, returns
   immediately — **zero filesystem side effects**, not even the rotation
   state directory gets created.
2. Resolves project_id-scoped state via `resolve_central_data_dir(project_id)`
   (`scripts/lib/vnx_paths.py`) — always `~/.vnx-data/<project_id>/...`,
   regardless of dev-checkout vs central-install mode, so the respawned
   successor (running from the same project root) resolves the exact same
   paths.
3. Checks the **request marker** (`<state>/rotation/<terminal>_request.json`)
   for an `in_progress` entry younger than `request_ttl_seconds` (default
   120s) — if found, the call is a duplicate and returns
   `already_in_progress` without writing anything new. A marker older than
   the TTL is treated as a crashed prior attempt and the call proceeds.
4. Loads the **durable debounce counter** and calls `decide_rotation()`.
   - Not rotating: increments the counter (if at a boundary) and returns.
   - Rotating: writes the request marker (`status: in_progress`), then
     `write_t0_handoff()`, then — only if `policy.respawn ==
     "tmux_new_session"` — calls `respawn()`.
5. **Debounce state (the counter reset to 0, `last_rotation_at` stamped) and
   the `context_rotation_continuation` receipt only fire on a CONFIRMED
   success** — respawn returning `success=True`, or immediately when
   `respawn` policy is `"off"`. An **ABORTed** respawn:
   - leaves the counter and `last_rotation_at` untouched, so the very next
     boundary is eligible to retry;
   - marks the request marker `status: aborted` (not blocking future
     attempts);
   - retains the handoff.md that was already written;
   - logs loudly (`ERROR`) with the rotation_id and reason;
   - does **not** emit the continuation receipt.

---

## Durable state layout (all under `resolve_central_data_dir(project_id)`)

```
~/.vnx-data/<project_id>/
├── state/rotation/
│   ├── T0_durable.json      # {"boundaries_since_last_rotation": int, "last_rotation_at": iso|null}
│   ├── T0_request.json      # {"rotation_id", "status": in_progress|success|aborted, "created_at", ["reason"]}
│   └── T0.ready             # written by the successor: {"rotation_id", "terminal", "marked_at"}
└── rotation_handovers/T0/
    └── handoff.md           # the resume contract (see below)
```

Every path is a function of `(project_id, terminal)` — a shared central
install never collides two projects' rotation state (round-3 finding #8).

---

## The `handoff.md` contract

Written by `write_t0_handoff()`, read by `handoff_reader.read_handoff()` /
`vnx handoff show` — both sides must stay in lockstep on this shape:

```markdown
---
context: t0-rotation
project: <project_id>
date: <ISO-8601 UTC>
branch: <git branch, or "unknown">
---

# T0 Context Rotation Handoff

## Waar we middenin zitten

<short prose: uncommitted-changes summary + active NOW-horizon tracks>

## State

- Branch, uncommitted file list, last 5 commits
- Horizon NOW / NEXT tracks (from `scripts/lib/tracks.list_tracks`)
- Unresolved open items linked to those tracks

## Next steps

<unresolved open items, or the active NOW tracks, or a pointer to `vnx horizon list`>
```

**Fail-soft per source**: a git-command failure degrades to `branch:
unknown` with empty commit/status lists; a horizon/open-items read failure
(missing DB, schema mismatch) degrades to empty NOW/NEXT/open-items lists.
Neither failure prevents the handoff from being written — `write_t0_handoff`
always produces a complete, contract-shaped file.

---

## The respawn: non-destructive, with a readiness handshake

`respawn()` does exactly this, and nothing else:

1. `tmux new-session -d -s <session> -c <project_root>` — a fresh detached
   session. **Never** touches the caller's own/current session.
2. `tmux send-keys -l claude` + `Enter` — a bare, **interactive** `claude`
   invocation. No `-p`, `--print`, or `--dangerously-skip-permissions`.
3. A short boot delay, then `tmux send-keys -l "<resume prompt>"` + `Enter`
   — the resume prompt is **typed text for the fresh Claude session's first
   turn**, not a raw shell command. It instructs the successor to run:
   ```
   vnx handoff show --mark-ready --terminal T0 --rotation-id <id> --project-id <id>
   ```
4. Polls (bounded, default 60s) for `<state>/rotation/T0.ready` to appear
   **with a matching `rotation_id`** (round-3 finding #6 — a stale `.ready`
   from a previous rotation is rejected by content, not just presence).
5. **On ready**: returns `success=True`. The caller (`checkpoint()`) then
   marks the rotation confirmed; the old T0 is expected to exit shortly
   after (outside this module's scope — the actual "exit" instruction is
   part of T0's own boundary logic, not `context_rotation.py`).
6. **On timeout**: `tmux kill-session -t <the-session-this-call-just-
   spawned>` (round-3 finding #4 — reap the orphan so no duplicate T0
   dangles), logs an `ERROR`-level ABORT, and returns `success=False`. The
   caller's session is never touched.

The `tmux_spawn_fn` / `tmux_kill_fn` parameters are injectable — production
uses the real `subprocess`-based implementations; tests inject recording
mocks (see `tests/test_context_rotation.py`), so no test ever spawns a real
tmux session.

### Why this does not collide with the raw-claude-spawn guard

`scripts/hooks/pretooluse_block_raw_claude_spawn.sh` (via
`pretooluse_spawn_detector.py`) hard-blocks `claude -p` / `--print` /
`--dangerously-skip-permissions`, and its own header comment marks bare
`claude` (no flags) as **"Always allowed... benign/interactive"**. Every
subprocess call `respawn()` makes has `argv[0] == "tmux"` — `claude` only
ever appears as a *later positional argument* to `tmux send-keys` (a single
quoted string), never as the executable actually being invoked. The
detector's hard-block classifies on `exe = basename(argv[0])`, so it
structurally cannot match here (the same reasoning the detector itself
applies to skip a lane `.py` name that shows up only as a `-c` code-string
argument). In production this call also happens **inside an already-running
Python process** (`checkpoint()`), not as a literal Bash-tool-call string
Claude Code itself proposes — so the `PreToolUse` hook is not even in the
invocation path. The guard-safe argv shape is defense-in-depth on top of
that, and is asserted directly in `TestGuardSafeSpawnShape` /
`TestRespawn.test_never_calls_a_destructive_or_kill_path_on_success`.

---

## The audit trail: `context_rotation_continuation`

On a **confirmed** rotation, `checkpoint()` emits the same
`context_rotation_continuation` event the worker rotation system
(`hooks/vnx_rotate.sh`) already emits, so both producers feed the ONE read
model (`scripts/lib/conversation_read_model.py`) that chains rotation events
by `dispatch_id`:

```json
{
  "event_type": "context_rotation_continuation",
  "terminal": "T0",
  "dispatch_id": "<rotation_id>",
  "handover_path": "/path/to/handoff.md",
  "skill": "t0-orchestrator",
  "context_used_pct_at_rotation": 0,
  "timestamp": "2026-07-12T12:00:00Z",
  "project_id": "<project_id>",
  "source": "context_rotation"
}
```

No receipt is emitted on an aborted rotation — only a confirmed one is part
of the audit chain.

---

## The `vnx handoff` CLI

```
vnx handoff show [--logdir DIR] [--terminal T0] [--mark-ready --rotation-id ID] [--project-id ID] [--project-dir DIR]
vnx handoff mark-ready --rotation-id ID [--terminal T0] [--project-id ID] [--project-dir DIR]
```

- `show` prints the parsed briefing (`Waar we middenin zitten` / `State` /
  `Next steps`). With no `--logdir`, it resolves the SAME project_id+terminal
  -scoped path `checkpoint()` writes to, so a freshly-respawned T0 running
  `vnx handoff show` from the project root finds the real handoff with zero
  extra flags.
- `mark-ready` (or `show --mark-ready --rotation-id ID`) writes the
  rotation_id-stamped `.ready` signal the waiting `respawn()` call in the old
  session is polling for.

This is a **repo-level** contract — deliberately not the personal
`/build-log wrap` + `/kickoff` skill chain. Those skills remain available for
manual use; they are not part of this control-plane.

---

## The safety-net hook: `session_stop_rotation.py`

Wired under `Stop` in `.claude/settings.json`, **NO-OP unless
`VNX_T0_ROTATION=1`**. When enabled, it does exactly one thing: ensure
`handoff.md` exists (by calling `write_t0_handoff()`) for the case where T0
goes idle without ever having called `checkpoint()` — e.g. no boundary was
reached that session, or rotation was toggled on mid-session. It makes **no
claim to spawn a successor** — that is exclusively `checkpoint()` ->
`respawn()`'s job. This preserves the "T0-initiated, never hook-initiated"
invariant from the rev-3 decision (a shell `Stop` hook genuinely cannot
launch an interactive Claude session).

---

## Enabling it

1. Flip `enabled: true` in `configs/context_rotation.yaml`, or export
   `VNX_T0_ROTATION=1` in the T0 session's environment.
2. Decide whether you want the dry-run pipeline (`respawn: off` — handoff +
   debounce bookkeeping only) or the real respawn (`respawn:
   tmux_new_session`).
3. Wire `checkpoint()` into T0's own governance-boundary logic (dispatch
   completion, plan-gate resolution, etc.) — this module does not decide
   *when* a boundary occurs; it only decides what to do once T0 says one has.
4. Optionally set `VNX_T0_ROTATION_MIN_BOUNDARIES` / `_PCT_CEILING` to tune
   the debounce for your session cadence.

**Rollback**: unset `VNX_T0_ROTATION` (or leave `configs/context_rotation.yaml`
at its shipped `enabled: false`). Zero code paths execute — this document
describes an entirely additive, default-off feature. Native compaction keeps
running exactly as it always has.

---

## What this explicitly does NOT do (out of scope)

- No headless-T0 fork.
- No change to native-compaction reliance between rotations — it is still
  the thing that handles everything this control-plane doesn't.
- No auto-enable / cross-project rollout.
- No fully hands-off auto-respawn from a daemon with no T0 present — that
  needs a governed interactive-session-spawn primitive and is explicitly
  parked as a follow-up track (`t0-rotation-auto-spawn-primitive`).
