# blast-radius

Compares a proposed fire's paths with live work and open PR scopes. Incomplete scope or unavailable evidence remains unknown; the door holds one lock across the check and dispatch claim so concurrent fires see earlier claims.
Background: [subsystem register](../core/SUBSYSTEMS.md).

## Entry points
[`scripts/lib/blast_radius_gate.py::judge`](../../scripts/lib/blast_radius_gate.py), [`scripts/lib/blast_radius_gate.py::section`](../../scripts/lib/blast_radius_gate.py).

## Tests
[tests/test_blast_radius_gate.py](../../tests/test_blast_radius_gate.py), [tests/test_blast_radius_section.py](../../tests/test_blast_radius_section.py).

## Decisions
[ADR-096](../governance/decisions/ADR-096-dependency-per-deliverable-and-blast-radius.md).

## Flag
`VNX_BLAST_RADIUS_MODE`
