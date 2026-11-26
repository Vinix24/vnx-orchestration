# Sample review: ten merged Codex-built PRs from 25 November 2026

**Dispatch-ID**: steekproef-20261126-codex-review-tien-prs
**Model**: gpt-6-astra
**Provider**: codex

## Summary

**3 of 10 PRs fail (30%)**, with three verified errors and one non-blocking warning. The comparison supplied by the dispatch is **2% for the review seats** and **11% for Codex reviewing other builders in the preceding week**. This sample is deliberately selected and risk-weighted (six high, four medium), not a random or blinded experiment; the difference does not establish either model quality or reviewer leniency across the population. Test failures alone did not determine these verdicts.

| PR | Risk | Verdict | Errors | Warnings | Still on main |
|---|---|---|---:|---:|---|
| #3158 | high | fail | 1 | 0 | yes |
| #3169 | high | pass | 0 | 0 | no |
| #3171 | high | pass | 0 | 0 | no |
| #3174 | medium | pass | 0 | 1 | yes |
| #3183 | medium | pass | 0 | 0 | no |
| #3185 | high | pass | 0 | 0 | no |
| #3186 | high | fail | 1 | 0 | yes |
| #3189 | medium | pass | 0 | 0 | no |
| #3192 | high | fail | 1 | 0 | yes |
| #3196 | medium | pass | 0 | 0 | no |

“Still on main” means at least one reported finding reproduces on `origin/main` at `e0d46e7b88f9e6e86224dcec02a4f408010b9cdc`. “No” means no finding was established for that PR, not that a defect was fixed. None of the four findings is fixed since merge.

## Changes

Review-only, no code changed. The only tracked addition is `claudedocs/2026-11-26-steekproef-codex-review/rapport.md`. Findings are measurements; no fixes, gate configuration changes, or runtime-state repairs were made. The completion report and JUnit evidence are separate artifacts in `$VNX_DATA_DIR/unified_reports/`.

## Verification

Read `gh pr diff <number>` for all ten PRs, checked GitHub's `headRefOid` against every supplied merged head, and extracted each exact tree with `git archive` into separate scratch directories. No reviewed head was checked out over the report worktree. Each cited finding line was verified with `git show <full-sha>:<path>` and checked programmatically to be an **added line of that PR's diff**.

Inspected changed implementations, their callers, their test changes, and the ADR-003/005/010 contracts. Checked project identity and tenant boundaries, error versus absence handling, audit-before-state ordering, provider routing, and fail-closed behavior. No additional SDK or Claude transport violation was established in these diffs. A pass means no verified in-scope blocker found, not proof of defect-free code.

Tests used `/opt/homebrew/opt/python@3.12/bin/python3.12 -m pytest <files> -q -p no:cacheprovider --junitxml=<artifact> -o junit_suite_name=steekproef-20261126-codex-review-tien-prs`. Selection was every changed `tests/test_*.py` file in each PR; `conftest.py` was loaded normally. Also ran #3196's direct neighbour `tests/test_streaming_drainer.py` (32 passed). Never ran `pytest tests/` or a full suite. The changed CI-excluded `test_cli_init.py` (#3183) and `test_quickstart_validation.py` (#3186) were included explicitly.

| PR | Exact merged head | Targeted test result |
|---|---|---|
| #3158 | `1adf7364451bba83ee3585862be650e105561d75` | 48 passed |
| #3169 | `ae64e1fb92a4b9744a10856f5ed40d69f06bdd9c` | 94 passed |
| #3171 | `7bcb6c5627ea6fb13bc4515dd04b6f4764bdc2b2` | 173 passed; 12 pytest runtime warnings |
| #3174 | `bd8b26025764d12d21eb9aa0fce4d43b4968124b` | 65 passed |
| #3183 | `1ab3f54d8ccb3a5b460b1f8a1f317bf7977117f4` | 197 passed, 2 skipped |
| #3185 | `4519e02d63e301c65156275d0415d2f1e4d6828f` | 134 passed |
| #3186 | `b82e5412f1f9b79fabc2379df98fe77283ceade0` | Initial interrupted run: 470 passed, 2 failed; continuation/retries: 279 + 8 passed (757 unique cases finally passed) |
| #3189 | `e58e26cb01eea5d1ddd1a45f033c68db777ee162` | 85 passed |
| #3192 | `22faf104570fa1f3fc46f019b42809555cd49242` | Initially 445 passed, 1 failed; affected file rerun: 2 passed |
| #3196 | `e4426bb5097bb7a3edc86625b6b9d14ef2d8769c` | 8 passed; direct neighbour: 32 passed |

