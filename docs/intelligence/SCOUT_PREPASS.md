# Scout Pre-Pass: Cheap-Model Recon Before the Permit

**Status**: shipped, opt-in, off by default (`VNX_SCOUT_PREPASS=0`). A new project starts with the scout off.
**Ground-truth source**: `scripts/lib/scout_prepass.py` + `scripts/lib/dispatch_cli.py`
(door wiring, `run_dispatch`) + `scripts/lib/intelligence_sources/scout_sketch.py` (consumer).

---

## What it does

Before a dispatch's permit is issued, a cheap key-auth model ranks the deterministic code-anchor
candidates for that dispatch into `INCLUDE` / `MAYBE` / `EXCLUDE` verdicts plus a short plan
sketch. The verdicts are written to a per-dispatch sidecar file. The intelligence selector then
renders that sidecar as a ranked, pointer-only grounding block injected into the worker's context,
instead of (or alongside) the raw deterministic anchor list.

This is a **re-ranker over grep-of-5 style deterministic candidates**, not a free-form search: the
scout model can only rank refs that `code_anchor_finder` already surfaced (anti-hallucination, see
[Anti-hallucination](#anti-hallucination-snap-to-candidates) below). For a dispatch without
paths, `_discover_candidate_paths` first `git grep`s the repo for the instruction's salient terms
and keeps at most five files. The scout never reads or rewrites the instruction file, so the
permit / `instruction_sha256` TOCTOU contract is untouched.

## Why a sidecar, not an instruction edit

The sidecar is a **separate file keyed by dispatch_id**, `<state_dir>/scout/<dispatch_id>.json`.
It never mutates the instruction the permit was issued against. Reading it is best-effort and
fail-open: a missing or malformed sidecar returns `None` and the worker falls back to the
deterministic `code_anchor` injection it would have gotten anyway.

The module docstring of `scripts/lib/scout_prepass.py` states the design contract.

## Producer: wired in the door, before the permit

In `run_dispatch` (`scripts/lib/dispatch_cli.py`), after the lane verdict and before the permit is
issued, `maybe_run_scout()` runs. It is skipped on `dry_run`. The call site catches every exception
and records it through `_record_bookkeeping_failure`: a scout failure can never block the door.

`maybe_run_scout()` is the single entry point and wraps its body in `try/except Exception`
("best-effort, NEVER raises" per its docstring). Internally it:

1. Checks `scout_prepass_enabled()` and bails immediately if off.
2. Returns the existing sidecar when `is_scouted()` finds one made against the same instruction
   (SHA-256 of the instruction text). A changed instruction is scouted again.
3. Checks `_scout_gate_ok()`, the scope/lane/task_class gate (below).
4. Computes `_candidate_refs()` from `code_anchor_finder.fetch_code_anchors()` and bails if empty.
5. Resolves `_scout_provider_name()` and builds a bounded prompt (`_build_scout_prompt`).
6. Calls the model through `_invoke_scout_model()` (the key-auth classifier providers).
7. Assembles the verdicts and snaps them to real candidates (`_assemble_sidecar`, `_snap_refs`).
8. Writes the sidecar atomically (`write_scout_sidecar`: tmp file + `os.replace`) and appends one
   receipt to `scout_receipts.ndjson` in the state dir (`_emit_scout_receipt`: provider, model,
   `cost_usd`, duration, sidecar path, anchor counts). An unknown cost is `null`, never 0.

`scout_pending_sweep()` can scout the staged bundles under `dispatches/pending/` ahead of time. On
main nothing calls it: the door is the only producer (`git grep scout_pending_sweep` finds only the
definition).

## Enable flag

| Flag | Default | Resolution | Code |
|------|---------|------------|------|
| `VNX_SCOUT_PREPASS` | `"0"` (off), category `intelligence` | `scout_prepass_enabled()` reads it through `config_runtime.get_bool()`: an operator config toggle wins, then the environment, then the registry default | `CONFIG_REGISTRY` in `scripts/lib/config_registry.py` |

Turn it on per project through the config control plane, or with `VNX_SCOUT_PREPASS=1` for a local
trial. When the family of the scout provider (`VNX_SCOUT_PROVIDER`) is switched off, `vnx providers`
reports that scout calls will be skipped.

## Scope/lane gate: `_scout_gate_ok()`

`_scout_gate_ok()` in `scripts/lib/scout_prepass.py`. Even with the flag on, the scout skips:

- Instructions shorter than the applicable minimum: 40 characters when the dispatch has paths
  (`_MIN_INSTRUCTION_CHARS`), 80 characters when it does not (`_MIN_INSTRUCTION_CHARS_PATHLESS`).
  A pathless dispatch is scouted by instruction-driven discovery, so it needs a richer instruction.
- `task_class` in `{docs_synthesis, channel_response, ops_watchdog}` (`_SKIP_TASK_CLASSES`): no
  source to rank.
- `lane == "claude_headless"` (`_SKIP_LANES`): the only claude lane, which runs on the subscription.
  The scout never spends a call there.

## Provider allowlist: never a subscription lane

`_ALLOWED_SCOUT_PROVIDERS = frozenset({"deepseek", "ollama", "gemini", "codex"})`.
`_scout_provider_name()` reads `VNX_SCOUT_PROVIDER` through `config_runtime.get()` and
default-denies anything outside this set (including `haiku`, a subscription lane). It falls back to
`deepseek`. The scout must never run on a claude/subscription lane, only on a key-auth classifier
lane.

If the operator switched the provider family off (`vnx providers`), `_invoke_scout_model` makes no
call, logs one warning and counts the skip in `SCOUT_SWITCH_SKIPS`. The dispatch continues with the
deterministic anchors.

## Anti-hallucination: snap-to-candidates

The model receives an exact list of candidate refs and is told "Only use refs from the candidate
list" (`_build_scout_prompt`). On the way back, `_snap_refs()` discards any verdict whose `ref` is not literally one of the candidate
strings passed in. A model that invents a file:line range gets silently dropped, not injected.

## Path-containment contract

`_SAFE_DISPATCH_ID = re.compile(r"^[A-Za-z0-9._-]+$")`, found by a review gate during the original
build. A `dispatch_id` becomes a filesystem path segment
(`<state_dir>/scout/<dispatch_id>.json`), so it is validated before ever touching the filesystem:
no separators, no `..`, no absolute-path markers. `scout_sidecar_path()` raises `ValueError` on a
hostile id; the read path catches this and fails open (`read_scout_sidecar`).

## Sidecar schema (v1)

```json
{
  "schema_version": 1,
  "dispatch_id": "<id>",
  "generated_at": "<ISO8601>",
  "provider": "deepseek",
  "model": "<model name reported by the provider>",
  "scouted_instruction_sha256": "<sha256 of the instruction>",
  "include": [{"ref": "scripts/lib/foo.py:10-20", "why": "..."}],
  "maybe":   [{"ref": "scripts/lib/foo.py:50-60", "why": "..."}],
  "exclude": [{"ref": "...", "why": "..."}],
  "tests":   ["tests/test_foo.py"],
  "docs":    ["docs/foo.md"],
  "plan_sketch": "one or two line sketch"
}
```

(module docstring of `scout_prepass.py`, plus `scouted_instruction_sha256`, which `is_scouted` uses).
The consumer rejects a sidecar whose `schema_version` doesn't match or whose embedded `dispatch_id`
doesn't match the one requested (`read_scout_sidecar`), defense against a stale or misplaced file being misread as this
dispatch's context.

Defensive caps on write and read (both sides apply the same bounds):
`_MAX_REFS_PER_BUCKET = 8`, `_MAX_AUX_ITEMS = 5`, `_MAX_WHY_CHARS = 120`, `_MAX_PLAN_CHARS = 300`.

## Consumer: `scout_sketch` intelligence source

`build_scout_sketch_item()` in `scripts/lib/intelligence_sources/scout_sketch.py` reads the
sidecar for the current dispatch via `read_scout_sidecar()`, renders it with
`format_scout_sketch()` (bounded to `SCOUT_SKETCH_MAX_CHARS = 1200`, pointer-only, file:line ranges, never code bodies), and returns an `IntelligenceItem` with
`confidence=1.0` (model-curated recon over deterministic candidates) and `evidence_count` equal to
the number of ranked pointers (`sidecar_evidence_count`).

`EXCLUDE` verdicts are intentionally never rendered, they are simply omitted from the sketch
(nothing removes an `INCLUDE` pointer for also appearing in `EXCLUDE`), since rendering them would
add noise a worker shouldn't anchor on (`format_scout_sketch`).

### Selection weight

Registered in `intelligence_selector.py`'s direct-injection source table as the **last** class
(`_DIRECT_SOURCES` in `scripts/lib/intelligence_selector.py`), its eviction order is the longest, so it
survives a budget squeeze over the other direct classes while still being droppable as a last
resort. Under `VNX_INTEL_RANK_THEN_BUDGET` (see below), its composite-score class weight is
**1.45**, above `proven_pattern` (1.2) and `prior_round_finding` (1.4), below `failure_prevention`
(1.5) (`_CLASS_WEIGHT`).

---

## Rank-then-budget (a related, separately-gated build step)

Scout injection interacts with a second opt-in flag: `VNX_INTEL_RANK_THEN_BUDGET` (default `"0"`,
`CONFIG_REGISTRY`). When off (the class-priority default), items are dropped by fixed
class order until the budget fits. When on, `intelligence_selector._relevance_score()` replaces that with a composite-score knapsack:

```
score = confidence * (1 + tag_overlap) * recency_decay * class_weight
```

- `tag_overlap`: the item's own `scope_tags` plus tags derived on-the-fly from
  `vnx_tag_vocabulary.derive_tags()` over its title+content, intersected with the query scope.
- `recency_decay`: 30-day half-life, floored at `0.3` (`_recency_decay`): `max(0.3, 0.5 ** (age_days / 30.0))`.
- `class_weight`: the `_CLASS_WEIGHT` table above.
- `_RESERVED_CLASSES = frozenset({"failure_prevention"})` gets
  first claim on the budget regardless of score, so a cheap high-scoring anchor can never evict a
  critical failure-prevention rule.

Both flags are independent: scout can inject its sketch under the legacy class-priority eviction
too. Rank-then-budget just changes *which* items survive a tight budget, using the tag vocabulary
documented in [`TAG_TAXONOMY.md`](TAG_TAXONOMY.md).

---

---

*Verified against `origin/main` at c41411fea on 2026-11-12.*
