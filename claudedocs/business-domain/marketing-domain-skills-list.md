# Marketing-domain skills catalog

This document catalogs all the `skills/<task>.md` templates that ship across w16-2..w16-6, with a one-paragraph description, expected inputs/outputs, the wave that delivers it, and any operator-input dependency.

Each skill file follows the FR-4 skill format (separate from BEHAVIOR.md): it's an invocable prompt that a folder-loaded agent can execute. The agent's BEHAVIOR.md provides persona + scope; the skill provides the specific procedure for one task.

## Format reminder (per FR-4)

A `skills/<task>.md` file looks roughly like:

```markdown
---
skill_id: <task-name>
agent: <agent-name>
description: <one-line>
inputs:
  <input_name>: <type, required/optional, description>
outputs:
  <output_name>: <type, description>
provider_compatibility: [claude, gemini]
---

# <Task name>

## Procedure
1. ...
2. ...

## Quality checks
- ...
```

---

## Marketing-lead orchestrator skills (w16-2)

### `plan-content-calendar.md`
- **Owner**: marketing-lead
- **Purpose**: produce a 1-quarter content calendar from operator inputs + memory.
- **Inputs**:
  - `quarter`: e.g. `"2026-Q3"`
  - `themes`: list of themes (defaults from operator)
  - `cadence`: posts-per-week (default 1 blog + 2 LinkedIn)
- **Outputs**:
  - `calendar_markdown`: a week-by-week table
  - `decisions_required`: list of operator-decision items raised
- **Memory**: queries `vec_artifacts_marketing` for past performance signals on similar themes; queries `vec_operator_prefs` for content-cadence preferences.
- **Operator-input dependency**: `<OPERATOR_INPUT_NEEDED: CONTENT_CALENDAR_REF>` from marketing-lead BEHAVIOR.

### `kies-onderwerp.md` (Dutch — "pick a topic")
- **Owner**: marketing-lead
- **Purpose**: pick the next topic to draft, given the calendar (or ad-hoc) + recent performance + operator constraints.
- **Inputs**:
  - `format`: blog | linkedin_post | linkedin_carousel
  - `urgency`: now | this_week | next_week
  - `must_avoid`: optional list of recent topics (auto-pulled from memory if absent)
- **Outputs**:
  - `chosen_topic`: title + brief
  - `rationale`: why this topic now
  - `alternative_topics`: 2 fallbacks
- **Memory**: `vec_artifacts_marketing` for "what have we covered recently" + GA4 hooks via `analyseer-performance` to bias toward proven-resonant themes.

### `analyseer-performance.md` (Dutch — "analyze performance")
- **Owner**: marketing-lead
- **Purpose**: orchestrate a performance retrospective.
- **Inputs**:
  - `period`: e.g. `"last_28_days"` or absolute range
  - `output_format`: report-only | report-plus-blog | report-plus-linkedin
- **Outputs**:
  - `retrospective_artifact`: markdown summary
  - `optional_chained_artifact`: blog or LinkedIn post if requested
  - `dispatch_chain`: receipts of ga4-analyst → optional blog-writer/linkedin-writer
- **Dispatch chain**: `ga4-analyst (weekly-snapshot or custom)` → optionally `blog-writer (draft-post)` or `linkedin-writer (draft-post)` → `marketing-lead self-review` → commit.

---

## Blog-writer worker skills (w16-3)

### `draft-post.md`
- **Owner**: blog-writer
- **Purpose**: draft a blog post on a given topic to a target length, following operator voice + structure.
- **Inputs**:
  - `topic`: required
  - `target_word_count`: int (default 1200)
  - `tolerance`: int (default 150)
  - `research_input`: optional markdown from seo-analyst
  - `structural_overrides`: optional per-mission deviations
- **Outputs**:
  - `artifact_markdown`: the post
  - `artifact_word_count`: int
  - `structural_self_check`: pass/fail summary
- **Operator-input dependency**: `BLOG_TONE_REFERENCES`, `BLOG_LENGTH_RANGE`, `BLOG_STRUCTURE_CONVENTIONS`, `BLOG_BANNED_PHRASES`.

