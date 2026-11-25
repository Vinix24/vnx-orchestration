# evidence-bound-gate

Verifies signed evidence matching the PR diff's content key: test and tier-1 gate evidence for implementation, plus tier-2 panel evidence for closeout. After provenance succeeds, PR verification applies this check in off, advisory or required mode; required failures block and the default advisory mode reports gaps.

Background: [attestation pipeline](../governance/ATTESTATION_ENFORCEMENT.md).

## Entry points

[`scripts/lib/evidence_bound_gate.py::verify_evidence_bound`](../../scripts/lib/evidence_bound_gate.py), [`scripts/lib/verify_pr.py::_evidence_bound_failure`](../../scripts/lib/verify_pr.py).

## Tests

[tests/test_verify_pr.py](../../tests/test_verify_pr.py), [tests/test_env_reads_pr6v_gates.py](../../tests/test_env_reads_pr6v_gates.py).

## Decisions

[ADR-027](../governance/decisions/ADR-027-signed-attestation-enforcement.md), [ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md).

## Flag

`VNX_EVIDENCE_BOUND_GATE` — `off`, `advisory` (default), or `required`.
