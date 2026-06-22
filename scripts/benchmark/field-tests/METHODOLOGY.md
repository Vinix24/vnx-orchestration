# Field-tests benchmark — methodology

> How the VNX field-tests model benchmark is built, how a run executes, and the
> specific mechanisms that exist to score every model **as fairly as possible**.
> Written for AI peers who want to judge the methodology against the source, not
> take the numbers on faith. Every claim below cites the file and line it lives in.

The benchmark compares frontier coding agents on **real engineering tasks** drawn
from merged PRs in production systems, run through **each model's own native
tooling**, scored by a mix of **deterministic verifiers** and a **cross-provider
LLM-judge panel**. It is deliberately small-n and high-signal: a handful of tasks
that discriminate, run several times, with the failure modes treated as
first-class engineering problems rather than swept into an average.

---

## 1. The standing principle: 100% fair completion

The bar the whole harness is built around:

> **No model may score low for an infrastructure reason. Only genuine,
> multi-attempt incapacity counts as a low score.**

A rate-limit, a depleted API balance, a CLI that exits non-zero after writing a
correct file, a cold-start timeout — none of these are the model's fault, and
none are allowed to land as a `0`. Everything in §4 (retries, the DNF
discipline, the credit pre-flight, the deliverable-on-failure path) is machinery
in service of that one principle. When a cell scores low, it should be because
the model could not do the work across repeated honest attempts — nothing else.

This is also why results are published with the failure modes visible (§5)
rather than hidden: a benchmark that silently turns infra noise into model
scores is worse than no benchmark.

---

## 2. How the matrix is composed

A run is the cross-product **lanes × tasks × replications**. Each `(lane, task,
replication)` triple is one **cell**; one cell produces one scored row.

### Lanes

A *lane* is a specific model reached through a specific tool-path. Lanes are
declared per tier in [`tasks.yaml`](tasks.yaml) (the `lanes:` anchor) and
resolved against [`../models.yaml`](../models.yaml) in
[`run_field_tests.py:65,81`](runners/run_field_tests.py). The current set spans
four families:

| Family | Lanes | Tool-path |
|---|---|---|
| Claude (subscription) | `claude-opus-4-8/4-7/4-6`, `claude-sonnet-4-6` | interactive `claude` CLI in an isolated worktree |
| Codex | `codex-gpt-5-4`, `codex-gpt-5-5` | `codex exec --json` |
| Open models (runner) | `glm-5`, `glm-5-1`, `glm-5-2`, `kimi-k2-7-code` | provider CLI / flat agentic runner |
| Open models (harness) | `glm-5-harness`, `glm-5-1-harness`, `glm-5-2-harness`, `deepseek-v4-pro-harness`, `deepseek-v4-flash-harness` | the **full `claude` CLI harness** pointed at the model via a local proxy |

The **harness-vs-runner split is deliberate and is itself a measurement.** The
same model (e.g. GLM-5.2) is run twice: once through a thin agentic loop
(`glm-5-2`) and once through the complete `claude` CLI tool-harness
(`glm-5-2-harness`, a local litellm proxy exposing an Anthropic-compatible
endpoint backed by OpenRouter). The two are scored as separate lanes because the
harness materially changes outcomes — and a benchmark that hid that behind one
number would mislead. We label which is which and never average across them.

### Tiers and tasks

Tasks live in `tasks/<tier>/<NN_slug>/` and are registered in
[`tasks.yaml`](tasks.yaml). Each tier sets its own replication count and
wallclock deadline:

| Tier | Character | Tasks | n | Source of tasks |
|---|---|---|---|---|
| `t1_trivial` | mechanical refactor, ~5 min | 3 | real merged PRs |
| `t2_medium` | multi-file + tests + migration | 3 | real merged PRs |
| `t3_complex` | cross-module state, security boundaries | 3 | real merged PRs |
| `t4_frontier` | graded 0–5 discriminators (naive ≈ 2–3, deep ≈ 5) | 2 | SWE-bench-style + adversarial |
| `t5_review_design` | review/design *character*, not code-writing | 2 | planted-defect + system-design |

