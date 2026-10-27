# Golden corpus — governed_lane (GE-5a)

Real gate report texts, copied verbatim (nothing under `.vnx-data` was touched)
from `~/.vnx-data/vnx-dev/unified_reports/`. Each `goldens/<stem>.json` is the
output of the pre-GE-5a per-module parser (`glm_gate._extract_verdict` /
`kimi_gate._extract_verdict`) for that text, committed BEFORE the parser moved
into `scripts/lib/governed_lane.py`. `extract_verdict(text, provider)` must
reproduce every golden field by field.

| fixture | source report | shape |
|---|---|---|
| `kimi-pass-clean.md` | `kimi-gate-pr1715-1788000240.md` | PASS, no findings |
| `kimi-pass-advisory.md` | `kimi-gate-pr1710-1787998584.md` | PASS, advisory (warning) only |
| `kimi-pass-advisory-2.md` | `kimi-gate-pr1713-1787999641.md` | PASS, advisory (warning) only |
| `kimi-pass-info-1953.md` | `kimi-gate-pr1953-1790571445.md` | PASS, advisory (info) only |
| `kimi-pass-info-2033.md` | `kimi-gate-pr2033-1791015355.md` | PASS, advisory (info) only |
| `kimi-pass-clean-pr905.md` | `kimi-gate-pr905-1782240399.md` | PASS, no findings |
| `kimi-fail-blocking.md` | `kimi-gate-pr1712-1787999422.md` | FAIL with a blocking (`error`) finding |
| `kimi-truncated.md` | `kimi-gate-pr1711-1788000528.md` | truncated: frontmatter only, no verdict fence |
| `glm-pass-clean.md` | `glm-gate-pr1849-1789235537.md` | PASS, no findings |
| `glm-pass-advisory.md` | `glm-gate-pr1848-1789230239.md` | PASS, advisory (info) only |
| `glm-fail-blocking.md` | `glm-gate-pr1855-1789395957.md` | FAIL with a blocking (`error`) finding |
| `glm-truncated.md` | `glm-gate-pr1691-1787770630.md` | truncated: frontmatter only, no verdict fence |