### `edit-for-tone.md`
- **Owner**: blog-writer
- **Purpose**: take an existing draft (operator's first attempt, or marketing-lead's review feedback) and rework it for tone without changing structure or substance.
- **Inputs**:
  - `existing_draft_markdown`: required
  - `tone_feedback`: optional list of specific feedback points
- **Outputs**:
  - `edited_markdown`: the revised post
  - `change_summary`: what was changed and why (for review by marketing-lead)
- **Constraint**: word count must stay within ±10% of original; structural sections preserved.

### `add-seo-meta.md`
- **Owner**: blog-writer
- **Purpose**: given a draft + an seo-analyst keyword-research sidecar, generate frontmatter (title, slug, meta_description) and inline SEO touches (internal link suggestions, alt-text placeholders).
- **Inputs**:
  - `draft_markdown`: required
  - `keyword_research_sidecar`: structured JSON from seo-analyst
- **Outputs**:
  - `frontmatter_yaml`: title, slug, meta_description, canonical (if available), tags
  - `internal_link_suggestions`: list of {anchor_text, target_path}
  - `alt_text_placeholders`: list of {image_position, suggested_alt}

---

## LinkedIn-writer worker skills (w16-4)

### `draft-post.md`
- **Owner**: linkedin-writer
- **Purpose**: draft a single LinkedIn post (150–300 words) following hook + structure conventions.
- **Inputs**:
  - `topic`: required
  - `hook_style_override`: optional
  - `target_word_count`: int (default 220)
- **Outputs**:
  - `artifact_markdown`
  - `hook_line`: the chosen first-line hook (extracted for review)
  - `structural_self_check`
- **Operator-input dependency**: `LINKEDIN_HANDLE`, `LINKEDIN_REFERENCE_POSTS`, `LINKEDIN_HOOK_STYLE`, `HASHTAG_POLICY`.

### `draft-carousel.md`
- **Owner**: linkedin-writer
- **Purpose**: produce a slide-numbered carousel outline (6–10 slides).
- **Inputs**:
  - `topic`: required
  - `slide_count`: int (default 8)
  - `narrative_arc`: optional (problem-solution | listicle | story | comparison)
- **Outputs**:
  - `slides_markdown`: numbered slide sections
  - `slides_structured`: list of {slide_n, title, body, suggested_visual}

### `respond-to-comment.md`
- **Owner**: linkedin-writer
- **Purpose**: draft a ≤60-word reply to a specific comment.
- **Inputs**:
  - `parent_post_text`: optional context
  - `comment_text`: required
  - `tone_hint`: optional (warm | direct | curious | grateful)
- **Outputs**:
  - `reply_markdown`
  - `reply_word_count`

---

## SEO-analyst worker skills (w16-5)

### `keyword-research.md`
- **Owner**: seo-analyst
- **Purpose**: research keywords for a topic; produce a structured cluster + meta recommendations.
- **Inputs**:
  - `topic`: required
  - `market_geo`: default from BEHAVIOR
- **Outputs**:
  - `report_markdown`: sectioned report
  - `structured_data`: JSON sidecar for blog-writer's `add-seo-meta` consumption
- **Tool dependency**: `mcp__brave-search__brave_web_search` (+ `brave_local_search` for geo).

### `competitor-content-audit.md`
- **Owner**: seo-analyst
- **Purpose**: audit N competitor sites; identify patterns + content gaps + opportunities.
- **Inputs**:
  - `competitor_urls`: list (defaults from BEHAVIOR)
  - `depth`: `"shallow"` (homepage + blog index) | `"medium"` (top 10 posts each)
- **Outputs**:
  - `report_markdown`: per-competitor + cross-competitor sections
  - `opportunities`: list of {gap_topic, competitor_count_missing, priority}
- **Tool dependency**: WebFetch + brave-search.

### `on-page-audit.md`
- **Owner**: seo-analyst
- **Purpose**: audit a single URL's on-page SEO health.
- **Inputs**:
  - `target_url`: required
- **Outputs**:
  - `report_markdown`
  - `issues_prioritized`: list of {priority, issue, fix_suggestion}
- **Tool dependency**: WebFetch.

---

## GA4-analyst worker skills (w16-6)