Every task's `source:` field cites the real PR it was distilled from
([`tasks.yaml`](tasks.yaml) `source` keys). These are not synthetic puzzles —
they are real engineering changes that shipped, which is what makes the
correctness bar meaningful.

**t4 and t5 are the discriminators.** t1–t3 verify that a model can do bounded
work at all; t4/t5 are graded rubrics designed so a shallow answer scores ~2–3
and a deep one scores ~5, which is where the top models separate.

### t6 — internal, anonymized on publication

A sixth tier, **real-world code review on two private production-scale codebases
(~300K+ LOC combined)**, lives in a **gitignored overlay**,
`tasks.local.yaml`, merged on top of the public config at
[`run_field_tests.py:39-52`](runners/run_field_tests.py). Its instructions and
verifiers reference proprietary repositories, so the tier's task definitions are
**never committed or published**. When t6 *scores* are published they are
anonymized: the codebases are described only by scale, and the task ids are
relabeled (`real_review_A` / `real_review_B`). The free-text score fields are
abstract by construction (detection counts, coverage ratios) and carry no code.

---

## 3. How a run executes

```
run_field_tests.main()
  └─ filter_cells()                      # lanes × tasks × reps  → cell list
  └─ OpenRouter credit pre-flight        # abort before burning a run (§4)
  └─ ThreadPoolExecutor(--parallel)
       └─ _run_with_retry()              # per cell, up to --max-retries
            └─ run_one_cell()
                 ├─ lane_adapter.dispatch()      # isolation + skill frame + provider route
                 │     └─ provider_dispatch.py / tmux_interactive_dispatch.py
                 │            └─ worker runs in an ISOLATED git worktree
                 │                 └─ writes a unified report  → DispatchResult
                 └─ scorer.score_cell()          # verify.py + judge → CellScore
  └─ reporter.write_raw_csv / summary / per-lane / methodology
```

- **Dispatch.** [`lane_adapter.dispatch()`](runners/lane_adapter.py) builds a
  `dispatch_id`, applies the seed guard (§4), wraps the instruction in a uniform
  skill frame (§4), and routes to the lane's tool-path: Claude lanes via the
  interactive/headless `claude` CLI
  ([`lane_adapter.py:96,138`](runners/lane_adapter.py)); every other provider via
  [`scripts/lib/provider_dispatch.py`](../../lib/provider_dispatch.py)
  ([`lane_adapter.py:261`](runners/lane_adapter.py)).
- **Isolation.** Each dispatch runs the worker in a **fresh git worktree** outside
  the repo (`~/.vnx-bench-worktrees`,
  [`lane_adapter.py:287`](runners/lane_adapter.py)), based on the bench HEAD so it
  carries the committed task seed but cannot leak edits back into the shared
  checkout.
- **Scoring.** [`scorer.score_cell()`](runners/scorer.py) verifies the checkout
  the worker actually wrote to (not the repo root —
  [`scorer.py:321-325`](runners/scorer.py)), runs the task's `verify.py`, then the
  judge panel, and emits a `CellScore`.
- **Outputs.** [`reporter.py`](runners/reporter.py) writes `raw.csv` (one row per
  cell) plus `summary.md`, `per-lane.md`, and a `methodology.md` stamp per run.

---

## 4. Fairness mechanisms (what keeps the score honest)

Each item below is a concrete safeguard with its source location.

### Worker isolation, fail-loud
Every cell runs in its own ephemeral worktree, and isolation failure is fatal,
never silent: `VNX_BENCH_REQUIRE_ISOLATION=1` is set on both the headless and
tmux paths ([`lane_adapter.py:117,160`](runners/lane_adapter.py)) so a worker is
**never** run in the shared checkout. This prevents one cell's edits from
contaminating another's baseline.

