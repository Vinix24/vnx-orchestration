# VNX Self-Learning Loop — Operator-Gated Intelligence

**Status**: Active (D1–D6 shipped on main, 2026-07-04). Verified against code again 2026-07-05
(PRs #1001–#1017 drift-sweep).
**Related doc**: [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md) covers the scout pre-pass and rank-then-budget
selection in depth — this doc summarizes rank-then-budget below and defers detail there.
**Ground truth**: the code modules cited throughout this doc (`scripts/learning_loop.py`,
`scripts/lib/confidence_reconcile.py`, `scripts/lib/vnx_tagger.py`, `scripts/lib/skill_refinement.py`,
`scripts/lib/rework_attribution.py`, `scripts/lib/intelligence_persist.py`).

---

## Philosophy

The system PROPOSES. The operator DECIDES.

Nothing in the intelligence layer changes agent behavior without an explicit operator step. Every tier — rule proposals, archival candidates, skill diffs, tagger activation, outcome-grounding flip — writes to a pending file and stops. Activation is always a separate, reviewed, approved step.

The loop is deliberate about signal starvation: when autonomous dispatching is paused, the cycle runs against the historical receipt trail. Value grows as governed builds resume activity.

---

## What the Loop Does

The daily learning cycle mines `t0_receipts.ndjson` and produces three classes of output, all pending operator review:

| Output | File | Activated by |
|--------|------|--------------|
| Prevention-rule proposals | `state/pending_rules.json` | `vnx learning run` |
| Archival / supersede candidates | `state/pending_archival.json` | `vnx learning run` |
| Skill-body refinement diffs | `state/pending_skill_refinements.json` | `vnx learning skill-refine` |

None of these files are consumed automatically. Ingestion into the live DB (`prevention_rules` table) requires operator approval — see the review ritual below.

---

## Operator CLI

```
vnx learning run [--from-history]
vnx learning status
vnx learning review [--mode rules|archival|all]
vnx learning skill-refine [--threshold 0.3]
vnx learning skill-review [--show-diff]
vnx learning tagger-ab [--sample 20]
vnx learning grounding-shadow [--limit 50]
```

### `vnx learning run`

Runs `LearningLoop.daily_learning_cycle()` (`scripts/learning_loop.py:990`). Steps:

1. Read patterns used/ignored in the last 24 h from `pattern_usage` (or full history with `--from-history`).
2. Boost/decay in-memory confidence scores (10% boost, 5% decay; unclamped accumulator, clamps at write boundary).
3. Extract failure patterns from `t0_receipts.ndjson`; propose prevention rules → **`pending_rules.json`** (status=pending, G-L1 enforced — `learning_loop.py:496–541`).
4. Write unused-pattern archival candidates → **`pending_archival.json`** (status=pending — `learning_loop.py:712–770`).
5. Flush in-memory metrics to `pattern_usage` table.
5.5. Persist high-confidence used patterns and recurring failures to `success_patterns` / `antipatterns` (off-switchable via `VNX_LEARN_PERSIST=0`).
5.6. Queue stale low-confidence patterns for operator-gated supersede → **`pending_archival.json`** with `action=supersede` (`learning_loop.py:883`). No `valid_until` is written without operator approval (D3 gate — closes the prior ungated bypass).
5.7. Reconcile `success_patterns.confidence_score` via Beta-Laplace (`confidence_reconcile.reconcile_pattern_confidence`).
6. Save `learning_report_<ts>.json` to state dir.

### `vnx learning status`

Prints pending counts: rules awaiting approval, archival candidates, supersede candidates, pending skill refinements. No writes.

### `vnx learning review`

Prints pending proposals with pattern text, prevention text, confidence, and occurrence count. No writes.

### `vnx learning skill-refine`

Reads rework attribution data (from `rework_attribution.py`, shipped in D6) and generates unified diffs against `.claude/skills/<role>/SKILL.md` for roles with rework rate > threshold (default 30%). Writes proposals to **`pending_skill_refinements.json`** (`skill_refinement.py:1–15`).

Safety contracts (`skill_refinement.py:1–15`):
- Read-only w.r.t. skill markdown files during proposal generation.
- Proposals target ONLY `.claude/skills/` (project-editable per CLAUDE.md), NEVER `.vnx/skills/` (shipped template).
- Application is a separate operator-approved step.

#### The rework→skill-refine chain (D6, #1008)

Two modules, two slices, one loop:

1. **Slice 1 — `rework_attribution.py`** (read-only attribution engine, unlocked by the commit
   provenance chain, #969): for each git commit carrying a `Dispatch-ID:` trace token,
   `_changed_regions()` finds the pre-image lines that commit replaced, then `_blame_origin_counts()`
   blames those lines against the commit's parent. Whichever earlier token-commit introduced the
   replaced lines is the dominant origin, persisted (fill-once) into the existing but previously
   dormant `dispatch_metadata.parent_dispatch` column (`compute_rework_edges`,
   `scripts/lib/rework_attribution.py:137-181`). `success_by_role()` computes per-role first-pass
   success **excluding benchmark/headless runs** (`_GOVERNED_PREDICATE`,
   `scripts/lib/rework_attribution.py:187-213`) — the field-test track carries ~99% of
   role-stamped rows and would otherwise dominate the rate. `rework_by_origin_role()`
   (`scripts/lib/rework_attribution.py:228-242`) self-joins `dispatch_metadata` on
   `parent_dispatch` to count how often each origin role's work was later reworked.
2. **Slice 2 — `skill_refinement.py`**: `compute_rework_rates()` joins those two queries into
   `{role: {total, reworked, rework_rate}}` (`scripts/lib/skill_refinement.py:37-68`);
   `find_rework_prone_roles()` filters to `rework_rate > REWORK_THRESHOLD` (`= 0.3`,
   `scripts/lib/skill_refinement.py:27, 71-79`). For each prone role, `resolve_skill_path()`
   locates `.claude/skills/<role>/SKILL.md` (never `.vnx/skills/`,
   `scripts/lib/skill_refinement.py:82-89`) and `generate_proposal()` inserts a
   `## Rework Attribution Signal` section (with a rework-reduction checklist) before the
   `## Skill Activation Announcement` heading, producing a unified diff
   (`scripts/lib/skill_refinement.py:92-222`). Idempotency guard: if the attribution heading
   already exists in the skill file, no duplicate proposal is generated
   (`scripts/lib/skill_refinement.py:185-186`).

The edge only accrues forward from #969 — only commits stamped with a trace token (governed lanes)
carry the link, so `parent_dispatch` fills in over time as governed dispatches land. No new table:
this is ADR-007-free, a self-join over an existing dormant column.

### `vnx learning skill-review`

Prints pending skill refinements, optionally showing the full unified diff (`--show-diff`). No writes.

### `vnx learning tagger-ab`

Read-only A/B diagnostic (`vnx_cli/commands/learning.py:293`). Compares tag-overlap precision with vs without the LLM tagger on a seeded sample. Does NOT set `VNX_TAGGER_ENABLED`. Reports rescue rate and cost-per-pattern. Default-on criterion: `rescue_rate >= 20% AND cost_per_pattern <= $0.001 USD` (hardcoded threshold — `learning.py:382–386`).

### `vnx learning grounding-shadow`

Read-only shadow run (`vnx_cli/commands/learning.py:531`). Compares V1 (substring join) vs V2 (junction) outcome grounding on recent dispatches. Does NOT flip `VNX_OUTCOME_GROUNDING_V2`. Shows diverged dispatches and pattern counts.

---

## Review Ritual (Proposal → Activation)

Prevention rules follow this path:

```
vnx learning run
  → pending_rules.json  (status="pending")

Operator: vnx learning review --mode rules
  → inspect pattern, prevention text, confidence, occurrences

Operator: edit pending_rules.json — set status="approved" for accepted rules

vnx learning run  (next cycle)
  → ingest_approved_rules() reads approved rows → inserts into prevention_rules table
  → accepted rules are now live in the selector
```

Archival / supersede candidates:

```
vnx learning run
  → pending_archival.json  (status="pending", action="archive" or "supersede")

Operator: vnx learning review --mode archival
  → inspect title, confidence, age, reason

Operator: set status="approved" on accepted candidates
  → next cycle applies valid_until = now (supersede) or hard-deletes (archive)
  → pattern excluded from selector
```

Skill refinements:

```
vnx learning skill-refine
  → pending_skill_refinements.json  (status="pending")

Operator: vnx learning skill-review --show-diff
  → review diff + rationale + operator-test note

Operator: manually apply the diff to .claude/skills/<role>/SKILL.md
  → NEVER touches .vnx/skills/
```

**Note on `--apply`**: the generated proposal text and CLI help strings (`skill_refinement.py:119,
215`, `vnx_cli/commands/learning.py:468, 527`) describe `vnx learning skill-refine --apply` as the
activation step. **This flag does not exist.** `vnx_cli/main.py:578-592` registers only
`--project-dir` and `--threshold` for the `skill-refine` subcommand — there is no `apply_*` function
in `skill_refinement.py` either (only `generate_*`/`write_proposals`). Until `--apply` is
implemented, activation is **manual only**: apply the unified diff from
`pending_skill_refinements.json` to the target `.claude/skills/<role>/SKILL.md` yourself (e.g.
`git apply` or a manual patch), then verify the section landed correctly.

---

## Gating Model (G-L1 / G-L4)

| Gate | What it protects | Code location |
|------|-----------------|---------------|
| **G-L1** | New prevention rules never auto-activate | `learning_loop.py:647–710` — `ingest_approved_rules()` skips `status != "approved"` |
| **G-L4 (archival)** | Pattern archival candidates require approval | `learning_loop.py:712–770` — writes pending only |
| **G-L4 (supersede)** | Stale pattern suppression requires approval | `learning_loop.py:883` — queues to `pending_archival.json`; `valid_until` NOT written automatically (D3 fix) |

Prior to D3, `_supersede_stale_patterns()` wrote `valid_until` directly, silently removing low-confidence patterns from the selector. D3 closed this bypass: suppression candidates now land in `pending_archival.json` and wait for operator approval before `valid_until` is set.

---

## Rank-Then-Budget Selection (opt-in)

`VNX_INTEL_RANK_THEN_BUDGET` (default `"0"`, `config_registry.py:60-62`) replaces the legacy
class-priority eviction in `intelligence_selector.py` with a composite-score knapsack:
`score = confidence * (1 + tag_overlap) * recency_decay * class_weight`
(`intelligence_selector.py:131-146`). `tag_overlap` is computed against
`vnx_tag_vocabulary.derive_tags()` (the same closed vocabulary the tagger validates against — see
[`TAG_TAXONOMY.md`](TAG_TAXONOMY.md)); `recency_decay` is a 30-day half-life floored at `0.3`
(`intelligence_selector.py:114-128`); `_RESERVED_CLASSES = {"failure_prevention"}`
(`intelligence_selector.py:106`) always gets first claim on the budget so a cheap high-scoring
anchor can never evict a critical failure-prevention rule. Full detail, including the
`scout_sketch` class weight of `1.45`, is in [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md#rank-then-budget-a-related-separately-gated-build-step).

Default OFF — the legacy class-priority path stays the default until operator-enabled per project.

## Tagging Events Audit Trail

Every time the tagger successfully tags a pattern (`enrich_pattern_tags`,
`scripts/lib/vnx_tagger.py:157-240`), it writes an audit row to a `tagging_events` table in the
same `quality_intelligence.db` the tagger already writes to (`_TAGGING_EVENTS_SCHEMA`,
`scripts/lib/vnx_tagger.py:52-65`):

```sql
CREATE TABLE IF NOT EXISTS tagging_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL DEFAULT 'vnx-dev',
    table_name    TEXT NOT NULL,
    pattern_id    INTEGER NOT NULL,
    pattern_title TEXT,
    tags_json     TEXT NOT NULL,
    provider      TEXT,
    tagged_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, table_name, pattern_id, tagged_at)
);
```

One row per pattern the tagger actually enriches (non-empty tag result only — a pattern the LLM
couldn't tag doesn't generate a row). ADR-007-compliant: `UNIQUE` is composite over `project_id`.
This is the audit trail behind the dashboard's tagging-observability panel (#966): "what did the
tagging agent do, and with which model." It is distinct from the A/B harness below — the A/B
harness is a read-only diagnostic that never writes to the DB; `tagging_events` is the persistent
record of live, persist-time tagging once `VNX_TAGGER_ENABLED=1`.

## Off-Switches and Opt-In Flags

All intelligence flags default OFF (feature-dark until explicitly enabled). Precedence: `VNX_OVERRIDE_<BARE>` env → `project_config` DB → `VNX_<BARE>` env → registry default. Registry source: `scripts/lib/config_registry.py`.

| Flag | Default | What it controls | Code location |
|------|---------|-----------------|---------------|
| `VNX_LEARNING_ENABLED` | `0` (off) | Supervisor tick that invokes the daily learning cycle automatically. Requires `VNX_SUPERVISOR_MODE=unified`. When unset, the cycle only runs when explicitly invoked via `vnx learning run`. | `scripts/lib/dispatcher_supervisor_ticks.sh:100` |
| `VNX_LEARN_PERSIST` | `1` (on) | Step 5.5 of the daily cycle — persists high-confidence used patterns and recurring failures to `success_patterns`/`antipatterns`. Set to `0` to run the cycle without writing to the intelligence DB (observation-only mode). | `scripts/learning_loop.py:1043` |
| `VNX_LEARN_SUPERSEDE` | `1` (on) | Step 5.6 — queues stale low-confidence patterns to `pending_archival.json`. Set to `0` to skip the supersede-candidate scan entirely. | `scripts/learning_loop.py:897` |
| `VNX_SCOUT_PREPASS` | `0` (off) | Cheap-model scout recon pre-pass in the door, before the permit (see [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md)). Fail-open. | `scripts/lib/scout_prepass.py:268-272`, `config_registry.py:51` |
| `VNX_INTEL_RANK_THEN_BUDGET` | `0` (off) | Replaces class-priority intelligence eviction with a composite-score knapsack (confidence × tag-overlap × recency × class-weight). | `scripts/lib/intelligence_selector.py:109-146`, `config_registry.py:60` |
| `VNX_TAGGER_ENABLED` | `0` (off) | LLM tagger enrichment at persist-time. When on, enriches `success_patterns.tags` and `antipatterns.tags` with closed-vocabulary tags via the configured provider (default: DeepSeek-Flash), and writes a `tagging_events` audit row per enrichment. The selector works without it via deterministic `derive_tags()`. | `scripts/lib/vnx_tagger.py:43`, `config_registry.py:54` |
| `VNX_TAGGER_PROVIDER` | `"deepseek"` | Provider for the LLM tagger. Model-agnostic. | `scripts/lib/config_registry.py:57` |
| `VNX_HAIKU_CLASSIFY` | `0` (off) | LLM-based receipt classification (Haiku) at report ingestion time. When off, rule-based classification is used. | `scripts/lib/report_classifier.py:244`, `config_registry.py:66` |
| `VNX_OUTCOME_GROUNDING_V2` | `0` (off) | Junction-grounded confidence updates. When on, `update_confidence_from_outcome()` links receipts to patterns via `dispatch_pattern_offered` (exact match, no cap). When off, uses the legacy substring join on `source_dispatch_ids` (capped at 20 entries). | `scripts/lib/intelligence_persist.py:56`, `config_registry.py:63` |

### Note on `VNX_LEARN_PERSIST` and `VNX_LEARN_SUPERSEDE` defaults

These default ON, not off — they are off-switches for sub-steps of the learning cycle, not opt-in gates. Setting either to `0` makes the cycle run in a reduced mode (observation without DB writes). They are documented here because they provide surgical rollback if a step causes unexpected behavior.

### Current per-project state — `vnx-dev`

The registry defaults above never change (a fresh `vnx init` project starts with every intelligence
flag OFF). This project's `runtime_coordination.db: project_config` table currently overrides two of
them, set by the operator through the config control-plane:

| Flag | Value | Updated |
|------|-------|---------|
| `VNX_SCOUT_PREPASS` | `1` | 2026-07-05T14:47:57Z |
| `VNX_TAGGER_ENABLED` | `1` | 2026-07-05T15:35:56Z |

The tagger flip followed a `vnx learning tagger-ab` run that observed a 100% rescue rate at
~$0.0005/pattern (DeepSeek-Flash) — comfortably past the `rescue_rate >= 20% AND
cost_per_pattern <= $0.001` decision criterion below. This is a per-project operator decision, not a
code change; `VNX_INTEL_RANK_THEN_BUDGET` and `VNX_OUTCOME_GROUNDING_V2` remain OFF for this project.

---

## Confidence Authority

The selector reads `success_patterns.confidence_score`. The authoritative writer is `confidence_reconcile.reconcile_pattern_confidence()` (Beta-Laplace from `pattern_usage.success_count/failure_count`). It fires in two places:

- Step 5.7 of the daily cycle (`learning_loop.py:1061`).
- At every proven-pattern query via `maybe_reconcile()` (TTL=300 s safety-net, `confidence_reconcile.py:183–220`).

The internal accumulator `pattern_usage.confidence` can exceed 1.0 (learning loop boosts to 2.0 cap). The reconcile clamps to 1.0 on write to `confidence_score`. Do not read `pattern_usage.confidence` as a selector-ready score.

### Range contract (D1, #1001)

`reconcile_pattern_confidence()` asserts the invariant at its single write boundary before every
`UPDATE`:

```python
# scripts/lib/confidence_reconcile.py:164-167
assert 0.0 <= new_score <= 1.0, (
    f"confidence_score out of range before write: "
    f"{new_score!r} for sp_id={sp_id}"
)
```

`beta_score()` (`confidence_reconcile.py:76-83`) and `_recency_decay()`
(`confidence_reconcile.py:48-56`) always compose to `[0.0, 1.0]`, and the legacy
`_aggregate_for_pattern()` fallback clamps explicitly (`confidence_reconcile.py:118-122`) — so the
assert is a tripwire for a future writer that breaks the invariant, not a normal-path check.

### Subprocess-lane fixed-delta path

The Beta-Laplace reconcile above only fires from receipt-driven events
(`update_confidence_from_outcome` listens exclusively for `task_complete`/`task_failed`). Dispatches
routed through the **subprocess lane** that complete without emitting one of those events (a
`subprocess_completion` receipt instead) never trigger it. For those, a separate fixed-delta path is
the **only** confidence update they receive:

```python
# scripts/lib/subprocess_dispatch_internals/pattern_confidence.py:187-236
# success: confidence_score = MIN(confidence_score + 0.05, 1.0)
# failure: confidence_score = MAX(confidence_score - 0.10, 0.0)
```

`_update_pattern_confidence()` (`pattern_confidence.py:104-159`) looks up patterns offered to the
dispatch (`dispatch_pattern_offered`, falling back to `pattern_usage.dispatch_id` for older DBs) and
applies the delta by title match. It deliberately does **not** touch
`pattern_usage.used_count`/`success_count`/`failure_count` — those columns are reserved for
confirmed-usage signals from the Beta-Laplace path, so the two update mechanisms never double-count
the same evidence. Do not retire this path until a receipt-level grounding path exists for
`subprocess_completion` events (`pattern_confidence.py:129-136`).

### Schema note (D2, #1002)

`success_patterns.success_rate` was dropped (`_migrate_v26`, reversible via `_migrate_v26_down`,
`scripts/quality_db_init.py:994-1044`) — the column had always been `0.0` in production; no path
ever wrote a non-zero value. `confidence_score DESC` was already the sole effective sort before the
drop (`_INTELLIGENCE_BRIEF_SQL`, `scripts/build_t0_state.py:1009-1014`); the column no longer
physically exists in `schemas/quality_intelligence.sql`. (Unrelated: `build_t0_state.py` also reads
a `success_rate` key elsewhere, at line 1093 — that one comes from
`DispatchParameterTracker.stats()` in `dispatch_tracker.db`, a different table entirely, and is
untouched by this migration.)

---

## Tagger Decision Criterion

Enable `VNX_TAGGER_ENABLED=1` only after running `vnx learning tagger-ab` and observing:
- `rescue_rate >= 20%` (LLM adds tags the deterministic floor misses for ≥ 20% of patterns)
- `cost_per_pattern_usd <= 0.001` (DeepSeek-Flash tier)

Without these numbers from a live run, do not flip the default. The A/B command is read-only and never modifies the DB or sets the flag.

For `vnx-dev`, a live run cleared both thresholds (100% rescue, ~$0.0005/pattern), so the operator
flipped `VNX_TAGGER_ENABLED=1` via the config DB (see [above](#current-per-project-state--vnx-dev)).
This is a per-project decision — a fresh project starts back at OFF and needs its own A/B run.

---

## Outcome Grounding Decision Criterion

Enable `VNX_OUTCOME_GROUNDING_V2=1` after running `vnx learning grounding-shadow` and confirming:
- The diverged-dispatch count is non-zero (V2 and V1 differ — so V2 is worth using).
- V2-only grounded patterns outnumber V1-only (V2 is more precise, not just different).
- Rollback path tested: unset the flag restores V1 behavior immediately.

The shadow command is read-only and never writes to the DB or sets the flag.

---

## Signal Starvation Warning

The loop runs against whatever is in `t0_receipts.ndjson`. While autonomous dispatching is paused, new receipts arrive only from manual dispatches. The historical trail (13,000+ receipts in the governed audit trail as of 2026-07-04) provides a useful baseline — run with `--from-history` on the first cycle.

Value grows as governed builds resume. The proposal tier is not a no-op even against history: it finds recurring failure patterns and unused patterns regardless of whether new dispatches are running.

---

## Claim-to-Code Map

| Claim | File:line |
|-------|-----------|
| G-L1: rules land in pending_rules.json | `scripts/learning_loop.py:496–541` |
| G-L1: ingest skips non-approved rules | `scripts/learning_loop.py:647–710` |
| G-L4: archival candidates land in pending | `scripts/learning_loop.py:712–770` |
| G-L4 supersede gated (D3) | `scripts/learning_loop.py:883–930` |
| `VNX_LEARN_SUPERSEDE=0` off-switch | `scripts/learning_loop.py:897–899` |
| `VNX_LEARN_PERSIST=0` off-switch | `scripts/learning_loop.py:1043–1046` |
| `VNX_TAGGER_ENABLED` default "0" | `scripts/lib/config_registry.py:54–56` |
| Tagger call site (persist-time) | `scripts/intelligence_daemon.py:236–247` |
| `VNX_HAIKU_CLASSIFY` default "0" | `scripts/lib/config_registry.py:66–68` |
| Haiku classify guard | `scripts/lib/report_classifier.py:244` |
| `VNX_OUTCOME_GROUNDING_V2` default "0" | `scripts/lib/config_registry.py:63–65` |
| V2 grounding enabled check | `scripts/lib/intelligence_persist.py:56–66` |
| `VNX_LEARNING_ENABLED` supervisor gate | `scripts/lib/dispatcher_supervisor_ticks.sh:100` |
| Tagger A/B read-only guard | `vnx_cli/commands/learning.py:303` |
| Grounding shadow read-only | `vnx_cli/commands/learning.py:531–615` |
| Skill refinement targets .claude/skills/ only | `scripts/lib/skill_refinement.py:82–89` |
| Skill refinement never mutates directly | `scripts/lib/skill_refinement.py:1–15` |
| `--apply` CLI flag does NOT exist (doc/help text claim it; code doesn't) | `vnx_cli/main.py:578–592` (missing); refs in `vnx_cli/commands/learning.py:468, 527`, `skill_refinement.py:119, 215` |
| REWORK_THRESHOLD = 0.3 | `scripts/lib/skill_refinement.py:27` |
| Rework attribution: git-churn blame → origin dispatch | `scripts/lib/rework_attribution.py:137–181` |
| Rework-prone role filter | `scripts/lib/skill_refinement.py:71–79` |
| Beta-Laplace authority | `scripts/lib/confidence_reconcile.py:76–83, 127–180` |
| maybe_reconcile safety-net (TTL=300s) | `scripts/lib/confidence_reconcile.py:183–220` |
| pattern_usage.confidence cap 2.0 | `scripts/learning_loop.py:283–297` |
| confidence_score range assert at write boundary | `scripts/lib/confidence_reconcile.py:164–167` |
| Subprocess-lane fixed-delta path (+0.05/-0.10) | `scripts/lib/subprocess_dispatch_internals/pattern_confidence.py:104–159, 187–236` |
| D2: success_rate column dropped (reversible) | `scripts/quality_db_init.py:994–1044` |
| `VNX_SCOUT_PREPASS` default "0" | `scripts/lib/config_registry.py:51–53` |
| Scout pre-pass producer + consumer | see [`SCOUT_PREPASS.md`](SCOUT_PREPASS.md) claim-to-code map |
| `VNX_INTEL_RANK_THEN_BUDGET` default "0" | `scripts/lib/config_registry.py:60–62` |
| Composite relevance score | `scripts/lib/intelligence_selector.py:131–146` |
| `tagging_events` audit table | `scripts/lib/vnx_tagger.py:52–65` |
| Tagger closed-vocab floor + snap-to-vocab | `scripts/lib/vnx_tag_vocabulary.py:72–95` |

---

*Doc written 2026-07-05 for D7; updated 2026-07-05 for the docs-intelligence sweep (PRs #1001–#1017).*
*Dispatch-ID: D-docs-intelligence (update); originally D-selfimprove-d7-docs.*
