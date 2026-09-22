# Smart Router Design

**Status**: Canonical
**Code**: `scripts/lib/smart_router.py` (+ `scripts/lib/providers/smart_router/` re-export package, `scripts/lib/cost_loader.py`)
**Config (SSOT)**: `scripts/lib/providers/routing_recommendations.yaml`, `scripts/lib/providers/wave7_models.yaml`, `scripts/lib/providers/provider_constraints.yaml`
**Date**: 2026-07-22 (model-registry-refresh)

This document describes what the smart router actually does today, grounded in the current code and config — not a design intent that code hasn't caught up to. Where behavior is opt-in or dormant, that is called out explicitly.

---

## 1. Smart Router Architecture

### 1.1 What it's for

A dispatch instruction is free text ("implement a new handler", "debug the flaky test", "review this PR for security issues"). The smart router turns that text into a ranked list of model recommendations, so a dispatcher can pick a model without a human manually choosing one every time. It is consulted, not authoritative — the dispatch door's own rules (provider constraints, pins) can still override or reject its pick.

### 1.2 Data flow per dispatch

```
instruction text ──► classify_task() ──► task_class (one of 7)
                                              │
                                              ▼
                            recommend(task_class) ──► ranked RouteCandidate list
                                              │        (routing_recommendations.yaml
                                              │         + cost_loader enrichment from
                                              │         wave7_models.yaml)
                                              ▼
                    _filter_by_constraints() ──► drop candidates that violate
                                              │    provider_constraints.yaml (G8)
                                              ▼
                  optional tag promotion ──► cost-tier-zero / privacy-required
                                              │    moves cost_tier=0 models (e.g.
                                              │    gemma-4b-local) to the front
                                              ▼
                     RouteDecision(primary, fallback, reason, ...)
                                              │
                              ┌───────────────┴────────────────┐
                              ▼                                 ▼
                 parse_route_model_id()              write_route_decision()
                 → (provider_flag, model_alias)       → route_decisions.ndjson
                 for --provider/--model CLI flags       + per-dispatch JSON
```

The entry point that does all of this in one call is `route(instruction, dispatch_id, state_dir, ...)`. `decide(...)` does classify+recommend+filter+promote without the CLI-flag resolution or the NDJSON write, and is what most callers and tests use directly.

### 1.3 Core components

| Component | File | Responsibility |
|---|---|---|
| Classifier | `smart_router.py::classify_task` | Instruction text → task class |
| Recommendation loader | `smart_router.py::_load_recommendations` | Parses `routing_recommendations.yaml`, applies quality-tier filters, sorts candidates |
| Cost enrichment | `cost_loader.py::enrich_candidates` | Fills `cost_usd_per_call` from `wave7_models.yaml` rates when the yaml entry itself is `null` |
| Constraint filter | `smart_router.py::_filter_by_constraints` | Drops candidates that `providers.constraint_enforcer` flags as blocking (fail-open on import/lookup error) |
| Model-ID resolver | `smart_router.py::parse_route_model_id` | `model_id` string → `(provider_flag, model_alias)` for dispatch CLI flags |
| Telemetry writer | `smart_router.py::write_route_decision` | Appends to `route_decisions.ndjson`, writes per-dispatch strategy JSON |
| Dormant tier router | `providers/smart_router/tier_routing.py` | A **separate**, default-off LOC-based tier classifier (see §1.5) |

### 1.4 The classifier cascade

`classify_task(instruction, role=None, dispatch_paths=None)` resolves a task class in three steps, first match wins:

