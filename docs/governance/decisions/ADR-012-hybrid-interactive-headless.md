# ADR-012 — Hybrid Interactive + Headless Execution (No Retire-Interactive)

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Cross-references:** ADR-003 (OAuth-only Claude routing), ADR-010 (subprocess adapter), ADR-011 (manager+worker hierarchy)

## Context

VNX worker terminals (T1, T2, T3) can run in one of two modes:

- **Interactive (tmux)** — the worker is a persistent tmux pane. The operator can attach, watch the live transcript, type into the pane, and steer the worker mid-flight. Dispatches arrive via `popup_editor.sh` / queue popup watcher; output is observed by reading the pane buffer. This is the original VNX delivery path.
- **Headless (subprocess)** — the worker is a `claude -p --output-format stream-json` subprocess spawned by `subprocess_dispatch.py` (`scripts/lib/subprocess_dispatch.py`). The dispatcher streams stdout to `.vnx-data/events/T{n}.ndjson` for the duration of one dispatch, then archives to `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson` and truncates the live file. There is no interactive surface — the operator observes via the SSE event stream and the receipt.

F32 flipped the **default** to headless for T1/T2/T3 (`VNX_ADAPTER_T1=subprocess` etc., per the project root `CLAUDE.md` "Subprocess Adapter Feature Flag" section). T0 remains interactive by design.

A reasonable-looking next step would be a **"retire-interactive"** wave: delete `tmux_adapter.py`, `queue_popup_watcher.sh`, `popup_editor.sh`, and all `tmux send-keys` paths — then the codebase has one execution mode and one less surface to maintain. Operator instruction (`project_hybrid_interactive_headless` memory, 2026-04-23): **do not propose this**. The hybrid model is the design, not a transitional state.

This ADR codifies that decision and records the reasoning so that future "let's simplify by removing tmux" proposals are auto-rejected with a link here.

## Decision

VNX permanently supports **both** interactive (tmux) and headless (subprocess) execution paths for worker terminals. There is **no** "retire interactive" wave, sunset plan, or deprecation timeline. Specifically:

- `TmuxAdapter` and all tmux-driven delivery paths (queue popup watcher, `popup_editor.sh`, `send-keys` wrappers, pane-id state in `panes.json`) are first-class code and must be maintained alongside `SubprocessAdapter`.
- Headless is the **default** for T1/T2/T3 (set via `VNX_ADAPTER_T{n}=subprocess` env vars). Operators or specific dispatches can opt back into tmux delivery by unsetting that variable or by explicit per-dispatch override.
- T0 stays tmux-routed by default (T0 is operator-facing; no SubprocessAdapter for T0 unless specifically requested).
- New features must be designed to work in **both** modes. A feature that only works in headless mode (e.g., depends on the per-terminal NDJSON ring buffer) is acceptable only if it degrades gracefully in tmux mode (no-ops, fallback path, or explicit "headless-only" flag).
- Documentation describes the hybrid model as the **design**, not as a transitional state.

## Reasoning

1. **Operator-driven sessions need attention/visibility that headless does not provide.** Headless dispatches are fast and audit-clean — the receipt + the archived NDJSON are the entire record. But for high-stakes work (architecture decisions, irreversible migrations, manual debugging of a stuck loop), the operator wants to *watch the worker think* in real time and intervene mid-stream. tmux is the only surface in VNX that supports that. Removing it removes the operator's primary trust-but-verify mechanism.

2. **Modal cases require an interactive surface.** Some terminal states are modal — for example, T3's input-mode probe issue (`feedback_t3_input_mode_probe` memory) requires `/clear` before the pane will accept new dispatches. That interaction is fundamentally interactive: a headless subprocess can't `/clear` itself out of an input-mode trap. Other modal cases include: prompting the operator for an oauth code refresh, accepting a `/login` flow, recovering from a crashed Claude Code process where the pane is alive but the inner CLI exited. Each is rare; each is unsolvable headless-only.

3. **Headless trust-but-verify still depends on tmux as the escalation path.** When a headless dispatch goes wrong (stuck for 2 hours, emitting suspicious events, looping), the operator's escalation path is to *attach* — either to the headless subprocess via re-launch as interactive, or to a parallel tmux pane to inspect state. If tmux paths were deleted, that escalation path would also be deleted, leaving only "kill the subprocess and restart from scratch." That is operationally unacceptable for long-running migrations.

4. **The hybrid model is codified in operator memory.** The 2026-04-23 memory `project_hybrid_interactive_headless` is explicit: "Operator decided to keep interactive tmux terminals permanently — headless is opt-in default, not replacement. Never remove interactive code paths." Subsequent dispatches that proposed Wave-E retire-interactive work were operator-rejected. This ADR makes that policy a public, durable artifact rather than a memory file.

