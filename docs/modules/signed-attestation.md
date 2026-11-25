# signed-attestation

Builds governed lineage manifests, signs them with a caller-supplied SSH key and appends them to the attestation hash chain. Verification checks the detached signature against an allowed-signers file; key provisioning remains an operator step.

Background: [attestation pipeline](../governance/ATTESTATION_ENFORCEMENT.md), [key provisioning](../governance/KEY_PROVISIONING.md).

## Entry points

[`scripts/lib/attestation.py::emit_governed_attestation`](../../scripts/lib/attestation.py), [`scripts/lib/attestation.py::verify_attestation`](../../scripts/lib/attestation.py).

## Tests

[tests/test_attestation.py](../../tests/test_attestation.py), [tests/test_attest_d2.py](../../tests/test_attest_d2.py).

## Decisions

[ADR-027](../governance/decisions/ADR-027-signed-attestation-enforcement.md).

## Flag

`VNX_ATTESTATION_REQUIRED` — display metadata only; registering it does not enable signature enforcement.
