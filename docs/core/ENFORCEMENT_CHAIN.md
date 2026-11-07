# The enforcement chain: from the door to branch protection

> Read against `origin/main` `d8b58c359` (VERSION 1.8.0, tag `v1.8.0` is an ancestor), 2026-11-07. The branch protection quoted below is a `GET` of `repos/Vinix24/vnx-orchestration/branches/main/protection` made the same day. Code is referred to by symbol, not by line, so the page does not rot on the next edit. Where this page and the code differ, the code wins.

## What the chain prevents

An agent that writes code can also open a pull request and merge it. Without a chain, one agent can write a change, judge it fine, and put it on `main` with nobody else having read it.

The chain makes that impossible at five places, one behind the other. A writing task is refused when it carries no reviewer. A merge is refused when the reviewers did not approve the exact commit that is about to land. GitHub refuses the merge as well when its own required checks are not green, and that includes the administrator. Each link answers the same question from a different side, so one link failing open does not open `main`.

The links, in the order a change meets them:

1. The dispatch door refuses a writing task that has no review gate.
2. The review gates run reviewers and write one record per seat, per head commit.
3. The merge door (`scripts/pr_merge.py`) reads those records and runs five preflights.
4. The `vnx-gate` GitHub App publishes the result as the check `vnx-gate/review` on the PR head.
5. Branch protection on `main` requires that check and fourteen CI checks, for administrators too.

## 1. The dispatch door

`scripts/lib/dispatch_cli.py`, function `_fill_gate_and_check_required`: when a dispatch spec carries no `gate` and the smart router derives none, a writing dispatch is rejected with the code `gate-required`. A dispatch counts as writing (`_spec_is_writing`) when it declares a `pr_id`, grants write access on a dispatch path, or has a worker role that may edit.

Two more refusals live in the same door:

- `plan-gate-unresolved` (`_check_track_link_verdict`, helper module `scripts/lib/plan_gate_enforcement.py`): a build on a track whose plan-first gate is unresolved. It blocks only when `VNX_PLAN_GATE_ENFORCE=required`. The default is `advisory`, which warns and lets the dispatch through. An operator override exists (`VNX_OVERRIDE_PLAN_GATE=1`).
- `stash-unrefused-build-lane` (`_stash_build_reject`): a build on a lane that cannot refuse `git stash` (OI-2588), because `refs/stash` is shared by every worktree of the repository.

The lanes and their failure modes: `docs/core/DISPATCH_RULES.md`.

## 2. Review seats

The project's review stack (default `codex_gate,kimi_gate`) is a list of seats. A seat is filled when its own reviewer wrote a complete record with no blocking finding, on the head commit that is about to merge. The rule lives in one module, `scripts/lib/review_seat_evaluation.py`, and every consumer calls its `evaluate_review_seats`: the merge door, `vnx-gate/review`, `vnx pr-ready` and track closure.

- **Exact head.** `on_head` accepts a record only when its `commit_sha` equals the head. An empty head matches nothing.
- **Absence is not a vote.** A reviewer that is missing or failed leaves its seat unfilled. It neither approves nor rejects ([ADR-037](../governance/decisions/ADR-037-gate-uitval-is-afwezigheid-niet-tegenstem.md)). A FAIL or a blocking finding from any reviewer always blocks.
- **Seats follow the risk class.** `seats_for_risk` requires the first seat of the stack at risk class `low` and `medium`, and every seat at `high` and `critical`. An unknown, empty or missing class requires every seat. The class comes from `scripts/lib/review_risk_class.py`: derived from the PR diff against the PR's own `baseRefName`, `high` for a schema, security, gate or merge-logic path or an `irreversible` spec, `high` again for anything it cannot read. A caller can raise the class, never lower it. This is release 1.8.0 (R3a and R3b); before it every seat was required. The module `review_risk_class.py` first appears in `v1.8.0`.
- **Takeover.** When a reviewer is out, the next gate in the chain fills its seat. The default chain is `_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN` in `scripts/lib/gate_request_handler.py`: `codex_gate,kimi_gate,claude_gate,deepseek_gate`. `claude_gate` entered the chain in `v1.7.6` (R7, 2026-10-22); `v1.7.5` had `codex_gate,kimi_gate,deepseek_gate`. A reviewer of the builder's own vendor is passed over like an unavailable one. `glm_gate` left the default chain on 2026-10-17 and stays a registered gate that an explicit chain can name. A taken-over seat carries `takeover_from` in its receipt. With every subscription reviewer out, `deepseek_gate` reads one seat and the other ends as `chain_exhausted`: no merge, no silent re-dispatch.
- **Signal rule (off by default).** `VNX_REVIEW_SEAT_RULE`, read through `active_seat_rule`, narrows or widens the required seats by signal. The merge door and `vnx-gate/review` read the same reader, so they decide alike.

