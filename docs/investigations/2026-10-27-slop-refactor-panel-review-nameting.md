# Slop-refactor-panel-review: nameting of the four goal points

Dispatch-ID: dlv-3dae497ecb87
**Model**: deepseek-flash
**Provider**: deepseek-harness

Track `slop-refactor-panel-review`. This document measures; it changes no code.

- **Measured on:** `origin/main` `7877dbea` (2026-10-27, "fix(harness): allow plugins/store builtin marker files in the harness config dir (OI-2373) (#2249)").
- **Baseline:** `1ca67db2` — the commit the plan's reproduction line names for the 24-10 count.
- **Plan:** `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/slop-refactor-panel-review.md`, revision 2 (25-10), section *Doel* (four points) and section *Meetcontract S1*.
- **Instrument:** `scripts/lib/refactor_census.py` (S1, `dlv-dec2cd2b8e75`, PR #2228).
- **Sibling evidence:** the five track reports `dlv-dec2cd2b8e75` (S1), `dlv-92a8405367ec` (S2), `dlv-9aacc366daf4` (S3), `dlv-7ebd662f8161` (S4), `dlv-edf3a609835a` (S5) under `$VNX_DATA_DIR/unified_reports/`.

Every number below carries the command that produced it. Where a class is heuristic rather than deterministic, the plan's contract says so and this document repeats the label.

## 1. Verdicts

| # | Goal point (plan revision 2, *Doel*) | Verdict | Owner of what is missing |
|---|---|---|---|
| 1 | `python3 -m refactor_census --paths <set>` counts the eight classes in a table plus a machine-readable summary | **met** | — |
| 2 | `close_track_if_done` and `run_reconcile` are under 200 raw lines with the same behaviour | **met** | — |
| 3 | One panel sweep read the census; every surviving finding is a deliverable or an OI with an owner | **not met** | T0 |
| 4 | The rest of the census is in this nameting with an owner per candidate | **met** | — (but see §6: 43 of 57 long functions, and 413 of 418 restating comments, fall to "T0 to decide") |

The track cannot close on this nameting alone: goal point 3 has no evidence that it ran, and only the T0 can run it (§2.3). Points 1, 2 and 4 are met.

## 2. The four goal points

### 2.1 Goal point 1 — the census counts the eight classes

> "`python3 -m refactor_census --paths <set>` (nieuw, `scripts/lib/refactor_census.py`) telt per klasse: functies naar ruwe en uitvoerbare lengte ..., exacte duplicaten, dode module-functies, commentaar dat de volgende regel herhaalt, dubbele stapbanners, versie-achtervoegsels in paden en defensieve markers. Een tabel en een machineleesbare samenvatting."

**Commands and output** (all run from the repository root):

```bash
$ python3 scripts/lib/refactor_census.py --commit 7877dbea
Refactor census — 7877dbea

python files              2413  47990 functions
unparsed                     0
long functions             311  raw>=100 311, raw>=150 103, raw>=200 57, raw>=300 18, raw>=400 6; executable>70 366
exact duplicates            18  45 functions in groups
doubled step banners         0
version-suffixed paths      40
defensive markers            7
dead module functions       17  11 outside seeds, 6 in seeds
restating comments         418  188 files
# exit 0
```

```bash
$ PYTHONPATH=scripts/lib python3 -m refactor_census --commit 7877dbea --json > /tmp/census_main.json
# exit 0 — keys: source, commit, tracked_files, python_files, functions, unparsed,
# long_functions{counts,executable_over_70,function_count,items}, exact_duplicates{group_count,function_count,items},
# doubled_banners, version_suffixed_paths, defensive_markers, dead_module_functions{count,outside_seeds,seeds,items},
# restating_comments{count,file_count,items}
```

```bash
$ python3 scripts/lib/refactor_census.py --commit 7877dbea --paths scripts/panel.py --json
# exit 0 — python_files 1, functions 4, every raw threshold 0: --paths narrows the set
```

**Verdict: met.** The instrument exists, counts all eight classes in one pass, and emits both the table above and the JSON summary. It reads a commit through git objects (`git ls-tree` / `git show`), so it never touches the working tree, and `--paths` narrows the set.

**Two caveats, both measured, neither blocking this verdict:**

1. *The plan's literal command needs `PYTHONPATH`.* `python3 -m refactor_census …` only resolves from a VNX shell, where `PYTHONPATH` already contains `scripts/lib`. Without it the module form fails: `env -u PYTHONPATH python3 -m refactor_census --commit 1ca67db2 --json` → `No module named refactor_census`, exit 1. The script-path form (`python3 scripts/lib/refactor_census.py …`) always works.
2. *Three deterministic classes do not reproduce the inventory's 24-10 numbers exactly.* The S1 dispatch documents this in full (`$VNX_DATA_DIR/unified_reports/dlv-dec2cd2b8e75.md`, section *Reproduction on 1ca67db2*): the plan's 4 groups / 11 functions is the non-test residue the inventory measured (including gitignored vendored copies of one skill script), while the census reports every exact duplicate, tests included — 18 groups / 46 functions on the git-tracked set of that commit; the version-suffix count is 40 against 38 because two `_new` substrings are not versioned copies; and the defensive-marker count is 7 against 5 because of one phrase the inventory's list did not include. The plan's reproduction line was not retuned to hit the stated numbers, so the right-hand column of §3 is the census's own measurement, not a copy of the plan.

### 2.2 Goal point 2 — both functions under 200 raw lines

> "`close_track_if_done` (`scripts/lib/track_reconciler_closure.py:44`, 349 ruw / 224 uitvoerbaar) en `run_reconcile` (`scripts/lib/objective_reconcile.py:860`, 369 ruw / 249 uitvoerbaar) zijn onder de 200 ruwe regels, met hetzelfde gedrag."

**Command (census, both files, on the main commit):**

```bash
$ PYTHONPATH=scripts/lib python3 -m refactor_census --commit 7877dbea \
    --paths scripts/lib/track_reconciler_closure.py,scripts/lib/objective_reconcile.py --json
# exit 0
#   scripts/lib/track_reconciler_closure.py:43  close_track_if_done  raw 147  exec 62
#   scripts/lib/objective_reconcile.py:799      run_reconcile        raw 196  exec 111
#   (third item, for completeness: objective_reconcile.py:483 gather_close_evidence raw 140 exec 89)
#   long_functions.counts: raw>=200 0
```

**Second, independent measurement** (raw = `end_lineno - lineno + 1` from `ast`, executable = `function_size_gate.measure_source`, read from `git show 7877dbea:<path>`):

```bash
$ PYTHONPATH=scripts/lib python3 - <<'EOF'
import ast, subprocess
from function_size_gate import measure_source
...
EOF
scripts/lib/track_reconciler_closure.py:43 close_track_if_done raw=147 exec=62
scripts/lib/objective_reconcile.py:799 run_reconcile raw=196 exec=111
# exit 0
```

**Behaviour ("hetzelfde gedrag"):**

```bash
$ python3 -m pytest tests/test_close_track_if_done.py tests/test_objective_reconcile.py -q \
    --junitxml=…/dlv-3dae497ecb87.junit.xml -o junit_suite_name=dlv-3dae497ecb87
95 passed
# exit 0
```

That is the plan's own pair of suites: 28 tests in `tests/test_close_track_if_done.py` plus 67 in `tests/test_objective_reconcile.py` on this commit (the plan counted 26 and 65 when it was written; S3 and S5 added the seam tests). The five track reports carry the per-PR red run and the AST-equivalence check for each move.

| function | 24-10 (plan) | today | raw delta | exec delta |
|---|---|---|---|---|
| `close_track_if_done` | 349 raw / 224 exec | 147 raw / 62 exec | −202 | −162 |
| `run_reconcile` | 369 raw / 249 exec | 196 raw / 111 exec | −173 | −138 |

**Verdict: met.** Both are under 200 raw lines. `run_reconcile` sits at 196, four lines under the plan's boundary; the plan's own rule for this case ("Staat `run_reconcile` na S5 nog op 200 ruwe regels of meer, dan meet de T0 dat op de kop na S5") does not fire, but the margin is thin enough that any six-line addition to that function re-opens the point. That is a boundary note, not a verdict change.

### 2.3 Goal point 3 — one panel sweep read the census

> "Eén panel-sweep (`python3 scripts/panel.py sweep`) heeft de telling gelezen; elke bevinding die de sweep overleeft is een levering of een OI via de bestaande schrijvers, met een eigenaar."

I did not run the sweep (the dispatch forbids it). I looked for the evidence that it ran. There is none.

| What I looked for | Command | Result |
|---|---|---|
| A deliverable for the sweep (P in the plan's table) | `bash bin/vnx deliverable list --objective slop-refactor-panel-review --json` | 6 deliverables: S1 `pr:dlv-dec2cd2b8e75`, S2 `pr:dlv-92a8405367ec`, S3 `pr:dlv-9aacc366daf4`, S4 `pr:dlv-7ebd662f8161`, S5 `pr:dlv-edf3a609835a`, nameting `pr:dlv-3dae497ecb87`. No P. Exit 0. |
| A panel artefact over the census | `grep -l refactor_census $VNX_DATA_DIR/unified_reports/*.md` | 6 files: four of the five track reports (S1, S2, S3, S4 — S5 does not cite it), the 24-10 inventory and its plan-gate review. None is a panel artefact. Exit 0. |
| Any sweep artefact at all | `ls -t $VNX_DATA_DIR/unified_reports/panel-sweep*.md \| head -1` | `panel-sweep-diverge-0-220b7d.md`, 2026-09-03 14:55 — seven weeks before S1 landed, so it cannot have read the census. The newest panel artefact of any mode is `panel-strategy-synth-claude-45a244.md`, 2026-10-23. |
| A panelist dispatch in the register | `grep -c deliberation-panelist $VNX_DATA_DIR/state/dispatch_register.ndjson` | 0 of 6983 entries. Exit 1 (no match). |
| An OI citing the census or a sweep | scan of `state/open_items.json` (2360 items, 805 open) for `slop`, `census`, `refactor_census`, `panel sweep` | No OI postdates the track; the only sweep-related hit is OI-854 (2026-07-30, a different panel). |
| The plan's command form | `python3 scripts/panel.py sweep` | exit 2, `the following arguments are required: question` — the plan's bare line cannot work; a sweep needs a question and `--context-file`. |

**Verdict: not met. Owner: T0.** Nothing in the record shows a sweep ran, and no finding was written anywhere through `vnx deliverable add` or the open-items ledger. The next step is the T0's, not a builder's: run `python3 scripts/panel.py sweep "<question>" --context-file <census json> …`, then file each surviving finding. Note the two facts that make the bare command fail today (a question is required, and the roster refuses when the provider switch leaves fewer than `synthesis_min_seats` seats).

### 2.4 Goal point 4 — the rest of the census, with an owner per candidate

> "De rest van de telling staat in de nameting met eigenaar per kandidaat."

**Verdict: met**, by §5 of this document. Every remaining candidate above the plan's thresholds has a row with `file:line` and an owner. Three ownership rules, applied in this order and stated again in §4: the plan's explicit assignment wins; otherwise a track is named only when one of its open deliverables lists the file in `dispatch_paths` (evidence, not intent); otherwise the cell says `T0 to decide`, as the dispatch allows.

The distribution is the finding: of 57 long functions, 43 fall to `T0 to decide`; of 18 duplicate groups, 17; of 40 version-suffixed paths, 40; of 7 defensive markers, 5; of 17 dead module functions, 14; of 418 restating comments, 413. The census named the residue of this track, but the registry still owns almost none of it.

## 3. The count today against 24-10, per class

Both census runs, side by side with the plan's 24-10 numbers (which came from the pre-instrument inventory `20261024-inv-slop-refactor`, not from this tool — see §2.1 caveat 2).

```bash
$ python3 scripts/lib/refactor_census.py --commit 1ca67db2
Refactor census — 1ca67db2

python files              2389  47501 functions
unparsed                     0
long functions             310  raw>=100 310, raw>=150 105, raw>=200 58, raw>=300 20, raw>=400 6; executable>70 373
exact duplicates            18  46 functions in groups
doubled step banners         4
version-suffixed paths      40
defensive markers            7
dead module functions       17  11 outside seeds, 6 in seeds
restating comments         429  192 files
# exit 0

$ python3 scripts/lib/refactor_census.py --commit 7877dbea
Refactor census — 7877dbea

python files              2413  47990 functions
unparsed                     0
long functions             311  raw>=100 311, raw>=150 103, raw>=200 57, raw>=300 18, raw>=400 6; executable>70 366
exact duplicates            18  45 functions in groups
doubled step banners         0
version-suffixed paths      40
defensive markers            7
dead module functions       17  11 outside seeds, 6 in seeds
restating comments         418  188 files
# exit 0
```

| Class (plan unit) | 24-10 as stated in the plan | census on `1ca67db2` (today's tool) | census on `7877dbea` (main today) | delta main − baseline | kind |
|---|---|---|---|---|---|
| files parsed / functions | 2,448 / 47,626 | 2,389 / 47,501 | 2,413 / 47,990 | +24 files / +489 functions | deterministic |
| long functions, raw ≥ 200 | 58 | 58 | **57** | −1 | deterministic |
| long functions, raw ≥ 300 | 20 | 20 | **18** | −2 | deterministic |
| long functions, raw ≥ 400 | 6 | 6 | 6 | 0 | deterministic |
| functions, executable > 70 (ratchet unit) | not stated | 373 | 366 | −7 | deterministic |
| exact duplicates | 4 groups / 11 functions | 18 groups / 46 functions | **18 groups / 45 functions** | −1 function, 0 groups | deterministic |
| doubled step banners | 4 | 4 | **0** | −4 | deterministic |
| version-suffixed paths | 38 | 40 | 40 | 0 | deterministic |
| defensive markers | 5 | 7 | 7 | 0 | deterministic |
| dead module functions | 16 (11 outside seeds) | 17 (11 outside seeds) | 17 (11 outside seeds) | 0 | heuristic |
| restating comments | 81 in 36 files | 429 in 192 files | **418 in 188 files** | −11 / −4 files | heuristic |

Read the middle column against the left one before reading the right one: only the long-function classes and the banner class reproduce the 24-10 numbers exactly. The duplicate columns differ for a measurable reason (scope: the census counts test-module fixtures too; and path set: the inventory counted gitignored vendored copies), the version column for two substring hits, the defensive column for one phrase, and the restating-comment column not at all (§2.1 caveat 2, and the S1 report's *Divergence 5*).

## 4. How an owner was assigned in §5

Owner cells are filled by three rules, applied in this order:

1. **The plan assigns it.** The plan names `gate-engine-consolidation` for `_frontmatter_run_outcome` (deliverable GE-5b, `pr:dlv-b436071defc4`) and reserves two candidates to Vincent (`_build_parser`; the two compat shims). Those cells carry the plan's word, not a guess. `_govern_impl` gets "a later refactor deliverable without a file conflict" in the plan — no such deliverable exists, and the OI the plan says should carry the near-duplicate measurement (`_govern` ↔ `_govern_impl`) does not exist either, so both cells say `T0 to decide`.
2. **An open deliverable names the file.** If a deliverable that is `proposed`, `ready` or `in_progress` lists the file in its `dispatch_paths`, the cell names that track. This is adjacency with evidence — the track is already working in that file — not a commitment to refactor it.
3. **Otherwise `T0 to decide`.**

The basis column in §5 says which rule fired. Seeded benchmark fixtures are kept in the dead-function table with `kind = seed` because the plan's own definition excludes them from the live count.


## 5. The rest of the census: one row per candidate
### 5.1 Long functions, raw ≥ 200 (57)

| file:line | function | raw | exec | owner | basis |
|---|---|---|---|---|---|
| `scripts/gate_obligation_runner.py:2813` | `fulfill_obligation` | 795 | 569 | T0 to decide | none |
| `scripts/glm_gate.py:524` | `main` | 573 | 300 | gate-engine-consolidation | open deliverable names this file |
| `scripts/kimi_gate.py:484` | `main` | 525 | 288 | gate-engine-consolidation | open deliverable names this file |
| `scripts/lib/envelope_govern.py:243` | `_govern` | 498 | 330 | T0 to decide | plan: later refactor deliverable, none exists |
| `scripts/planning_cli.py:6030` | `_build_parser` | 457 | 424 | T0 to decide | plan reserves to Vincent |
| `scripts/lib/dispatch_cli.py:3437` | `run_dispatch` | 456 | 265 | plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/migrate_to_central_vnx.py:1754` | `_run_apply` | 389 | 281 | T0 to decide | none |
| `scripts/lib/provider_dispatch.py:1480` | `_emit_governance` | 382 | 244 | refactor-skill-en-levering | open deliverable names this file |
| `tests/test_migrate_to_central_vnx.py:2513` | `test_round6_apply_against_all_4_real_source_schemas` | 380 | 277 | T0 to decide | none |
| `scripts/lib/orphan_sweep.py:646` | `sweep` | 366 | 246 | T0 to decide | none |
| `scripts/lib/report_to_receipt_converter.py:960` | `_build_receipt_from_report_core` | 350 | 212 | T0 to decide | none |
| `scripts/planning_cli.py:4652` | `run_plan_gate_for_track` | 349 | 265 | deliverable-retarget-withdraw, plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/lib/dispatch_cli.py:2478` | `build_runtime_snapshot` | 344 | 216 | plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/gate_obligation_runner.py:2010` | `_pre_execution_decision` | 316 | 96 | T0 to decide | none |
| `scripts/lib/governance_emit.py:78` | `emit_dispatch_receipt` | 313 | 140 | refactor-skill-en-levering | open deliverable names this file |
| `scripts/lib/dispatch_govern.py:671` | `_govern_impl` | 304 | 211 | T0 to decide | plan: later refactor deliverable, none exists |
| `scripts/lib/dispatch_envelope.py:526` | `run_envelope_plan` | 300 | 217 | T0 to decide | none |
| `scripts/report_parser.py:299` | `ReportParser.extract_metadata` | 300 | 191 | T0 to decide | none |
| `dashboard/serve_dashboard.py:291` | `DashboardHandler.do_GET` | 297 | 226 | T0 to decide | none |
| `scripts/lib/report_to_receipt_converter.py:1556` | `_convert_one_detailed` | 295 | 168 | T0 to decide | none |
| `scripts/lib/dispatch_envelope.py:828` | `run_envelope_headless_plan` | 292 | 213 | T0 to decide | none |
| `scripts/lib/gate_artifacts.py:577` | `materialize_artifacts` | 281 | 169 | T0 to decide | none |
| `scripts/lib/provider_spawns/claude_spawn.py:187` | `spawn_claude` | 278 | 158 | T0 to decide | none |
| `scripts/lib/dispatch_metadata_db.py:112` | `upsert_dispatch_provider_row` | 270 | 177 | T0 to decide | none |
| `vnx_cli/commands/dispatch_agent.py:187` | `vnx_dispatch_agent` | 266 | 184 | T0 to decide | none |
| `scripts/planning_cli.py:1805` | `cmd_objective_close` | 265 | 174 | deliverable-retarget-withdraw, plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/lib/provider_dispatch.py:3865` | `main` | 259 | 202 | refactor-skill-en-levering | open deliverable names this file |
| `scripts/lib/verify_pr.py:153` | `verify_pr` | 259 | 204 | T0 to decide | none |
| `scripts/lib/dispatch_worktree_isolation.py:947` | `remove_dispatch_worktree` | 256 | 153 | T0 to decide | none |
| `dashboard/api_operator.py:1117` | `_operator_get_system_health` | 255 | 230 | T0 to decide | none |
| `scripts/lib/receipt_provenance.py:688` | `reconcile_commit_provenance` | 247 | 133 | T0 to decide | none |
| `scripts/lib/governance_emit.py:668` | `emit_unified_report` | 241 | 137 | refactor-skill-en-levering | open deliverable names this file |
| `scripts/lib/tenant_stamping.py:488` | `_rebuild_table_phase1` | 239 | 136 | T0 to decide | none |
| `scripts/dream/consolidator.py:469` | `run_dream_cycle` | 235 | 197 | T0 to decide | none |
| `scripts/lib/pr_enforcement.py:410` | `enforce_pr_exists` | 233 | 140 | T0 to decide | none |
| `scripts/planning_cli.py:4296` | `_run_tiebreaker_for_track` | 231 | 170 | deliverable-retarget-withdraw, plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/lib/deliberation_panel.py:475` | `run_deliberation` | 227 | 175 | T0 to decide | none |
| `scripts/lib/dispatch_plan.py:258` | `compile_plan` | 226 | 140 | T0 to decide | none |
| `scripts/gate_runner.py:992` | `GateRunner._run_harness_lane_path` | 224 | 93 | T0 to decide | none |
| `scripts/lib/chain_origin_anchor.py:1037` | `verify_chain` | 220 | 156 | T0 to decide | none |
| `scripts/lib/envelope_adapters_provider.py:254` | `ProviderAdapter.run` | 219 | 175 | T0 to decide | none |
| `scripts/planning_cli.py:1584` | `_close_with_attest` | 219 | 185 | deliverable-retarget-withdraw, plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/gather_intelligence.py:2143` | `main` | 218 | 192 | T0 to decide | none |
| `scripts/pre_merge_gate.py:884` | `check_ci_workflow` | 215 | 171 | T0 to decide | none |
| `scripts/lib/merge_preflight_ci_check.py:255` | `check_ci_run_for_head` | 214 | 154 | T0 to decide | none |
| `scripts/gate_obligation_runner.py:3615` | `run` | 212 | 101 | T0 to decide | none |
| `scripts/lib/gate_executor.py:192` | `GateExecutorMixin._execute_ci_gate` | 212 | 173 | gate-engine-consolidation | open deliverable names this file |
| `scripts/lib/gate_recorder.py:1094` | `_check_overwrite_guard` | 212 | 93 | gate-engine-consolidation | open deliverable names this file |
| `scripts/spikes/laya_finding_triage/03d_laya_mlx_classify.py:269` | `main` | 211 | 179 | T0 to decide | none |
| `scripts/lib/provider_spawns/kimi_spawn.py:1191` | `spawn_kimi` | 205 | 129 | T0 to decide | none |
| `scripts/retroactive_backfill.py:689` | `simulate_feedback_loop` | 205 | 154 | T0 to decide | none |
| `scripts/learning_loop.py:1404` | `LearningLoop.daily_learning_cycle` | 204 | 154 | T0 to decide | none |
| `scripts/lib/subprocess_adapter.py:394` | `SubprocessAdapter.deliver` | 204 | 119 | T0 to decide | none |
| `scripts/quality_db_init.py:1399` | `_migrate_v29` | 204 | 133 | T0 to decide | none |
| `scripts/lib/dispatch_spec.py:410` | `validate` | 203 | 120 | T0 to decide | none |
| `scripts/lib/subprocess_dispatch_internals/delivery.py:290` | `deliver_via_subprocess` | 203 | 151 | T0 to decide | none |
| `scripts/llm_benchmark.py:1503` | `generate_markdown_report` | 203 | 165 | T0 to decide | none |

### 5.2 Exact duplicates (18 groups, 45 functions)

| members | n | raw | owner | basis |
|---|---|---|---|---|
| `tests/test_migrate_v23_preserve.py:28` _base_db<br>`tests/test_migrate_v24_dedupe_phase_history.py:34` _base_db_v22<br>`tests/test_migrate_v24_multi_project_preservation.py:27` _base_db<br>`tests/test_migrate_v24_orphan_handling.py:30` _base_db<br>`tests/test_migrate_v24_stale_0023_compat.py:32` _base_db_v22 | 5 | 32 | T0 to decide | none |
| `tests/test_deliverable_task_class_routing_floor.py:464` _bootstrap_plan_gate<br>`tests/test_plan_gate_batch.py:65` _bootstrap<br>`tests/test_plan_gate_deliverables_source.py:157` _bootstrap<br>`tests/test_plan_gate_goal_state_plan.py:46` _bootstrap | 4 | 45 | T0 to decide | none |
| `tests/test_pool_worktree_manager.py:38` _init_git_repo_with_origin<br>`tests/test_provider_dispatch_worktree_isolation.py:577` _init_git_repo_with_origin<br>`tests/test_tmux_worktree.py:38` _init_git_repo_with_origin<br>`tests/test_worktree_release.py:37` _init_git_repo_with_origin | 4 | 47 | T0 to decide | none |
| `tests/test_conversation_read_model.py:42` _create_index_db<br>`tests/test_conversation_resume.py:44` _create_index_db<br>`tests/test_conversation_timeline.py:40` _create_index_db | 3 | 45 | T0 to decide | none |
| `tests/test_objective_set_goal.py:46` _bootstrap<br>`tests/test_planning_cli_lane_hint.py:47` _bootstrap<br>`tests/test_planning_cli_objective_add.py:29` _bootstrap | 3 | 34 | T0 to decide | none |
| `scripts/glm_gate.py:266` _frontmatter_run_outcome<br>`scripts/kimi_gate.py:243` _frontmatter_run_outcome | 2 | 48 | gate-engine-consolidation (GE-5b `pr:dlv-b436071defc4`) | plan (GE-5b) |
| `tests/test_closure_verifier_evidence_contract.py:45` verifier_env<br>`tests/test_closure_verifier_unknown_gate.py:43` verifier_env | 2 | 43 | T0 to decide | none |
| `tests/test_deliverable_close.py:42` _build_db<br>`tests/test_deliverable_task_class_routing_floor.py:71` _build_db | 2 | 66 | T0 to decide | none |
| `tests/test_dispatch_cli_obligation_loud.py:58` _make_bundle<br>`tests/test_gate_obligations.py:76` _make_bundle | 2 | 39 | T0 to decide | none |
| `tests/test_dlv5_review_gate_takeover.py:57` manager_env<br>`tests/test_wiring_gate_shadow_mode.py:47` manager_env | 2 | 30 | T0 to decide | none |
| `tests/test_governance_digest_certification.py:44` _install_stub_modules<br>`tests/test_governance_digest_runner.py:37` _install_stub_modules | 2 | 45 | T0 to decide | none |
| `tests/test_horizon_cli.py:46` _bootstrap_store<br>`tests/test_horizon_parity.py:159` _bootstrap_store | 2 | 44 | T0 to decide | none |
| `tests/test_intelligence_per_provider.py:44` _setup_quality_db<br>`tests/test_intelligence_selector.py:52` _setup_quality_db | 2 | 45 | T0 to decide | none |
| `tests/test_migrate_per_project.py:38` _make_source_qi_db<br>`tests/test_migrate_test_apply_mode.py:39` _make_source_qi_db | 2 | 36 | T0 to decide | none |
| `tests/test_objective_close.py:38` _build_db<br>`tests/test_planning_cli.py:37` _build_db | 2 | 43 | T0 to decide | none |
| `tests/test_objective_reconcile.py:52` _build_db<br>`tests/test_reconciler_dependency_kind.py:59` _build_db | 2 | 55 | T0 to decide | none |
| `tests/test_oi1624_gate_absence_vs_rejection.py:306` manager_env<br>`tests/test_oi1750_tweede_plek_audit_line_count.py:125` manager_env | 2 | 33 | T0 to decide | none |
| `tests/test_orphan_sweep.py:53` env<br>`tests/test_orphan_sweep_fail_open.py:54` env | 2 | 30 | T0 to decide | none |

### 5.3 Version-suffixed paths (40)

| path | kind | owner | basis |
|---|---|---|---|
| `schemas/runtime_coordination_v10.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v2.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v3.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v4.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v5.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v6.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v7.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v8.sql` | migration schema series | T0 to decide | none |
| `schemas/runtime_coordination_v9.sql` | migration schema series | T0 to decide | none |
| `schemas/unified_report_v1.json` | schema | T0 to decide | none |
| `scripts/lib/pool_scaling_policies/cost_aware_v1.py` | policy series (_v1 is a selector value) | T0 to decide | none |
| `scripts/lib/pool_scaling_policies/queue_depth_v1.py` | policy series (_v1 is a selector value) | T0 to decide | none |
| `scripts/lib/rp_find_newest_report_mtime.py` | substring hit, not a versioned copy | T0 to decide | none |
| `scripts/lib/vnx_exec_new_group.py` | substring hit, not a versioned copy | T0 to decide | none |
| `scripts/receipt_processor_v4.sh` | compat shim | T0 to decide | plan reserves to Vincent |
| `scripts/userpromptsubmit_intelligence_inject_v5.sh` | compat shim | T0 to decide | plan reserves to Vincent |
| `tests/fixtures/migration_0034/sales_copilot_v33_seven_tables.sql` | fixture (pinned schema copy) | T0 to decide | none |
| `tests/test_build_current_state_v2.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_complete_v2_flow.sh` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_mc_v4_columns.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v22_dispatches_seq_preserve.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v23_preserve.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v24_dedupe_phase_history.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v24_multi_project_preservation.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v24_orphan_handling.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v24_stale_0023_compat.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v26_drop_success_rate.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v29_guard.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_migrate_v29_index_preservation.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_outcome_grounding_v2.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_receipt_v2_pr4_wiring.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_receipt_v2_pr5_schema_cutover.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_receipt_v2_pr_b2_wiring.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_rp_find_newest_report_mtime.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_track_v23_cli.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_tracks_v23_lib.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_tracks_v23_schema.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_tracks_v24_audit_ordering.py` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_v2_gate_progression.sh` | test named for the schema version it pins | T0 to decide | none |
| `tests/test_v2_metadata_flow.sh` | test named for the schema version it pins | T0 to decide | none |

### 5.4 Defensive markers (7)

| file:line | comment | owner | basis |
|---|---|---|---|
| `scripts/lib/append_receipt_internals/validation.py:316` | # confidence impossible. Receipts whose producer has no model concept by | T0 to decide | none |
| `scripts/lib/deliberation_panel.py:524` | # receipt ledger is impossible when the id is built here and thrown away. | T0 to decide | none |
| `scripts/lib/dispatch_bridge.py:874` | # bridge is the trust boundary for legacy callers, so an impossible | T0 to decide | none |
| `scripts/lib/dispatch_worktree_isolation.py:547` | # impossible by construction (the task's own warning: "een lock die niet | T0 to decide | none |
| `scripts/lib/gate_recorder.py:82` | # cannot happen. | gate-engine-consolidation | open deliverable names this file |
| `scripts/lib/guard_reachability_calibration.py:161` | # Cannot happen while CALIBRATION_CASES is non-empty, but an empty | T0 to decide | none |
| `scripts/lib/provider_dispatch.py:558` | # A same-length tie for candidates[0] is impossible by construction: two | refactor-skill-en-levering | open deliverable names this file |

### 5.5 Dead module functions (17; 6 are seeded fixtures)

| file:line | function | kind | owner | basis |
|---|---|---|---|---|
| `packages/claude-code-toolkit/context-rotation/lib/enforcement_state.py:78` | `journal_path` | live | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t3_complex/09_mock_introspection_trap/seed/builders.py:10` | `build_user_query_safe` | seed | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t5_review_design/01_planted_review/seed/seoaudit/db.py:37` | `cache_report` | seed | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t5_review_design/01_planted_review/seed/seoaudit/db.py:42` | `get_cached_report` | seed | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t5_review_design/01_planted_review/seed/seoaudit/db.py:60` | `fetch_report` | seed | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t5_review_design/01_planted_review/seed/seoaudit/report_builder.py:37` | `enrich_with_history` | seed | T0 to decide | none |
| `scripts/benchmark/field-tests/tasks/t5_review_design/02_agent_engine_design/seed/backend/pipeline.py:33` | `_titles` | seed | T0 to decide | none |
| `scripts/lib/delegation_mandate.py:193` | `_utc_now_str` | live | T0 to decide | none |
| `scripts/lib/dispatch_outcome_classifier.py:812` | `peek_dispatch_outcome` | live | T0 to decide | none |
| `scripts/lib/dispatch_process_registry.py:325` | `collect_descendant_pgids` | live | T0 to decide | none |
| `scripts/lib/final_prompt_integrity.py:119` | `strip_known_wrappers` | live | T0 to decide | none |
| `scripts/lib/headless_adapter.py:157` | `headless_config_from_env` | live | T0 to decide | none |
| `scripts/lib/regulated_strict_approval.py:74` | `is_regulated_strict_enabled` | live | T0 to decide | none |
| `scripts/lib/runtime_supervisor.py:634` | `load_supervisor` | live | deliverable-retarget-withdraw | open deliverable names this file |
| `scripts/lib/t0_decision_summarizer.py:296` | `load_recent_decisions` | live | T0 to decide | none |
| `vnx_cli/commands/horizon.py:442` | `vnx_horizon` | live | plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `vnx_cli/commands/horizon.py:457` | `vnx_objective` | live | plan-gate-alleen-voor-architectuur | open deliverable names this file |

### 5.6 Restating comments, per file (418 comments in 188 files)

| file | comments | owner | basis |
|---|---|---|---|
| `scripts/pr_queue_manager.py` | 21 | T0 to decide | none |
| `tests/test_prompt_assembler.py` | 16 | T0 to decide | none |
| `tests/test_control_centre_cli.py` | 11 | T0 to decide | none |
| `tests/test_dispatch_serialization.py` | 10 | T0 to decide | none |
| `tests/test_runtime_state_reconciliation.py` | 9 | T0 to decide | none |
| `tests/test_worker_permissions.py` | 9 | T0 to decide | none |
| `scripts/lib/subprocess_adapter.py` | 8 | T0 to decide | none |
| `tests/test_open_items_acceptance_criterion_guard.py` | 8 | T0 to decide | none |
| `tests/test_runtime_cutover.py` | 8 | T0 to decide | none |
| `tests/test_tmux_adapter.py` | 8 | T0 to decide | none |
| `tests/test_governance_audit.py` | 7 | T0 to decide | none |
| `scripts/lib/governance_evaluator.py` | 6 | T0 to decide | none |
| `tests/test_acceptance_idempotency.py` | 6 | T0 to decide | none |
| `tests/test_dispatch_broker.py` | 6 | T0 to decide | none |
| `tests/test_failed_delivery_lease_cleanup.py` | 6 | T0 to decide | none |
| `tests/test_stream_type_normalization.py` | 6 | T0 to decide | none |
| `tests/test_benchmark_suite.py` | 5 | T0 to decide | none |
| `tests/test_dispatch_daemon.py` | 5 | T0 to decide | none |
| `tests/test_dispatch_parameter_tracker.py` | 5 | T0 to decide | none |
| `tests/test_pr_dispatch_integration.py` | 5 | T0 to decide | none |
| `scripts/lib/dashboard_read_model.py` | 4 | T0 to decide | none |
| `scripts/lib/tmux_session_profile.py` | 4 | T0 to decide | none |
| `scripts/lib/vnx_start_runtime.py` | 4 | T0 to decide | none |
| `scripts/open_items_manager.py` | 4 | T0 to decide | none |
| `scripts/query_quality_intelligence.py` | 4 | T0 to decide | none |
| `tests/test_codex_adapter_prompt_assembly.py` | 4 | T0 to decide | none |
| `tests/test_dispatch_cli.py` | 4 | T0 to decide | none |
| `tests/test_dispatch_enricher.py` | 4 | T0 to decide | none |
| `tests/test_fail_closed_dispatch_guard.py` | 4 | T0 to decide | none |
| `tests/test_gemini_adapter_prompt_assembly.py` | 4 | T0 to decide | none |
| `tests/test_llm_decision_router.py` | 4 | T0 to decide | none |
| `tests/test_smart_router_quality_tier.py` | 4 | T0 to decide | none |
| `tests/test_vnx_doctor.py` | 4 | T0 to decide | none |
| `dashboard/serve_dashboard.py` | 3 | T0 to decide | none |
| `scripts/build_feature_plan.py` | 3 | T0 to decide | none |
| `scripts/intelligence_aggregator_cli.py` | 3 | T0 to decide | none |
| `scripts/lib/auto_report_contract.py` | 3 | T0 to decide | none |
| `scripts/lib/dispatch_parameter_tracker.py` | 3 | T0 to decide | none |
| `scripts/lib/envelope_adapters_provider.py` | 3 | T0 to decide | none |
| `scripts/lib/preference_store.py` | 3 | T0 to decide | none |
| `scripts/lib/preference_surface.py` | 3 | T0 to decide | none |
| `scripts/reconcile_queue_state.py` | 3 | T0 to decide | none |
| `tests/test_feedback_loop.py` | 3 | T0 to decide | none |
| `tests/test_intelligence_selector.py` | 3 | T0 to decide | none |
| `tests/test_tracks_tenant_regression.py` | 3 | T0 to decide | none |
| `scripts/intelligence_export.py` | 2 | T0 to decide | none |
| `scripts/learning_loop.py` | 2 | T0 to decide | none |
| `scripts/lib/fpc_certification_checks.py` | 2 | T0 to decide | none |
| `scripts/lib/headless_dispatch_writer.py` | 2 | T0 to decide | none |
| `scripts/lib/inbound_inbox.py` | 2 | T0 to decide | none |
| `scripts/lib/llm_decision_router.py` | 2 | T0 to decide | none |
| `scripts/lib/runtime_coordination.py` | 2 | T0 to decide | none |
| `scripts/vnx_install.py` | 2 | T0 to decide | none |
| `scripts/vnx_structural_doctor.py` | 2 | T0 to decide | none |
| `tests/test_api_operator_exception_handling.py` | 2 | T0 to decide | none |
| `tests/test_build_t0_state_hook_install.py` | 2 | T0 to decide | none |
| `tests/test_failure_classification.py` | 2 | T0 to decide | none |
| `tests/test_governance_audit_stamping.py` | 2 | T0 to decide | none |
| `tests/test_hookpin_check.py` | 2 | T0 to decide | none |
| `tests/test_operator_memory_indexer.py` | 2 | T0 to decide | none |
| `tests/test_pr_queue_integration.py` | 2 | T0 to decide | none |
| `tests/test_provider_dispatch_deepseek_harness.py` | 2 | T0 to decide | none |
| `tests/test_quickstart_validation.py` | 2 | T0 to decide | none |
| `tests/test_receipt_processor_bootstrap.py` | 2 | T0 to decide | none |
| `tests/test_scaling_policies.py` | 2 | T0 to decide | none |
| `tests/test_skill_refinement.py` | 2 | T0 to decide | none |
| `tests/test_wave4_central_install_e2e.py` | 2 | T0 to decide | none |
| `ledger/engine/correlation_engine.py` | 1 | T0 to decide | none |
| `packages/claude-code-toolkit/context-rotation/lib/tmux_clear.py` | 1 | T0 to decide | none |
| `scripts/aggregator/t0_lifecycle.py` | 1 | T0 to decide | none |
| `scripts/analysis/role_scope_outside_triage.py` | 1 | T0 to decide | none |
| `scripts/build_project_status.py` | 1 | T0 to decide | none |
| `scripts/build_t0_quality_digest.py` | 1 | T0 to decide | none |
| `scripts/check_ci_slug_match.py` | 1 | T0 to decide | none |
| `scripts/ci/check_out_of_repo_symlinks.py` | 1 | T0 to decide | none |
| `scripts/cleanup_orphan_gates.py` | 1 | T0 to decide | none |
| `scripts/closure_verifier.py` | 1 | T0 to decide | none |
| `scripts/conversation_analyzer/runner.py` | 1 | T0 to decide | none |
| `scripts/doc_section_extractor.py` | 1 | T0 to decide | none |
| `scripts/headless_orchestrator.py` | 1 | T0 to decide | none |
| `scripts/intelligence_backfill.py` | 1 | T0 to decide | none |
| `scripts/lib/audit_bundle.py` | 1 | T0 to decide | none |
| `scripts/lib/config_registry.py` | 1 | T0 to decide | none |
| `scripts/lib/cost_ledger.py` | 1 | T0 to decide | none |
| `scripts/lib/cost_loader.py` | 1 | T0 to decide | none |
| `scripts/lib/dashboard_actions.py` | 1 | T0 to decide | none |
| `scripts/lib/deliberation_panel.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_broker.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_cleanup.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_cli.py` | 1 | plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/lib/dispatch_govern.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_plan.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_router.py` | 1 | T0 to decide | none |
| `scripts/lib/dispatch_worktree_isolation.py` | 1 | T0 to decide | none |
| `scripts/lib/event_analyzer.py` | 1 | T0 to decide | none |
| `scripts/lib/execution_target_registry.py` | 1 | T0 to decide | none |
| `scripts/lib/failure_classification.py` | 1 | T0 to decide | none |
| `scripts/lib/headless_adapter.py` | 1 | T0 to decide | none |
| `scripts/lib/headless_run_registry.py` | 1 | T0 to decide | none |
| `scripts/lib/lease_manager.py` | 1 | T0 to decide | none |
| `scripts/lib/lesson_conflict.py` | 1 | T0 to decide | none |
| `scripts/lib/mixed_execution_router.py` | 1 | T0 to decide | none |
| `scripts/lib/operator_memory_indexer.py` | 1 | T0 to decide | none |
| `scripts/lib/pool_manager.py` | 1 | T0 to decide | none |
| `scripts/lib/pool_state_repo.py` | 1 | T0 to decide | none |
| `scripts/lib/preference_injector.py` | 1 | T0 to decide | none |
| `scripts/lib/projection_reconciler.py` | 1 | T0 to decide | none |
| `scripts/lib/receipt_schema.py` | 1 | refactor-skill-en-levering | open deliverable names this file |
| `scripts/lib/recommendation_tracker.py` | 1 | T0 to decide | none |
| `scripts/lib/report_extraction.py` | 1 | T0 to decide | none |
| `scripts/lib/runtime_core.py` | 1 | T0 to decide | none |
| `scripts/lib/runtime_reconciler.py` | 1 | T0 to decide | none |
| `scripts/lib/runtime_state_reconciler.py` | 1 | T0 to decide | none |
| `scripts/lib/runtime_supervisor.py` | 1 | deliverable-retarget-withdraw | open deliverable names this file |
| `scripts/lib/safe_autonomy_cutover.py` | 1 | T0 to decide | none |
| `scripts/lib/stop_conditions.py` | 1 | T0 to decide | none |
| `scripts/lib/strategy/decisions.py` | 1 | T0 to decide | none |
| `scripts/lib/t0_role_sources_audit.py` | 1 | T0 to decide | none |
| `scripts/lib/terminal_snapshot.py` | 1 | T0 to decide | none |
| `scripts/lib/vnx_worktree.py` | 1 | T0 to decide | none |
| `scripts/lib/worker_registry.py` | 1 | T0 to decide | none |
| `scripts/lib/worker_state_manager.py` | 1 | T0 to decide | none |
| `scripts/lib/workflow_supervisor.py` | 1 | T0 to decide | none |
| `scripts/llm_benchmark.py` | 1 | T0 to decide | none |
| `scripts/memory_consolidator.py` | 1 | T0 to decide | none |
| `scripts/migrate_to_central_vnx.py` | 1 | T0 to decide | none |
| `scripts/panel.py` | 1 | T0 to decide | none |
| `scripts/planning_cli.py` | 1 | deliverable-retarget-withdraw, plan-gate-alleen-voor-architectuur | open deliverable names this file |
| `scripts/runtime_cutover_check.py` | 1 | T0 to decide | none |
| `scripts/t0_intelligence_aggregator.py` | 1 | T0 to decide | none |
| `scripts/t0_query.py` | 1 | T0 to decide | none |
| `scripts/update_progress_state.py` | 1 | T0 to decide | none |
| `scripts/vnx_settings_merge.py` | 1 | fleet-release-and-rollout | open deliverable names this file |
| `tests/headless_t0/setup_sandbox.py` | 1 | T0 to decide | none |
| `tests/pool/test_pool_lease_e2e.py` | 1 | T0 to decide | none |
| `tests/test_agent_validation.py` | 1 | T0 to decide | none |
| `tests/test_api_intelligence_exception_handling.py` | 1 | T0 to decide | none |
| `tests/test_auto_commit_stash_isolation.py` | 1 | T0 to decide | none |
| `tests/test_auto_lease_release_on_receipt.py` | 1 | T0 to decide | none |
| `tests/test_chunk_timeout_complexity.py` | 1 | T0 to decide | none |
| `tests/test_ci_check_docs_file_line_refs.py` | 1 | T0 to decide | none |
| `tests/test_claude_spawn_byte_identity.py` | 1 | T0 to decide | none |
| `tests/test_commit_enforcement.py` | 1 | T0 to decide | none |
| `tests/test_conversation_resume.py` | 1 | T0 to decide | none |
| `tests/test_cost_aware_policy.py` | 1 | T0 to decide | none |
| `tests/test_decision_executor.py` | 1 | T0 to decide | none |
| `tests/test_dispatch_register.py` | 1 | T0 to decide | none |
| `tests/test_dream_consolidator.py` | 1 | T0 to decide | none |
| `tests/test_event_store.py` | 1 | T0 to decide | none |
| `tests/test_gate_request_handler_w3f.py` | 1 | T0 to decide | none |
| `tests/test_headless_inspect.py` | 1 | T0 to decide | none |
| `tests/test_headless_run_registry.py` | 1 | T0 to decide | none |
| `tests/test_headless_smoke.py` | 1 | T0 to decide | none |
| `tests/test_headless_system.py` | 1 | T0 to decide | none |
| `tests/test_incident_log.py` | 1 | T0 to decide | none |
| `tests/test_intelligence_recency_suppression.py` | 1 | T0 to decide | none |
| `tests/test_learning_loop_proposal_tier.py` | 1 | T0 to decide | none |
| `tests/test_migrate_backup_retention.py` | 1 | T0 to decide | none |
| `tests/test_migrate_env_isolation.py` | 1 | T0 to decide | none |
| `tests/test_obligation_forward_linkage.py` | 1 | T0 to decide | none |
| `tests/test_pattern_matching.py` | 1 | T0 to decide | none |
| `tests/test_placebo_arm.py` | 1 | T0 to decide | none |
| `tests/test_pool_state_aggregator.py` | 1 | T0 to decide | none |
| `tests/test_project_id_scoping_2026-05-20.py` | 1 | T0 to decide | none |
| `tests/test_provider_adapter.py` | 1 | T0 to decide | none |
| `tests/test_provider_dispatch_kimi.py` | 1 | T0 to decide | none |
| `tests/test_receipt_processor_sc1091.py` | 1 | T0 to decide | none |
| `tests/test_repo_map.py` | 1 | T0 to decide | none |
| `tests/test_report_assembler.py` | 1 | T0 to decide | none |
| `tests/test_report_extraction.py` | 1 | T0 to decide | none |
| `tests/test_report_to_receipt_converter.py` | 1 | T0 to decide | none |
| `tests/test_review_gate_dispatch_id.py` | 1 | T0 to decide | none |
| `tests/test_rich_headless_certification.py` | 1 | T0 to decide | none |
| `tests/test_routing_preflight_py.py` | 1 | T0 to decide | none |
| `tests/test_runtime_overrides_shared.py` | 1 | T0 to decide | none |
| `tests/test_sec_hardening_20260530.py` | 1 | T0 to decide | none |
| `tests/test_seed_tracks_from_roadmap.py` | 1 | T0 to decide | none |
| `tests/test_subprocess_health.py` | 1 | T0 to decide | none |
| `tests/test_tagger_ab.py` | 1 | T0 to decide | none |
| `tests/test_tmux_conversation_normalizer.py` | 1 | T0 to decide | none |
| `tests/test_tracks_multitenant.py` | 1 | T0 to decide | none |
| `tests/test_verification_junit_counts.py` | 1 | T0 to decide | none |
| `tests/test_version_readonly.py` | 1 | T0 to decide | none |
| `tests/test_vnx_doctor_runtime.py` | 1 | T0 to decide | none |
| `tests/test_vnx_recover_runtime.py` | 1 | T0 to decide | none |
| `tests/test_week_1_skills.py` | 1 | T0 to decide | none |
| `vnx_cli/commands/status.py` | 1 | T0 to decide | none |
| `vnx_cli/commands/subsystems.py` | 1 | T0 to decide | none |

## 6. What is left unowned, and what this measurement cannot say

**Unowned volume.** Rule 3 catches most of the census: 43 of 57 long functions, 17 of 18 duplicate groups, all 40 version-suffixed paths, 5 of 7 defensive markers, 14 of 17 dead module functions and 413 of 418 restating comments. "T0 to decide" is an owner in the sense the dispatch allows, but it is not a plan. The three things the track's own scope was written around — the two reconcile functions, the four doubled banners and the one live duplicate pair — are down to zero or to an assigned owner; the residue is everything else the census can see.

**The restating-comment class is not comparable.** 418 today against 81 in the 24-10 inventory is not a 5× regression: the inventory's heuristic ran over its own corpus and its own tokenisation, and the S1 dispatch could not reproduce it even approximately (*Divergence 5*). Only the census's own 429 → 418 is a measurement of change. The class is heuristic by the plan's own label.

**The duplicate class measures something narrower than "duplication".** The census hashes normalised bodies; it does not see near-duplicates. `_govern` versus `_govern_impl` (Jaccard 0.23 on call targets, per the plan) is invisible to it, which is why the plan wanted that pair as an OI rather than a census row — and that OI does not exist.

**Owner cells are adjacency, not ownership.** Rule 2 in §4 reads a file out of a deliverable's `dispatch_paths`. A track that touches `scripts/planning_cli.py` to add one verb is not thereby the owner of a 457-line `_build_parser`; the cell says who is in the file, and the basis column says that is all it says. Where the row matters, the T0 has to convert it into a deliverable or an OI.

**`run_reconcile` at 196 raw lines** is four lines from the plan's boundary. Nothing pins it there: the CI ratchet gates executable lines (111 here, threshold 70, and existing over-threshold functions are grandfathered), not the raw count. A six-line addition re-opens goal point 2 silently.

**Point 3 is not measurable from here at all.** Absence of a panel artefact, a deliverable, an OI and a panelist dispatch is strong evidence the sweep did not run; it cannot prove a sweep was not run and thrown away. The T0 holds that record.

## 7. Open items

| # | Item | Owner |
|---|---|---|
| 1 | Run one panel sweep over the census output (`python3 scripts/panel.py sweep "<question>" --context-file <census.json>`), then file every surviving finding through `vnx deliverable add` or the open-items ledger. Goal point 3 stays `not met` until this exists. | T0 |
| 2 | Decide what to do with the 43 unowned long functions in §5.1; the six biggest are `fulfill_obligation` (795 raw), `glm_gate.main` (573), `kimi_gate.main` (525), `_govern` (498), `_build_parser` (457, reserved to Vincent) and `run_dispatch` (456). | T0 to decide |
| 3 | The near-duplicate OI the plan requires for `_govern` ↔ `_govern_impl` (with the Jaccard 0.23 measurement) does not exist in the open-items ledger. File it, or drop the requirement explicitly. | T0 |
| 4 | `_build_parser` (457 raw / 424 exec) is reserved to Vincent under the plan's *Voorbehouden* 2; the two compat shims (`scripts/receipt_processor_v4.sh`, `scripts/userpromptsubmit_intelligence_inject_v5.sh`) under *Voorbehouden* 1. Both remain as designed. | Vincent |
| 5 | 40 version-suffixed paths stay: 9 migration schema versions, 23 tests named for the schema version they pin, 1 schema (`unified_report_v1.json`), 2 policy series whose `_v1` is a selector value, 1 fixture, 2 compat shims and 2 substring hits (`rp_find_newest_report_mtime.py`, `vnx_exec_new_group.py`). They are not copies. No action; recorded so the class is not re-litigated. | — |
| 6 | 17 dead module functions: 11 live candidates, 6 seeded benchmark fixtures. No owner exists for the 11. | T0 to decide |
| 7 | The plan's reproduction line depends on `PYTHONPATH`; the script-path form does not. If the census is quoted in a later doc, quote `python3 scripts/lib/refactor_census.py --commit <sha>`. | T0 |

## 8. Sources

- Primary — the measurements in this document: `scripts/lib/refactor_census.py` at `7877dbea`, run four ways (§2.1, §2.2, §3), plus `function_size_gate.measure_source` as a second method (§2.2).
- Primary — `scripts/lib/track_reconciler_closure.py` and `scripts/lib/objective_reconcile.py` at `7877dbea`, read through `git show`, and their test suites (`tests/test_close_track_if_done.py`, `tests/test_objective_reconcile.py`, 95 passed).
- Primary — the VNX record read read-only: `$VNX_DATA_DIR/unified_reports/` (reports and panel artefacts), `$VNX_DATA_DIR/state/open_items.json`, `$VNX_DATA_DIR/state/dispatch_register.ndjson`, and `vnx deliverable list --objective slop-refactor-panel-review --json`.
- Secondary — the plan, `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/slop-refactor-panel-review.md` revision 2 (25-10), *Doel* and *Meetcontract S1*.
- Secondary — the pre-instrument inventory `20261024-inv-slop-refactor` (report in `$VNX_DATA_DIR/unified_reports/`), which is where the 24-10 column comes from.
- Secondary — the S1 report `dlv-dec2cd2b8e75` (its *Reproduction on 1ca67db2* section documents the five divergences) and the S2–S5 reports `dlv-92a8405367ec`, `dlv-9aacc366daf4`, `dlv-7ebd662f8161`, `dlv-edf3a609835a`.
