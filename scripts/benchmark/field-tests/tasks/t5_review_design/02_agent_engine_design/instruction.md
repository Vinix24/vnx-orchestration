# Task t5-02 — Design the agent-team backend engine for a SEO-audit SaaS

Tier: T5 review/design. This task profiles your **design character**: how you
decompose a multi-agent system, fit tools to agents, and reason about operations,
cost and governance. The deliverable is a design document, not code.

## Context

Read the fixture in this directory:

- `README.md` — what the SaaS does (research → daily checks → reporting) and the
  existing backend you must plug into (`backend/pipeline.py`, `backend/storage.py`).
- `dataforseo_notes.md` — the third-party SERP/SEO-data API (DataForSEO): endpoint
  families, the async/queued vs live task model, rate limits, and pricing shape.
- `backend/pipeline.py`, `backend/storage.py` — the production interfaces the engine
  drives. Extractors are pure functions; storage is multi-tenant and idempotent per
  `(tenant_id, domain, day)`.

## Your job

Design the **agent-team backend engine** that becomes the motor of this SaaS:
the system of cooperating agents that runs research, the DAILY SEO checks, and the
reporting — on top of the existing pipeline and storage, fed by the SERP/SEO-data API.

Write the full design to `./DESIGN.md` in this directory. It must cover ALL of:

1. **Agent decomposition** — define the distinct agent roles (at least three) and the
   rationale for splitting work this way. State each agent's single responsibility.
2. **Tools per agent** — for each agent, the concrete tools/capabilities it gets
   (which API endpoints, which backend functions, which storage calls). No agent gets
   tools it does not need.
3. **The three workflows** — how the agents cooperate to run (a) onboarding research,
   (b) the daily SEO checks, and (c) reporting. Show the flow between agents.
4. **SERP/SEO-data API (DataForSEO) usage** — which endpoint families each workflow
   uses; queued vs live mode choice; the rate-limit strategy; the cost-control /
   budgeting strategy; and the caching strategy (cache key + TTL) that stops
   double-pulling and double-billing.
5. **Backend integration** — exactly how the engine drives `pipeline.run_pipeline`
   and persists through `storage` (idempotent per tenant/domain/day), multi-tenant safe.
6. **Operations** — scheduling of the daily run; idempotency / dedup of a re-run;
   failure modes and retry/timeout policy for async API tasks that fail or never return.
7. **Governance** — the audit trail for automated decisions, and the human-in-the-loop
   gate for material actions (e.g. client-facing alerts).

## Rules

- Be concrete and specific: name endpoints, name agents, name the cache key, name the
  schedule. Avoid generic "we will ensure scalability" filler.
- The design must respect the existing interfaces — drive the pipeline and storage,
  do not propose throwing them away.
- The deliverable is `DESIGN.md` only. Do not write implementation code.
