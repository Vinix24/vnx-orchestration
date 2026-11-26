# t0-promoveert-zelf: Account permission for T0 promotion

The operator recorded a user-level Claude Code permission allowing T0 to invoke deliverable promotion without another tool-permission refusal on that account. This is account-specific operational setup; VNX still checks the dispatch state, project and applicable promotion gates.

## How

The recorded setup used the operator’s `/permissions` interface to allow `Bash(./bin/vnx deliverable promote *)` and `Bash(vnx deliverable promote *)`. With that permission installed, use `vnx deliverable promote <dispatch-id> --project-id vnx-dev`, substituting the intended project; see `vnx deliverable promote --help` for flags.

## Modules

- [Horizon planning](../modules/horizon-planning.md)
- [Plan-gate panel](../modules/plan-gate-panel.md)

## Pull requests

Track closed: 2026-10-22 (UTC).

No code PR: operational attestation `ops-attest:2026-10-22` records the operator-installed user permissions and successful T0 promotions. No repository permission default was changed by this track.
