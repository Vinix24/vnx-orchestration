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

Cost accounting is lane-dependent, not model-dependent, and is decided by the dispatch door's D2 rule (`dispatch_plan.py`), independent of the router's own `cost_usd_per_call` estimates:

| Lane | `billing` label | Why |
|---|---|---|
| `claude` (tmux subscription) | `subscription` | OAuth subscription seat, not metered per call |
| `claude` headless (`allow_headless=true`) | `api_metered` | Explicit opt-in to API-key billing |
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

The registry's current, non-deprecated model set: `claude-opus-4-8` (T0-tier), `claude-sonnet-5` (worker-tier), `glm-5.2` (preferred GLM; `glm-5.1` remains available for explicit override), `kimi-k2-7` (via `kimi_cli`, OAuth), `deepseek-v4-pro` (and `deepseek-v4-flash`, cheaper/faster). `provider_constraints.yaml` is the authority on which of these are actually pinned for T0/worker roles — this document describes routing *recommendations*, not the pin policy itself.

---

## Cross-references

- Pin/lane enforcement: `scripts/lib/providers/provider_constraints.yaml`
- Model registry (cost, litellm names): `scripts/lib/providers/wave7_models.yaml`
- Recommendation data: `scripts/lib/providers/routing_recommendations.yaml`
- Dispatch door rules (D1–D12): `docs/core/DISPATCH_RULES.md`
- Terminal-pinned provider/model verification contract (a distinct, related concern): `docs/core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md`
- Provider lane mechanics: `docs/core/PROVIDER_LANES.md`
