# ADR-004 — VNX Positioning: Self-Hosted Alternative to Anthropic Managed Agents

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Codification of strategic-replan §1, §4, §7 — VNX-as-product vs VNX-as-thin-wrapper

## Context

On 2026-04-08 Anthropic launched **Claude Managed Agents** — a serverless agent runtime where each agent runs in a gVisor-isolated container on Anthropic infrastructure, with default-deny network egress, scoped filesystem (`/workspace` writable, `/source` read-only), built-in OAuth for user-delegated actions, and pricing at $0.08/session-hour plus standard token costs. Notion, Rakuten, and Sentry are in production on it ([Anthropic Managed Agents InfoQ](https://www.infoq.com/news/2026/04/anthropic-managed-agents/)). Three feature updates shipped by 2026-05-07.

Industry-research synthesis in `claudedocs/2026-05-09-vnx-industry-research.md` (Topic 12) initially recommended "ADOPT alongside — Managed Agents is the right substrate for *production* agent execution; VNX's local-first 4-terminal model remains correct for *development* and *governance experimentation*." The closing 250-word summary went further: "VNX should pivot to a **thin governance layer over Claude Agent SDK + MCP + Managed Agents** — keep the audit/gate/review IP, drop the subprocess plumbing."

The operator has rejected this pivot framing explicitly. VNX's strategic purpose is to be a **self-hosted alternative** to Managed Agents — same coordination + governance + audit goals, but on operator hardware, with Claude Code OAuth login (no per-session billing), no vendor lock-in, and full visibility into the runtime. "Just use Managed Agents" defeats the purpose of building VNX. Per the operator's memory (`project_vnx_is_alternative_not_layer.md`), Managed Agents is "the commoditization VNX is competing with, not a target to converge onto."

This ADR codifies that positioning so future strategic syntheses, framework evaluations, and roadmap proposals do not re-introduce the "pivot to thin wrapper" framing.

## Decision

**VNX is built and maintained as a self-hosted, OAuth-only, glass-box alternative to Anthropic Managed Agents. NOT a layer on top of Managed Agents. NOT a wrapper for it. The product is the alternative itself.**

Concrete positioning:

- VNX targets the **same problem space** as Managed Agents (spawn-supervise-cleanup of agent processes, OAuth-delegated execution, scoped filesystem, structured event protocol).
- VNX competes on **operator-control, OAuth-only credentials, glass-box observability, multi-vendor adversarial review, and human-approval gating**.
- VNX may **learn architecturally** from Managed Agents (gVisor-style isolation, declarative session lifecycle, structured event protocol) without consuming the service.
- The Wave 5 differentiation roadmap (per strategic-replan §7.3) explicitly closes parity gaps where Managed Agents is currently ahead (gVisor isolation, HTTP/webhook triggers) and exploits structural advantages where Managed Agents cannot follow (dual-LLM gating, append-only audit ledger, OAuth-only credential, per-tenant project_id isolation across operator's own DBs).

## Reasoning

Six reinforcing reasons for the alternative-not-layer positioning:

1. **Local control is the moat, not a tactical convenience.** Managed Agents is hosted by definition; the operator cannot inspect the runtime, modify the supervision logic, or patch a bug on their own timeline. VNX runs on operator hardware where every behavior is inspectable and patchable. This is a structural property no hosted service can match — Anthropic would have to ship VNX-as-a-product to compete, and they have no incentive to do so.

2. **No per-session billing.** Managed Agents charges $0.08/session-hour on top of token costs. VNX runs on the operator's existing Claude Code subscription — "free at the margin." For the operator's actual workload (3-4 worker terminals, dozens of dispatches per day), the per-session-hour line is non-trivial. More importantly, it removes the cost-anxiety from running long-lived agent loops (overnight feature work, supervisor daemons, HTTP trigger receivers).

3. **No vendor lock-in on the runtime.** Managed Agents binds the operator to Anthropic's session-lifecycle protocol, their event schema, their billing portal, their support SLA. VNX's runtime is portable — `claude -p` subprocess works on any host with a Claude Code login. If Anthropic raises prices or changes terms, VNX continues to run unchanged.

4. **OAuth-only credential model.** Per ADR-003, VNX drives Claude via `claude -p` subprocess under the operator's existing Claude Code OAuth login — no Anthropic API key required. Managed Agents requires API-account billing. The operator's licensing context makes the API-key path policy-violating (third-party-API ban risk for OAuth credentials). VNX is structurally compatible with the operator's existing Claude subscription; Managed Agents is structurally incompatible. This is not a preference — it is the binary fact that determines which path is even available.

5. **Multi-vendor adversarial review.** Per the strategic-replan §4 (M3), VNX's dual-LLM gate uses Codex to gate Claude's work, with Gemini as a second reviewer, bound by `contract_hash` evidence to PR records. **Anthropic will never ship "use Codex to gate Claude"** — structural conflict of interest. Managed Agents' code-review story is single-vendor by definition. The dual-LLM gate is genuinely novel in 2026 (per industry-research Topic 15: "the 'two LLMs as adversarial reviewers with mandatory evidence binding before merge' pattern remains VNX-specific") and only a self-hosted system can ship it.

6. **Glass-box governance, every layer inspectable.** Per ADR-005, VNX's append-only NDJSON audit ledger gives the operator a `tail -f`-able, `git diff`-friendly view into every dispatch lifecycle event, lease transition, gate outcome, and receipt. Managed Agents is a black box by service-design — Anthropic exposes whatever observability surface they choose, on their schedule. Per ADR-006, VNX's mandatory staging→promote human-approval gate is the audit trail's anchor: every active dispatch has explicit operator consent recorded. Managed Agents has `TaskCreated` hooks but no architectural approval gate. Both are structural advantages a hosted service cannot replicate without becoming a self-hosted product.

The combined effect: VNX's strategic moat is not "we built it ourselves" — it is "we built a system whose key properties (operator-control, OAuth-only, glass-box, dual-vendor adversarial review, human gate) cannot be replicated by a hosted service for structural reasons." That moat is durable as long as VNX remains self-hosted.

## Consequences

### Accepted

- The Wave 5 differentiation features in `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §7.3 are the active development priority once Waves 0-4 close: (1) 3-Layer Trigger System + HTTP/webhook receiver, (2) optional Docker/Podman sandbox isolation, (3) `vnx init` PyPI quickstart. Each closes a parity gap with Managed Agents while preserving VNX's structural advantages.
- A differentiation table is maintained in the public-facing PRD-UH-002 v1.0 (per strategic-replan §10 D3 / D4) showing "what Managed Agents does well that VNX matches" vs "what Managed Agents structurally can't do that VNX uniquely offers."
- The VNX public narrative shifts from "universal headless harness" (PRD-UH-001 v1.3) to "self-hosted alternative to Managed Agents with full glass-box governance" (PRD-UH-002 v1.0 framing, per strategic-replan §10 D4 default = clean break).
- Industry research that surfaces Managed Agents (or any vendor-managed agent runtime — Cloudflare Workers AI agents, Vercel AI runtime, Replit Agent, etc.) as a "platform shift to adopt" is reframed as **competitive/commoditization signal** that informs *what VNX must beat or differentiate from*, not a migration target.
- ADR-003 is treated as a hard prerequisite of this positioning: without OAuth-only Claude routing, the alternative-not-wrapper claim is unsupported.

### Rejected

- "Pivot to thin governance layer over Managed Agents" — the closing recommendation of `claudedocs/2026-05-09-vnx-industry-research.md` §250-word summary.
- "Wrap Managed Agents with our gating" patterns — VNX governance does not sit downstream of a hosted Claude execution; it sits over the operator's own subprocess execution.
- "Consume Managed Agents service" as a deployment target. VNX's deployment target is the operator's own hardware (or a future PyPI-installable footprint per Wave 5 candidate 3), not Anthropic's runtime.
- Cloudflare Dynamic Workers, Daytona, E2B, Fly Machines, Vercel Sandbox, or other hosted Agent SDK substrates as the VNX runtime layer. These are valid for projects whose requirements differ; they are not valid for VNX given M6 + this ADR.
- Any roadmap framing where VNX's value reduces to "the audit/gate IP" while the execution layer commoditizes to a hosted vendor.

## Implementation note

- `CLAUDE.md` (project root) is updated in Wave 0 to add a Q2 2026 platform-shift note pointing at this ADR and ADR-003.
- The differentiation table in §7.1 / §7.2 of `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` is the canonical reference until PRD-UH-002 v1.0 supersedes it.
- Wave 5 candidate selection (operator decision D5 in the strategic replan) is the next concrete output of this positioning.

## See also

- ADR-003 — OAuth-only Claude routing via `claude -p` subprocess
- ADR-005 — Append-only NDJSON audit ledger as primary observability surface
- ADR-006 — Staging→promote with mandatory human approval gate
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §1, §4, §7 — strategic positioning
- `claudedocs/2026-05-09-vnx-industry-research.md` Topic 12, Topic 15, §250-word summary — the rejected pivot framing
- Memory: `project_vnx_is_alternative_not_layer.md` — operator strategic-intent feedback
- Memory: `project_vnx_strategic_direction.md` — VNX evolution from governance-first to multi-manager system
