# VNX tmux/Runtime Identity Invariants

**Feature**: FP-B — Runtime Recovery, tmux Hardening, And Operability
**PR**: PR-0
**Status**: Canonical
**Governance**: G-R4, A-R3, A-R4, A-R5

This document defines the separation between terminal identity (canonical, runtime-owned) and tmux pane state (derived, adapter-owned). Later PRs enforce these invariants in code.

---

## Core Principle

**Terminal identity is canonical. Pane identity is derived.**

A terminal (T1, T2, T3) is a logical execution slot in the VNX runtime. A tmux pane is a physical rendering surface. The two are connected by the tmux adapter but their lifecycles are independent.

---

## Identity Layers

### Layer 1: Canonical Terminal Identity (Runtime-Owned)

| Property | Source | Mutability |
|----------|--------|------------|
| `terminal_id` | `terminal_leases` table | Immutable (T1, T2, T3 seeded at init) |
| `state` | `terminal_leases.state` | Transition-controlled (idle, leased, expired, recovering, released) |
| `dispatch_id` | `terminal_leases.dispatch_id` | Set on lease acquire, cleared on release |
| `generation` | `terminal_leases.generation` | Monotonically increasing, incremented on acquire |
| `track` | Dispatch assignment | Set per-dispatch, not per-terminal |

**Invariants:**
- Terminal IDs are fixed strings (`T1`, `T2`, `T3`). They never change.
- Terminal identity survives tmux pane destruction, remapping, and session restart.
- Terminal state transitions follow the canonical lease state machine.
- The `generation` field prevents stale operations from affecting current state.

### Layer 2: tmux Pane Mapping (Adapter-Owned)

| Property | Source | Mutability |
|----------|--------|------------|
| `pane_id` | `panes.json` | Updated by adapter on remap/reheal |
| `session_name` | tmux session | Set by session profile bootstrap |
| `window_index` | tmux window | Set by session profile |
| `pane_index` | tmux pane | Volatile — tmux may reassign on split/close |

**Invariants:**
- `panes.json` is the adapter's mapping file, not ownership truth.
- Pane ID changes (remaps) update `panes.json` only — they never modify `terminal_leases`, `dispatches`, or `coordination_events`.
- A pane remap is a non-event from the runtime's perspective.
- Loss of pane mapping is a `delivery_failure` or `terminal_unresponsive` incident, not an identity crisis.

### Layer 3: Session Profile (Config-Owned)

| Property | Source | Mutability |
|----------|--------|------------|
| Home layout | Declarative session profile | Rebuilt from config on session start/recover |
| Ops windows | Declarative session profile | Dynamic, created/destroyed as needed |
| Recovery windows | Declarative session profile | Temporary, created by `vnx recover` |

**Invariants:**
- Session profiles define what the tmux session should look like, not what it currently looks like.
- Profile mismatches are detected by `vnx doctor`, resolved by `vnx recover`.
- Profile changes never redefine terminal identity (G-R4).

---

## Remap And Reheal Rules

### Remap (pane ID changed)

**Trigger**: tmux pane was destroyed and recreated, or operator manually rearranged panes.

**Allowed actions:**
1. Query tmux for current pane layout
2. Update `panes.json` with new pane ID for the terminal
3. Log `adapter_pane_remapped` coordination event

**Forbidden actions:**
- Modifying `terminal_leases` (identity unchanged)
- Modifying `dispatches` (dispatch state unchanged)
- Changing `terminal_id` assignment (G-R4)
- Reassigning a dispatch to a different terminal

### Reheal (session/window restored)

**Trigger**: tmux session crashed or was detached, then restored from profile.

**Allowed actions:**
1. Rebuild tmux windows/panes from declarative session profile
2. Update all entries in `panes.json` with new pane IDs
3. Log `adapter_session_rehealed` coordination event
4. Verify terminal leases against rebuilt pane layout

**Forbidden actions:**
- Acquiring or releasing leases (that requires explicit runtime action)
- Marking dispatches as delivered (delivery must be retried)
- Skipping `vnx doctor` preflight after reheal

---

## What Pane Loss Means

When a tmux pane is lost (killed, session crashed), the impact depends on the terminal's lease state:

| Lease State | Pane Lost Impact | Recovery Path |
|-------------|-----------------|---------------|
| `idle` | No impact | Remap when pane is recreated |
| `leased` | `terminal_unresponsive` incident | Expire lease -> recover -> remap -> re-deliver |
| `expired` | Already in recovery | Recover lease -> remap |
| `recovering` | Recovery continues | Remap when pane is recreated |

The key insight: pane loss triggers an **incident**, not an identity change. The terminal still exists, the dispatch still exists, and the lease still tracks ownership. Only the physical rendering surface is gone.

---

## Enforcement Points (For Later PRs)

| PR | Enforcement |
|----|-------------|
| PR-1 | Incident log records pane-loss as `terminal_unresponsive`, not identity mutation |
| PR-2 | Workflow supervisor treats pane loss as recoverable incident, not dispatch failure |
| PR-3 | tmux session profiles define layout; remap/reheal use canonical identity |
| PR-4 | `vnx doctor` validates pane mapping consistency against lease state |
| PR-5 | `vnx recover` reconciles pane state before resuming work |
