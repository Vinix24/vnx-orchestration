# SEO-audit SaaS — backend context (design fixture)

This is the abstracted context for an architecture-design task. It describes a
real-shaped SaaS so the design lands on something concrete, without exposing any
proprietary product internals.

## What the SaaS does

A subscription SEO-audit platform for agencies and SMBs. A customer registers one
or more **domains**. For each domain the platform:

1. **Research** — on first onboarding, builds a baseline: discovers the domain's
   ranking keywords, top competitors, backlink profile, and on-page issues.
2. **Daily checks** — every day, re-pulls rank positions for the tracked keywords,
   re-crawls a sample of pages for on-page regressions, and watches for new/lost
   backlinks.
3. **Reporting** — rolls the signals into a 0-100 health score per domain, renders
   a dashboard, and emails a weekly digest. Alerts fire on material regressions.

## Existing backend you plug into

- `backend/pipeline.py` — the current extractor pipeline. Extractors are pure
  functions `extract(raw) -> dict`; a runner fans raw inputs through them. This is
  deterministic and already in production. The agent engine must drive / extend this,
  not replace it.
- `backend/storage.py` — the persistence interface (domains, runs, findings,
  reports). Multi-tenant: every row is stamped with `tenant_id`. Writes must be
  idempotent per (tenant, domain, day).

## External data source

The platform buys SEO data from a third-party **SERP/SEO-data API (DataForSEO)**.
See `dataforseo_notes.md` for the endpoint families, async task model, rate limits,
and pricing shape. API spend is a real cost line — the design must control it.

## Constraints that matter

- Multi-tenant: never leak one tenant's data into another's run or report.
- API spend is metered and billed per call — caching and budgeting are first-class.
- Daily job must be idempotent: re-running a day must not double-write or double-bill.
- Governance: every automated decision needs an audit trail; material actions
  (e.g. sending a client a regression alert) get a human-in-the-loop gate.
