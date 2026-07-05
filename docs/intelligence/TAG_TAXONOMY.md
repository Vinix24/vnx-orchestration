# VNX Tag Taxonomy

**Last Updated**: 2026-07-05 (refreshed for the docs-intelligence sweep, PRs #1001–#1017)

**There are two separate, still-live tag systems in this codebase.** They serve different
consumers and do not share a vocabulary. This doc covers both, in order of which is now the SSOT
for the governed dispatch pipeline.

---

## 1. VNX closed vocabulary — SSOT for selector matching + the LLM tagger

**Module**: `scripts/lib/vnx_tag_vocabulary.py` (95 lines).
**Consumers**: `intelligence_selector.py`'s rank-then-budget scoring (opt-in
`VNX_INTEL_RANK_THEN_BUDGET`), `vnx_tagger.py`'s LLM tagger (opt-in `VNX_TAGGER_ENABLED`), and
`scripts/lib/scout_prepass.py` indirectly via the tag-overlap score. Full pipeline detail:
[`SELF_LEARNING_LOOP.md`](SELF_LEARNING_LOOP.md) and [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md).

This is a **closed, faceted taxonomy** purpose-built for the VNX Orchestration codebase (not
SEOcrawler) — three flat, independent facets used to match injected intelligence to a dispatch by
intent + subsystem, not just file-path overlap.

### Facets

| Facet | Purpose | Tag count | Code |
|-------|---------|-----------|------|
| **Domain** | The subsystem the work touches | 12 | `vnx_tag_vocabulary.py:23-36` |
| **Intent** | What the work is | 10 | `vnx_tag_vocabulary.py:39-50` |
| **Component** | Cross-cutting concern | 8 | `vnx_tag_vocabulary.py:53-62` |

**Domain tags**: `dispatch`, `intelligence`, `receipts_audit`, `governance_gates`,
`providers_routing`, `schema_migrations`, `tenant_project_id`, `tests_harness`, `docs`,
`dashboard_ui`, `benchmark`, `learning_loop`.

**Intent tags**: `fix_bug`, `implement_feature`, `refactor`, `add_test`, `harden`, `migrate_schema`,
`wire_integration`, `review_audit`, `document`, `investigate_rootcause`.

**Component tags**: `fail_closed`, `idempotency`, `concurrency_lease`, `project_id_stamping`,
`cli_subprocess`, `ndjson_contract`, `fts5`, `provider_constraint`.

Each facet maps tags to keyword lists (`_DOMAIN_KEYWORDS`, `_INTENT_KEYWORDS`,
`_COMPONENT_KEYWORDS`, `vnx_tag_vocabulary.py:23-62`). `VNX_TAG_VOCABULARY` is the union of all
three (`vnx_tag_vocabulary.py:66-69`) — the single closed set every tag, deterministic or LLM, must
belong to.

### Deterministic floor — `derive_tags()`

```python
# vnx_tag_vocabulary.py:72-86
def derive_tags(text, paths=None) -> List[str]:
    """Keyword/path scan only — no LLM. Deduplicated, facet-ordered."""
```

A pure keyword/path substring scan, no LLM call, always available. This is the floor every dispatch
gets regardless of whether the LLM tagger is enabled — the selector's `rank_then_budget` tag-overlap
score (`intelligence_selector.py:131-146`) calls this on-the-fly against an item's title+content
even when no tags were ever persisted to the DB.

### LLM enrichment layer — `vnx_tagger.py`

Opt-in (`VNX_TAGGER_ENABLED=1`, default off), model-agnostic (`VNX_TAGGER_PROVIDER`, default
`deepseek`). `enrich_tags()` (`vnx_tagger.py:145-154`) always starts from `derive_tags()` and only
*adds* LLM-suggested tags on top — the LLM never replaces the deterministic floor.

**Snap-to-vocab validation** — the load-bearing safety property:

```python
# vnx_tag_vocabulary.py:89-95
def validate_tags(tags) -> List[str]:
    """Keep only tags that are in the closed vocabulary."""
    return [t for t in (tags or []) if t in VNX_TAG_VOCABULARY]
```

The LLM prompt (`_build_prompt`, `vnx_tagger.py:78-95`) lists the exact closed vocabulary and
instructs "choosing ONLY from the closed vocabulary. Do not invent tags." The response is then
passed through `validate_tags()` (`vnx_tagger.py:138`) regardless — an off-vocabulary hallucination
from the model can never enter the matching layer. Max 6 tags per pattern (`_MAX_TAGS`,
`vnx_tagger.py:46`).

