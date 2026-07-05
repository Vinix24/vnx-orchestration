# VNX Self-Learning Loop — Operator-Gated Intelligence

**Status**: Active (D1–D6 shipped on main, 2026-07-04)
**Ground-truth source**: `claudedocs/2026-07-04-intelligence-dataflow-MAP.md`

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

Operator: vnx learning skill-refine --apply  (or manual patch application)
  → applies diff to .claude/skills/<role>/SKILL.md
  → NEVER touches .vnx/skills/
```

---

## Gating Model (G-L1 / G-L4)

| Gate | What it protects | Code location |
|------|-----------------|---------------|
| **G-L1** | New prevention rules never auto-activate | `learning_loop.py:647–710` — `ingest_approved_rules()` skips `status != "approved"` |
| **G-L4 (archival)** | Pattern archival candidates require approval | `learning_loop.py:712–770` — writes pending only |
| **G-L4 (supersede)** | Stale pattern suppression requires approval | `learning_loop.py:883` — queues to `pending_archival.json`; `valid_until` NOT written automatically (D3 fix) |

Prior to D3, `_supersede_stale_patterns()` wrote `valid_until` directly, silently removing low-confidence patterns from the selector. D3 closed this bypass: suppression candidates now land in `pending_archival.json` and wait for operator approval before `valid_until` is set.

---

## Off-Switches and Opt-In Flags

All intelligence flags default OFF (feature-dark until explicitly enabled). Precedence: `VNX_OVERRIDE_<BARE>` env → `project_config` DB → `VNX_<BARE>` env → registry default. Registry source: `scripts/lib/config_registry.py`.

| Flag | Default | What it controls | Code location |
|------|---------|-----------------|---------------|
| `VNX_LEARNING_ENABLED` | `0` (off) | Supervisor tick that invokes the daily learning cycle automatically. Requires `VNX_SUPERVISOR_MODE=unified`. When unset, the cycle only runs when explicitly invoked via `vnx learning run`. | `scripts/lib/dispatcher_supervisor_ticks.sh:100` |
| `VNX_LEARN_PERSIST` | `1` (on) | Step 5.5 of the daily cycle — persists high-confidence used patterns and recurring failures to `success_patterns`/`antipatterns`. Set to `0` to run the cycle without writing to the intelligence DB (observation-only mode). | `scripts/learning_loop.py:1043` |
| `VNX_LEARN_SUPERSEDE` | `1` (on) | Step 5.6 — queues stale low-confidence patterns to `pending_archival.json`. Set to `0` to skip the supersede-candidate scan entirely. | `scripts/learning_loop.py:897` |
| `VNX_TAGGER_ENABLED` | `0` (off) | LLM tagger enrichment at persist-time. When on, enriches `success_patterns.tags` and `antipatterns.tags` with closed-vocabulary tags via the configured provider (default: DeepSeek-Flash). The selector works without it via deterministic `derive_tags()`. | `scripts/lib/vnx_tagger.py:43`, `config_registry.py:54` |
| `VNX_TAGGER_PROVIDER` | `"deepseek"` | Provider for the LLM tagger. Model-agnostic. | `scripts/lib/config_registry.py:57` |
| `VNX_HAIKU_CLASSIFY` | `0` (off) | LLM-based receipt classification (Haiku) at report ingestion time. When off, rule-based classification is used. | `scripts/lib/report_classifier.py:244`, `config_registry.py:66` |
| `VNX_OUTCOME_GROUNDING_V2` | `0` (off) | Junction-grounded confidence updates. When on, `update_confidence_from_outcome()` links receipts to patterns via `dispatch_pattern_offered` (exact match, no cap). When off, uses the legacy substring join on `source_dispatch_ids` (capped at 20 entries). | `scripts/lib/intelligence_persist.py:56`, `config_registry.py:63` |

### Note on `VNX_LEARN_PERSIST` and `VNX_LEARN_SUPERSEDE` defaults

These default ON, not off — they are off-switches for sub-steps of the learning cycle, not opt-in gates. Setting either to `0` makes the cycle run in a reduced mode (observation without DB writes). They are documented here because they provide surgical rollback if a step causes unexpected behavior.

---

## Confidence Authority

The selector reads `success_patterns.confidence_score`. The authoritative writer is `confidence_reconcile.reconcile_pattern_confidence()` (Beta-Laplace from `pattern_usage.success_count/failure_count`). It fires in two places:

- Step 5.7 of the daily cycle (`learning_loop.py:1061`).
- At every proven-pattern query via `maybe_reconcile()` (TTL=300 s safety-net, `confidence_reconcile.py:162`).

The internal accumulator `pattern_usage.confidence` can exceed 1.0 (learning loop boosts to 2.0 cap). The reconcile clamps to 1.0 on write to `confidence_score`. Do not read `pattern_usage.confidence` as a selector-ready score.

---

## Tagger Decision Criterion

Enable `VNX_TAGGER_ENABLED=1` only after running `vnx learning tagger-ab` and observing:
- `rescue_rate >= 20%` (LLM adds tags the deterministic floor misses for ≥ 20% of patterns)
- `cost_per_pattern_usd <= 0.001` (DeepSeek-Flash tier)

Without these numbers from a live run, do not flip the default. The A/B command is read-only and never modifies the DB or sets the flag.

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
| Skill refinement targets .claude/skills/ only | `scripts/lib/skill_refinement.py:1–15` |
| Skill refinement never mutates directly | `scripts/lib/skill_refinement.py:1–15` |
| Beta-Laplace authority | `scripts/lib/confidence_reconcile.py:151` |
| maybe_reconcile safety-net (TTL=300s) | `scripts/lib/confidence_reconcile.py:162` |
| pattern_usage.confidence cap 2.0 | `scripts/learning_loop.py:283–297` |
| confidence_score clamped ≤1.0 at write | `scripts/lib/confidence_reconcile.py:151` |

---

*Doc written 2026-07-05 for D7. Verified against shipped code (D1–D6 on main).*
*Dispatch-ID: D-selfimprove-d7-docs*
