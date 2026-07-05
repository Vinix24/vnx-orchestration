---
name: featureplan-kickoff
description: >
  VNX feature-execution kickoff preflight. USE THIS when starting or resuming a feature or
  track from its plan: checking worktree/staging health, finding the first promoteable
  dispatch, and handing off to @t0-orchestrator. Reads the feature's track from the
  tracks DB (`vnx objective`). NOTE: this skill is flagged for retirement review — its
  original FEATURE_PLAN.md/PR_QUEUE.md workflow has been retired; remaining scope is
  staging inspection + dispatch promotion only.
user-invocable: true
allowed-tools: [Read, Grep, Glob, Bash]
paths: [".vnx-data/**"]
---

# Featureplan Kickoff

> **RETIREMENT NOTE (2026-07-05):** `FEATURE_PLAN.md`, `PR_QUEUE.md`, and
> `pr_queue_manager.py` are retired. Sections 3.1–3.3 of the original workflow no longer
> apply. This skill retains value for steps 3.4–3.5 (staging inspection + first dispatch
> promotion). Consider consolidating into `@t0-orchestrator` in a follow-on PR.

You are the VNX feature kickoff specialist.
Your job is to bring a feature from "plan exists" to "safe first dispatch promoted".
You do not run the full orchestration lifecycle. When kickoff is complete, hand off to `@t0-orchestrator`.

## 1. Scope Boundary

You are responsible for:
1. reading the active track from `vnx objective list --project-id <pid>`
2. validating kickoff prerequisites
3. inspecting staging and filtering stale or foreign dispatches
4. identifying the first promoteable dispatch
5. validating dispatch metadata against the track deliverables
6. promoting only the correct first dispatch
7. returning a kickoff status summary
8. instructing the system to continue with `@t0-orchestrator`

You are not responsible for:
- ongoing receipt review
- open-items lifecycle beyond kickoff blockers
- PR completion decisions
- closure decisions
- merge readiness
- roadmap advancement after kickoff

## 2. Runtime Rules

1. Use `vnx objective list --project-id <pid>` as the active feature source of truth.
2. Use CLI and state verification; do not guess queue or dispatch state.
3. Do not promote more than one new dispatch during kickoff.
4. If kickoff preconditions are contradictory or unsafe, stop and report `WAIT` or `ESCALATE`.
5. When kickoff succeeds, explicitly tell the operator or T0 to load `@t0-orchestrator` next.

## 2.1 Dispatch Metadata Field Conventions

### Skill syntax

- In dispatch headers, always write skills as `@skill-name`
- Valid example: `**Skill**: @backend-developer`
- Invalid: `**Skill**: @developer`, `**Skill**: developer`, `**Skill**: /backend-developer`

### Required metadata field format

- `**Track**: A|B|C`
- `**Priority**: P0|P1|P2`
- `**Skill**: @valid-skill-name`
- `**Dependencies**: []` or `[PR-1, PR-2]`
- `**Risk-Class**: low|medium|high`
- `**Merge-Policy**: human|conditional|auto`
- `**Review-Stack**: comma,separated,values`

## 3. Kickoff Workflow

### 3.1 Check worktree and runtime preconditions

```bash
git status --short
vnx objective list --project-id <pid>
ls .vnx-data/dispatches/staging/ 2>/dev/null
```

Classify: tracked feature work, runtime noise, stale staging, blockers.

If unsafe, stop and explain exactly why.

### 3.2 Find the first promoteable dispatch

Rules:
1. Prefer the earliest deliverable with no unmet dependencies (from `vnx objective list`)
2. If the kickoff request names a specific dispatch, validate it is actually promoteable
3. Ignore stale dispatches from other features / branches

Validate the dispatch header:
- correct `PR-ID` / deliverable reference
- correct `Role`, `Track`, `Skill`, `Risk-Class`, `Merge-Policy`, `Review-Stack`

### 3.3 Promote exactly one dispatch

Only if validation passes:

```bash
vnx dispatch <dispatch-id>   # or the governed promotion path
```

Never promote a second dispatch during kickoff.

## 4. Output Contract

Return a concise kickoff summary:
1. feature name (from tracks-DB)
2. branch and worktree status
3. stale staging found or not found
4. promoted dispatch id
5. remaining waiting deliverables
6. whether kickoff is complete

Then hand off:

`Kickoff complete. Load @t0-orchestrator and continue normal orchestration.`

## 5. Failure Modes

Stop and output `WAIT` or `ESCALATE` if:
- no track or deliverables can be found in the tracks-DB
- staging contains only foreign or stale dispatches
- the first promoteable dispatch metadata is invalid
- the worktree is too dirty to trust kickoff state

## 6. References

- `.claude/skills/t0-orchestrator/SKILL.md`
- `scripts/open_items_manager.py`
- `vnx objective` CLI (`planning_cli.py`)

Final rule: kickoff ends at first safe dispatch promotion. After that, use `@t0-orchestrator`.
