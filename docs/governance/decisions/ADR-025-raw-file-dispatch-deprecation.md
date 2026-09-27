# ADR-025 — Raw-file Dispatch Deprecation

**Status:** Accepted — Implemented 2026-09-27 in v1.6.7-dev (item B)
**Date:** 2026-06-24
**Decided by:** Operator (Vincent van Deth)
**Resolves:** The deprecation half of the door-flip (item B). Companion to ADR-024 (single-entry door as default dispatch lane).

## Context

`vnx dispatch <file.md>` (a raw dispatch markdown path) is the original, documented manual CLI form. It carries a rich legacy contract: file-search fallback (`exact → pending/<f> → pending/<f>.md`), `--terminal/--model/--adapter` overrides, lane precedence (`--adapter > Adapter: header > VNX_ADAPTER > VNX_AUTO_ROUTE`), and an active→completed lifecycle.

ADR-024 makes the single-entry door the default dispatch lane. Under that flip the routing splits (Option X1): STAGED forms (`--spec-file`, a `<pending-id>` resolving to a promoted bundle) route through the door; the RAW `vnx dispatch <file.md>` form stays on the legacy lane to preserve its lane precedence (routing it through the door's bridge would silently drop `--adapter`, since the bridge defaults claude→tmux — the regression A2 avoids).

Keeping two manual forms indefinitely is structural debt. Production callers are already staged (the in-process `deliver_via_door` callers and `<pending-id>`); the raw form is the human-typed manual path. The intent is to converge on the staged form so that, eventually, ALL input is staged→governed through one door.

## Decision

**The raw `vnx dispatch <file.md>` form is DEPRECATED. It continues to work on the legacy delivery lane (full lane precedence preserved), but emits a one-time deprecation warning when the door is the default lane, and is scheduled for removal in a 1.x release (door-flip item B). The canonical form is `vnx dispatch <pending-id>` (a promoted staged bundle).**

Concrete rules:

- The raw form keeps working unchanged on the legacy lane (tmux/subprocess delivery, all overrides, file-search, lifecycle). No behavior is removed in this ADR — only a warning is added and a removal is scheduled.
- The deprecation warning is emitted to **stderr** (not stdout — avoids breaking stdout-parsing callers; not via `err`, which would misleadingly prefix `ERROR:` for a non-fatal notice). Text: `[dispatch] DEPRECATED: raw-file dispatch (vnx dispatch <file.md>) — stage to a pending-id (vnx dispatch <pending-id>) instead. The raw form is scheduled for removal per ADR-025.`
- The warning fires **only when the single-entry door is the default lane** (`_door_on=1`). Under explicit rollback (`VNX_DISPATCH_LEGACY=1`) or `VNX_SINGLE_ENTRY_DISPATCH=0`, the raw form is the sanctioned path, so no warning fires — warning noise there would be confusing.
- A staged-bundle hint is added to the legacy "File not found" error: when a missing arg is a safe id resolving to a promoted bundle, the error notes it is a staged bundle requiring the door. This helps a user who typed a `<pending-id>` while the door is off.
- `--help` documents the staged `<pending-id>` form as canonical and the raw form as deprecated (with the file-first ordering requirement).

## Reasoning

1. **Backward-compatibility first, removal later (warn → observe → remove).** Vincent chose option A (backward-compat now) over a hard cutover. Flipping the door default must not break the documented raw form on day one. A deprecation warning seeds the eventual removal without a flag-day break.
2. **Lane precedence is the raw form's value.** The raw form exists to let an operator pick a lane (`--adapter subprocess`, `VNX_AUTO_ROUTE`). Routing it through the door's bridge would silently ignore `--adapter` (the bridge defaults claude→tmux). Keeping it on legacy preserves that value until removal; the deprecation steers users to the governed staged form for the future.
3. **Convergence on one governed door.** Per ADR-024 + ADR-006 (staging→promote human gate), the governed path is staged→door. The raw manual form is the last unstaged input. Deprecating it sets the direction: when item B removes it, all input is staged→governed, and the door is the single structural entry (modulo the daemon tmux-send-keys path, tracked separately as a structural-consolidation follow-up).

## Consequences

### Accepted

- The raw form keeps working through at least one minor release, with a deprecation warning, before removal (item B).
- Documentation (`--help`, the routing comment in `dispatch.sh`) presents `<pending-id>` as canonical and the raw form as deprecated.
- The deprecation warning is additive on stderr; it appears on raw-form dispatches under the door default. Existing stderr-asserting tests are updated to tolerate it.
- The staged-bundle "File not found" hint is an additive change on an already-error path (it does not alter any happy path).

### Rejected

- **Hard-removing the raw form now.** Rejected — that is a flag-day break of a documented form; warn→observe→remove is the chosen path (item B does the removal).
- **Routing the raw form through the door/bridge.** Rejected — it would silently drop `--adapter`/lane precedence (the regression Option X1 avoids), or require replicating legacy lane features in the bridge (the replication A2 explicitly avoided).
- **Warning under explicit rollback/legacy.** Rejected — under `VNX_DISPATCH_LEGACY=1` / `VNX_SINGLE_ENTRY_DISPATCH=0` the raw form is the sanctioned path; a deprecation warning there is noise.

## Implementation note

- Routing split + warning live in `scripts/commands/dispatch.sh` (`cmd_dispatch`, `_d_is_staged_form`, `_d_valid_dispatch_id`). The raw form falls through to the unchanged legacy tmux/subprocess delivery.
- The removal (item B) is a separate future change; this ADR only schedules it.

## See also

- ADR-024 — Single-entry door as default dispatch lane (the flip; this ADR is its deprecation half)
- ADR-006 — Staging→promote with mandatory human approval gate (the governed staged path)
- ADR-010 — Subprocess adapter as canonical Claude routing (superseded-in-part by ADR-024 on the canonical-lane question)
- `scripts/commands/dispatch.sh` — `cmd_dispatch`, `_d_is_staged_form`, `_d_valid_dispatch_id`
- Memory: `dispatch-structure-not-consolidated-friction` — the daemon tmux-send-keys path, a separate structural-consolidation follow-up

## Amendment (2026-09-27)

Implemented 2026-09-27 in v1.6.7-dev: raw form refused when the door is the default; still
available under the rollback flag. Measured before the change: zero DEPRECATED warnings had
ever shown up in `~/.vnx-data/*/logs`, and production callers were already staged
(`deliver_via_door`, `<pending-id>`) — the warn-then-remove window observed no raw-form
traffic worth protecting, so item B (removal) proceeded. `cmd_dispatch` now refuses a raw
`vnx dispatch <file.md>` with a non-zero exit and a stderr message pointing at
`vnx dispatch stage ...` / `dispatch_bridge.py stage ...` + `vnx dispatch <pending-id>`,
citing this ADR. `VNX_DISPATCH_LEGACY=1` (or `VNX_SINGLE_ENTRY_DISPATCH=0`) still reaches the
unchanged legacy lane byte-identically — the rollback hatch for when the door itself is
broken is not removed by this amendment.
