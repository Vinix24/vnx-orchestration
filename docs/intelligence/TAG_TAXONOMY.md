# VNX Tag Taxonomy

**Verified against `origin/main`** at c41411fea on 2026-11-12.

**There are two separate, still-live tag systems in this codebase.** They serve different
consumers and do not share a vocabulary. This doc covers both, in order of which is now the SSOT
for the governed dispatch pipeline.

---

## 1. VNX closed vocabulary: SSOT for selector matching + the LLM tagger

**Module**: `scripts/lib/vnx_tag_vocabulary.py`.
**Consumers**: `intelligence_selector.py`'s rank-then-budget scoring (opt-in
`VNX_INTEL_RANK_THEN_BUDGET`, default off), which calls `derive_tags` to compute the tag-overlap
score, and `vnx_tagger.py`'s LLM tagger (opt-in `VNX_TAGGER_ENABLED`, default off). Full pipeline detail:
[`SELF_LEARNING_LOOP.md`](SELF_LEARNING_LOOP.md) and [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md).

This is a **closed, faceted taxonomy** purpose-built for the VNX Orchestration codebase (not
SEOcrawler), three flat, independent facets used to match injected intelligence to a dispatch by
intent + subsystem, not just file-path overlap.

### Facets

| Facet | Purpose | Tag count | Code |
|-------|---------|-----------|------|
| **Domain** | The subsystem the work touches | 12 | `_DOMAIN_KEYWORDS` |
| **Intent** | What the work is | 10 | `_INTENT_KEYWORDS` |
| **Component** | Cross-cutting concern | 8 | `_COMPONENT_KEYWORDS` |

**Domain tags**: `dispatch`, `intelligence`, `receipts_audit`, `governance_gates`,
`providers_routing`, `schema_migrations`, `tenant_project_id`, `tests_harness`, `docs`,
`dashboard_ui`, `benchmark`, `learning_loop`.

**Intent tags**: `fix_bug`, `implement_feature`, `refactor`, `add_test`, `harden`, `migrate_schema`,
`wire_integration`, `review_audit`, `document`, `investigate_rootcause`.

**Component tags**: `fail_closed`, `idempotency`, `concurrency_lease`, `project_id_stamping`,
`cli_subprocess`, `ndjson_contract`, `fts5`, `provider_constraint`.

Each facet maps tags to keyword lists (`_DOMAIN_KEYWORDS`, `_INTENT_KEYWORDS`,
`_COMPONENT_KEYWORDS`). `VNX_TAG_VOCABULARY` is the union of all three, the single closed set every tag, deterministic or LLM, must
belong to.

### Deterministic floor: `derive_tags()`

```python
# scripts/lib/vnx_tag_vocabulary.py
def derive_tags(text, paths=None) -> List[str]:
    """Keyword/path scan only, no LLM. Deduplicated, facet-ordered."""
```

A pure keyword/path substring scan, no LLM call, always available. This is the floor every dispatch
gets regardless of whether the LLM tagger is enabled: the selector's `rank_then_budget` tag-overlap
score (`_relevance_score` in `intelligence_selector.py`) calls this on-the-fly against an item's title+content
even when no tags were ever persisted to the DB.

### LLM enrichment layer: `vnx_tagger.py`

Opt-in (`VNX_TAGGER_ENABLED=1`, default off), model-agnostic (`VNX_TAGGER_PROVIDER`, default
`deepseek`; the `deepseek` key-auth lane). `enrich_tags()` always starts from `derive_tags()` and only
*adds* LLM-suggested tags on top. The LLM never replaces the deterministic floor.

**Snap-to-vocab validation**: the load-bearing safety property:

```python
# scripts/lib/vnx_tag_vocabulary.py
def validate_tags(tags) -> List[str]:
    """Keep only tags that are in the closed vocabulary."""
    return [t for t in (tags or []) if t in VNX_TAG_VOCABULARY]
```

The LLM prompt (`_build_prompt` in `vnx_tagger.py`) lists the exact closed vocabulary and
instructs "choosing ONLY from the closed vocabulary. Do not invent tags." The response is then
passed through `validate_tags()` regardless, an off-vocabulary hallucination
from the model can never enter the matching layer. Max 6 tags per pattern (`_MAX_TAGS`).
If the operator switched the tagger's provider family off (`vnx providers`), the LLM call is
skipped and counted, and the deterministic floor stands.

