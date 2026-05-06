# Feature: Phase 16 — Business-Domain Bootstrap (marketing-lead + content/SEO/GA4 workers)

**Status**: Draft
**Priority**: P1
**Branch**: `feature/phase-16-business-domain-bootstrap` (umbrella; per-wave branches listed in each sub-PR)
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review (per-PR), codex_gate (feature-end + w16-6), claude_github_optional

Primary objective:
Prove VNX's universal-harness vision by extending the orchestration system from the code domain into the content/marketing/sales domain. Same machinery (folder agents, capability tokens, governance variants, memory partitions, dispatch flow), different agent folders + governance variant + provider chain. By the end of Phase 16, an operator can dispatch "schrijf 1 blog van begin tot eind" and the mission flows main → marketing-lead → seo-analyst (research) → blog-writer (draft) → marketing-lead (review) → commit, with full audit trail and domain-isolated memory.

Strategic rationale (the USP):
- CrewAI: agents-as-tools within one crew, no governance variants per domain
- AutoGen: teams without governance variants per role
- Aider/Cline: code-only
- Mem0: memory only, no orchestration
- VNX after Phase 16: the only single-operator multi-domain orchestration framework with audit-grade governance per domain (coding-strict for code workers, business-light for content workers, both running off one operator and one VNX install). Worth a Show-HN.

References:
- `.vnx-data/strategy/ROADMAP.md` Phase 16 section (waves w16-1..w16-8)
- `.vnx-data/strategy/roadmap.yaml` lines 661–778
- `.vnx-data/strategy/backlog.yaml` items BL-2026-05-001..004 (content ideas), BL-2026-05-008 (GA4 MCP server), BL-2026-05-013 (sales-outreach worker — Phase 16 follow-up)
- `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` §5 FR-4 (folder agents — foundation), FR-6 (governance variants — `business-light`), FR-7 (multi-tier hierarchy — marketing-lead is a sub-orchestrator)
- `.vnx-data/state/PROJECT_STATE_DESIGN.md` Layer 2 (memory partitioning per domain — model for `vec_artifacts_marketing` / `vec_artifacts_sales`)
- Companion drafts in `agent_drafts/` (BEHAVIOR.md drafts for each agent + governance variant + GA4 MCP design + skills catalog)

## Pre-flight: depends on these earlier phases having landed

This feature plan presupposes:
- Phase 7 (W8 folder-based agents) is in place — `.claude/agents/orchestrators/<id>/` and `.claude/agents/workers/<id>/` are the canonical agent surface; legacy `_inject_skill_context()` is fallback only.
- Phase 9 (W10 capability tokens + governance variants) is live — `governance.yaml` per orchestrator is honored, including a registered set of variants. Phase 16 introduces a NEW variant: `business-light`.
- Phase 12 (W-mem memory layer) is live — `quality_intelligence.db` hosts `vec_artifacts_<domain>` virtual tables; `nomic-embed-text` via Ollama is operational; the retrieval API and CLI exist. Phase 16 adds two new domain partitions (`vec_artifacts_marketing` already proposed in design but not yet bootstrapped + `vec_artifacts_sales`).

If any of those preconditions has not landed, w16-1 must not begin. T0 will surface a blocker open-item rather than backfill those phases inside this feature.

## Dependency Flow

```text
w16-1 (governance variant + permissions templates)
   │
   ├──► w16-3 (blog-writer)
   ├──► w16-4 (linkedin-writer)
   ├──► w16-5 (seo-analyst + brave-search MCP)
   ├──► w16-6 (ga4-analyst + custom GA4 MCP server)
   └──► w16-2 (marketing-lead orchestrator) ── needs w16-1 + Phase 12 W-mem-3
                              │
                              └──► w16-7 (per-domain memory partitions for marketing/sales)
                                                    │
   ┌───── w16-2 ─────────┐                          │
   │                     ▼                          │
   │   w16-3 ─┐                                     │
   │   w16-5 ─┤  ◄── all required for ──►           │
   │   w16-7 ─┘                                     │
   └─────────────────────────────────────────►  w16-8 (end-to-end smoke-test mission)
                                                  (FEATURE-END codex_gate fires here)
```