5. **Per-terminal NDJSON is headless-only by design — and that is fine.** Per `CLAUDE.md` (project root): "Only subprocess-routed terminals produce this stream. TmuxAdapter-routed terminals (T0 default; T2/T3 unless `VNX_ADAPTER_T{n}=subprocess`) produce no per-terminal NDJSON." This asymmetry is intentional, not a bug. Tmux-routed terminals produce evidence via the pane buffer + receipt; headless-routed terminals produce evidence via NDJSON + archived ring buffer + receipt. Both are valid audit surfaces. The `project_ndjson_ring_buffer` memory codifies this: "Empty live file ≠ broken writer."

6. **Hybrid is a competitive moat, not legacy debt.** Anthropic Managed Agents (2026-04-08 launch) is headless-only by service design — there is no "attach to a Managed Agent and watch it work" affordance. VNX's hybrid model is *uniquely able* to give the operator both: cheap fast headless for routine work, and full interactive observability for the cases that need it. Deleting tmux would erase that differentiator.

7. **Subprocess routing already covers the common case.** Per F32, T1/T2/T3 default to subprocess. The 95% case of "dispatch a worker, get a receipt" is already headless. The remaining 5% (operator-driven debugging, modal recovery, T0-from-T0 escalation) needs tmux. Maintaining both is cheap because the subprocess path is the default — tmux paths see only the long-tail use cases and rarely need updates.

8. **No SDK is used in either path.** Per ADR-003 / project root `CLAUDE.md`: "No Anthropic SDK is used. Only `subprocess.Popen(["claude", ...])`." Both interactive (tmux pane runs `claude` interactively) and headless (`subprocess.Popen` with `-p --output-format stream-json`) routes invoke the Claude Code CLI binary — not the Anthropic SDK. The hybrid model does not introduce new licensing or dependency risk; both modes share the same OAuth-only credential surface.

## Consequences

### Accepted

- `tmux_adapter.py`, `queue_popup_watcher.sh`, `popup_editor.sh`, all `tmux send-keys` paths, and all pane-id tracking (`.vnx-data/state/panes.json`) are **maintained code**, not legacy. Bug fixes and feature parity work targets both modes when applicable.
- Headless remains the default for T1/T2/T3 (per F32); flipping to tmux is a per-terminal env-var override (`unset VNX_ADAPTER_T{n}` or set to `tmux`).
- T0 stays tmux-routed by default. There is no SubprocessAdapter for T0 unless an operator explicitly requests one for a specific dispatch.
- New features must be hybrid-aware: design must specify behavior in both modes, even if one mode is a no-op or graceful degrade.
- Documentation (READMEs, runbooks, ADR cross-references) describes hybrid as **the design**, not as a transitional state. Phrases like "until headless is mature" or "transitional tmux fallback" are forbidden in new docs.
- Per-terminal NDJSON asymmetry (only headless terminals emit) is documented as intentional and not a bug — operators do not file issues for "T0 has no NDJSON."
- Archive durability: when a headless dispatch ends, the live file is truncated and the durable record lives in `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson` (per `project_ndjson_ring_buffer` memory).

### Rejected

- "Wave E retire interactive." Rejected — explicitly removed from any next-step proposals per the operator memory.
- "Headless-only refactor to remove tmux deps." Rejected — would eliminate the operator's primary trust-but-verify mechanism, break modal recovery, and erase a competitive moat vs. Managed Agents.
- "Sunset tmux paths after N months of stable headless." Rejected — there is no time-based deprecation. The hybrid model is permanent.
- "Move T0 to headless." Rejected for the default — T0 is the operator's interactive cockpit. A specific T0 dispatch can run headless if explicitly requested, but the default stays tmux.
- "Auto-promote tmux to headless after first successful dispatch." Rejected — operator chooses per-dispatch or per-terminal which mode to use; VNX does not silently flip modes.

## Implementation note

- `scripts/lib/subprocess_adapter.py` and `scripts/lib/subprocess_dispatch.py` are the headless path.
- `scripts/lib/tmux_adapter.py` and the popup-watcher / popup-editor scripts are the interactive path.
- The dispatcher reads `VNX_ADAPTER_T{n}` per terminal and routes accordingly. Both paths share the same dispatch envelope, the same lease arbitration in `runtime_coordination.db`, and the same receipt format in `t0_receipts.ndjson` (with `source` field distinguishing them: `subprocess` vs implicit-tmux).
- New OIs that propose deleting tmux paths, retiring interactive mode, or sunsetting `tmux_adapter.py` are auto-rejected with a link to this ADR.

## See also

- `project_hybrid_interactive_headless` memory (2026-04-23) — origin policy
- `project_ndjson_ring_buffer` memory — per-terminal NDJSON is headless-only by design
- `feedback_t3_input_mode_probe` memory — modal cases that require interactive surface
- ADR-003 — OAuth-only Claude routing (no SDK in either mode)
- ADR-010 — Subprocess adapter (the headless path)
- ADR-011 — Manager+worker hierarchy with depth>1 (every worker, in either mode, is a separate-agent-per-task primitive)
- `CLAUDE.md` (project root) — "Subprocess Adapter Feature Flag" and "Event Streams" sections
- F32 PR — defaulted T1/T2/T3 to subprocess routing
