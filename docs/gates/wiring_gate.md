# Wiring Gate

Dead-code detection gate that catches unwired public definitions added in a PR.

## Problem

26 dead symbols shipped across 92 PRs in the last week. Public functions and classes
get defined but never called from production code, inflating maintenance surface.

## How it works

1. Fetches the PR diff via `gh pr diff <number>`
2. AST-parses added lines for new public definitions (`def`/`class` without `_` prefix)
3. Greps the codebase for callers (excludes `tests/` and the defining file itself)
4. Flags symbols with zero callers as unwired

## Integration

Add `wiring_gate` to the `--review-stack` option of `review_gate_manager.py`:

```bash
python3 scripts/review_gate_manager.py request-and-execute \
  --pr 567 --branch feat/my-feature \
  --review-stack "gemini_review,codex_gate,wiring_gate"
```

## Environment

| Variable | Default | Effect |
|----------|---------|--------|
| `VNX_WIRING_GATE_REQUIRED` | `0` | `0` = shadow mode (advisory), `1` = hard-fail (blocking) |

Week 1 runs in shadow mode to calibrate false positives. Set to `1` for enforcement.

## Skip list

File: `.vnx-data/state/wiring_skip.yaml`

```yaml
library_exports:
  - emit_governance_receipt
  - WiringGateResult

decorator_registry:
  - register_handler

all_reexports:
  - GateRunner

cli_dispatch:
  - handle_wiring_command
```

Categories cover common edge cases where zero callers is intentional:
- **library_exports**: public API symbols consumed by external callers
- **decorator_registry**: symbols registered via decorator (runtime discovery)
- **all_reexports**: symbols exposed through `__all__` for package consumers
- **cli_dispatch**: entry points reached via CLI argument dispatch dicts

## API

```python
from wiring_gate import check_pr_wiring, WiringGateResult

result: WiringGateResult = check_pr_wiring(pr_number=567)
# result.status: "pass" | "fail" | "advisory"
# result.unwired: List[UnwiredSymbol]
# result.skipped: List[str]
# result.total_checked: int
# result.summary: str
```

## Gate result schema

```json
{
  "status": "advisory",
  "unwired": [
    {"name": "orphan_func", "file": "scripts/lib/new.py", "line": 42, "kind": "function"}
  ],
  "skipped": ["register_handler"],
  "total_checked": 8,
  "summary": "1 unwired symbol(s): orphan_func"
}
```