The three initial test failures were archive-environment failures, not product findings: #3186's `test_every_other_written_file_matches_origin_main` needs historical `git show`, its `test_every_finding_has_a_table_row_and_every_row_a_finding` needs `git ls-files`, and #3192's `test_no_deleted_reads` also needs `git ls-files`. Scratch-only Git indexes and read access to the existing object database supplied those requirements; all three then passed without editing implementation or test files. The first #3186 run was interrupted after 360 seconds in installer-driven tests; pytest's teardown also raised `KeyError`. Its JUnit file retained 470 passed and 2 failed cases; the remaining selections were run separately. An initial continuation used a nonexistent test class and collected no tests; the corrected class was run afterward. These setup/interruption limitations are disclosed rather than reclassified as a product failure or as a pre-fix red run.

Eight additional scratch-only checks assert the four reported failure scenarios, once at each merged head and once at the pinned main: **8 passed**. A passing reproduction means the documented wrong behavior was observed, not that the defect was repaired. These checks used real permission denial, real marker writes in temporary stores, a corrupt temporary SQLite file, and the actual shell root-resolution/config-snapshot code in an embedded layout; the embedded CLI was a deterministic stand-in returning the configured values, with no provider or email call.

JUnit files are named `steekproef-20261126-codex-review-tien-prs.pr<PR>[suffix].junit.xml` and `steekproef-20261126-codex-review-tien-prs.probes.junit.xml`, beside the unified report. The combined `steekproef-20261126-codex-review-tien-prs.junit.xml` retains completed attempts, including the three initial failures and reruns: 2,048 passed, 3 failed, 2 skipped (2,053 recorded executions). It is an execution count, not a count of unique tests. Logs and reproduction scripts remain in `/tmp/vnx-ten-pr-review.zhR1ce/` for local inspection.

No fix was implemented, so no new-tests-on-old-code red run applies. Publication validation of the single documentation addition is recorded in the unified completion report after commit and `vnx pre-push-check`.

## Open Items

Findings are ranked errors first, then warnings. Each location below is a new line at the stated merged head; supporting unchanged code is identified as context, not a separate blocker.

### #3158 — fail — error: inaccessible dispatch evidence passes the base check

