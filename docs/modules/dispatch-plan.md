# dispatch-plan

Compiles a validated dispatch specification and runtime snapshot into an execution plan or an explicit rejection. It checks promotion, role, task class, skill and constraints, then fixes lane, billing, model, target, worktree isolation and report requirements.

Background: [dispatch architecture](../core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md), [enforcement chain](../core/ENFORCEMENT_CHAIN.md).

## Entry points

[`scripts/lib/dispatch_plan.py::compile_plan`](../../scripts/lib/dispatch_plan.py), [`scripts/lib/dispatch_plan.py::ExecutionPlan`](../../scripts/lib/dispatch_plan.py).

## Tests

[tests/test_dispatch_plan.py](../../tests/test_dispatch_plan.py).

## Decisions

[ADR-006](../governance/decisions/ADR-006-staging-promote-human-gate.md), [ADR-024](../governance/decisions/ADR-024-single-entry-door-default-dispatch-lane.md), [ADR-025](../governance/decisions/ADR-025-raw-file-dispatch-deprecation.md).

## Flag

—

D4 and the door share `_resolve_model`: an explicit builder model wins, followed
by the provider operator override through its existing resolver (`VNX_CODEX_MODEL`,
`VNX_KIMI_MODEL`, or `VNX_LITELLM_MODEL`). With no override, `resolve_worker`
selects the terminal env/config pair, project worker registry,
then the packaged default. A provider-only request uses that pair when its
provider matches, otherwise that provider's `wave7_models.yaml` `default_model`.
The door passes `resolve_consumer_project_root()` explicitly, using the same
consumer root as worktree allocation instead of the engine CWD. Importing
`resolve_worker` does not load the legacy singleton; it loads on first legacy
access, so a fresh door process never reads a CWD registry at import. D4 uses
`snapshot.repo_root` when resolving independently and retains the door-checked
`effective_model` otherwise. A missing model source, an unresolvable root, or
an unreadable or wrong-shape selected registry blocks as
`worker_model_unresolved`, naming slot, provider and registry key. An operator override is honoured or the dispatch is refused.
Builder pins (`workers-kimi-pinned`, historical id retained) only warn on a
mismatch; they never supply a model. T0's floor and the Claude override audit
gate remain enforced. Broker pair propagation is a separate migration.