## 3. The merge door

`scripts/pr_merge.py`, function `_run_door`, runs five preflights in the order of `PREFLIGHT_ORDER` and stops at the first refusal. A refusal writes a `pr_merge_refused` receipt and exits non-zero.

| Order | Preflight | Function | What it refuses |
|---|---|---|---|
| 1 | `ci` | `_run_ci_gate` | The project's CI workflow (`ci_workflow` in the protection YAML on `main`, else `VNX_CI_WORKFLOW_NAME`, else `VNX CI`) has no `conclusion=success` on exactly the PR head |
| 2 | `review` | `_run_review_gate` | A required seat is not filled on the head (calls `closure_verifier.check_review_gate_for_merge`, which calls `evaluate_review_seats`) |
| 3 | `adr` | `_run_adr_gate` | A new ADR number that collides |
| 4 | `contract_invalid` | `_run_contract_invalid_gate` | The latest deliverable-outcome receipt of the PR's dispatch is `contract_invalid` |
| 5 | `branch_protection` | `_run_branch_protection_gate` | See below |

The branch-protection preflight runs four steps: (a) the door's own files are unchanged (`_DOOR_INTEGRITY_PATHS`, eight files), (b) the protection YAML on `main` is read, (c) the live protection is compared to that YAML, (d) the YAML on the PR head must not weaken it.

After the preflights, `merge_pr` calls `_do_merge`, which runs `gh pr merge --match-head-commit <sha>` so GitHub refuses any other commit. Only when GitHub reports `MERGED` (`_pr_actually_merged`) does `_emit_receipt` write the binding `pr_merged` receipt. A raw `gh pr merge` runs none of these preflights. Branch protection is the answer to that (section 5).

### How strict the branch-protection preflight is

The field `enforcement` at the top of `.vnx/branch_protection.yaml`, else `scripts/forge/branch_protection.yaml` (`PROTECTION_YAML_SEARCH_PATHS`, first existing file wins). Modes and default are in `scripts/lib/forge_protection_drift.py` (`ENFORCEMENT_MODES`, `DEFAULT_ENFORCEMENT` is `enforce`).

| Mode | Drift between YAML and live protection | CI workflow floor |
|---|---|---|
| `enforce` | Refuses. No override. | Applies |
| `warn` | Reported, the merge continues. A read error other than "branch not protected" (auth, network, rate limit) still refuses | Applies |
| `off` | Steps (c) and (d) are skipped (`_judge_enforcement_off`) | Applies |
| no file | Treated as `warn` with reason `protection_file_missing` (`_judge_project_without_protection_file`) | Applies |

Lowering the mode (`enforce` to `warn` to `off`) and any change of `ci_workflow` needs `--allow-weaken "<reason>"` in every mode (`is_weakening`, `_judge_ci_workflow_floor`). The CI check is the floor under every setting.

## 4. `vnx-gate/review`

The merge door runs on the operator's machine. GitHub does not know it. To let GitHub enforce the review as well, the review result is published as a check run on the PR head by the GitHub App `vnx-gate` (app id `4869217`).

- `scripts/lib/gate_recorder.py` `publish_forge_check_run` publishes one check per gate (`vnx-gate/<gate>`), and `publish_forge_review_summary` publishes the summary. Both call `scripts/lib/forge_gate_publisher.py`.
- `review_verdict` computes the summary verdict through `evaluate_review_seats`, with the same risk class and base branch as the door. `success` only on a proven pass by every required seat on the head (`_proven_pass_on_head`). `failure` on a rejection. `action_required` on absence. Never `neutral` or `skipped`, because GitHub reads those as satisfied. `check_run_name` refuses the name `review` for a single gate, so no gate can impersonate the summary.
- A capped PR that the door may merge with `--override-b3` gets the same verdict from `refresh_review_summary`, with the reason in the check text.
- The HTTP side and the App authentication are in `scripts/lib/forge_check_run.py`: `load_app_config`, `read_private_key`, `read_installation_id` (both from the keychain), `build_app_jwt`, `installation_token`, `publish_check_run`.
- The App is bound in `scripts/forge/branch_protection.yaml`: the context `vnx-gate/review` with `app_id: 4869217`, and the `app:` block (`slug: vnx-gate`). `parse_protection_config` refuses a `vnx-gate/*` entry with another or a missing `app_id`, so no other app and no workflow can satisfy that check.

