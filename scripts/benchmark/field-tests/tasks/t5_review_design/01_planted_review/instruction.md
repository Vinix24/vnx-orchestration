# Task t5-01 — Code review of a SEO-audit SaaS backend

Tier: T5 review/design. This task profiles your **review character**: which classes
of defect you catch, how you categorise them, and how much noise you generate. It is
NOT a code-writing task. Do not modify the source files.

## Context

The package `./seoaudit/` is the backend of a small SEO-audit SaaS. It pulls SERP
data from a third-party API, fetches target pages, scores them, persists the result,
and runs a daily loop over every tracked domain. Four modules:

- `seoaudit/db.py` — SQLite persistence + an in-process report cache
- `seoaudit/serp_client.py` — the SERP/SEO-data API client + live page fetch
- `seoaudit/crawl_queue.py` — the daily scheduler that runs each domain
- `seoaudit/report_builder.py` — scoring + report assembly + pagination

## Your job

Review all four modules and report every real defect you find. Cover these five
classes explicitly: **security, performance, correctness, operational, maintainability.**

For EACH defect, emit one finding block in EXACTLY this format (the grader parses it):

```
### FINDING
- file: <module path, e.g. seoaudit/db.py>
- line: <line number or range>
- category: <security|performance|correctness|operational|maintainability>
- severity: <low|medium|high|critical>
- title: <one short line>
- description: <what is wrong, why it matters, and the concrete consequence>
```

Write the full review — all finding blocks — to `./REVIEW.md` in the working
directory (the same directory that contains `seoaudit/`).

## Rules

- One finding block per distinct defect. Name the exact file in the `file:` field.
- Be specific: name the mechanism (e.g. "f-string interpolation into SQL", "mutable
  default argument", "regex compiled inside the loop"), not a vague "could be better".
- Categorise each finding into the single best-fit class.
- Do not invent defects to pad the list — unfounded findings count as noise.
- Do not rewrite the code. The deliverable is `REVIEW.md` only.