### Dirty-seed guard
Before any cell dispatches, [`_main_seed_status()`](runners/lane_adapter.py#L350)
runs `git status --porcelain` over the task's seed paths; if the main-repo seed
is dirty the cell is **refused** with a DNF rather than scored against a polluted
baseline ([`lane_adapter.py:387-397`](runners/lane_adapter.py)). This is why the
repo tree must be clean while a run is live.

### DNF discipline — a no-show scores zero, not a baseline
If the dispatcher itself failed, [`score_cell()`](runners/scorer.py#L277) records
`composite = 0`. Without this guard, `verify.py` would run against the
materialized seed (the reference starting point) and award the naive baseline
(~3.5/5) to a worker that did **nothing**
([`scorer.py:284-319`](runners/scorer.py)). A no-show deserves a zero.

### Deliverable-on-failure, gated by real wallclock
Some harness lanes produce a **correct** deliverable but the `claude` CLI exits
`rc=1` (the open model doesn't close the agentic loop cleanly), so
`dispatch_result.success` is `False` even though the work is done. The
`VNX_BENCH_SCORE_DELIVERABLE_ON_FAILURE` flag lets the scorer fall through and
verify the on-disk deliverable instead of auto-DNF — **but only when the cell
actually ran** (`_ran_for_real = wallclock >= 5.0s`,
[`scorer.py:296-304`](runners/scorer.py)). An immediate-exit (rate-limit,
session-create failure) makes no real attempt and leaves only the seed, so it
stays a DNF. This credits genuine work without ever crediting a no-op.

### Retry on infra failure, with backoff
[`_run_with_retry()`](runners/run_field_tests.py#L291) retries a DNF cell up to
`--max-retries` (default 2) with exponential backoff
([`run_field_tests.py:307-325`](runners/run_field_tests.py)). The success
discriminator is **report-presence + non-zero composite**, deliberately *not*
wallclock — a fast lane that legitimately finishes a bounded task in 2–3s must
not be re-run as if it failed
([`run_field_tests.py:291-303`](runners/run_field_tests.py)).

### DNF detection is report-based, not duration-based
[`_load_dnf_cells_from_csv()`](runners/run_field_tests.py#L155) classifies a cell
as DNF only on hard signals: an explicit `DNF:` evidence prefix, a
**hang-at-deadline** (wallclock ≥ 90% of the task's *own* deadline with
composite < 4.5), or a **missing report** for a sub-5s cell. Short wallclock
*alone* is never enough — fast is not the same as failed
([`run_field_tests.py:184-220`](runners/run_field_tests.py)). `--retry-from`
re-runs only those cells and **merges** them back with the healthy prior rows so
no data is lost ([`run_field_tests.py:462-469`](runners/run_field_tests.py)).

### Credit pre-flight (the silent-402 trap)
OpenRouter lanes return a `402` that, from outside, reads identically to a
throttle — a depleted balance would otherwise instant-reject every cell and look
like model failure. The run aborts loudly up front when every lane is an
OpenRouter lane and credits are depleted
([`run_field_tests.py:435-456`](runners/run_field_tests.py),
[`check_openrouter_credits.py`](runners/check_openrouter_credits.py)).

### Subscription rate-limit avoidance
`--claude-serial` runs Claude subscription lanes sequentially while other lanes
stay parallel, to dodge a measured subscription rate-limit cliff
([`run_field_tests.py:357-374`](runners/run_field_tests.py)). The cliff is an
account constraint, not model behaviour, so it is engineered around rather than
scored.

### Uniform prompt frame for every lane
Skill injection is identical across **all** providers — Claude, Codex, Kimi,
GLM, DeepSeek alike get the same structured role/SOP/resources frame via
[`build_structured_prompt()`](../../lib/skill_prefix.py)
([`lane_adapter.py:399-404`](runners/lane_adapter.py)). No lane gets a
hand-tuned prompt advantage.

### Deterministic verification first, judge only where it must
Correctness and completeness are **programmatic**, never vibes: each task ships a
`verify.py` returning `{pass, evidence, details}`, run by
[`scorer._load_verify_module()`](runners/scorer.py#L52) — pytest pass-counts, SQL
constraint checks, adversarial-attack matrices, hang detection (the
`verify_type` per task in [`tasks.yaml`](tasks.yaml), rules in
[`scoring.yaml`](scoring.yaml)). The LLM-judge is used for **one** dimension only
— `code_quality` (10% weight) — the thing programmatic checks genuinely cannot
measure.

### Cross-provider judge panel
The judge is not a single model marking its own family's homework.
[`_llm_judge_code_quality()`](runners/scorer.py#L175) runs **Opus + Kimi in
parallel**, averages them, and flags disagreement when the spread exceeds 1.5
([`scorer.py:175-216`](runners/scorer.py)). The judge prompt is **identical for
every cell** ([`scorer.py:108-125`](runners/scorer.py)) so the comparison is
fair, and a both-judges-failed outcome returns `judge_unavailable` — explicitly
distinct from a real `0/5` verdict ([`scorer.py:207-208`](runners/scorer.py)).

### Composite weighting
Five dimensions, fixed weights ([`scoring.yaml`](scoring.yaml),
[`scorer.py:357-364`](runners/scorer.py)):

| Dimension | Weight | Source |
|---|---|---|
| correctness | 0.40 | `verify.py` (deterministic) |
| completeness | 0.20 | `verify.py` (files written) |
| cost_efficiency | 0.15 | cost from report frontmatter |
| wallclock_efficiency | 0.15 | dispatch start/end |
| code_quality | 0.10 | cross-provider judge panel |

---

## 5. What the scores rest on — and what they do not claim

A benchmark is only as honest as its caveats. These are surfaced, not buried.

- **Small n.** Tiers run n = 2–3. This is a high-signal, low-volume design: the
  tasks are chosen to discriminate, not to drive confidence intervals. Treat
  cell scores as point estimates, read the per-lane spread before concluding.
- **The judge is an LLM.** `code_quality` (10%) is model-judged. The panel +
  identical-prompt + disagreement-flag design (§4) reduces but does not eliminate
  judge bias. The other 90% of the composite is deterministic.
- **Cost/token zeros are "not measured," not "free."** Subscription lanes report
  no per-call usage; `_extract_tokens_from_report()` returns `0` there and the
  scorer treats `0` as unmeasured, not zero-work
  ([`scorer.py:237-274`](runners/scorer.py)). Do not read cost_efficiency across
  metered and unmetered lanes as a like-for-like.
- **Harness ≠ runner.** GLM/DeepSeek harness lanes run through the `claude`
  tool-harness; the flat-runner lanes do not. They are different systems and are
  reported as different lanes. The gap between `glm-5-2` and `glm-5-2-harness` is
  a finding, not noise.
- **Known limitations are tracked in code, not hidden.** Several edge cases are
  documented inline and deferred with ticket references: headless-lane retry
  checkout-reset (`F4`), the prefix-match report-presence heuristic (`F5`), and
  scoring-worktree leak on teardown (`F8`) — all at
  [`run_field_tests.py:299-303`](runners/run_field_tests.py),
  [`:198-204`](runners/run_field_tests.py), and
  [`run_one_cell()`](runners/run_field_tests.py#L135). One task (`08_ssrf`) has a
  known cross-lane filename-convergence effect that is footnoted wherever its
  numbers appear.
- **Provider constraints are pinned.** Lane routing honours hard provider rules
  (open models via their sanctioned endpoints only; no Anthropic SDK; CLI-driven
  throughout). The benchmark measures models under the same routing the
  production runtime uses, not a privileged path.

---

## 6. Source map

| Concern | File |
|---|---|
| Orchestration, retries, DNF detection, credit pre-flight | [`runners/run_field_tests.py`](runners/run_field_tests.py) |
| Lane routing, isolation, seed guard, skill frame | [`runners/lane_adapter.py`](runners/lane_adapter.py) |
| Verify + judge panel + composite | [`runners/scorer.py`](runners/scorer.py) |
| Output writers | [`runners/reporter.py`](runners/reporter.py) |
| Dimensions, weights, judge rubric | [`scoring.yaml`](scoring.yaml) |
| Tiers, tasks, lanes, deadlines | [`tasks.yaml`](tasks.yaml) |
| Per-provider dispatch | [`../../lib/provider_dispatch.py`](../../lib/provider_dispatch.py) |
| Uniform skill frame | [`../../lib/skill_prefix.py`](../../lib/skill_prefix.py) |
| Internal t6 overlay (gitignored) | `tasks.local.yaml` (not published) |

Every number in a published matrix traces back through `raw.csv` → `scorer.py`
→ a task's `verify.py`. If a claim in a write-up is not reproducible from those,
treat it as wrong.
