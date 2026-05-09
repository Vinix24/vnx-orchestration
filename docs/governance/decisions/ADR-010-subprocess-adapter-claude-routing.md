# ADR-010 — Subprocess Adapter (`claude -p`) as Canonical Claude Routing

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Canonical Claude invocation path; SDK-ban implementation; ADR-003 codification

## Context

VNX dispatches work to Claude worker terminals (T1, T2, T3 by default; T0 progressively). Two routing options exist:

1. **Tmux send-keys.** The legacy interactive path: a worker tmux pane is open, the dispatcher injects keystrokes via `tmux send-keys`, and the operator/observer watches the pane stream. Output is scraped from the pane buffer.
2. **Subprocess.** A headless path: the dispatcher spawns `claude -p --output-format stream-json` as a child process and consumes the event stream directly. Output is structured NDJSON; no scraping required.

The subprocess path was introduced in F32 (`VNX_ADAPTER_T1=subprocess`) as opt-in per-terminal. By Q1 2026 it had become the dominant production path for T1; T2 and T3 followed. T0 progressively migrated through F36 + W7 (PRs #411-#424 streaming-drainer / canonical-event work).

The canonical implementation is `scripts/lib/subprocess_dispatch.py`. The first 100 lines establish the contract:

- Lines 11-12: "BILLING SAFETY: only `subprocess.Popen(['claude', ...])` is invoked downstream — no Anthropic SDK is imported anywhere in this package."
- Lines 13-19: Success-path call order (receipt → pattern confidence → dispatch outcome) with per-dispatch event archival in a finally block.
- Lines 41-99: Imports from `subprocess_adapter`, `headless_context_tracker`, `worker_health_monitor`, plus internals for delivery, recovery, manifest, pattern confidence, receipt writing, skill injection, state paths, and git helpers.

The strategic replan (`claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M6) calls this out as **the** licensing-critical decision: "Claude Agent SDK with OAuth Code credential = third-party-API risk (opencode/openclaw precedent). Non-negotiable. Lock this in `subprocess_adapter.py`; pin via `CLAUDE.md` rule 'No Anthropic SDK is used'; CI gate to block accidental SDK imports."

This ADR codifies the implementation as binding architecture and the SDK-ban as a CI-enforced rule.

## Decision

**`scripts/lib/subprocess_dispatch.py` is the canonical Claude invocation path in VNX. It spawns `claude -p --output-format stream-json` as a subprocess and consumes the event stream into NDJSON ledger entries. All Claude worker terminals route through this adapter by default. Tmux fallback (interactive panes) remains supported for operator-driven sessions but is NOT the dispatch primary. The Anthropic Python SDK is never imported.**

Concretely:

1. **Subprocess as default.** Per-terminal feature flags (`VNX_ADAPTER_T1=subprocess`, `VNX_ADAPTER_T2=subprocess`, `VNX_ADAPTER_T3=subprocess`) default to subprocess. Tmux is opt-in via `VNX_ADAPTER_T<N>=tmux` for operator-driven debugging.

2. **`claude -p --output-format stream-json` as the invocation form.** The `-p` flag selects print mode; `--output-format stream-json` produces line-delimited JSON events that map directly to the canonical event schema (W7-A PR #411). No other Claude CLI invocation form is permitted in dispatch code.

3. **No Anthropic SDK.** No file in `scripts/lib/subprocess_dispatch_internals/` or its callers may import `anthropic`, `@anthropic-ai/sdk`, or any client library that talks to the Anthropic HTTP API. The OAuth credential used by `claude` CLI is the Code subscription credential and is not authorized for SDK use (opencode/openclaw precedent). Strategic replan §4 moat M6: "This is the licensing-required path."

4. **Tmux retained as fallback.** Per the operator memory `project_hybrid_interactive_headless`: "Keep tmux code paths permanently; headless is opt-in default, not replacement. No Wave E retire-interactive." Interactive panes remain useful for operator-driven sessions, manual recovery, and debugging. They are not removed.

5. **Per-terminal NDJSON event archival.** Per the operator memory `project_ndjson_ring_buffer`: "T{n}.ndjson truncated post-dispatch; durable record in events/archive/{terminal}/." Subprocess delivery's finally block calls `event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)` to archive then truncate. Empty live file is not a bug — it is the post-dispatch state.

6. **Canonical event schema.** The stream-json events map directly to `canonical_event` schema entries written into `t0_receipts.ndjson` and per-dispatch archives. This is the same schema W7 (PRs #411-#424) standardized.

7. **CI-enforced SDK ban.** A grep-based test in CI scans `scripts/`, `dashboard/`, and `tests/` for forbidden imports (`import anthropic`, `from anthropic`, `require('@anthropic-ai/sdk')`) and fails the build on any match. The ban is structural, not policy.

## Reasoning

1. **Concrete implementation of ADR-003.** ADR-003 established subprocess as the architectural direction; ADR-010 codifies the specific implementation (`subprocess_dispatch.py`), the specific invocation form (`claude -p --output-format stream-json`), and the specific ban (no Anthropic SDK). Without ADR-010, ADR-003 is a direction; with it, the direction is enforceable at code-review time.

2. **Stream-json gives full per-event observability.** The tmux send-keys path scrapes a pane buffer — fundamentally a screen-scrape. The subprocess path consumes structured events: tool calls, message deltas, tool results, completion signals, errors. Every event maps to a `canonical_event` row. This observability is the foundation of the W7 streaming drainer, the smart tap, the worker health monitor, and the dashboard SSE feed. Tmux scraping cannot give VNX this surface.

3. **No SDK is the licensing bedrock.** The OAuth credential used by `claude` CLI is bound to the operator's Claude Code subscription. The Code subscription terms permit CLI use; SDK access requires API billing and a separate credential. Mixing them (using the OAuth credential to drive the SDK) is the failure mode of opencode/openclaw. VNX explicitly stays on the CLI-subprocess side of that line. Strategic replan §4 moat M6: "Claude Agent SDK with OAuth Code credential = third-party-API risk. Non-negotiable."

4. **Subprocess is a cleaner contract than tmux.** Tmux send-keys requires the pane to be ready, the input mode to be correct, the output buffer to be clean, the operator to not be typing. Operator memory `feedback_t3_input_mode_probe` records the recurring "T3 needs /clear before accepting new dispatches" failure — a tmux-specific failure. Subprocess has no such state: each dispatch is a fresh process with a clean stdin/stdout pipe.

5. **Hybrid is intentional, not transitional.** Per `project_hybrid_interactive_headless` memory: tmux is not on a deprecation path. Interactive panes serve a different purpose (operator-driven exploration) than headless dispatch (automated work). Both stay. The subprocess primary is for governed dispatch; tmux is for hands-on operator work.

6. **It composes with the canonical event schema.** W7-A (PR #411) standardized `canonical_event` as the audit ledger entry shape. Stream-json events map 1:1 to canonical events. Tmux scraping requires reverse-engineering events from text. Subprocess gives them as structured data. ADR-010 is therefore the upstream of ADR-005 (NDJSON ledger) — the ledger entries originate in the subprocess event stream.

7. **The CI ban is the only durable enforcement.** Asking engineers to "not import the SDK" is policy; greppping the codebase at CI time for `import anthropic` is structural. Strategic replan §4 moat M6: "CI gate to block accidental SDK imports." The ban is checked on every PR, not just at architecture review.

8. **Subprocess is also where multi-tenant identity flows.** Per the single-VNX migration plan §3.5: "Subprocess dispatch (`scripts/lib/subprocess_dispatch.py:128`): identity flows from T0's resolver through the env (`VNX_PROJECT_ID`, `VNX_ORCHESTRATOR_ID`, `VNX_AGENT_ID=<T1|T2|...>`) into the spawned `claude -p` process. The spawned worker's own `resolve_identity` then reads those env vars." ADR-007's project_id stamping requires a routing path that propagates env reliably; subprocess does, tmux does not.

9. **It is a reversible decision.** If a future Anthropic offering provides equivalent observability over an OAuth-permitted programmatic interface (not the API SDK), this ADR can be revised. The reversibility is preserved because the contract surface (stream-json events → canonical events) is independent of the transport.

## Consequences

### Accepted

- Any new Claude integration MUST extend `subprocess_dispatch.py`, not bypass it. New worker types (e.g., a fifth terminal, a sub-orchestrator pool) plug into the same adapter.
- The CI grep-ban on `anthropic` SDK imports runs on every PR and blocks merge on hits.
- T0 progressive migration to subprocess (per F36 + W7) continues; tmux remains as fallback per operator memory.
- The `canonical_event` schema (W7-A PR #411) is the contract between the subprocess event stream and the audit ledger; stream-json events that don't map to canonical events are a schema drift bug.
- Per-terminal NDJSON ring buffer truncation post-dispatch is part of the contract; archive lives at `events/archive/{terminal}/{dispatch_id}.ndjson` per operator memory `project_ndjson_ring_buffer`.
- LiteLLM adoption (strategic replan §5 A5) is permitted ONLY for non-Claude providers (Codex, Gemini, Ollama, Kimi). The Claude path stays subprocess.

### Rejected

- **Direct `claude` CLI invocation that doesn't capture stream-json.** Calling `claude` without `-p --output-format stream-json` (e.g., shelling out to `claude` interactively from a script) loses the event surface and is rejected.
- **Anthropic Python SDK.** `import anthropic` is banned at CI. The SDK is not importable from VNX dispatch code under any scenario, including testing.
- **`@anthropic-ai/sdk` (TypeScript).** Same ban applies to the dashboard's TypeScript code.
- **REST shim wrappers.** Wrapping the Anthropic HTTP API in a thin shim that "just makes the calls" is the SDK ban under a different name. Rejected.
- **LiteLLM for Claude.** LiteLLM-Claude routes through the API and the SDK shape; not authorized. Per strategic replan §5 A5 explicitly: "Claude path stays subprocess — non-negotiable per M6."
- **Tmux send-keys as default for new terminals.** New terminals default to subprocess. Tmux is opt-in fallback only.
- **Removing the tmux path.** Per `project_hybrid_interactive_headless`: tmux is permanent. No Wave E retire-interactive.

## Cross-references

- ADR-003 (architectural direction this ADR implements)
- ADR-005 (NDJSON ledger — downstream consumer of subprocess events)
- Strategic replan `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M6 (OAuth-only Claude routing, never SDK), §5 A5 (LiteLLM scope)
- Operator memory `project_hybrid_interactive_headless` (tmux retained permanently)
- Operator memory `project_ndjson_ring_buffer` (per-terminal NDJSON archival contract)
- Operator memory `feedback_t3_input_mode_probe` (tmux-specific failure mode subprocess avoids)
- `claudedocs/2026-04-30-single-vnx-migration-plan.md` §3.5 (identity propagation via subprocess env)
- `scripts/lib/subprocess_dispatch.py` (canonical implementation), `scripts/lib/subprocess_adapter.py` (delivery primitive)
- Project root `CLAUDE.md` "Subprocess Adapter Feature Flag" section
- F32 (subprocess opt-in introduction), F36 (T0 migration), W7-A PR #411 (canonical event schema)