`enrich_pattern_tags()` is the persist-time entry point: it tags untagged rows in
`success_patterns`/`antipatterns` (JSON `tags` column) and writes one audit row per non-empty
tagging to a `tagging_events` table (`_TAGGING_EVENTS_SCHEMA`, composite UNIQUE over `project_id`).
With the tagger disabled it does nothing. It is called from
`intelligence_daemon.GovernanceDigestRunner.run_once()` after signal persistence
(`scripts/intelligence_daemon.py`), at most 100 rows per run.

### Decision criterion + current state

Enable `VNX_TAGGER_ENABLED=1` per project only after `vnx learning tagger-ab` shows
`rescue_rate >= 20%` and `cost_per_pattern_usd <= $0.001`. The command prints PASS or FAIL against
both thresholds (`RESCUE_THRESHOLD` and `COST_CEILING` in `vnx_cli/commands/learning.py`). It does
not change the database and does not set the flag.

---

## 2. Legacy taxonomy: `tag_intelligence.py` (SEOcrawler-flavored, still live)

**Module**: `scripts/tag_intelligence.py`.
**Consumers**: `scripts/gather_intelligence.py`, `scripts/lib/intelligence_hygiene.py`,
`scripts/intelligence_daemon.py` (imports `TagIntelligenceEngine`) and the `stale` step of
`scripts/nightly_intelligence_pipeline.sh`, the interactive/skill-invoked
intelligence-gathering path (`skills/*/scripts/intelligence.sh`,
`scripts/userpromptsubmit_worker_intelligence_inject.sh`), separate from the governed-dispatch
`IntelligenceSelector` pipeline above.

This is an **older, unrelated vocabulary** originally written for SEOcrawler-style work
(`crawler-component`, `storage-component`, `api-component`) and is **not** the vocabulary the
selector's rank-then-budget scoring or the LLM tagger validate against. It is still live code, not
dead, do not delete it, but it is a parallel system, not a predecessor that `vnx_tag_vocabulary.py`
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
storage/matching (`generate_tag_subsets`), full n-tuples of 8-12 tags produced nearly-unique
combinations that never matched, so this was narrowed to pairs/triples. A combination generates a
prevention rule after **2+ occurrences** (`analyze_multi_tag_patterns`).

This engine writes the rule straight into the `prevention_rules` table (`_store_prevention_rule`).
It has no human approval step of its own: `injectable_clause` in `scripts/lib/pattern_activation.py`
lets every legacy row (source not `learning_loop`) through. The operator-gated path is the newer
learning loop, whose rules need an activation record. See
[`SELF_LEARNING_LOOP.md`](SELF_LEARNING_LOOP.md).

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
python3 scripts/tag_intelligence.py stale
```

```python
from tag_intelligence import TagIntelligenceEngine
engine = TagIntelligenceEngine()
result = engine.analyze_multi_tag_patterns(["validation-error", "api-component"], phase="implementation")
```

### Recommendation Manager

`RecommendationManager` (also in `scripts/tag_intelligence.py`) keeps structured recommendations in
`t0_recommendations.json` and `pending_edits.json` in the state dir:

```json
{
  "type": "claude_md_patch|prevention_rule|routing_hint",
  "target": "file_path_or_component",
  "symptom": "detected_issue",
  "evidence_ids": ["dispatch_1", "receipt_2", "OI-042"],
  "confidence": 0.75,
  "status": "pending|superseded"
}
```

Rules enforced in code: **G-L2** (evidence trail required, `ValueError` on empty `evidence_ids`)
and **G-L8** (max 5 active pending recommendations, excess supersedes lowest-confidence). Pending
edits older than 7 days are marked stale for operator review (`stale` command);
duplicate `target + symptom` recommendations are merged or superseded.

---

## Which one should new code use?

If you are extending the **governed dispatch pipeline** (intelligence selector, tagger,
scout pre-pass, rank-then-budget), use `vnx_tag_vocabulary.py`. It is the closed vocabulary those
subsystems validate against, and adding tags there requires updating the keyword dicts in that
module (Section 1).

If you are extending the **skill-invoked gather_intelligence.py path** (interactive
`intelligence.sh` scripts, `TagIntelligenceEngine.analyze_multi_tag_patterns`), use the legacy
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
