# Scout Pre-Pass — Cheap-Model Recon Before the Permit

**Status**: Live, opt-in (`VNX_SCOUT_PREPASS`). Enabled for project `vnx-dev` via the operator
config DB since 2026-07-05.
**Ground-truth source**: `scripts/lib/scout_prepass.py` (453 lines) + `scripts/lib/dispatch_cli.py`
(door wiring) + `scripts/lib/intelligence_sources/scout_sketch.py` (consumer).

---

## What it does

Before a dispatch's permit is issued, a cheap key-auth model ranks the deterministic code-anchor
candidates for that dispatch into `INCLUDE` / `MAYBE` / `EXCLUDE` verdicts plus a short plan
sketch. The verdicts are written to a per-dispatch sidecar file. The intelligence selector then
renders that sidecar as a ranked, pointer-only grounding block injected into the worker's context —
instead of (or alongside) the raw deterministic anchor list.

This is a **re-ranker over grep-of-5 style deterministic candidates**, not a free-form search: the
scout model can only rank refs that `code_anchor_finder` already surfaced (anti-hallucination —
see [Anti-hallucination](#anti-hallucination-snap-to-candidates) below). It never reads or rewrites
the instruction file, so the permit / `instruction_sha256` TOCTOU contract is untouched.

## Why a sidecar, not an instruction edit

The sidecar is a **separate file keyed by dispatch_id** — `<state_dir>/scout/<dispatch_id>.json`.
It never mutates the instruction the permit was issued against. Reading it is best-effort and
fail-open: a missing or malformed sidecar returns `None` and the worker falls back to the
deterministic `code_anchor` injection it would have gotten anyway.

`scripts/lib/scout_prepass.py:1-33` (module docstring) states the design contract explicitly.

## Producer — wired in the door, before the permit

`scripts/lib/dispatch_cli.py:575-593`, inside `run_dispatch`: after `compile_plan()` succeeds and
before `issue_permit()`, `maybe_run_scout()` runs (skipped on `dry_run`). It is wrapped in a bare
`try/except` at the call site — a scout failure can never block the door.

```python
# scripts/lib/dispatch_cli.py:581-593
if not dry_run:
    try:
        from scout_prepass import maybe_run_scout
        maybe_run_scout(
            dispatch_id=plan.dispatch_id,
            instruction_text=vspec.instruction_text,
            dispatch_paths=[dp.path for dp in vspec.spec.dispatch_paths],
            state_dir=state_dir,
            task_class=getattr(vspec.spec, "task_class", None),
            lane=plan.lane,
        )
    except Exception as exc:
        logger.debug("[dispatch_cli] scout pre-pass skipped: %s", exc)
```

`maybe_run_scout()` itself (`scout_prepass.py:419-453`) is the single entry point and wraps its
whole body in `try/except Exception` — "best-effort — NEVER raises" per its docstring. Internally it:

1. Checks `scout_prepass_enabled()` — bails immediately if off.
2. Checks `_scout_gate_ok()` — scope/lane/task_class gate (below).
3. Computes `_candidate_refs()` from `code_anchor_finder.fetch_code_anchors()` — bails if empty.
4. Resolves `_scout_provider_name()` and builds a bounded prompt (`_build_scout_prompt`).
5. Calls the model via `_invoke_scout_model()` (the key-auth classifier harness).
6. Assembles + snaps the verdicts to real candidates (`_assemble_sidecar` → `_snap_refs`).
7. Writes the sidecar atomically (`write_scout_sidecar` — tmp file + `os.replace`).

## Enable flag

| Flag | Default | Resolution | Code |
|------|---------|------------|------|
| `VNX_SCOUT_PREPASS` | `"0"` (off), category `intelligence` | `scout_prepass_enabled()` reads it through `config_runtime.get_bool()` — honours an operator dashboard/DB toggle, falls back to env, falls back to the registry default | `scripts/lib/config_registry.py:51-53`, `scripts/lib/scout_prepass.py:268-272` |

**Current state for `vnx-dev`**: the registry default stays `"0"` — this is a per-project operator
decision, not a code default change. The `project_config` DB row shows:

```
project_id=vnx-dev  config_key=VNX_SCOUT_PREPASS  config_value=1  updated_by=operator  updated_at=2026-07-05T14:47:57.660Z
```

A fresh `vnx init` project starts with scout OFF, exactly like every other intelligence flag in the
registry. Flip it per-project the same way (dashboard config page, or `VNX_SCOUT_PREPASS=1` env for
a quick local trial).

## Scope/lane gate — `_scout_gate_ok()`

`scripts/lib/scout_prepass.py:285-304`. Even with the flag on, the scout skips:

- Dispatches touching fewer than `VNX_SCOUT_MIN_PATHS` paths (env, default `2` — `_DEFAULT_MIN_PATHS`, `scout_prepass.py:255`).
- Instructions shorter than 40 characters (`_MIN_INSTRUCTION_CHARS`, `scout_prepass.py:256`).
- `task_class` in `{docs_synthesis, channel_response, ops_watchdog}` — `_SKIP_TASK_CLASSES` (`scout_prepass.py:258`): no source to rank.
- `lane == "claude_headless"` — `_SKIP_LANES` (`scout_prepass.py:261`): the rare API-metered opt-in path; the scout never spends a call there.

**Note**: `VNX_SCOUT_PROVIDER` (`_scout_provider_name`, `scout_prepass.py:275-282`) and
`VNX_SCOUT_MIN_PATHS` (`scout_prepass.py:293`) are read via plain `os.environ.get`, **not** through
`config_runtime` — only the top-level enable flag is dashboard-flippable today. Flipping the
dashboard toggle does not change the provider or the path threshold; those still require an env
var. This split is intentional (infra-level tuning vs. an operator feature switch) but is worth
knowing if a dashboard flip doesn't change behavior the way you expect.

## Provider allowlist — never a subscription lane

`_ALLOWED_SCOUT_PROVIDERS = frozenset({"deepseek", "ollama", "gemini", "codex"})`
(`scout_prepass.py:265`). `_scout_provider_name()` default-denies anything outside this set
(including `haiku`, a subscription lane) and falls back to `deepseek`. This mirrors a hard
constraint stated directly in the module comments: the scout must NEVER run on a claude/subscription
lane, only a cheap key-auth classifier lane (`scout_prepass.py:248-253, 259-265`).

## Anti-hallucination: snap-to-candidates

The model receives an exact list of candidate refs and is told "Only use refs from the candidate
list" (`_build_scout_prompt`, `scout_prepass.py:317-336`). On the way back, `_snap_refs()`
(`scout_prepass.py:361-382`) discards any verdict whose `ref` is not literally one of the candidate
strings passed in. A model that invents a file:line range gets silently dropped, not injected.

## Path-containment contract

`_SAFE_DISPATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")` (`scout_prepass.py:55`) — a kimi-gate finding
during the original build. A `dispatch_id` becomes a filesystem path segment
(`<state_dir>/scout/<dispatch_id>.json`), so it is validated before ever touching the filesystem:
no separators, no `..`, no absolute-path markers. `scout_sidecar_path()` (`scout_prepass.py:82-88`)
raises `ValueError` on a hostile id; every read-path caller catches this and fails open
(`read_scout_sidecar`, `scout_prepass.py:99-104`).

## Sidecar schema (v1)

```json
{
  "schema_version": 1,
  "dispatch_id": "<id>",
  "generated_at": "<ISO8601>",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "include": [{"ref": "scripts/lib/foo.py:10-20", "why": "..."}],
  "maybe":   [{"ref": "scripts/lib/foo.py:50-60", "why": "..."}],
  "exclude": [{"ref": "...", "why": "..."}],
  "tests":   ["tests/test_foo.py"],
  "docs":    ["docs/foo.md"],
  "plan_sketch": "one or two line sketch"
}
```

(`scout_prepass.py:19-32`). The consumer rejects a sidecar whose `schema_version` doesn't match or
whose embedded `dispatch_id` doesn't match the one requested (`read_scout_sidecar`,
`scout_prepass.py:119-129`) — defense against a stale or misplaced file being misread as this
dispatch's context.

Defensive caps on write and read (both sides apply the same bounds, `scout_prepass.py:76-79`):
`_MAX_REFS_PER_BUCKET = 8`, `_MAX_AUX_ITEMS = 5`, `_MAX_WHY_CHARS = 120`, `_MAX_PLAN_CHARS = 300`.

## Consumer — `scout_sketch` intelligence source

`scripts/lib/intelligence_sources/scout_sketch.py:21-58` — `build_scout_sketch_item()` reads the
sidecar for the current dispatch via `read_scout_sidecar()`, renders it with
`format_scout_sketch()` (bounded to `SCOUT_SKETCH_MAX_CHARS = 1200`, `scout_prepass.py:73`,
pointer-only — file:line ranges, never code bodies), and returns an `IntelligenceItem` with
`confidence=1.0` (model-curated recon over deterministic candidates) and `evidence_count` equal to
the number of ranked pointers (`sidecar_evidence_count`, `scout_prepass.py:181-184`).

`EXCLUDE` verdicts are intentionally never rendered — they only shrink the `INCLUDE` set and would
add noise a worker shouldn't anchor on (`format_scout_sketch`, `scout_prepass.py:190-192`).

### Selection weight

Registered in `intelligence_selector.py`'s direct-injection source table as the **last** class
(`_DIRECT_SOURCES`, `intelligence_selector.py:75-86`) — its eviction order is the longest, so it
survives a budget squeeze over the other direct classes while still being droppable as a last
resort. Under `VNX_INTEL_RANK_THEN_BUDGET` (see below), its composite-score class weight is
**1.45** — above `proven_pattern` (1.2) and `prior_round_finding` (1.4), below `failure_prevention`
(1.5) (`_CLASS_WEIGHT`, `intelligence_selector.py:94-105`).

---

## Rank-then-budget (a related, separately-gated build step)

Scout injection interacts with a second opt-in flag: `VNX_INTEL_RANK_THEN_BUDGET` (default `"0"`,
`config_registry.py:60-62`). When off (the class-priority default), items are dropped by fixed
class order until the budget fits. When on, `intelligence_selector._relevance_score()`
(`intelligence_selector.py:131-146`) replaces that with a composite-score knapsack:

```
score = confidence * (1 + tag_overlap) * recency_decay * class_weight
```

- `tag_overlap` — the item's own `scope_tags` plus tags derived on-the-fly from
  `vnx_tag_vocabulary.derive_tags()` over its title+content, intersected with the query scope
  (`intelligence_selector.py:138-143`).
- `recency_decay` — 30-day half-life, floored at `0.3` (`_recency_decay`,
  `intelligence_selector.py:114-128`): `max(0.3, 0.5 ** (age_days / 30.0))`.
- `class_weight` — the `_CLASS_WEIGHT` table above.
- `_RESERVED_CLASSES = frozenset({"failure_prevention"})` (`intelligence_selector.py:106`) gets
  first claim on the budget regardless of score, so a cheap high-scoring anchor can never evict a
  critical failure-prevention rule.

Both flags are independent: scout can inject its sketch under the legacy class-priority eviction
too. Rank-then-budget just changes *which* items survive a tight budget, using the tag vocabulary
documented in [`TAG_TAXONOMY.md`](TAG_TAXONOMY.md).

---

## Claim-to-code map

| Claim | File:line |
|-------|-----------|
| Scout producer wired in the door, before permit issuance, fail-open | `scripts/lib/dispatch_cli.py:575-593` |
| `maybe_run_scout()` — opt-in, gated, never raises | `scripts/lib/scout_prepass.py:419-453` |
| `scout_prepass_enabled()` reads `VNX_SCOUT_PREPASS` via `config_runtime` | `scripts/lib/scout_prepass.py:268-272` |
| `VNX_SCOUT_PREPASS` registry default `"0"`, category `intelligence` | `scripts/lib/config_registry.py:51-53` |
| Per-project operator override (`vnx-dev` = `1` since 2026-07-05) | `runtime_coordination.db: project_config` table |
| `_scout_gate_ok()` — scope/lane/task_class gate | `scripts/lib/scout_prepass.py:285-304` |
| `VNX_SCOUT_PROVIDER` / `VNX_SCOUT_MIN_PATHS` read via raw env (not `config_runtime`) | `scripts/lib/scout_prepass.py:275-282, 293` |
| Provider allowlist — never a subscription lane | `scripts/lib/scout_prepass.py:265` |
| Anti-hallucination snap-to-candidates | `scripts/lib/scout_prepass.py:361-382` |
| Path-containment `_SAFE_DISPATCH_ID` (kimi-gate finding) | `scripts/lib/scout_prepass.py:43-55` |
| Sidecar schema v1 | `scripts/lib/scout_prepass.py:19-32` |
| Schema/dispatch-id mismatch rejected on read | `scripts/lib/scout_prepass.py:119-129` |
| Sidecar defensive caps | `scripts/lib/scout_prepass.py:73-79` |
| Consumer renders sketch, confidence 1.0, pointer-only | `scripts/lib/intelligence_sources/scout_sketch.py:21-58` |
| EXCLUDE verdicts never rendered | `scripts/lib/scout_prepass.py:190-192` |
| `scout_sketch` in direct-injection source table (last, longest eviction order) | `scripts/lib/intelligence_selector.py:75-86` |
| `scout_sketch` class-weight 1.45 | `scripts/lib/intelligence_selector.py:94-105` |
| `VNX_INTEL_RANK_THEN_BUDGET` default `"0"` | `scripts/lib/config_registry.py:60-62` |
| Composite relevance score formula | `scripts/lib/intelligence_selector.py:131-146` |
| 30-day half-life recency decay, floor 0.3 | `scripts/lib/intelligence_selector.py:114-128` |
| `_RESERVED_CLASSES = {"failure_prevention"}` | `scripts/lib/intelligence_selector.py:106` |

---

*Doc written 2026-07-05 for the docs-intelligence sweep (PRs #1001–#1017 drift-brief).*
*Dispatch-ID: D-docs-intelligence*
