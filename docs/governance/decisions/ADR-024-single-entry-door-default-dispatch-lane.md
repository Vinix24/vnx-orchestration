# ADR-024 — Single-entry Door as the Default Dispatch Lane

**Status:** Accepted
**Date:** 2026-06-24
**Decided by:** Operator (Vincent van Deth)
**Resolves:** The door-flip (item E). Companion to ADR-025 (raw-file dispatch deprecation, item B).

## Context

PR-12 built the single-entry door: every governed dispatch funnels through one
`validate → snapshot → compile_plan → permit → execute` gate (`dispatch_cli.run_dispatch`),
with `dispatch_bridge` as the sanctioned bridge for the legacy callers. The door shipped behind a
flag (`VNX_SINGLE_ENTRY_DISPATCH`) with the default OFF (`dispatch_flags._DEFAULT_ENABLED = False`).

Until this ADR, the default lane was the legacy path. The flip — making the door the default — was
gated on burn-in and an explicit operator go. The naïve flip (just `_DEFAULT_ENABLED = True`) was
unsafe: it surfaced a real contract mismatch. The door (`_d_single_entry_dispatch`) accepts only
STAGED forms (`--spec-file`, a `<pending-id>` resolving to a promoted bundle), but the documented,
still-used raw form `vnx dispatch <file.md>` routed to the door post-flip and was rejected. The raw
form is rich (file-search, `--terminal/--model/--adapter` overrides, lane precedence
`--adapter > Adapter: > VNX_ADAPTER > VNX_AUTO_ROUTE`, active→completed lifecycle). Routing it
through the door's bridge would silently drop `--adapter` (the bridge defaults claude→tmux).

The flip therefore required a routing-split first (D1, shipped separately as the routing-split PR):
STAGED forms route through the door; the RAW form stays on the legacy lane (deprecated per ADR-025).
This ADR records the flip itself and the lane decision.

## Decision

**`dispatch_flags._DEFAULT_ENABLED` is True: the single-entry door is the DEFAULT dispatch lane.
Unset `VNX_SINGLE_ENTRY_DISPATCH` resolves to the door. The legacy lane is reachable via the
rollback hatch `VNX_DISPATCH_LEGACY=1` (always wins) or the explicit opt-out
`VNX_SINGLE_ENTRY_DISPATCH=0`.**

Routing split (Option X1):

- STAGED forms (`--spec-file`, `<pending-id>` with a promoted bundle, `--force-release-lock`) route
  through the door (`_d_single_entry_dispatch` → `dispatch_cli`, directly; the bridge is the door's
  delivery for the in-process/daemon callers).
- The RAW `vnx dispatch <file.md>` form stays on the legacy tmux/subprocess delivery — preserving
  its full lane precedence — and emits a one-time deprecation warning (ADR-025; removed in 1.x).
- The dead `dispatch.sh:475` bridge branch was removed: under the narrowed intercept a raw form
  falls through to legacy delivery, NOT the bridge (routing it through the bridge would drop
  `--adapter`).
- Provider propagation: the daemon subprocess delivery forwards `--provider` to the bridge so a
  non-claude terminal-pinned worker keeps its provider lane post-flip (with the `claude_code`
  domain-string aliased to `claude`).

## Reasoning

1. **One governed door.** Per ADR-006 (staging→promote human gate) and the PR-12 design, the goal is
   that every dispatch funnels through `run_dispatch`. Making the door the default moves production
   onto that single gate by default instead of by opt-in.
2. **The flip is safe only with the routing-split.** Backward-compat first (the operator chose A over
   a hard cutover): the documented raw form must keep working on day one. The routing-split keeps it
   on the legacy lane (deprecated, ADR-025) rather than breaking it.
3. **Reversibility.** `VNX_DISPATCH_LEGACY=1` is an absolute rollback honored by the single-source
   predicate at every reader, so the flip is reversible without a code change.

## Consequences

### Accepted

- The door is the default; unset resolves to it. Production callers (already staged) route through
  `run_dispatch` by default.
- The raw form keeps working on the legacy lane with a deprecation warning (ADR-025); item B removes
  it later.
- Rollback is `VNX_DISPATCH_LEGACY=1` (always wins) or `VNX_SINGLE_ENTRY_DISPATCH=0`.
- The daemon tmux-send-keys path (`dispatch_deliver.sh`, interactive panes) is NOT door-gated — it is
  a separate delivery structure, tracked as a structural-consolidation follow-up, not part of this
  flip.

### Rejected

- **The naïve flip (flip the constant only).** Rejected — it breaks the documented raw form (the
  door rejects it). The routing-split is the precondition.
- **Routing the raw form through the door/bridge (Option Y).** Rejected — it silently drops
  `--adapter`/lane precedence, or requires replicating legacy lane features in the bridge.
- **A conditional raw split (Option Z).** Rejected — too complex for a deprecated path.

## Reconciliation with ADR-010 (superseded-in-part)

ADR-010 ("Subprocess Adapter (`claude -p`) as Canonical Claude Routing") established the subprocess
adapter as the canonical Claude lane. ADR-024 supersedes it on the canonical-lane question, while
ADR-010's SDK/LiteLLM ban remains binding. Per clause:

- ADR-010 Decision *"`subprocess_dispatch.py` is the canonical Claude invocation path"* and *"All
  Claude worker terminals route through this adapter by default"* → **SUPERSEDED**: post-June-15
  cutover, headless `claude -p` bills API credits, so the subscription-preserving tmux-spawn lane is
  now canonical for Claude (the provider→lane rule); the door routes claude→tmux-spawn.
- ADR-010 Accepted *"Any new Claude integration MUST extend `subprocess_dispatch.py`"* → **AMENDED**:
  new Claude integrations route via the door (`run_dispatch` → tmux-spawn).
- ADR-010 Accepted *"T0 progressive migration to subprocess continues"* → **SUPERSEDED**: claude
  routes via tmux-spawn-subscription as the primary.
- ADR-010 Rejected *"Direct `claude` CLI invocation that doesn't capture stream-json … shelling out
  to `claude` interactively … is rejected"* → **SUPERSEDED**: ADR-024's tmux-spawn lane IS
  interactive claude. Post-June-15, subscription preservation (no API-credit burn) outweighs the
  stream-json event surface; the tmux-spawn lane has its own receipt/event capture.
- ADR-010 *"The Anthropic Python SDK is never imported"* + the LiteLLM-Claude ban → **UNCHANGED /
  BINDING**: both lanes run the claude CLI, never the SDK or LiteLLM.

## See also

- ADR-025 — Raw-file dispatch deprecation (the deprecation half of the door-flip)
- ADR-006 — Staging→promote with mandatory human approval gate
- ADR-010 — Subprocess adapter as canonical Claude routing (superseded-in-part here)
- `scripts/lib/dispatch_flags.py` — `_DEFAULT_ENABLED` (the flip), `single_entry_enabled`
- `scripts/commands/dispatch.sh` — `cmd_dispatch`, `_d_is_staged_form`, `_d_valid_dispatch_id`
- Memory: `plan-gate-glm-parse-flake-blocks-pass` (the gate-tooling note from this flip's plan-gate)
- Memory: `dispatch-structure-not-consolidated-friction` (the daemon tmux-path follow-up)