Critical-path summary:
- w16-1 unblocks five children (w16-2..w16-6 all need the governance variant + permission templates).
- w16-2 (marketing-lead) is the orchestrator hub; w16-3/w16-4/w16-5/w16-6 can be drafted in parallel before w16-2 lands but cannot be dispatched-to until w16-2 is merged (you need an orchestrator to dispatch them).
- w16-7 is gated by w16-2 (it needs the marketing-lead's `domain` field) and by Phase 12 W-mem-3.
- w16-8 is the integration test and gates the feature-end codex review.

Recommended execution order (sequential):
1. w16-1 → 2. w16-2 → 3. w16-5 → 4. w16-3 → 5. w16-4 → 6. w16-7 → 7. w16-6 → 8. w16-8.

Recommended execution order (with maximum parallelism, two workers):
- Lane A: w16-1 → w16-2 → w16-7 → w16-8
- Lane B: (after w16-1) w16-5 → w16-3 → w16-4 → w16-6 (joins back at w16-8)

## Gate placement strategy (read this before reviewing per-PR Review-Stack fields)

- Per-PR: every wave runs `gemini_review`. This is the cheap, fast quality gate appropriate for content-domain config + markdown-heavy PRs.
- Feature-end: `codex_gate` runs ONLY on w16-8 (the smoke test) where it audits the integrated behavior of the full Phase 16 surface. Running codex on every config-only PR would be expensive and wrong-tool-for-the-job (codex tone is a poor fit for prose review).
- One mid-feature exception: w16-6 also runs `codex_gate + claude_github_optional` because it ships a custom MCP server (~300 LOC of real Python) that touches credential handling for the GA4 Data API. Security-sensitive surface area justifies the extra gate.

## Model assignment (per-PR)

| Wave | Model | Justification |
|------|-------|---------------|
| w16-1 | Sonnet | ~100 LOC; YAML schema + permission template; deterministic config work. |
| w16-2 | Sonnet | ~200 LOC; mostly markdown (BEHAVIOR.md) + YAML wiring. No novel architecture. |
| w16-3 | Sonnet | ~150 LOC; markdown persona + skill templates. |
| w16-4 | Sonnet | ~120 LOC; markdown persona + skill templates. |
| w16-5 | Sonnet | ~130 LOC; markdown + MCP grant config. Brave-search wiring is well-trodden. |
| w16-6 | **Opus** | ~450 LOC; **deviation justified**: custom Python MCP server with credential handling (GA4 service account JSON), API-design surface, security-sensitive. Opus's stronger reasoning is warranted for getting credential boundaries right and for designing a clean MCP API the operator may give away as a community repo. |
| w16-7 | Sonnet | ~100 LOC; sqlite schema migration + table bootstrap. Mechanical. |
| w16-8 | Sonnet | ~50 LOC; integration test harness + assertions. Mechanical. |

Total Phase 16: ~1300 LOC across 8 PRs. Roadmap.yaml estimate is 1150 LOC; ~12% overage budget reserved for test plan additions discovered during plan drafting.

## Operator-input placeholders (must be filled BEFORE certain waves can ship)

These are flagged inside the BEHAVIOR.md drafts under `<OPERATOR_INPUT_NEEDED>` blocks. Summary list:

| Wave | Placeholder | What operator must provide |
|------|-------------|----------------------------|
| w16-3 | blog-writer tone | 3–5 reference blog posts Vincent already wrote (URLs or markdown), preferred length range, structural conventions (headings? TL;DR top? footer CTA?), preferred reading level |
| w16-4 | LinkedIn voice | LinkedIn handle, 5–10 reference posts, hook style preference, hashtag conventions, posting cadence |
| w16-5 | SEO scope | Target keyword themes, competitor URLs to track, audit checklist preferences |
| w16-6 | GA4 access | GA4 property ID, service-account JSON file path (env-only — not committed), preferred dimensions/metrics, conversion event names, exclusion filters (internal traffic) |
| all | content calendar | Reference document (or "no calendar — use ad-hoc") of approved blog topics; if calendar exists, location |

If any placeholder is unfilled, the corresponding wave's worker is non-functional and w16-8 (smoke test) will fail. T0 must surface unfilled placeholders as blocking open-items before kicking off the dependent wave.

---

## w16-1: business-light governance variant + permissions templates

**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @architect (for governance schema decisions) executing as @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: 100
**Branch**: `feat/w16-1-business-light-governance`
**Dependencies**: [Phase 7 W8-B (governance.yaml plumbing exists), Phase 9 W10 (governance variants registered)]

### Description
Introduce the `business-light` governance variant — the policy bundle for content/marketing workers. Unlike `coding-strict` (the existing code-domain default), `business-light` skips codex_gate per-PR, drops the PR_size_limit, replaces source-quality gates with `content_review` (operator or marketing-lead self-review), and allows auto_merge for low-risk content artifacts. Plus: ship a reusable `permissions.yaml` template for content workers (allowed: Read, Write, WebFetch; denied: Bash, code execution, Edit on `.py`/`.ts`/`.sh`).

### Scope
- New file `docs/governance/variants/business-light.yaml` (or wherever Phase 9 placed variant definitions)
- New file `.claude/agents/_templates/permissions/content-worker.yaml` — reusable permissions template
- Variant schema entry in the governance variant registry (extend Phase 9's variant enum)
- Documentation snippet: when to use `business-light` vs `coding-strict`, what changes, what stays the same (capability-token verification still mandatory; only the per-PR gate stack differs)

### Out of scope
- Any worker or orchestrator that consumes the variant (those are w16-2..w16-6).
- Any change to `coding-strict`.
- Any new variant beyond `business-light`.

### Success Criteria
- A new orchestrator can declare `governance: business-light` in its `governance.yaml` and the dispatcher accepts it.
- Variant registry validation rejects unknown variants (existing behavior preserved).
- The `content-worker.yaml` permissions template, when symlinked or copied into a worker folder, denies `Bash` and code-execution surface but allows `Read` / `Write` / `WebFetch`.
- A unit test demonstrates that a worker with `content-worker.yaml` permissions cannot invoke a Bash tool call (verifier raises a structured error in the receipt).

### Quality Gate
`gate_w16_1_business_light_governance`:
- [ ] `business-light` variant registered + validated by governance schema
- [ ] `content-worker.yaml` permissions template denies Bash/code execution
- [ ] Dispatcher accepts `governance: business-light` without warnings
- [ ] Unit test: forbidden-tool dispatch is rejected with structured receipt error

### Test Plan
- Schema validation test: load `business-light.yaml`, assert it conforms to the variant schema; assert mandatory fields (`variant_id`, `per_pr_gates`, `feature_end_gates`, `auto_merge_policy`, `pr_size_limit`).
- Negative test: load a malformed variant (missing `variant_id`) → schema validator raises with a specific error message.
- Permissions template test: instantiate a worker with `content-worker.yaml`, attempt a `Bash` tool call → assert the dispatcher rejects with `permission_denied` and that the rejection lands in the receipt (not silently dropped).
- Backward-compat test: `coding-strict` variant still resolves correctly; existing code-domain dispatch flow is unchanged.

### Operator-input placeholders
None for this wave — config-only.

---

## w16-2: marketing-lead orchestrator agent folder

**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @architect for orchestrator persona design, executing as @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Estimated LOC**: 200
**Branch**: `feat/w16-2-marketing-lead-orchestrator`
**Dependencies**: [w16-1, Phase 12 W-mem-3]

### Description
Stand up the `marketing-lead` orchestrator as a folder-based agent under `.claude/agents/orchestrators/marketing-lead/`. This is a Tier-2 orchestrator (sub-orch dispatched by main): it owns its own worker pool (`blog-writer`, `linkedin-writer`, `seo-analyst`, `ga4-analyst`), runs under `governance: business-light`, has a provider chain `claude → gemini` (no codex — codex is poor fit for prose orchestration), and exposes 3 invocable skills: `plan-content-calendar`, `kies-onderwerp` (Dutch — pick a topic), `analyseer-performance`.

The folder layout (per FR-4):
```
.claude/agents/orchestrators/marketing-lead/
├── BEHAVIOR.md             # persona + workflow (operator-tone agnostic; that lives in workers)
├── CLAUDE.md  -> BEHAVIOR.md   # symlink for Claude provider
├── GEMINI.md  -> BEHAVIOR.md   # symlink for Gemini provider
├── governance.yaml         # variant: business-light
├── runtime.yaml            # provider_chain: [claude, gemini]
├── workers.yaml            # [blog-writer, linkedin-writer, seo-analyst, ga4-analyst]
├── permissions.yaml        # orchestrator scope: dispatch, read receipts, read memory
└── skills/
    ├── plan-content-calendar.md
    ├── kies-onderwerp.md
    └── analyseer-performance.md
```

### Scope
- BEHAVIOR.md drafted per `agent_drafts/marketing-lead-BEHAVIOR-draft.md` — operator fills in any company-specific tone, but the persona scaffolding ships in this PR.
- governance.yaml referencing `business-light` (from w16-1)
- runtime.yaml with provider chain and explicit `excluded_providers: [codex]`
- workers.yaml listing the 4 worker agents (those workers may not exist yet at merge time; agent registry validator must accept forward references with a warning, not a hard fail)
- permissions.yaml: orchestrator can dispatch to its declared workers, read receipts, query memory partitions for `domain in [marketing, sales]`, but cannot reach code-domain workers
- 3 skill files in `skills/` matching the registered names

### Out of scope
- Worker BEHAVIOR.md content (those are w16-3..w16-6).
- Custom MCP server (w16-6).
- Memory partitions (w16-7).
- Smoke test (w16-8).

### Success Criteria
- The agent registry validator accepts `marketing-lead` and reports its variant, provider chain, and worker pool.
- A dry-run dispatch from main → marketing-lead → resolves the BEHAVIOR.md correctly via Claude symlink (and via Gemini symlink in failover).
- `governance.yaml` validation confirms `business-light` is the resolved variant.
- The `excluded_providers: [codex]` enforcement is verified: a synthesized failover that tries to fall through to codex is rejected with a structured error.
- All 3 skill files exist and are listed in the agent's skill manifest.

### Quality Gate
`gate_w16_2_marketing_lead`:
- [ ] Agent folder structure validates per FR-4
- [ ] Provider chain `claude → gemini` resolves; codex is excluded with audit trail
- [ ] Workers.yaml accepts 4 forward references (workers may not exist yet)
- [ ] All 3 skills load and validate

### Test Plan
- Agent loader test: instantiate `marketing-lead` from disk, assert BEHAVIOR.md is non-empty, governance is `business-light`, runtime chain matches `[claude, gemini]`.
- Provider exclusion test: simulate Claude + Gemini both failing health-check → verify the resolver does not fall through to codex; instead emits `provider_chain_exhausted` (structured) with `excluded_providers` attached.
- Forward-reference test: with workers/blog-writer not yet on disk, validator emits a warning + still loads marketing-lead; once blog-writer lands the warning clears.
- Symlink test: rename BEHAVIOR.md → confirm both CLAUDE.md and GEMINI.md follow the rename; rename back.
- Cross-domain isolation test: marketing-lead's permissions reject a dispatch to `backend-developer` (a code-domain worker) with `permission_denied: out_of_domain`.

### Operator-input placeholders
- `<OPERATOR_INPUT_NEEDED: COMPANY_VOICE>` — high-level brand voice for marketing-lead's decision-making (concise/cheeky vs corporate/formal vs technical/dry). Placed in BEHAVIOR.md draft.
- `<OPERATOR_INPUT_NEEDED: CONTENT_CALENDAR_REF>` — pointer to a content calendar (markdown file path, Notion URL, or "ad-hoc"). Used by `plan-content-calendar.md` skill.

---

## w16-3: blog-writer worker agent folder

**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day (much of which is operator filling in tone references)
**Estimated LOC**: 150
**Branch**: `feat/w16-3-blog-writer`
**Dependencies**: [w16-1]

### Description
Stand up the `blog-writer` worker as a folder-based agent under `.claude/agents/workers/blog-writer/`. This worker drafts long-form blog content (target range 800–2000 words). Its BEHAVIOR.md captures operator tone, structural conventions (e.g. TL;DR at top? subheadings every 200 words? CTA in footer?), and length adherence rules. It exposes 3 skills: `draft-post`, `edit-for-tone`, `add-seo-meta`.

Provider chain: `claude → gemini`. Codex is explicitly excluded (codex prose tone is wrong for blog work — see test plan).

### Scope
- BEHAVIOR.md drafted per `agent_drafts/blog-writer-BEHAVIOR-draft.md` with operator-input placeholders for tone references, length range, and structural preferences.
- CLAUDE.md / GEMINI.md symlinks.
- permissions.yaml using the `content-worker.yaml` template from w16-1 (Read, Write, WebFetch; deny Bash + code execution).
- runtime.yaml with `provider_chain: [claude, gemini]`, `excluded_providers: [codex]`.
- 3 skill files: `skills/draft-post.md`, `skills/edit-for-tone.md`, `skills/add-seo-meta.md`.

### Out of scope
- LinkedIn variant (w16-4).
- SEO research input (provided by seo-analyst — w16-5).
- GA4 performance analysis (w16-6).

### Success Criteria
- Folder validates per FR-4.
- Worker is dispatchable from `marketing-lead` (workers.yaml resolution succeeds once blog-writer lands).
- Permissions template prevents the worker from invoking Bash even if a malicious prompt asks for it.
- Length-adherence is enforced at the skill level: `draft-post.md` declares a `target_word_count` parameter and the worker self-checks the output before returning.

### Quality Gate
`gate_w16_3_blog_writer`:
- [ ] Folder validates; BEHAVIOR.md present and non-empty
- [ ] Permissions deny Bash (verified by negative dispatch test)
- [ ] `draft-post.md` skill declares and enforces a length range
- [ ] runtime.yaml excludes codex from provider chain

### Test Plan
- Output-quality test (operator-graded, NOT automatable): operator dispatches "draft a 1000-word blog about VNX governance" → operator reviews on tone/structure/length adherence → if the output drifts from operator voice, iterate BEHAVIOR.md placeholders + re-test. This is a manual, recurring check during initial bring-up. Mark BEHAVIOR.md ready only when operator signs off.
- Length-adherence test: dispatch with `target_word_count: 1200, tolerance: 150` → assert generated word count is in `[1050, 1350]` → if outside, the worker re-drafts (max 1 retry) before returning.
- No-codex-in-prose test: read `runtime.yaml`, assert `codex` is in `excluded_providers`. Synthesize a failover scenario where claude + gemini both fail → assert the dispatch returns `provider_chain_exhausted` rather than falling through to codex.
- Permissions test: dispatch a prompt that asks the worker to run `Bash` → assert refusal + structured receipt error `permission_denied`.
- Cross-domain isolation test (joint with w16-7): blog-writer's memory queries are scoped to `domain=marketing` only.

### Operator-input placeholders
- `<OPERATOR_INPUT_NEEDED: BLOG_TONE_REFERENCES>` — 3–5 URLs or markdown file paths to existing Vincent-authored blog posts that exemplify the desired tone.
- `<OPERATOR_INPUT_NEEDED: BLOG_LENGTH_RANGE>` — preferred default range (e.g. 800–1500), absolute floor/ceiling.
- `<OPERATOR_INPUT_NEEDED: BLOG_STRUCTURE_CONVENTIONS>` — TL;DR top? Subheading cadence? Footer CTA template? Reading-level target?
- `<OPERATOR_INPUT_NEEDED: BLOG_BANNED_PHRASES>` — phrases the worker must avoid (e.g. "in today's fast-paced world", "delve into", "leveraging").

---

## w16-4: linkedin-writer worker agent folder

**Track**: A
**Priority**: P2
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5–1 day
**Estimated LOC**: 120
**Branch**: `feat/w16-4-linkedin-writer`
**Dependencies**: [w16-1]

### Description
Stand up `linkedin-writer` worker under `.claude/agents/workers/linkedin-writer/`. LinkedIn-specific tone (shorter, hook-driven, scannable, line-break-heavy), length conventions (single-post: 150–300 words; carousel: 6–10 slide outlines), and 3 skills: `draft-post`, `draft-carousel`, `respond-to-comment`.

Same provider chain as blog-writer (claude → gemini, exclude codex).

### Scope
- BEHAVIOR.md per `agent_drafts/linkedin-writer-BEHAVIOR-draft.md` with hook-style and length conventions
- CLAUDE.md / GEMINI.md symlinks
- permissions.yaml from `content-worker.yaml` template
- runtime.yaml with claude→gemini chain
- 3 skill files

### Out of scope
- Auto-posting to LinkedIn (no LinkedIn API integration in v1; output is a markdown file the operator copies manually).
- Image generation for carousels (operator handles separately).

### Success Criteria
- Folder validates.
- Hook style enforced: `draft-post.md` includes a "first-line is a hook" structural assertion.
- `draft-carousel.md` produces a slide-numbered outline (`Slide 1: ...`).
- `respond-to-comment.md` accepts a comment + optional context, returns a single short reply.

### Quality Gate
`gate_w16_4_linkedin_writer`:
- [ ] Folder validates
- [ ] Length-bound for single posts (150–300 words) is enforced
- [ ] Carousel skill produces slide-numbered output
- [ ] Permissions deny Bash

### Test Plan
- Length test (single post): dispatch `draft-post` with topic → assert word count ∈ [150, 300]; one auto-retry on overshoot.
- Carousel structure test: dispatch `draft-carousel` for 8 slides → assert output contains exactly 8 `Slide N:` headers.
- Hook test: assert first line of any `draft-post` output is < 80 chars and ends with `?`, `!`, `.`, or `:`. (Heuristic; operator-graded for finer judgement.)
- Operator-graded tone test: same as w16-3 — operator reviews 3 sample posts before signing off.
- Permissions test: same negative as w16-3.

### Operator-input placeholders
- `<OPERATOR_INPUT_NEEDED: LINKEDIN_HANDLE>` — for self-reference/voice anchoring.
- `<OPERATOR_INPUT_NEEDED: LINKEDIN_REFERENCE_POSTS>` — 5–10 existing posts (URLs or text).
- `<OPERATOR_INPUT_NEEDED: LINKEDIN_HOOK_STYLE>` — question-led? bold-claim? story-led?
- `<OPERATOR_INPUT_NEEDED: HASHTAG_POLICY>` — 0 / 1–3 / 3–5 hashtags? Domain-specific tags?

---

## w16-5: seo-analyst worker + brave-search MCP wiring

**Track**: A
**Priority**: P1 (blocks w16-8 smoke test)
**Complexity**: Low–Medium
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Estimated LOC**: 130
**Branch**: `feat/w16-5-seo-analyst`
**Dependencies**: [w16-1]

### Description
Stand up `seo-analyst` worker under `.claude/agents/workers/seo-analyst/`. This worker performs keyword research, competitor content audits, and on-page SEO assessment. It's tools-grant-rich: it gets explicit MCP grants for `brave-search` (web search) and `context7` (documentation lookup; for technical-content auditing). It does NOT write content — its outputs are research reports the blog-writer or linkedin-writer consume.

Provider chain: `claude → gemini`. Exclude codex.

### Scope
- BEHAVIOR.md per `agent_drafts/seo-analyst-BEHAVIOR-draft.md` — research-oriented persona; no-prose-drafting role boundary explicitly stated
- CLAUDE.md / GEMINI.md symlinks
- tools.yaml granting `brave-search` and `context7` MCP servers
- permissions.yaml from `content-worker.yaml` template + the MCP grants from tools.yaml
- runtime.yaml with claude→gemini chain
- 3 skill files: `skills/keyword-research.md`, `skills/competitor-content-audit.md`, `skills/on-page-audit.md`

### Out of scope
- Drafting content (out of role — escalates to marketing-lead).
- GA4 data (that's w16-6).
- Paid-search analysis.

### Success Criteria
- MCP tool grants resolve at dispatch time; the worker can invoke `mcp__brave-search__brave_web_search` and receive results.
- Out-of-role test: when asked to "write me a blog", the worker refuses and recommends escalation to marketing-lead.
- Each of 3 skills produces a structured markdown report with named sections.

### Quality Gate
`gate_w16_5_seo_analyst`:
- [ ] MCP brave-search grant resolves; tool is callable
- [ ] Out-of-role refusal: worker rejects "draft a blog" with structured escalation
- [ ] Each skill produces a documented, sectioned output
- [ ] Permissions deny Bash

### Test Plan
- MCP grant test: dispatch a `keyword-research` skill call → worker invokes `mcp__brave-search__brave_web_search` → result format matches expected schema (keyword + estimated_volume placeholder + competitor URLs list).
- Out-of-scope test: dispatch "write me a 1000-word blog about X" → worker returns `out_of_role` with `escalate_to: marketing-lead` (this is a structured escalation, not a free-text refusal).
- Sectioned-output test: each skill output must contain the documented section headers (e.g. `keyword-research.md` requires `# Keywords`, `# Search Intent`, `# Competitors`, `# Recommendations`).
- Negative MCP test: revoke brave-search grant in tools.yaml → re-dispatch → worker reports `tool_unavailable: brave-search` cleanly (does not crash).

### Operator-input placeholders
- `<OPERATOR_INPUT_NEEDED: SEO_TARGET_KEYWORDS>` — initial seed list of keyword themes the operator cares about.
- `<OPERATOR_INPUT_NEEDED: SEO_COMPETITORS>` — 3–10 competitor URLs the analyst should benchmark against by default.
- `<OPERATOR_INPUT_NEEDED: SEO_MARKET_GEO>` — primary market (NL, US, EU) for region-aware search.

---

## w16-6: ga4-analyst worker + custom GA4 MCP server

**Track**: A
**Priority**: P2 (not on critical path of w16-8 first smoke test, but required for Phase 16 acceptance)
**Complexity**: High (custom Python MCP server + credential handling)
**Risk**: Medium (security-sensitive)
**Skill**: @api-developer + @security-engineer review pass
**Requires-Model**: opus (deviation from default; see Model assignment table)
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review, codex_gate, claude_github_optional
**Estimated Time**: 2–3 days
**Estimated LOC**: 450 (300 MCP server + 150 worker folder)
**Branch**: `feat/w16-6-ga4-analyst`
**Dependencies**: [w16-1]

### Description
Two artifacts ship in this wave:
1. A custom GA4 MCP server (`scripts/mcp_servers/ga4/`) wrapping the GA4 Data API. ~300 LOC. Endpoints: `runReport`, `runRealtimeReport`, `batchRunReports`, plus convenience helpers for funnel-analysis and content-performance queries. Credentials read from a service-account JSON file path in env (`VNX_GA4_SERVICE_ACCOUNT_JSON_PATH`); never logged; never sent to LLM.
2. The `ga4-analyst` worker folder under `.claude/agents/workers/ga4-analyst/` with BEHAVIOR.md, tools.yaml (grant: `ga4-data` MCP), 4 skills: `funnel-analysis`, `traffic-source-mix`, `content-performance`, `weekly-snapshot`.

The GA4 MCP server is a standalone artifact and is a candidate for community giveaway (BL-2026-05-008). See the open question section below.

### Scope
**MCP server (`scripts/mcp_servers/ga4/`):**
- `server.py` — MCP server entrypoint
- `client.py` — GA4 Data API client wrapper
- `auth.py` — service account JSON loading; redaction guard
- `tools/` — one file per exposed tool (`run_report.py`, `funnel.py`, `content_performance.py`, etc.)
- `pyproject.toml` (if standalone-ready) or integrated into VNX's existing build
- `tests/` — unit tests (mocked GA4 API), credential-handling tests
- README.md with operator setup instructions

**Worker folder (`.claude/agents/workers/ga4-analyst/`):**
- BEHAVIOR.md per `agent_drafts/ga4-analyst-BEHAVIOR-draft.md`
- CLAUDE.md / GEMINI.md symlinks
- tools.yaml granting the `ga4-data` MCP server
- permissions.yaml from `content-worker.yaml` template + ga4-data grant
- runtime.yaml with claude→gemini chain
- 4 skill files

### Out of scope
- Real-time push notifications (operator polls or schedules `weekly-snapshot`).
- BigQuery export (different API; future wave if needed).
- A/B test attribution (out of GA4-Data API surface).
- Auto-publishing reports anywhere; outputs are markdown files for the operator.

### Success Criteria
- MCP server starts cleanly with valid `VNX_GA4_SERVICE_ACCOUNT_JSON_PATH`.
- MCP server refuses to start (with a clear error) if the env var is missing OR the file is unreadable OR the JSON is malformed.
- Credential redaction: no path to logging or returning the service-account contents through any tool response or error message.
- All 4 worker skills produce structured markdown reports.
- A real-data smoke test (operator-driven, NOT in CI) against the operator's actual GA4 property succeeds for `weekly-snapshot`.

### Quality Gate
`gate_w16_6_ga4_analyst`:
- [ ] MCP server unit tests cover each endpoint wrapper
- [ ] Credential handling: redaction verified; missing-file path raises cleanly
- [ ] Worker folder validates; tools.yaml grants resolve
- [ ] Codex review passes (security-sensitive; see Review-Stack)
- [ ] Operator-driven real-data smoke test for `weekly-snapshot` succeeds (gated, not in CI)

### Test Plan
- **MCP server unit tests** (mocked GA4 API):
  - `runReport` with a minimal date range + one metric returns a structured response shape.
  - `funnel-analysis` with 3 step definitions returns step-completion counts.
  - `content-performance` with a date range returns top-N pages by sessions.
  - Each test mocks the underlying `google.analytics.data_v1beta` client.
- **Credential handling tests**:
  - Missing `VNX_GA4_SERVICE_ACCOUNT_JSON_PATH` → server start fails with `ga4_auth_missing_path`.
  - Path exists but is not valid JSON → server start fails with `ga4_auth_bad_json`.
  - Path exists but lacks the required service-account fields → server start fails with `ga4_auth_invalid_service_account`.
  - Redaction test: provoke an exception inside the GA4 client; assert the exception's serialized form does NOT contain the service-account `private_key`, `client_email`, or `private_key_id`. The redactor must normalize these fields out.
  - LLM-leak test: simulate a tool response that internally references the credential dict → assert the MCP response payload does not contain credential fields.
- **Real-data smoke test (gated, operator-driven):**
  - Marker: `pytest.mark.requires_real_ga4` (skipped in CI).
  - Operator runs locally with their actual property → `weekly-snapshot` completes; counts are non-zero (assuming non-zero traffic).
- **Worker folder tests:**
  - tools.yaml resolves the `ga4-data` MCP grant.
  - Permissions still deny Bash even though MCP is granted.
  - Out-of-role test: ask ga4-analyst to "write a blog about traffic" → escalate to marketing-lead.

### Operator-input placeholders
- `<OPERATOR_INPUT_NEEDED: GA4_PROPERTY_ID>` — the property the analyst defaults to.
- `<OPERATOR_INPUT_NEEDED: GA4_SERVICE_ACCOUNT_JSON_PATH>` — env-var only; the file itself must NOT be committed.
- `<OPERATOR_INPUT_NEEDED: GA4_DEFAULT_DIMENSIONS>` — e.g. `[date, sessionDefaultChannelGroup, pagePath]`.
- `<OPERATOR_INPUT_NEEDED: GA4_DEFAULT_METRICS>` — e.g. `[sessions, totalUsers, conversions]`.
- `<OPERATOR_INPUT_NEEDED: GA4_CONVERSION_EVENT_NAMES>` — list of event names that count as conversions in this property.
- `<OPERATOR_INPUT_NEEDED: GA4_INTERNAL_TRAFFIC_FILTER>` — IP/domain filter to exclude operator/internal traffic from analysis (or "none").

---

## w16-7: per-domain memory partitions for marketing/sales

**Track**: A
**Priority**: P1 (blocks w16-8)
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Estimated LOC**: 100
**Branch**: `feat/w16-7-business-memory-partitions`
**Dependencies**: [Phase 12 W-mem-3, w16-2]

### Description
Bootstrap two new memory partitions in `quality_intelligence.db`:
- `vec_artifacts_marketing` (already proposed in PROJECT_STATE_DESIGN.md §Layer 2 schema, but Phase 12 W-mem-3 only created `vec_artifacts_code`).
- `vec_artifacts_sales` (forward-looking; sales-lead orchestrator is Phase 16+1 follow-up per BL-2026-05-013).

The hard-partition invariant is critical: marketing-lead's memory queries CANNOT retrieve code-domain artifacts, even with high semantic similarity. The shared `vec_operator_prefs` table remains the single cross-domain bridge.

### Scope
- Schema migration adding `vec_artifacts_marketing` and `vec_artifacts_sales` virtual tables (`vec0(embedding float[768])`).
- Migration script idempotent + reversible.
- Domain-routing helper: `scripts/lib/memory_domain.py` exposes `route_query(domain) -> table_name` and `assert_partition_isolation(domain_a, domain_b)` for tests.
- Update marketing-lead's permissions.yaml to grant memory query access to `marketing` and `sales` domains.
- Documentation patch in PROJECT_STATE_DESIGN.md §Layer 2 noting marketing + sales partitions are now bootstrapped.

### Out of scope
- Populating the partitions with content (that happens organically as workers in w16-3..w16-6 produce artifacts; smoke test in w16-8 will be the first write).
- Cross-project federation (Phase 6 territory).
- New embedding models (still nomic-embed-text per ADR-001).

### Success Criteria
- Migration runs cleanly forward and back.
- Both new tables are queryable.
- Cross-domain isolation verified by automated test.
- `vec_operator_prefs` queries continue to work cross-domain.

### Quality Gate
`gate_w16_7_memory_partitions`:
- [ ] Migration idempotent (running twice is a no-op)
- [ ] Migration reversible (down-migration drops the new tables cleanly)
- [ ] Cross-domain isolation test passes
- [ ] Operator-pref shared test passes

### Test Plan
- Schema test: run migration → assert `vec_artifacts_marketing` and `vec_artifacts_sales` exist; re-run → still exists, no error.
- Cross-domain isolation test: insert artifact `A` with `domain=marketing` and content `"orchestration framework architecture"`; query with `domain=code` and the same content → assert top-k result does NOT include `A`. Then query with `domain=marketing` and confirm `A` is in the top-k.
- Operator-pref shared test: insert preference `P` in `vec_operator_prefs` with content `"prefer concise tone"`; query from a code-context with `include_operator_prefs=True` → assert `P` is retrievable. Same query from marketing-context → assert `P` is retrievable. Same query from marketing-context with `include_operator_prefs=False` → assert `P` is NOT in results.
- Permissions test: marketing-lead's memory query for `domain=code` → rejected by permissions.yaml with `domain_out_of_scope`.
- Down-migration test: revert → `vec_artifacts_marketing` and `vec_artifacts_sales` are dropped; existing `vec_artifacts_code` remains.

### Operator-input placeholders
None — internal infrastructure.

---

## w16-8: end-to-end smoke test mission

**Track**: B (this is testing/integration work, hence Track B)
**Priority**: P0 (closes Phase 16 acceptance)
**Complexity**: Medium (integration surface is wide)
**Risk**: Low (config + harness only; no new product code)
**Skill**: @test-engineer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review, **codex_gate** (FEATURE-END gate fires here)
**Estimated Time**: 0.5–1 day (heavy on operator-side debug if earlier waves had unfilled placeholders)
**Estimated LOC**: 50
**Branch**: `feat/w16-8-business-smoke-test`
**Dependencies**: [w16-2, w16-3, w16-5, w16-7]

### Description
End-to-end smoke test mission that validates the entire Phase 16 stack works together. The mission: operator dispatches "write a 1500-word blog about VNX's multi-orchestrator architecture" → main → marketing-lead → seo-analyst (research) → blog-writer (draft) → marketing-lead (review/approve) → commit to repo as `claudedocs/blog-drafts/<slug>.md`. The mission file lives at `.vnx-data/missions/<id>.json`.

This is also where the FEATURE-END `codex_gate` fires for Phase 16 — codex audits the integrated end-to-end behavior across the surface added by all 8 waves.

### Scope
- Integration test harness `tests/phase_16/test_smoke_mission.py` (or equivalent path per VNX test conventions)
- Mission template `.vnx-data/missions/templates/blog_smoke.json`
- Test asserts: 1 commit lands; receipt chain has ≥4 entries (main → marketing-lead dispatch, seo-analyst receipt, blog-writer receipt, marketing-lead review receipt); mission status: `done`; output file exists in `claudedocs/blog-drafts/` and is in word-count range.
- Brief operator runbook in `claudedocs/phase-16-smoke-runbook.md`

### Out of scope
- Iterating on tone/quality (that's an outcome of w16-3/w16-4's operator-graded sign-off, not this wave).
- Cross-domain isolation (verified in w16-7 unit tests, not re-run here).
- Performance benchmarking (out of scope for a smoke test).

### Success Criteria
- Mission completes end-to-end with status `done` in <10 minutes wall clock for a single 1500-word draft.
- Receipt chain has ≥4 entries with structurally correct dispatch IDs and clean status fields.
- The commit to `claudedocs/blog-drafts/` lands and is on the expected branch.
- Cross-domain memory query at the end of the mission returns the just-written artifact.
- Codex feature-end gate passes.

### Quality Gate
`gate_w16_8_smoke_mission`:
- [ ] Full mission completes with status `done`
- [ ] Receipt chain ≥4 entries; no `unknown-` dispatch IDs
- [ ] Output commit lands at expected path
- [ ] Memory query retrieves the new artifact under `domain=marketing`
- [ ] Codex feature-end gate passes for Phase 16

### Test Plan
- **Full mission test**: as described above. Operator-driven the first time (validates real claude/gemini behavior); then automatable with mocked provider responses for CI regression.
- **Receipt-chain integrity test**: parse the receipt NDJSON; assert each entry's `dispatch_id` is non-empty; assert `parent_dispatch_id` chain reconstructs to the original main dispatch.
- **Memory write test**: after mission, query `vec_artifacts_marketing` for the blog topic → top-1 result has the new artifact's path.
- **Cross-domain isolation regression**: same blog topic queried under `domain=code` → top-k does NOT contain the new marketing artifact.
- **Failure-path test**: kill claude provider mid-mission → assert failover to gemini; assert mission still completes.
- **Codex gate dispatch**: after mission completes, T0 dispatches the codex feature-end gate per Review-Stack policy; gate result is recorded under `.vnx-data/state/review_gates/results/`.

### Operator-input placeholders
None at this stage — but if w16-3's `<OPERATOR_INPUT_NEEDED: BLOG_TONE_REFERENCES>` etc. have not been filled, this smoke test will produce stylistically-default output and the operator-graded portion of the test will fail. T0 must verify all earlier placeholders are filled before kicking off w16-8.

---

## Phase 16 acceptance (rolled up from waves)

The phase is complete when ALL of the following hold:
1. All 8 waves merged on `main`.
2. `business-light` governance variant is in production use by at least one orchestrator (marketing-lead).
3. End-to-end smoke test (w16-8) passes operator-graded review on first or second iteration.
4. `vnx memory search "blog VNX architecture" --domain marketing` returns the smoke-test-produced artifact.
5. Cross-domain isolation invariant verified: marketing-lead cannot dispatch to a code-domain worker; `vec_artifacts_marketing` queries cannot retrieve code-domain artifacts.
6. Codex feature-end gate (w16-8) passes.
7. All `<OPERATOR_INPUT_NEEDED>` placeholders are filled OR explicitly deferred-to-followup with an open-item.
8. The agent_drafts/ folder is retained as the source-of-truth for any future operator updates to BEHAVIOR.md content (this is a deliberate convention — these are not throwaway drafts).

## Open question — should the GA4 MCP server be a standalone giveaway repo?

This is a design-time open decision. Recommendation: **YES, eventually — but ship inside VNX first.**

Rationale:
- **Pro standalone**: Many people want a clean, audited GA4 MCP server. Demand is real (BL-2026-05-008 lists it as `domain_secondary: giveaway`). Carving it out matches the F43 / context-rotation playbook (W6A–W6E in roadmap.yaml). Community visibility is good for VNX's positioning ("the multi-domain orchestration framework that ships its own MCPs").
- **Con standalone (right now)**: The first version needs operator real-data hardening (w16-6 has an operator-driven smoke test for that reason). Premature carve-out exports half-baked credential handling and a not-yet-stable API surface. Worse, it forces an internal+external dual-maintenance burden before VNX's own usage has shaken out the API contract.
- **Recommended path**: ship in-tree at `scripts/mcp_servers/ga4/` in w16-6 with a clean `pyproject.toml` already in place + an MIT license header. After Phase 16 acceptance + 4–6 weeks of operator usage proves the API stable + credential handling is bulletproof, carve out as `Vinix24/ga4-mcp-server` (or similar repo name) following the F43 playbook (W6A–W6E pattern). At that point it becomes a Phase-16-follow-up wave, NOT part of Phase 16 itself.
- **Decision log entry to write when this is finalized**: add to `.vnx-data/strategy/decisions.ndjson` once carve-out timing is confirmed. Reference BL-2026-05-008 promotion + the w16-6 retrospective.

---

*End of Phase 16 FEATURE_PLAN. ~1300 LOC across 8 PRs over ~7–10 effective working days, dependent on operator availability for placeholder fills + tone sign-offs.*
