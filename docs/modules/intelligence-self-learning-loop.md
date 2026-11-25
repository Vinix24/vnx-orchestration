# intelligence-self-learning-loop

Mines receipt usage and failures to adjust confidence and propose prevention rules or stale-pattern retirement. Shadow mode is the default; persistent learning requires approval, the learning and feedback switches, and an effectiveness probe reporting `ok`.

Background: [Self-learning loop](../intelligence/SELF_LEARNING_LOOP.md).

## Entry points

[`scripts/learning_loop.py::LearningLoop.daily_learning_cycle`](../../scripts/learning_loop.py); [`scripts/learning_loop.py::evaluate_activation_gate`](../../scripts/learning_loop.py).

## Tests

[tests/test_learning_loop_shadow_nightly.py](../../tests/test_learning_loop_shadow_nightly.py); [tests/test_learning_loop_gate.py](../../tests/test_learning_loop_gate.py).

## Decisions

[ADR-065](../governance/decisions/ADR-065-leren-is-voorstellen.md), [ADR-068](../governance/decisions/ADR-068-learning-signaal-attributie.md)

## Flag

`VNX_LEARNING_LOOP_ENABLED`
