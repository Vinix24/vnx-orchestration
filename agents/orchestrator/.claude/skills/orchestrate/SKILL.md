# T0 Orchestrator — Detailed Workflow Reference

This is the extended reference for the headless T0 orchestrator.
The condensed identity is in `agents/orchestrator/CLAUDE.md`.

## 1. Receipt Review Protocol

### Claim Verification (minimum 3 per receipt)

For each worker receipt, spot-check at least 3 specific claims:

```bash
# 1. Claimed file was modified
git log --oneline -1 -- <file>

# 2. Specific fix is present
grep -r "expected_pattern" <file>

# 3. Old problem no longer exists
grep -r "old_pattern" src/  # Must return 0 matches
```

Acceptance criteria:
- Automated test pass counts are acceptable evidence
- Code change descriptions are NOT acceptable without code verification
- Vague claims ("improved performance", "fixed bug") require concrete evidence
- If ANY claim fails verification: reject receipt, do not close items

### Quality Advisory Interpretation

| Advisory | Risk | Action |
|----------|------|--------|
| approve | < 0.3 | Standard review |
| approve | 0.3-0.5 | Careful review of flagged areas |
| hold | > 0.5 | Critical review, likely follow-up dispatch |
| hold | > 0.8 | Block progression unless explicitly mitigated |

## 2. Open Items Lifecycle

### Inspect
```bash
python3 scripts/open_items_manager.py digest
python3 scripts/open_items_manager.py list --status open
```

### Resolve (only with evidence)
```bash
# Verify fix exists before closing
grep -r "old_pattern" src/        # Must return 0
grep -r "new_pattern" src/        # Must return expected matches
git log --oneline -1 -- <file>    # Must show recent commit

# Then close
python3 scripts/open_items_manager.py close OI-XXX --reason "evidence: ..."
python3 scripts/open_items_manager.py defer OI-XXX --reason "non-blocking"
python3 scripts/open_items_manager.py wontfix OI-XXX --reason "out of scope"
```

### Create new item
```bash
python3 scripts/open_items_manager.py add \
  --title "<short risk title>" \
  --severity warn \
  --pr-id PR-X \
  --description "<what was discovered>"
```

## 3. PR Queue Lifecycle

### Read state
```bash
python3 scripts/pr_queue_manager.py status
python3 scripts/pr_queue_manager.py list
```

### Staging-first dispatch
```bash
python3 scripts/pr_queue_manager.py staging-list
python3 scripts/pr_queue_manager.py show <dispatch-id>
python3 scripts/pr_queue_manager.py promote <dispatch-id>
python3 scripts/pr_queue_manager.py reject <dispatch-id> --reason "..."
```

### Complete PR
```bash
python3 scripts/pr_queue_manager.py complete PR-X
```

Only after all blocker/warn obligations are satisfied and required gates pass.

## 4. Review Gate Verification

Before closing any PR with a review stack:

```bash
python3 scripts/review_gate_manager.py status --pr <number> --json
```

Verify ALL of these:
1. Request record exists in `.vnx-data/state/review_gates/requests/`
2. Result record exists in `.vnx-data/state/review_gates/results/`
3. `contract_hash` is non-empty and matches active contract
4. `report_path` is non-empty
5. Normalized markdown report exists under `$VNX_DATA_DIR/unified_reports/`
6. No unresolved blocking findings carried into PR completion
7. Gate is not stuck in `queued` with no completion evidence

Closure blockers:
- Request exists but execution never started
- Gate result with empty `contract_hash`
- Gate result with empty `report_path`
- Ad hoc shell output exists but no normalized report/result
- Structured JSON and normalized report content disagree (treat as evidence failure)

## 5. Dispatch Format

Every dispatch must include these headers:

| Header | Description |
|--------|-------------|
| `Role` | Worker role (e.g., backend-developer, architect) |
| `Track` | A, B, or C |
| `Terminal` | T1, T2, or T3 |
| `PR-ID` | Feature PR identifier |
| `Priority` | Dispatch priority |
| `Cognition` | Model hint (sonnet, opus) |
| `Dispatch-ID` | Unique dispatch identifier |
| `Parent-Dispatch` | Previous dispatch in chain (if any) |
| `Reason` | Why this dispatch exists |

Plus: `Workflow`, `Context`, and explicit success criteria.

Validate role names before dispatch:
```bash
python3 scripts/validate_skill.py --list
```

## 6. Headless T1 Dispatch

T1 is a headless backend-developer. Dispatch via:
```bash
python3 scripts/lib/subprocess_dispatch.py \
  --terminal-id T1 \
  --dispatch-id <id> \
  --model sonnet \
  --instruction "<task>"
```

T1 receipts arrive in `t0_receipts.ndjson` with `source="subprocess"`.

## 7. Decision Output Rules

When not dispatching, provide explicit status:

- **WAIT**: explain exact blocker (terminal busy, queue active, dependency unmet)
- **ESCALATE**: explain ambiguity and propose options
- **APPROVE/PROCEED**: show why all criteria are met

Final rule: if evidence is weak or contradictory, do not approve by default.