1. **Heuristic regex** — the instruction text is matched against ordered regex patterns for each of the 7 task classes (`05_debugging`, `02_code_review`, `06_design`, `07_translation`, `04_documentation`, `03_refactoring`, `01_code_generation` — checked in that order, so e.g. "review the debug output" matches debugging first since it's checked first in `_TASK_CLASS_PATTERNS`).
2. **Role-based fallback** — if no regex matches, the caller-supplied `role` (e.g. `backend-developer`, `security-engineer`) is looked up in `ROLE_TO_TASK_CLASS`.
3. **Default** — if neither matches, `01_code_generation` (the safest default; most dispatches are code work).

`dispatch_paths` is accepted but currently unused — reserved for future signal enrichment (e.g. docs-only paths → documentation class).

Separately, `decide()` accepts a `tags` sequence. Tags do **not** feed the classifier — they act *after* recommendation, re-ranking the already-sorted candidate list so `cost-tier-zero` or `privacy-required` promotes any `cost_tier=0` candidate (local/free, e.g. `gemma-4b-local`) to the front without re-scoring anything.

### 1.5 The tier-based router is a separate, dormant path

`providers/smart_router/` also ships `cost_tier.py` + `tier_routing.py`: a LOC-count/keyword classifier (`tier-zero` through `tier-high`) that resolves to a fixed `TierRoute` per tier, wired through `route_dispatch()`. This is **default-off** — `route_dispatch()` returns `None` unless `VNX_AUTO_ROUTE` is set to a truthy value, per the "smart-router-built-not-operative" decision. It does not read `routing_recommendations.yaml` at all; its routes are hardcoded constants in `tier_routing.py`. Treat it as a distinct subsystem from the classify→recommend pipeline described above — the two are not currently unified.

### 1.6 Integration point: dispatch_plan D4

The single-entry dispatch door's `compile_plan()` (`scripts/lib/dispatch_plan.py`) has its own, independent model-selection rule, **D4 — model tier**: for the Claude lane, a `model_pins` snapshot value for the target slot wins over the requested model (warn-only, not a hard reject, if they differ). Smart-router output and D4 pins are two different mechanisms; when `--auto-route` selects a non-Claude provider, D4 is a no-op (it only applies `is_claude_lane`). `provider_dispatch.py`'s `--auto-route` flag is the actual wiring: it calls `decide()`, and on a primary candidate overwrites `args.provider`/`args.model` before the door's normal constraint checks run.

### 1.7 Telemetry: route_decisions.ndjson

Every `route()` call (i.e., every `--auto-route` dispatch) appends one record to `<state_dir>/route_decisions.ndjson` via `state_writer.append_locked` (fcntl-locked, safe for concurrent writers): timestamp, `dispatch_id`, `task_class`, `chosen_route`/`fallback_route` (model_id + composite_score), `constraints_applied`, `cost_estimate`, and an `outcome` field left `null` at write time (not currently back-filled by any consumer — an audit-trail gap, not a bug in this pipeline). A parallel per-dispatch JSON at `<state_dir>/route_decisions/<dispatch_id>.json` lets `report_to_receipt_converter` tag the receipt's `strategy` field as `smart_router` instead of the default `default`.

---

## 2. Failure Modes + Fallback Policy

The router is designed to degrade to "do nothing" rather than block a dispatch:

| Failure | Behavior |
|---|---|
| `routing_recommendations.yaml` missing | `_load_recommendations` raises `FileNotFoundError`. In the `--auto-route` caller (`provider_dispatch.py`), this is caught by a blanket `except Exception`, logged as a warning, and the dispatch **falls back to the originally-requested `--provider`/`--model`** — auto-route is best-effort, never fail-closed. |
| Malformed yaml (missing `routing_by_task`) | `_load_recommendations` raises `ValueError`. Same fallback path as above. |
| Unknown task class | `recommend()` returns `[]`; `decide()` returns a `RouteDecision` with `primary=None`, `fallback=None`. The `--auto-route` caller sees no primary and leaves `args.provider`/`args.model` untouched. |
| `providers.constraint_enforcer` import fails, or a per-candidate constraint check raises | `_filter_by_constraints` is fail-open: on import error it returns the original candidate list unfiltered with no applied-constraints list; on a per-candidate exception it keeps that candidate. A constraint-checker bug never removes a model from consideration by accident — it can only fail to filter. |
| `wave7_models.yaml` missing (cost enrichment) | `cost_loader._load_wave7_costs` returns `{}`; `enrich_candidates` becomes a no-op. Candidates keep whatever `cost_usd_per_call` (often `null`) was already in `routing_recommendations.yaml`, and the sort falls back to score-descending for that tier. |
| Empty candidate cost known (`null`) for an above-threshold model | Sorts as `+inf` — ranked last within the capable band, never assumed free. |
| glm-harness spawn returns rc=0 with empty completion | Not a router failure per se, but downstream in `glm_harness_spawn.py`: coerced to a retryable failure (rc=1) rather than silently emitting an empty report — the adapter's retry budget re-attempts. |

**Net effect**: a broken or missing routing config degrades `--auto-route` dispatches to exactly the behavior they'd have had without `--auto-route` at all. The flag is opt-in and its failure mode is silent fallback, not a hard stop.

---

## 3. Cost Governance

### 3.1 cost_tier and quality_tier

Every `RouteCandidate` carries two independent axes:

- **`cost_tier`** — `0` means local/free inference (currently only `gemma-4b-local`, running on-device via MLX/Ollama with zero API cost). `None` means standard/metered billing (the default for everything else). There is no tier above 0 today; it exists to let `cost-tier-zero`/`privacy-required` tags do an exact-match promotion rather than a heuristic one.
- **`quality_tier`** — `1` (low) to `3` (premium capability). If a `routing_recommendations.yaml` entry sets it explicitly, that value is used (validated to be 1–3). Otherwise it's derived: `cost_tier=0` locks to tier `1` regardless of score; else composite_score `>= 7.5` → 3, `>= 5.0` → 2, else 1. Task nodes can additionally set `min_quality_tier`/`max_quality_tier` to gate the candidate pool (e.g. `02_code_review` requires `min_quality_tier: 3` — a weak model is never recommended for review).

### 3.2 The ranking matrix (cost-aware hybrid, operator-chosen 2026-06-28)

Candidates are sorted by `_cost_aware_sort_key`, a two-band policy:

- **Band 0 — capable** (`composite_score >= 7.0`, the `_CAPABILITY_THRESHOLD`): ranked by cost ascending (cheapest wins), composite_score descending as the tiebreak on equal cost. Unknown cost sorts as `+inf` (last within the band) — never assumed free.
- **Band 1 — sub-bar**: ranked by composite_score descending (best available), cost ascending as the tiebreak.

This means a cheap-and-strong model beats an expensive-and-stronger one, but a cheap-and-weak model can never outrank a model that actually clears the capability bar.

### 3.3 How cost gets labeled per lane

Cost accounting is decided by the dispatch door's D2 rule (`dispatch_plan.py`), independent of the router's own `cost_usd_per_call` estimates. For the `claude` lane the label is **auth-derived**, not lane-derived (OI-1156): the door computes `claude_auth_is_api_metered(env)` — the presence of an own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` (key-auth / redirect) is what makes the label `api_metered`. Without either, both the tmux and headless lanes bill as `subscription`.

| Lane | `billing` label | Why |
|---|---|---|
| `claude` (tmux or headless) | auth-derived | `api_metered` with an own `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`; `subscription` otherwise |
| `kimi` | `subscription` | CLI OAuth lane (`kimi-via-cli-only`), flat — never metered per call |
| `local-gemma` | `local` | On-device inference, zero API cost |
| everything else (`glm-harness`, `deepseek-harness`, `litellm:*`, `codex`, `gemini`) | `provider_metered` | Real per-token API billing |

The router's own `cost_usd_per_call` field (used for *ranking*, via `cost_loader.compute_cost_per_call` against `wave7_models.yaml` rates) is a separate, estimate-only number for comparing candidates — it is not the source of truth for what a dispatch actually gets billed. A subscription-lane candidate (e.g. `claude-sonnet-5`) still gets an estimated `cost_usd_per_call` for ranking purposes even though the real dispatch bills nothing per call; don't confuse the two.

---

## 4. Routing-Rules Governance

### 4.1 Where a routing rule belongs

- **Config (data), not code**: which model is recommended for which task class, at what score/cost/tier — belongs in `routing_recommendations.yaml`. This is what changes when a benchmark refreshes or a model is deprecated; it should never require a code change to update.
- **Code (logic)**: how a task class is inferred from text (`classify_task`), how candidates are sorted (`_cost_aware_sort_key`), how a `model_id` maps to a CLI provider flag (`parse_route_model_id`) — these are structural and change rarely. A model-ID rename does not touch this layer.
- **Hard constraints, not recommendations**: which provider/model/lane combinations are simply forbidden or required regardless of score — belongs in `provider_constraints.yaml`, enforced independently of the router (`_filter_by_constraints` consults it, but the dispatch door's own pre-flight check is the actual backstop).

### 4.2 The SSOT chain

```
provider_constraints.yaml   — hard allow/forbid rules (kimi-via-cli-only, deprecated-glm-models, ...)
        │  "is this model/lane even permitted"
        ▼
wave7_models.yaml           — the model REGISTRY: litellm names, cost per Mtok, task_classes,
        │                      dispatch_allowed flags. Defines what currently EXISTS and is callable.
        │  "does this model exist, what does it cost"
        ▼
routing_recommendations.yaml — the model RECOMMENDATIONS: which of the existing/permitted models
                               performed well on which task class, per the benchmark that produced
                               the scores. Defines what's currently PREFERRED.
```

A model must clear all three to actually get recommended and dispatched: it must be permitted (`provider_constraints.yaml`), it must exist in the registry (`wave7_models.yaml`), and it must have — or inherit — a recommendation entry (`routing_recommendations.yaml`).

### 4.3 Provenance discipline

`routing_recommendations.yaml` scores are benchmark-derived, tied to whatever model generation ran the benchmark. When model IDs are bumped forward (e.g. the 2026-07-22 refresh: `claude-sonnet-4-6 → claude-sonnet-5`, `claude-opus-4-6 → claude-opus-4-8`, `glm-5-1 → glm-5-2`) without a re-benchmark, the file carries a header provenance notice plus an inline `# remapped ...` comment on every changed entry. A reader must be able to tell, from the file alone, that a score attached to the current model name may actually describe the prior generation's behavior. Bumping model IDs and re-benchmarking are two different tracks — do not let a routing config update quietly imply the recommendation is freshly measured. Score refresh is a separate, explicit follow-up.

### 4.4 Current models (as of this refresh)

The registry's current, non-deprecated model set: `claude-opus-4-8` (T0-tier), `claude-sonnet-5` (worker-tier), `glm-5.2`/`glm-5.3`/`glm-5.3-flash` (the three GLM versions admitted by the deprecated-glm-models constraint; `glm-5.2` remains default, `glm-5.1` and base `glm-5` stay blocked — see `provider_constraints.yaml` for the current allowlist), `kimi-k2-7` (via `kimi_cli`, OAuth), `deepseek-v4-pro` (and `deepseek-v4-flash`, cheaper/faster). `provider_constraints.yaml` is the authority on which of these are actually pinned for T0/worker roles — this document describes routing *recommendations*, not the pin policy itself.

---

## 5. Where the scores come from, and how they stay current

### 5.1 The benchmark is the memory

The router never starts cheap and escalates on failure. A model that cannot do the job is excluded before the first attempt, by the capability bar in §3.2. That only works if the bar rests on something measured, which is what the benchmark suite is for.

The important property: **the suite runs on your own task corpus, not on a public leaderboard.** A published score tells you how a model does on someone else's problems. A routing decision needs to know how it does on yours.

```
scripts/benchmark/
  run_benchmark.py          N models x M tasks, one result file per (model, task)
  prompts/                  the task corpus — 7 seeds, one per task class
  judge_quality.py          scores each response on quality, correctness, completeness
  analyze_results.py        ranks models per task class
  export_routing_matrix.py  writes routing_recommendations.yaml
```

The framework is fixed; the corpus is not. `--tasks-dir` and `--models-file` (or `VNX_BENCH_TASKS_DIR` / `VNX_BENCH_MODELS_FILE`) point the same harness at your own prompts and your own model list. The bundled seven are seeds to be replaced, not a benchmark to be trusted as-is.

Each result carries the measurements the ranking needs: a quality score, wall-clock duration, launch success, and cost per call. `composite_score` is derived from those, per task class, and `n` travels with it — a score without its sample size is not a score.

### 5.2 Why a static table is not enough

A benchmark is a snapshot. Models change underneath a stable name: a provider ships a new checkpoint, a quantisation changes, a context policy shifts. A model that cleared the capability bar in one generation can drop below it without anything in the config changing.

The static table cannot see that. Everything it knows was true on the day the suite ran.

### 5.3 The self-learning loop

**Status: design, under active test.** This section describes the intended mechanism. The live behavior today is the capability bar in §3.2 over a benchmark-derived table; §4.3 governs how that table's provenance is recorded.

Every dispatch already emits a receipt carrying model, task class, outcome and duration, and every review gate emits a result carrying blocking and advisory findings. That is the same shape of evidence the benchmark produces, generated continuously as a by-product of real work. The loop closes the gap between the two.

```
              ┌─────────────── BENCHMARK (periodic, offline) ────────────────┐
              │                                                               │
              │   own task corpus ──► run_benchmark ──► judge_quality         │
              │                              │                                │
              │                              ▼                                │
              │                       analyze_results                          │
              └──────────────────────────────┼────────────────────────────────┘
                                             ▼
                              routing_recommendations.yaml
                          (composite_score, cost, n — per model,
                                    per task class)
                                             │
                                             ▼
                              ┌──────────────────────────┐
                              │   CAPABILITY BAR  7.0    │
                              │  above: cheapest wins    │
                              │  below: never attempted  │
                              └──────────────────────────┘
                                             │
                                             ▼
                                      dispatch runs
                                             │
              ┌──────────────────────────────┴────────────────────────────────┐
              │                                                               │
              │   receipt:      model, task_class, outcome, duration           │
              │   gate result:  blocking / advisory findings                   │
              │                              │                                │
              │                              ▼                                │
              │        rolling rate per (model, task_class), carrying n        │
              │                              │                                │
              │                              ▼                                │
              │          compare against the benchmarked baseline              │
              │                              │                                │
              │                    drifted below band?                         │
              │                              │                                │
              │                              ▼                                │
              │                     ┌─────────────────┐                        │
              │                     │  RAISE SIGNAL   │                        │
              │                     └─────────────────┘                        │
              └──────────────────────────────┼────────────────────────────────┘
                                             ▼
                                   ┌───────────────────┐
                                   │    HUMAN GATE     │
                                   │  operator decides │
                                   └───────────────────┘
                                             │
                             ┌───────────────┴───────────────┐
                             ▼                               ▼
                  re-run the suite for              accept, and record
                  that model / task class           the reason
                             │
                             └────────► new scores ────────► back to the table
```

### 5.4 Four properties the loop must have

**It raises a signal; it does not rewrite the table.** A routing config that silently re-ranks itself destroys the reason deterministic routing exists. `route_reason` is audit evidence: a reader must be able to reconstruct, months later, why a dispatch landed on a given model. That reconstruction breaks the moment the table changes without a recorded decision. The loop's output is therefore a signal into the open-items plane, and a human decides.

**Degradation and instrumentation failure look identical from inside the loop.** This is the property that rules out automation, and the one most easily underestimated. A rolling rate is computed from labels, and a label can be wrong: a status vocabulary that carries `failed` next to `failure`, a dispatch that records a provider it did not run on, a run that fails completely and still exits 0. Every one of those presents as "this model is getting worse", and an automatic loop would act on it — attributing one model's work to another and demoting a model that never degraded. Nothing inside the loop can tell the two apart, because both arrive as the same drop in the same number. Only a reader who can go back to the underlying receipts can, which is why the decision belongs outside the loop.

**A drift signal is not a score.** Production outcomes and benchmark scores are different measurements: the benchmark is controlled, production is not. A model can look worse because the incoming work got harder, not because the model changed. Drift therefore triggers a re-benchmark; it does not overwrite `composite_score` directly. Mixing the two makes the table's provenance unreadable, which §4.3 exists to prevent.

**Every rate carries its n.** A model with three production dispatches has no measurable rate. The threshold that raises a signal is a threshold on the rate *and* on the sample size. A rate printed without its denominator is a number nobody can act on.

### 5.5 What the human decides

The signal is deliberately cheap and reversible; the table change is expensive and permanent. That asymmetry is the whole reason for the split: automate the cheap half, keep a person in front of the expensive one.

A raised signal names the pair, both numbers, and the sample size — for example: `glm-5.2` on `01_code_generation`, benchmarked at 9.1, rolling production rate below band over the last n dispatches. The operator resolves it one of three ways:

| Verdict | Action | What the table does |
|---|---|---|
| Real degradation | Re-run the benchmark suite for that model and task class | Updates from the new controlled measurement, provenance intact |
| Instrumentation failure | Fix the labelling or the vocabulary, then re-measure | Unchanged — the model was never the problem |
| Real but acceptable | Record the reason against the signal | Unchanged, with the decision on the record |

In all three cases the table only ever changes as the output of a benchmark run. One kind of measurement, one provenance chain, and a recorded reason for every movement.

### 5.6 Outcome vocabulary is the precondition

The loop consumes dispatch outcomes, so those outcomes must mean one thing. A single normalized status vocabulary across receipt producers is a prerequisite for §5.3, not an implementation detail of it: a rolling success rate computed over an inconsistent vocabulary measures the vocabulary, not the model. Normalisation lands first; the rolling rate lands on top of it.

---

## Cross-references

- Pin/lane enforcement: `scripts/lib/providers/provider_constraints.yaml`
- Model registry (cost, litellm names): `scripts/lib/providers/wave7_models.yaml`
- Recommendation data: `scripts/lib/providers/routing_recommendations.yaml`
- Dispatch door rules (D1–D12): `docs/core/DISPATCH_RULES.md`
- Terminal-pinned provider/model verification contract (a distinct, related concern): `docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md`
- Provider lane mechanics: `docs/core/PROVIDER_LANES.md`