Operator runbook, App creation and the manual fallback: `docs/operations/FORGE_GATE.md`.

## 5. Branch protection on `main`

`GET repos/Vinix24/vnx-orchestration/branches/main/protection`, 2026-11-07. These are the 15 required contexts, in the order GitHub returns them:

```
"Profile A (doctor + core tests)"
"Profile B (snapshot integration)"
"Profile C (adoption smoke tests)"
"Profile D (pip install smoke)"
"ADR-003: No Anthropic SDK Imports"
"Anchor Immutability Check"
"Dashboard TS Lint (fixture-gate + typecheck)"
"Dispatch-ID Slug-Match Gate"
"docs/core/SUBSYSTEMS.md matches the live registry"
"Lint Patterns (silent-except + atomic-write)"
"Repo-local state-pin gate (#1043 footgun)"
"Trace Token Validation"
"secret scan (gitleaks)"
"vnx doctor smoke"
"vnx-gate/review"
```

Fourteen are bound to app id `15368` (GitHub Actions). `vnx-gate/review` is bound to `4869217`. The other settings:

| Setting | Value | Meaning |
|---|---|---|
| `enforce_admins` | `true` | The administrator is held to the same checks |
| `required_approving_review_count` | `0` | GitHub asks for no human approval. The review is the check `vnx-gate/review` |
| `strict` | `false` | A head need not be up to date with `main` to merge. The door's CI gate and `--match-head-commit` pin the head instead |
| `required_signatures` | `false` | Commits need no signature |
| `allow_force_pushes` / `allow_deletions` | `false` / `false` | `main` cannot be rewritten or removed |

This equals `scripts/forge/branch_protection.yaml` on `origin/main`, so at the moment of reading there is no drift. The door and `vnx doctor` (`check_branch_protection_drift`) check it on every merge and every run. The required contexts are job names, not workflow names: `VNX CI` as a workflow is not a required context, its jobs are.

`Red on base` is not in this list.

## Red on base: a check that is not required

