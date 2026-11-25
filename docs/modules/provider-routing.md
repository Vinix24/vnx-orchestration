# provider-routing

Resolves model identities against the provider registry and evaluates route constraints before dispatch. The plan compiler selects the execution lane, model and billing class from the validated request and runtime snapshot.

Background: [provider routing contract](../core/100_VERIFIED_PROVIDER_MODEL_ROUTING_CONTRACT.md), [provider lanes](../core/PROVIDER_LANES.md).

## Entry points

[`scripts/lib/providers/provider_registry.py::resolve_model_identity`](../../scripts/lib/providers/provider_registry.py), [`scripts/lib/providers/constraint_enforcer.py::ConstraintEnforcer.check_constraints`](../../scripts/lib/providers/constraint_enforcer.py), [`scripts/lib/dispatch_plan.py::compile_plan`](../../scripts/lib/dispatch_plan.py).

## Tests

[tests/test_constraint_enforcer.py](../../tests/test_constraint_enforcer.py), [tests/test_dispatch_plan.py](../../tests/test_dispatch_plan.py).

## Decisions

[ADR-003](../governance/decisions/ADR-003-oauth-only-claude-routing.md), [ADR-010](../governance/decisions/ADR-010-subprocess-adapter-claude-routing.md), [ADR-036](../governance/decisions/ADR-036-model-identity-from-registry.md).

## Flag

—
