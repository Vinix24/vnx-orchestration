# governance-enforcement-stack

Combines receipt chaining, signed attestations and evidence verification around PR attribution. The PR verifier checks feature changes using signers trusted by the base branch; docs/tests-only changes are exempt and evidence checking is advisory by default.

Trace-token CI, `vnx install-git-hooks`, the installed `commit-msg` hook and the Python validator read provenance switches in order: emergency override, project row, global config, environment, registry default. A project row of `0` therefore overrides environment `1`; a row of `1` blocks tokenless commits without an environment export. Unreadable or malformed configuration retains enforcement and refuses legacy-only tokens. Shell readers locate `bin/vnx` in their own installation, including embedded installs and symlinked hooks.

Background: [attestation pipeline](../governance/ATTESTATION_ENFORCEMENT.md), [enforcement chain](../core/ENFORCEMENT_CHAIN.md).

## Entry points

[`scripts/lib/verify_pr.py::verify_pr`](../../scripts/lib/verify_pr.py), [`scripts/lib/attestation.py::emit_governed_attestation`](../../scripts/lib/attestation.py).

## Tests

[tests/test_verify_pr.py](../../tests/test_verify_pr.py), [tests/test_attestation.py](../../tests/test_attestation.py).

## Decisions

[ADR-023](../governance/decisions/ADR-023-receipt-hash-chain.md), [ADR-027](../governance/decisions/ADR-027-signed-attestation-enforcement.md), [ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md).

## Flag

`VNX_GOVERNANCE_ENFORCED` — the ledger's master flag is display metadata only, with no enforcement read site.
