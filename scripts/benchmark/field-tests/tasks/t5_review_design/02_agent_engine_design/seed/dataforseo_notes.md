# SERP/SEO-data API (DataForSEO) — integration notes

Public, abstracted summary of the third-party data provider the engine must use.

## Endpoint families

- **SERP API** — live or queued search-engine result pages for a keyword + location +
  language. Returns organic positions, SERP features, paid results. The core of rank
  tracking.
- **Keywords Data API** — search volume, CPC, keyword difficulty, related/suggested
  keywords. Used for research / keyword discovery.
- **DataForSEO Labs** — derived datasets: ranked keywords for a domain, competitors,
  keyword ideas, historical SERP. Good for the onboarding baseline.
- **Backlinks API** — referring domains, new/lost backlinks, anchor profile, domain
  authority signals. Used by both research and daily watch.
- **On-Page API** — crawls a target page/site and returns technical SEO issues
  (status codes, meta, headings, duplicate content, page speed signals).
- **Domain Analytics / Content Analysis** — supplementary domain and content signals.

## Task model

Most endpoints support two modes:

- **Standard (queued/async)** — you POST a task, the provider processes it on its
  queue, and you either poll `tasks_ready` + `task_get` or register a postback/pingback
  webhook. Cheaper. Latency: seconds to minutes. Right for daily batch.
- **Live** — synchronous, result in the response. More expensive per call. Use only
  when a user is waiting interactively.

Requests are batched: one POST can carry up to 100 task objects.

## Rate limits (typical)

- Up to ~2000 simultaneous requests per account.
- ~30 requests/second per endpoint group.
- Exceeding limits returns `40402 / 40403`-style throttling errors — back off.

## Pricing shape

- Billed **per task / per result**, not a flat subscription. Costs vary by endpoint:
  SERP is cheap per result; Labs, Backlinks and On-Page are markedly pricier per call.
- Standard (queued) mode is cheaper than Live for the same data.
- Practical implication: the same keyword's SERP should not be pulled twice in one
  day, baseline (Labs/Backlinks) pulls should be cached for days/weeks, and On-Page
  crawls should be sampled — not run against every page every day.

## Operational notes

- Async tasks can fail or never return — every task needs a timeout + retry policy.
- Results are stable within a day for most endpoints → cache by (endpoint, params, day).
- Postback/webhook delivery is at-least-once → consumers must be idempotent.
