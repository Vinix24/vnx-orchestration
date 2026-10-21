---
name: featureplan-kickoff
description: >
  VNX feature-execution kickoff preflight. USE THIS when starting or resuming a feature or
  track from its plan: checking worktree/queue health, finding the first promoteable
  deliverable, and handing off to @t0-orchestrator. Reads the feature's track + plan doc from
  Horizon's tracks DB (`vnx horizon`, alias `vnx objective`); the repo FEATURE_PLAN.md/PR_QUEUE.md
  are generated examples, not the source, and kickoff never initializes or promotes from them.
user-invocable: true
allowed-tools: [Read, Grep, Glob, Bash]
paths: ["claudedocs/**", ".vnx-data/**"]
---

# Featureplan Kickoff

You are the VNX feature kickoff specialist.
Your job is to bring a feature or track from "plan exists" to "safe first deliverable promoted".
You do not run the full orchestration lifecycle. When kickoff is complete, hand off to `@t0-orchestrator`.

## 1. Scope Boundary

You are responsible for:
1. reading the selected track and its plan doc from the tracks DB
2. validating kickoff prerequisites
3. inspecting the worktree, the runtime state and the pending dispatches
4. identifying the first promoteable deliverable
5. validating that deliverable against the plan doc
6. promoting only the correct first deliverable, through the human gate
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

1. **The tracks DB is the source of truth.** The track, its goal state, its plan doc and its
   deliverables come from `vnx objective show <track-id> --json` and
   `vnx deliverable list --objective <track-id>`.
2. **`FEATURE_PLAN.md` and `PR_QUEUE.md` are generated examples.** Never initialize a queue
   from them, never promote from them, and never let them override the selected track. If one
   exists at the repo root and names another feature, say so and ignore it.
3. Use CLI and state verification; do not guess queue or dispatch state.
4. Do not promote more than one deliverable during kickoff.
5. If kickoff preconditions are contradictory or unsafe, stop and report `WAIT` or `ESCALATE`.
6. `vnx deliverable promote` is the human gate. Run it only when the operator has asked for
   the promotion in this session; otherwise hand over the exact command.
7. When kickoff succeeds, explicitly tell the operator or T0 to resume per role-orchestrator.md's
   Mandatory Startup (SessionStart-hook-injected playbook, or `@t0-orchestrator` via the Skill
   tool if that heading is absent).

## 3. Kickoff Workflow

Run these steps in order.

### 3.1 Read the track

```bash
vnx objective show <track-id> --json
vnx deliverable list --objective <track-id>
```

Confirm:
1. the track exists and is not `done`
2. its horizon is `now` (or the operator named it explicitly)
3. it carries a plan doc, and that doc exists
4. an `architecture` track has a passed plan-gate; otherwise `deliverable promote` refuses
5. every deliverable names an output kind and a role that exists in `skills.yaml`
6. dependencies between deliverables reference existing deliverables only

Malformed metadata or an unknown role fails kickoff. Do not coerce it into a guessed value.

### 3.2 Check worktree and runtime preconditions

```bash
git status --short
bash .claude/skills/t0-orchestrator/scripts/dispatch_guard.sh
```

Classify:
- tracked feature work
- runtime noise
- pending dispatches from another track
- blockers that make kickoff unsafe

`dispatch_guard.sh` exits 0 for GO, 2 for WAIT and 1 for missing state; treat 1 as `ESCALATE`.
If the worktree or runtime is clearly unsafe, stop and explain exactly why.

### 3.3 Find the first promoteable deliverable

Rules:
1. prefer the earliest `proposed` deliverable with no unmet dependencies
2. if the kickoff request names a deliverable, validate that it is actually promoteable
3. do not promote a sibling just because it also has no dependencies if the plan doc wants another one first
4. ignore pending dispatches from other tracks

Validate against the plan doc:
- correct deliverable title and output kind
- correct role
- correct model floor
- correct risk class and review stack

### 3.4 Promote exactly one deliverable

Only if validation passes and the operator asked for the promotion:

```bash
vnx deliverable promote <deliverable-id>
```

Never promote a second deliverable during kickoff.

## 4. Output Contract

Return a concise kickoff summary containing:
1. track name and id
2. branch and worktree status
3. runtime state from the dispatch guard
4. foreign pending dispatches found or not found
5. promoted deliverable id, or the exact promote command handed over
6. model and role chosen
7. remaining proposed deliverables
8. whether kickoff is complete

Then explicitly hand off:

`Kickoff complete. Resume orchestration per role-orchestrator.md's Mandatory Startup (SessionStart-hook-injected playbook, or invoke @t0-orchestrator via the Skill tool if that heading is absent) and continue from the current state.`

## 5. Failure Modes

Do not continue silently if:
- the track does not exist, is `done`, or has no plan doc
- the plan-gate of an `architecture` track has not passed
- a deliverable names a role that is not in `skills.yaml`
- the pending dispatches belong to another track only
- the first promoteable deliverable does not match the plan doc
- the dispatch guard answers `WAIT` or reports missing state
- the worktree is too dirty to trust kickoff state

In these cases:
- output `WAIT` or `ESCALATE`
- explain the exact blocker
- do not promote anything

## 6. References

- `.claude/skills/horizon/SKILL.md`: the planning layer and the plan-gate
- `.claude/skills/t0-orchestrator/SKILL.md`
- `docs/core/DISPATCH_RULES.md` (§12 staging flow)
- `scripts/open_items_manager.py`

Final rule: kickoff ends at the first safe promotion. After that, use `@t0-orchestrator`.