`.github/workflows/red-on-base.yml`, job `red-on-base` (check name `Red on base (claimed tests prove a behaviour change)`), first in `v1.8.0` (#2310).

- **What it does.** A PR description may claim that new tests were red on the old code. `scripts/ci/red_run_claims.py` reads those claims, `scripts/ci/red_on_base.py` runs the PR's test files on the merge base and on the head in two worktrees, and judges each test as `behavior`, `not_behavior`, `green_on_base`, `fails_on_head`, `skipped` or `incomplete`. It fails on `fails_on_head`, on `incomplete`, and on a claimed test that was not red on base. A test that was green on base and was not claimed stays neutral. The verdict is an artifact, `red-on-base-verdict`.
- **Why a workflow of its own.** It also runs on `edited`, so a change of the PR description re-runs this check and nothing else. Inside `vnx-ci.yml` an `edited` run would skip every other job, and a run of skipped jobs concludes `success`: an edit could then turn a red head green for the door's CI gate.
- **Not required.** It is not in the 15 contexts, so GitHub does not stop a merge on it, and the door's `ci` preflight reads `VNX CI`, not this workflow.

## Which required check enforces what

"Document" is the place that explains the check, not only names it. A dash means no document explains it yet.

| Required check | Produced by | What it enforces | Document |
|---|---|---|---|
| Profile A (doctor + core tests) | `.github/workflows/vnx-ci.yml` job `profile-a` | `vnx doctor` and the test suite, in a serial and a parallel (xdist) phase with a completeness step | `docs/core/CI_PROFILE_A.md` |
| Profile B (snapshot integration) | `vnx-ci.yml` job `profile-b`, which needs `profile-a` | `scripts/` and `tests/` copied into a snapshot laid out as an installed `.claude/vnx-system`, which then runs `tests/test_pr_recommendation_integration.py`. Before that the job fails on a legacy `state/` path literal in `scripts/` | this page |
| Profile C (adoption smoke tests) | `vnx-ci.yml` | The path-resolution regression tests, the docs-command validation tests and the quickstart validation tests | `docs/core/LOCKS_AND_RELEASES.md` (stage 5) |
| Profile D (pip install smoke) | `vnx-ci.yml` | `scripts/ci/pip_install_smoke.sh`: the package builds and installs | `docs/operations/PACKAGE_BUILD.md` |
| ADR-003: No Anthropic SDK Imports | `.github/workflows/anthropic-sdk-block.yml` | No `import anthropic` in repo code | `CONTRIBUTING.md`, `docs/core/LOCKS_AND_RELEASES.md` |
| Anchor Immutability Check | `.github/workflows/anchor-immutability-check.yml` | `governance/chain-origin.ndjson` only gains new keys: a line that existed on the base branch may not change or disappear. It runs on `pull_request_target`, so the trusted base-branch version of the job runs, never the PR's copy ([ADR-034](../governance/decisions/ADR-034-external-chain-origin-anchor.md)) | this page |
| Dashboard TS Lint (fixture-gate + typecheck) | `vnx-ci.yml` job `dashboard-lint` | `npm run test:lint` in `dashboard/token-dashboard`: the fixture gate and the TypeScript typecheck | this page |
| Dispatch-ID Slug-Match Gate | `vnx-ci.yml` | A commit body carries a `Dispatch-ID:` whose slug matches the branch slug (`scripts/check_ci_slug_match.py`). The workflow sets `VNX_SLUG_ENFORCEMENT` to `"0"`, which is shadow mode: it reports and does not fail | `docs/core/DISPATCH_RULES.md` |
| docs/core/SUBSYSTEMS.md matches the live registry | `.github/workflows/subsystems-drift.yml` | The deterministic columns of the subsystem ledger equal `scripts/lib/config_registry.py`, and a LIVE status carries measured health | `docs/core/SUBSYSTEMS.md` |
| Lint Patterns (silent-except + atomic-write) | `vnx-ci.yml` | Silent excepts and non-atomic state writes, the function-size ratchet ([ADR-052](../governance/decisions/ADR-052-functies-onder-200-in-scripts.md)), the changelog-fragment rules | `docs/core/DISPATCH_RULES.md`, `changelog.d/README.md` |
| Repo-local state-pin gate (#1043 footgun) | `vnx-ci.yml` job `state-pin-gate` | `ripgrep` bans a `VNX_STATE_DIR` pinned to a repo-local `.vnx-data` path across scripts, hooks, templates, skills, role files, `bin` and `vnx_cli`. Such a pin breaks central-mode resolution and forks the audit trail | this page |
| Trace Token Validation | `vnx-ci.yml` | The PR's own new commits (merge base to head) carry a valid trace token (`scripts/ci_trace_token_check.sh`, enforced since 2026-09-03). History on `main` is not re-checked | `docs/core/LOCKS_AND_RELEASES.md` |
| secret scan (gitleaks) | `.github/workflows/public-ci.yml` | `gitleaks` over the files that `install.sh` ships into a fresh project (`.gitleaks.toml`) | `docs/operations/PROVIDER_API_KEYS.md` |
| vnx doctor smoke | `public-ci.yml` job `smoke` | A fresh `install.sh` into an empty project, then `vnx init` and `vnx doctor` succeed | this page |
| vnx-gate/review | the GitHub App `vnx-gate`, no workflow | A proven review pass by every required seat on this head (section 4) | `docs/operations/FORGE_GATE.md` |
| *not required:* Red on base | `.github/workflows/red-on-base.yml` | Claimed red runs are true | this page |

Two more checks appear on PR heads without being required: `Context-rotation plugin tests (Python 3.9/3.12)` in `vnx-ci.yml`, and `Gov-D3: Attestation signature gate (advisory)` in `.github/workflows/attestation-gate.yml`, which runs with `continue-on-error: true` and reports without blocking.

## See also

- `docs/core/LOCKS_AND_RELEASES.md`: the register of every lock between a dispatch and a merge, and every way each opens.
- `docs/core/DISPATCH_RULES.md`: lanes, the door, the merge door's exit codes.
- `docs/operations/FORGE_GATE.md`: the operator runbook for the App and the check run.
- `docs/manifesto/GOVERNANCE_ARCHITECTURE.md` section 10: the same chain in summary.
