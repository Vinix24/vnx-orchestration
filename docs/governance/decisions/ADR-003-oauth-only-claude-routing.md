# ADR-003 — OAuth-Only Claude Routing via `claude -p` Subprocess (No SDK, No API Key)

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Codification of M6 (CLI-OAuth Claude routing) from `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4

## Context

VNX drives Claude through `claude -p --output-format stream-json` invoked as a subprocess from `scripts/lib/subprocess_adapter.py` and `scripts/lib/subprocess_dispatch.py`. The operator's Claude licensing context is **Claude Code (OAuth login)** — not a separately-billed Anthropic API account.

Three industry signals in Q2 2026 push toward adopting the Anthropic Python SDK or the new Claude Agent SDK at the transport layer:

1. The **Claude Agent SDK** (`claude-agent-sdk` Python package) now ships typed `HookEventMessage` events, `atexit`-based orphan cleanup, and a control-protocol message bus that re-implements much of `subprocess_adapter.py` natively (~600 LOC of VNX duplication per `claudedocs/2026-05-09-vnx-industry-research.md` Topic 2).
2. **Anthropic Managed Agents** (launched 2026-04-08) is the hosted execution path that the SDK is the natural client for.
3. Strategic-replan synthesis (Topic 2) initially proposed migrating the transport layer to the SDK as a "REPLACE" verdict.

The operator has rejected this transport-layer migration. The reason is structural and licensing-based, not preference-based: routing Claude through the SDK while authenticated via the Claude Code OAuth credential is treated by Anthropic as third-party-API usage and has resulted in account suspensions for wrapper projects in 2025-2026 (opencode / openclaude / opencrest-style forks). The supported "headless" pattern under a Claude Code OAuth credential is the CLI subprocess — same OAuth credential, same telemetry surface, no policy violation.

## Decision

**VNX MUST drive Claude exclusively via `claude -p --output-format stream-json` subprocess. The Anthropic Python SDK and the Claude Agent SDK are forbidden in VNX code.**

Concrete rules:

- The canonical Claude entry point is `scripts/lib/subprocess_dispatch.py` (which delegates to `scripts/lib/subprocess_adapter.py`). All Claude invocations route through this path.
- `import anthropic`, `from anthropic import ...`, `import claude_agent_sdk`, and equivalent imports are banned in VNX scripts. A CI gate enforces this (see §Implementation note).
- LiteLLM-style provider abstraction is **permitted only for non-Claude providers** — Codex CLI, Gemini, Ollama, Kimi, and any future model behind an API key the operator owns. The Claude path stays subprocess.
- For features the SDK provides natively (typed hook events, structured tool-use loops, prompt-cache control, orphan-process cleanup), VNX re-implements the pattern over the CLI subprocess output stream rather than calling the SDK. `active_dispatch_janitor` already covers orphan cleanup; further parity items are tracked as VNX-internal work.
- This rule applies until Anthropic explicitly states the SDK is permitted with a Claude Code OAuth credential. As of 2026-05-09 no such statement exists.

## Reasoning

VNX's value proposition depends on a Claude credential model the operator already owns and pays for, plus full visibility into what the model is doing. Five reinforcing reasons:

1. **OAuth ban-risk is real and observed.** Multiple wrapper projects (opencode, openclaw, opencrest-style forks) have been impacted by Anthropic's enforcement pattern when they routed OAuth-licensed Claude credentials through API-shaped clients in 2025-2026. The CLI subprocess is the documented "headless" pattern Anthropic publishes for the OAuth credential — same telemetry surface, same TOS posture. SDK adoption with the same credential is policy-violating regardless of code-quality benefits.

2. **VNX is a self-hosted alternative to Managed Agents, not a wrapper for it.** Per ADR-004, VNX competes with Managed Agents on local-control, no-per-session-billing, and glass-box observability. The SDK is the client library for Managed Agents and similar hosted runtimes; adopting it on the VNX path leaks that dependency in a way that defeats the alternative-positioning. This decision and ADR-004 reinforce each other.

3. **The CLI subprocess is the licensing-required path.** The operator's monthly Claude Code subscription covers `claude` CLI usage, including headless mode. There is no per-call charge on top of the subscription. Migrating to the SDK would require an Anthropic API key with token-metered billing, which (a) costs money the operator has already paid for in the subscription, and (b) introduces a separate credential to provision, rotate, and audit.

4. **Glass-box visibility into actual subprocess events.** The CLI's NDJSON stdout (`stream-json`) is what VNX captures into per-terminal ring buffers and archives in `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`. This is the canonical observable surface — operator-readable, `tail -f`-able, recoverable from disk alone. The SDK's typed hook events are higher-level but are produced by the same underlying CLI; using the SDK abstracts them behind a Python object model that hides the raw protocol. Per ADR-005, the NDJSON ledger is the canonical record; the CLI subprocess is the cheapest path to producing it.

5. **Structural moat preservation.** Per the strategic replan §4, M6 (CLI-OAuth Claude routing) is a non-negotiable VNX moat. Industry research recommendations to "REPLACE with claude-agent-sdk at the transport layer" (Topic 2) misread VNX's licensing constraint. Codifying this in an ADR makes the constraint visible to future research synthesis and prevents recurring "should we adopt the SDK" debate.

## Consequences

### Accepted

- `scripts/lib/subprocess_dispatch.py` and `scripts/lib/subprocess_adapter.py` are the canonical Claude transport. Both remain VNX-owned code; no upstream library replaces them.
- LiteLLM is approved for non-Claude providers (Codex API-direct, Gemini API-direct, Ollama, Kimi). CLI-subprocess adapters (Codex CLI, Claude CLI) stay custom.
- A CI gate scans VNX scripts for forbidden imports (`anthropic`, `claude_agent_sdk`) and fails the build if any are introduced. The gate is implemented as a small `grep`-style check in the test suite or pre-commit hook.
- New industry-research synthesis that recommends Claude Agent SDK adoption is auto-flagged as **INVALID under VNX licensing constraint** with a link to this ADR. The recommendation is reframed to "continue routing Claude via `claude -p` subprocess; only adopt SDK *patterns* (e.g. orphan cleanup, hook-event taxonomy) that don't replace the CLI invocation."
- Parity work for SDK-native features (typed hook events, tool-use loops, prompt-cache control) is tracked as VNX-internal feature requests against the subprocess adapter — never as SDK-adoption tickets.
- Future framework evaluations (LangGraph, AutoGen, smolagents, CrewAI, MAF, Claude Agent SDK) apply the same test: *does it require an Anthropic API key, or can it route to the Claude Code CLI subprocess?* If only the former, classify as **incompatible-with-our-licensing** and do not recommend adoption.

### Rejected

- Adopting `claude-agent-sdk` as the transport-layer replacement for `subprocess_adapter.py`.
- Adopting the Anthropic Python SDK (`anthropic` package) anywhere in VNX code.
- "Thin wrapper around SDK" patterns where VNX's governance layer sits on top of an SDK-driven Claude session.
- Consuming Anthropic Managed Agents as the Claude execution substrate (separately rejected in ADR-004).
- Any provider-abstraction layer that requires an Anthropic API key for the Claude path, even if the same layer is acceptable for non-Claude providers.

## Implementation note

- `CLAUDE.md` (project root) already states "**No Anthropic SDK is used.** Only `subprocess.Popen(['claude', ...])`." This ADR codifies that statement at the architectural-decision layer.
- A CI gate against `import anthropic` / `import claude_agent_sdk` is required before this ADR can be considered fully enforced. Tracked as a Wave 0 task in the strategic replan.
- Future SDK feature parity (e.g. Anthropic ships a new prompt-cache control surface) is implemented over the CLI subprocess output stream, not via SDK adoption.

## Amendment (2026-05-15): API-key + CLI explicitly permitted

After the June 15 2026 Anthropic Agent SDK credit changes, the operator commissioned strategic research (`claudedocs/agent-sdk-vs-claude-p-cli-strategic-comparison-2026-05-15.md`) comparing Agent SDK direct-API usage against `claude -p` subprocess on the same API key. The verdict: stay with `claude -p` subprocess for the governed worker dispatch path; Agent SDK can be a follow-up provider option for non-governed use cases.

This amendment makes the routing matrix explicit:

| Transport | Auth | Status | Reason |
|---|---|---|---|
| `claude -p` subprocess | OAuth subscription | **Permitted (current default)** | Lowest cost on Pro/Max tiers; ADR-005/010/013 invariants intact |
| `claude -p` subprocess | API key | **Permitted (per-token billing)** | Identical audit surface as OAuth subscription path; switch is operator economic choice |
| Agent SDK (in-process) | OAuth | **Banned** | OAuth credential abuse risk + SDK in-process loop breaks ADR-005/010/013 invariants |
| Agent SDK (in-process) | API key | **Banned for governed worker dispatch** | API-key resolves the licensing concern but SDK still runs the agent loop in-process — incompatible with NDJSON capture, subprocess isolation, and PID-per-worker pools |
| Anthropic Managed Agents | n/a | **Banned** (per ADR-004) | Vendor lock-in conflict with self-hosted positioning |

The Wave 4.6 finalization PR may introduce Agent SDK as an opt-in fifth provider-spawn handler for **non-governed** use cases (sandbox experiments, fast iteration without audit requirements). That path uses API-key only and is mutually exclusive with the worker-dispatch governed-pad; it is not a replacement for `claude -p` subprocess.

## Amendment (2026-05-15 evening): Wave 4.6 PR-4.6.7 — Agent SDK opt-in 5th provider

Per strategic research (`claudedocs/agent-sdk-vs-claude-p-cli-strategic-comparison-2026-05-15.md`), the Anthropic Agent SDK is permitted as an **opt-in fifth provider** with the following hard constraints:

1. **API-key only**: ANTHROPIC_API_KEY env var. OAuth tokens (`sk-ant-oat...` prefix) are runtime-refused.
2. **Single module boundary**: `import anthropic` is permitted ONLY in `scripts/lib/provider_spawns/claude_sdk_spawn.py`. CI gate enforces no SDK imports elsewhere.
3. **Non-governed pad**: this provider does NOT replace `claude -p` subprocess on the governed worker dispatch pad. Use cases: sandbox experiments, fast iteration without audit-isolation requirements.
4. **CanonicalEvent compliance**: per ADR-016, events emitted by this handler MUST conform to the unified event shape.

The governed worker pad (T1/T2/T3 dispatch) remains `claude -p` subprocess via `subprocess_dispatch.py`. This amendment only opens a fifth provider lane for non-critical workflows.

## See also

- ADR-004 — VNX positioning as self-hosted alternative to Anthropic Managed Agents
- ADR-005 — Append-only NDJSON audit ledger as primary observability surface
- ADR-010 — Subprocess adapter as canonical Claude routing (concrete implementation of this ADR)
- `CLAUDE.md` (project root) — "Subprocess Adapter Feature Flag" section
- `scripts/lib/subprocess_adapter.py` — canonical Claude transport
- `scripts/lib/subprocess_dispatch.py` — canonical dispatch entry point
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 (M6) — strategic-moat justification
- `claudedocs/2026-05-09-vnx-industry-research.md` Topic 2 — the rejected SDK-adoption recommendation
- Memory: `feedback_avoid_claude_sdk_use_cli_oauth.md` — operator licensing-constraint feedback
