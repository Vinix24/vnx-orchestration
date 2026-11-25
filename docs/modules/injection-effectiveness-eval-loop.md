# injection-effectiveness-eval-loop

Records heuristic used/ignored reasons for offered patterns and queues tuning proposals when WHY instrumentation and feedback are both enabled. The separate A/B effect estimator withholds an outcome verdict below 200 resolved dispatches per arm or when other cohort checks refuse.

Background: [Self-learning loop and effect window](../intelligence/SELF_LEARNING_LOOP.md).

## Entry points

[`scripts/lib/injection_effectiveness_probe.py::run_reason_evaluator_and_propose`](../../scripts/lib/injection_effectiveness_probe.py); [`scripts/lib/intel_effect_estimator.py::build_effect`](../../scripts/lib/intel_effect_estimator.py).

## Tests

[tests/test_injection_reason_evaluator.py](../../tests/test_injection_reason_evaluator.py); [tests/test_intel_effect_estimator.py](../../tests/test_intel_effect_estimator.py).

## Decisions

[ADR-069](../governance/decisions/ADR-069-intelligence-effectmeting.md)

## Flag

`VNX_INJECTION_WHY_ENABLED`
