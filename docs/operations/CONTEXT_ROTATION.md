# T0 Context Rotation — Operator Guide

> **Scope.** This document covers the T0 **handoff contract**: the
> `handoff.md` file a stopping T0 leaves behind, the project_id-scoped paths
> it lives under, and the `vnx handoff` CLI that reads it. Rotation
> **execution** — deciding when to rotate, `/clear`, resuming the session —
> is the worker rotation system (`hooks/vnx_rotate.sh`,
> `docs/core/technical/CONTEXT_ROTATION_SYSTEM.md`) plus the operator
> `/rotate` flow; it is not implemented here.
>
> **History (OI-1042).** An earlier in-module control-plane
> (`checkpoint()` / `decide_rotation()` / `RotationPolicy` / `respawn()`,
> shipped default-off 2026-07-12) never gained a production caller — the
> live rotation path shared none of its code. It was removed 2026-08-11
> rather than left as a mechanism that reads as if it runs. The design
> record lives in `claudedocs/plans/t0-context-rotation-revival.md` and
> `docs/governance/decisions/ADR-002-f43-context-rotation-packaging.md`.
> `tests/test_context_rotation.py::TestDeadRotationApiRemoved` keeps the
> dead API from being quietly reintroduced.

---

## The moving parts

| Piece | File | Role |
|---|---|---|
| `write_t0_handoff()` | `scripts/lib/context_rotation.py` | writes the `handoff.md` contract |
| `write_ready_signal()` | same | writes the rotation_id-stamped `.ready` ack |
| `rotation_handoff_dir()` etc. | same | project_id+terminal-scoped path contract |
| `handoff_reader.py` | `scripts/lib/handoff_reader.py` | parses `handoff.md` into a briefing |
| `vnx handoff show` / `mark-ready` | `vnx_cli/commands/handoff.py` | CLI a resumed session runs to resume + ack |
| `session_stop_rotation.py` | `scripts/hooks/session_stop_rotation.py` | Stop-hook safety net — writes the handoff, never spawns anything |

The single switch is the env var **`VNX_T0_ROTATION`** (exactly `"1"` to
enable the Stop-hook safety net; anything else, or unset, is a proven
zero-side-effect no-op — not even a logs dir gets created).

---

## State layout (all under the project's resolved data root)

```
<resolved data root for project_id>/
├── state/rotation/
│   └── T0.ready             # written by a resumed session: {"rotation_id", "terminal", "marked_at"}
└── rotation_handovers/T0/
    └── handoff.md           # the resume contract (see below)
```

Every path is a function of `(project_id, terminal)` — a shared central
install never collides two projects' rotation state. The root is resolved
via the SAME canonical resolver every other VNX surface uses
(`vnx_paths._resolve_state_root`), never a hardcoded central path — forcing
`~/.vnx-data/<project_id>` into existence for a project that resolves
project-local would split-brain the store (ADR-026). The `--terminal` CLI
flag is untrusted input and is validated as a bare identifier before it
becomes a path component (no separators, no `..`).

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

## The `vnx handoff` CLI

```
vnx handoff show [--logdir DIR] [--terminal T0] [--mark-ready --rotation-id ID] [--project-id ID] [--project-dir DIR]
vnx handoff mark-ready --rotation-id ID [--terminal T0] [--project-id ID] [--project-dir DIR]
```

- `show` prints the parsed briefing (`Waar we middenin zitten` / `State` /
  `Next steps`). With no `--logdir`, it resolves the SAME project_id+terminal
  -scoped path the Stop-hook safety net writes to, so a fresh T0 running
  `vnx handoff show` from the project root finds the real handoff with zero
  extra flags.
- `mark-ready` (or `show --mark-ready --rotation-id ID`) writes the
  rotation_id-stamped `.ready` ack — the stamp is what distinguishes this
  ack from a stale `.ready` left by a previous rotation.

This is a **repo-level** contract — deliberately not the personal
`/build-log wrap` + `/kickoff` skill chain. Those skills remain available for
manual use; they are not part of this contract.

---

## The safety-net hook: `session_stop_rotation.py`

Wired under `Stop` in `.claude/settings.json`, **NO-OP unless
`VNX_T0_ROTATION=1`**. When enabled, it does exactly one thing: ensure
`handoff.md` exists (by calling `write_t0_handoff()`) whenever a T0 session
stops. The Stop matcher is empty (fires for every session), so the hook
verifies it IS a T0 session — env identity first, then the same cwd-based
worker heuristic `stop_report_hook.sh` uses — before writing anything; a
stopping T1/T2/T3 or dispatch worker can never clobber the T0 handoff. It
makes no claim to spawn a successor: a shell `Stop` hook cannot launch an
interactive Claude session.

**Rollback**: unset `VNX_T0_ROTATION`. Zero code paths execute — the hook
(and its settings.json wrapper) creates no directories and no log files
when disabled.

---

## The audit trail: `context_rotation_continuation`

The worker rotation system (`hooks/vnx_rotate.sh`) emits the
`context_rotation_continuation` event on every rotation it performs;
`scripts/lib/conversation_read_model.py` chains those events by
`dispatch_id`. The handoff contract in this document carries no receipt of
its own — the handoff file plus the `.ready` ack are its evidence surface.