### `funnel-analysis.md`
- **Owner**: ga4-analyst
- **Purpose**: compute a funnel (step user counts + drop-off + bottleneck).
- **Inputs**:
  - `funnel_steps`: list of step definitions
  - `date_range`: required
- **Outputs**:
  - `report_markdown`
  - `structured_data`: per-step counts + percentages
- **Tool dependency**: `ga4_funnel` MCP tool (composed of `ga4_run_report` calls).

### `traffic-source-mix.md`
- **Owner**: ga4-analyst
- **Purpose**: traffic-source breakdown + WoW deltas.
- **Inputs**:
  - `date_range`: required
  - `dimension`: default `sessionDefaultChannelGroup`
- **Outputs**:
  - `report_markdown` (table + chart placeholder)
  - `structured_data`
- **Tool dependency**: `ga4_traffic_source_mix`.

### `content-performance.md`
- **Owner**: ga4-analyst
- **Purpose**: top pages by sessions / conversions / engagement; flag underperformers.
- **Inputs**:
  - `date_range`
  - `top_n`: default 20
- **Outputs**:
  - `report_markdown`
  - `structured_data`: per-page metrics
- **Tool dependency**: `ga4_content_performance`.

### `weekly-snapshot.md`
- **Owner**: ga4-analyst
- **Purpose**: combined one-pager for the past week. Smoke-test target.
- **Inputs**:
  - `week_ending`: optional (default = most recent complete week)
- **Outputs**:
  - `report_markdown`: traffic-source + content-performance + funnel (if configured) for the week
- **Tool dependency**: `ga4_weekly_snapshot` (composed).

---

## Skill count summary

| Wave | Skill count | Skills |
|------|-------------|--------|
| w16-2 | 3 | plan-content-calendar, kies-onderwerp, analyseer-performance |
| w16-3 | 3 | draft-post, edit-for-tone, add-seo-meta |
| w16-4 | 3 | draft-post, draft-carousel, respond-to-comment |
| w16-5 | 3 | keyword-research, competitor-content-audit, on-page-audit |
| w16-6 | 4 | funnel-analysis, traffic-source-mix, content-performance, weekly-snapshot |
| **Total** | **16 skills** | |

## Skill-level operator-input dependencies (cross-reference)

| Skill | Operator inputs needed |
|-------|------------------------|
| `plan-content-calendar` | `CONTENT_CALENDAR_REF`, `COMPANY_VOICE` |
| `kies-onderwerp` | `CONTENT_CALENDAR_REF`, recent-content awareness |
| `analyseer-performance` | none direct (chains to ga4-analyst which has its own) |
| `draft-post` (blog) | `BLOG_TONE_REFERENCES`, `BLOG_LENGTH_RANGE`, `BLOG_STRUCTURE_CONVENTIONS`, `BLOG_BANNED_PHRASES` |
| `edit-for-tone` | same as draft-post (blog) |
| `add-seo-meta` | none (consumes seo-analyst output) |
| `draft-post` (linkedin) | `LINKEDIN_HANDLE`, `LINKEDIN_REFERENCE_POSTS`, `LINKEDIN_HOOK_STYLE`, `HASHTAG_POLICY` |
| `draft-carousel` | same as draft-post (linkedin) |
| `respond-to-comment` | `LINKEDIN_HANDLE`, voice references |
| `keyword-research` | `SEO_TARGET_KEYWORDS`, `SEO_MARKET_GEO` |
| `competitor-content-audit` | `SEO_COMPETITORS` |
| `on-page-audit` | none direct (target_url is per-call) |
| `funnel-analysis` | `GA4_PROPERTY_ID`, `GA4_SERVICE_ACCOUNT_JSON_PATH`, conversion events |
| `traffic-source-mix` | `GA4_PROPERTY_ID`, `GA4_SERVICE_ACCOUNT_JSON_PATH` |
| `content-performance` | `GA4_PROPERTY_ID`, `GA4_SERVICE_ACCOUNT_JSON_PATH`, `GA4_CONVERSION_EVENT_NAMES` |
| `weekly-snapshot` | all of GA4 placeholders |

T0 must verify all relevant operator inputs are filled before kicking off a wave whose skills depend on them.