`enrich_pattern_tags()` (`vnx_tagger.py:157-240`) is the persist-time entry point: it tags
untagged rows in `success_patterns`/`antipatterns` (JSON `tags` column) and writes one audit row per
successful tagging to a `tagging_events` table (`vnx_tagger.py:52-65`) — see
[`SELF_LEARNING_LOOP.md`#tagging-events-audit-trail](SELF_LEARNING_LOOP.md#tagging-events-audit-trail).
Called from `intelligence_daemon.GovernanceDigestRunner.run_once()` after signal persistence
(`scripts/intelligence_daemon.py:236-247`).

### Decision criterion + current state

Enable `VNX_TAGGER_ENABLED=1` per-project only after `vnx learning tagger-ab` shows
`rescue_rate >= 20%` and `cost_per_pattern_usd <= $0.001`
(`vnx_cli/commands/learning.py:382-386`). For `vnx-dev` this ran 100% rescue at ~$0.0005/pattern and
was flipped on 2026-07-05 — see [`SELF_LEARNING_LOOP.md`](SELF_LEARNING_LOOP.md#current-per-project-state--vnx-dev)
for the live flag state.

---

## 2. Legacy taxonomy — `tag_intelligence.py` (SEOcrawler-flavored, still live)

**Module**: `scripts/tag_intelligence.py`.
**Consumers**: `scripts/gather_intelligence.py`, `scripts/lib/intelligence_hygiene.py`,
`scripts/intelligence_daemon.py` (via `TagIntelligenceEngine`) — the interactive/skill-invoked
intelligence-gathering path (`.claude/skills/*/scripts/intelligence.sh`,
`scripts/userpromptsubmit_worker_intelligence_inject.sh`), separate from the governed-dispatch
`IntelligenceSelector` pipeline above.

This is an **older, unrelated vocabulary** originally written for SEOcrawler-style work
(`crawler-component`, `storage-component`, `api-component`) and is **not** the vocabulary the
selector's rank-then-budget scoring or the LLM tagger validate against. It is still live code, not
dead — do not delete it — but it is a parallel system, not a predecessor that `vnx_tag_vocabulary.py`
replaced.

### Facets (five, not three)

| Category | Purpose |
|----------|---------|
| Phase | `design-phase`, `implementation-phase`, `testing-phase`, `production-phase` |
| Component | `crawler-component`, `storage-component`, `api-component` |
| Issue | `validation-error`, `performance-issue`, `memory-problem`, `race-condition` |
| Severity | `critical-blocker`, `high-priority`, `medium-impact` |
| Action | `needs-refactor`, `needs-validation`, `needs-retry-logic` |

### Prevention rule generation

`TagIntelligenceEngine` decomposes tag combinations into **pairwise and triple subsets** before
storage/matching (v2.0, 2026-03-28) — full n-tuples of 8-12 tags produced nearly-unique combinations
that never matched, so this was narrowed to pairs/triples. A combination generates a prevention rule
after **2+ occurrences**; rules land in `pending_rules.json` for operator review (G-L1: never
auto-activated) — the same governance principle as the newer `learning_loop.py` pipeline, but a
separate code path and a separate `prevention_rules` write site.

### Schema

```sql
CREATE TABLE tag_combinations (
    id INTEGER PRIMARY KEY,
    tag_tuple TEXT UNIQUE NOT NULL,
    occurrence_count INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    phases TEXT,
    terminals TEXT,
    outcomes TEXT
)
```

### CLI / API

```bash
python3 scripts/tag_intelligence.py analyze validation-error api-component \
  --phase implementation --terminal T1 --outcome failure
python3 scripts/tag_intelligence.py rules --min-confidence 0.7
python3 scripts/tag_intelligence.py stats
```

```python
from tag_intelligence import TagIntelligenceEngine
engine = TagIntelligenceEngine()
result = engine.analyze_multi_tag_patterns(tags=["memory", "crawler", "critical"], ...)
```

### Recommendation Manager

`RecommendationManager` (also in `scripts/tag_intelligence.py`) manages structured recommendations:

```json
{
  "type": "claude_md_patch|prevention_rule|routing_hint",
  "target": "file_path_or_component",
  "symptom": "detected_issue",
  "evidence_ids": ["dispatch_1", "receipt_2", "OI-042"],
  "confidence": 0.75,
  "status": "pending|superseded|accepted"
}
```

Governance rules: **G-L1** (never auto-activated), **G-L2** (evidence trail required —
`ValueError` on empty `evidence_ids`), **G-L8** (max 5 active pending recommendations, excess
supersedes lowest-confidence). Stale pending edits (>7 days) are flagged for operator review;
duplicate `target + symptom` recommendations are merged or superseded.

---

## Which one should new code use?

If you are extending the **governed dispatch pipeline** (intelligence selector, tagger,
scout pre-pass, rank-then-budget) — use `vnx_tag_vocabulary.py`. It is the closed vocabulary those
subsystems validate against, and adding tags there requires updating the keyword dicts in that
module (Section 1).

If you are extending the **skill-invoked gather_intelligence.py path** (interactive
`intelligence.sh` scripts, `TagIntelligenceEngine.analyze_multi_tag_patterns`) — use the legacy
taxonomy in `tag_intelligence.py` (Section 2). Do not attempt to merge the two vocabularies without
a design decision on the consumer side; they are read by different code and a merge would need
migration for both `tag_combinations` and the closed-vocab keyword tables.

---

## References

- Closed vocabulary SSOT: `scripts/lib/vnx_tag_vocabulary.py`
- LLM tagger: `scripts/lib/vnx_tagger.py`
- Legacy engine: `scripts/tag_intelligence.py`
- Legacy integration: `scripts/gather_intelligence.py`
- Selector + rank-then-budget: `scripts/lib/intelligence_selector.py`, [`SELF_LEARNING_LOOP.md`](SELF_LEARNING_LOOP.md)
- Scout pre-pass: [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md)
- Legacy tests: `tests/test_tag_intelligence.py`

---

*Doc written 2026-07-05 for the docs-intelligence sweep (PRs #1001–#1017 drift-brief).*
*Dispatch-ID: D-docs-intelligence*