Location: [`scripts/pr_merge.py:716`](https://github.com/Vinix24/vnx-orchestration/blob/1adf7364451bba83ee3585862be650e105561d75/scripts/pr_merge.py#L716).

**Failure scenario:** with a staged spec requesting `other` and a PR targeting `main`, making the store's `dispatches/` directory unreadable changes the result from `NO-GO/base_ref_mismatch` to `GO/base_ref_not_applied`, allowing the remaining merge gates to proceed without verifying the requested base.

Reproduced using a real temporary directory: the accessible spec refused; `chmod(0)` made `iterdir()` raise `PermissionError`, but the new guard returned GO with “no dispatch-spec.json”. The existing `review_vendor.dispatch_spec_paths()` uses `Path.glob`, which suppresses directory-scan permission failures; the new guard therefore never reaches its `except OSError` and interprets unreadable evidence as absence. This contradicts the PR's explicit unreadable-evidence refusal. Its new tests cover a failed **file read**, not a failed **directory enumeration**. Check enumeration errors before applying the absence warning.

**Main:** still on main at `e0d46e7b88f9e6e86224dcec02a4f408010b9cdc`; the same two-step permission probe gives the same results. The helper's pre-existing glob behavior is not separately counted; the finding is its new use as the merge-base guard's evidence boundary.

### #3186 — fail — error: a refused init persists the conflicting project identity

Location: [`scripts/vnx_init.py:552`](https://github.com/Vinix24/vnx-orchestration/blob/b82e5412f1f9b79fabc2379df98fe77283ceade0/scripts/vnx_init.py#L552).

**Failure scenario:** initializing an unmarked `.../.vnx-data/project-a/state` store with stale `VNX_PROJECT_ID=project-b` writes marker `project-b` before refusing the layout conflict, and retrying after removing the stale environment still refuses because init itself has persisted the wrong tenant marker.

Reproduced with `anchor_init_project_id()` and temporary stores: both invocations raised `RuntimeError`, and the marker remained `project-b` after each. Publication of the marker precedes `resolve_init_project_id()` at lines 553–557; validation failure has already changed the source of truth for the next attempt. The existing conflict tests start with a marker already present, so they do not exercise this first-write failure. Validate all existing identity sources before publishing the new marker (or undo only the marker this failed attempt created).

**Main:** still on main at `e0d46e7b88f9e6e86224dcec02a4f408010b9cdc`; the corrected-environment retry remains blocked. No tenant row contamination is claimed: the proven defect is persistent incorrect identity and failed recovery after correcting the input.

### #3192 — fail — error: embedded nightly jobs resolve the CLI under the consumer root

Location: [`scripts/conversation_analyzer_nightly.sh:214`](https://github.com/Vinix24/vnx-orchestration/blob/22faf104570fa1f3fc46f019b42809555cd49242/scripts/conversation_analyzer_nightly.sh#L214). The same new pattern appears in `scripts/nightly_intelligence_pipeline.sh:267` and `scripts/launchd/reload_receipt_classifier.sh:62`.

**Failure scenario:** in a supported embedded installation under `consumer/.claude/vnx-system`, with DeepSeek and a digest recipient configured, the new lookup invokes nonexistent `consumer/bin/vnx`, substitutes `ollama-only` and an empty recipient, skips the DeepSeek key-loading condition and skips email delivery.

Reproduced the actual new resolver/snapshot block inside a temporary consumer Git repository whose engine CLI returns `deepseek-harness` and `ops@example.com`: root resolution selected the consumer, the lookup reported “No such file or directory”, and the block exited 0 with `strategy=ollama-only recipient=`. `install.sh --layout claude` places the CLI under `.claude/vnx-system/bin/vnx`; the default `.vnx/bin/vnx` layout has the same mismatch. The Python analyzer independently reads its strategy, so the shell fallback does not switch that later reader to Ollama; it can instead leave DeepSeek selected without loading its required key. The new shell tests supply a fake `VNX_ROOT/bin/vnx` and slice out the root-resolution statements, hiding the installation-layout failure. Resolve the executable from the installed engine while retaining the consumer's project context.

**Main:** still on main at `e0d46e7b88f9e6e86224dcec02a4f408010b9cdc`; the embedded snapshot probe reproduces unchanged. The report does not claim a real email was sent or a live provider job was attempted.

### #3174 — pass — warning: the ordinary runtime getter still falls back on a corrupt store

Location: [`scripts/lib/config_registry.py:1109`](https://github.com/Vinix24/vnx-orchestration/blob/bd8b26025764d12d21eb9aa0fce4d43b4968124b/scripts/lib/config_registry.py#L1109).

**Failure scenario:** with a corrupt `runtime_coordination.db`, `config_runtime.get("VNX_CI_RECORD_MAX_CALLS")` returns default `1000` and source `default`, while `get_for_project()` on the same store correctly raises a `project_config_error` instead of supplying a budget.

Reproduced both public read paths against the same temporary non-SQLite file. The new strict flag only handles exceptions that escape a resolver; `config_runtime.autowire()` supplies the existing non-raising DB resolver, which converts this error to a missing row first. The new tests use an explicitly raising resolver for this failure case. This is a warning, not a blocking claim about a running collector: the PR intentionally registers these keys ahead of M3a/M3b (`read_site_wired=False`), and no production collection reader of this key exists at the reviewed head or checked main. Preserve the error through the ordinary runtime path before that consumer is wired.

**Main:** still on main at `e0d46e7b88f9e6e86224dcec02a4f408010b9cdc`; the runtime/default versus explicit-store/refusal discrepancy remains.

### #3169 — pass — no verified in-scope finding

Checked module-relative skill loading, missing/invalid rubric refusal, prompt ordering around untrusted diffs, all class/trigger names, and the existing advisory-result contract. The PR explicitly chooses advisory prompt policy rather than changing result classification; no guarantee that model verdicts become deterministic is inferred. All three changed test files passed.

### #3171 — pass — no verified in-scope finding

Checked both inline and file-reference plan prompts, required-section extraction, pre-dispatch INFRA_FAIL, prefix counting, retained tenant attribution and seat-ledger behavior. Both changed test files passed, including invalid/unreadable rubric cases and preservation of findings in the existing record shape.

### #3183 — pass — no verified in-scope finding

Checked preparation without launchctl for daily metrics, seed/collector lock compatibility, preservation of existing evidence, invalid IDs and symlink refusal, required-job doctor output, and interpreter scanning after removal of full-line comments. All seven changed test files ran; two no-Homebrew interpreter-selection cases skipped because Homebrew 3.12 is installed. The test harness intercepted launchctl, so this is not a claim that a live scheduled job was installed.

### #3185 — pass — no verified in-scope finding

Checked one pinned protection snapshot, target-repository reads, explicit CI waiver versus identity refusal, malformed/unreadable optional-state refusal, preservation of waiver evidence on refusal/merge receipts, and server verification under warn/off/enforce. All three changed test files passed. No live GitHub merge or branch-protection mutation was performed.

### #3189 — pass — no verified in-scope finding

Checked locked intent/replace/commit ordering, transaction UUID and replacement hash matching, recovery before/after replacement, legacy audit prefixes, ambiguous hand-edited evidence, and read warnings without recovery writes. All three changed test files passed. Crash tests inject failures at transaction boundaries; they do not establish physical power-loss durability of the filesystem.

### #3196 — pass — no verified in-scope finding

Checked the Codex adapter's exact forwarding of `float(spec.deadline_seconds)`, rejection before spawn for invalid input, zero deadline behavior, and the shared chunk-stall policy. The new file and the direct streaming-drainer neighbour passed. Longer total deadlines still permit the documented earlier chunk-stall timeout.

### Separate pre-existing / environmental observations

No untouched defect is promoted to a blocker or included in the fail-rate numerator. The three missing-Git test failures and interrupted installer run above are verification limitations resolved by scratch setup and continuation. Existing soft DB resolution and glob behavior are described only where newly introduced callers or contracts make them relevant. No other independently verified out-of-scope defect is reported.
