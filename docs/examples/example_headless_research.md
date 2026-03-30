# Example: Structured Research with Headless Execution

> Audit a codebase for security vulnerabilities across three agents without interactive tmux.

This walkthrough shows VNX coordinating a structured research task — a security audit that produces written analysis rather than code changes. It demonstrates headless execution mode where agents work independently and VNX collects their structured reports.

---

## Prerequisites

- VNX installed (`vnx init --starter` or `--operator`)
- `vnx doctor` passes cleanly
- At least one AI CLI installed

---

## Why VNX for Research?

Research tasks have the same coordination problems as coding:
- Multiple analysis angles that can run in parallel
- Results that need to be merged without duplication
- A need for structured output you can query later
- Audit trails showing what was analyzed and what was found

VNX dispatches work the same way for research as for code — scoped tasks, structured receipts, quality gates.

---

## The Task

You maintain an Express.js API and want a security audit covering:
1. Authentication and authorization patterns
2. Input validation and injection risks
3. Dependency vulnerability analysis

Each area is independent and can be analyzed by a separate agent simultaneously.

## 1. Create Dispatches

In T0 (or starter mode), describe the research goal:

```
Security audit of the Express API. I need three independent analyses:
1. Auth patterns: JWT handling, session management, RBAC implementation
2. Input validation: SQL injection, XSS, command injection, path traversal
3. Dependencies: known CVEs, outdated packages, supply chain risks

Each analysis should produce a structured report with severity ratings.
```

T0 creates three dispatches:

```markdown
## Dispatch: Auth Pattern Analysis (Track A)
Gate: review | Priority: P1

Objective: Analyze authentication and authorization implementation.
Deliverable: Structured report with findings rated Critical/High/Medium/Low.
Files to examine: src/middleware/auth.ts, src/routes/auth.ts, src/services/session.ts
Do NOT modify any files — read-only analysis only.

## Dispatch: Input Validation Audit (Track B)
Gate: review | Priority: P1

Objective: Identify injection and validation vulnerabilities.
Deliverable: Structured report with exploit scenarios and remediation steps.
Files to examine: src/routes/*.ts, src/middleware/validation.ts
Do NOT modify any files — read-only analysis only.

## Dispatch: Dependency Risk Assessment (Track C)
Gate: review | Priority: P1

Objective: Audit package.json and lock files for known vulnerabilities.
Deliverable: Structured report with CVE references and upgrade recommendations.
Commands to run: npm audit, check package versions against known CVE databases.
Do NOT modify any files — read-only analysis only.
```

## 2. Parallel Execution

### In Operator Mode

All three agents run simultaneously in T1, T2, T3:

```bash
vnx start
# Promote dispatches via Ctrl+G
# All three terminals analyze in parallel
```

### In Starter Mode

Dispatches execute sequentially in one terminal:

```bash
vnx staging-list
vnx promote <dispatch-id>    # One at a time
```

Same receipts, same report structure — just sequential instead of parallel.

## 3. Structured Reports

Each agent writes a report to `.vnx-data/unified_reports/`. The reports follow a consistent structure:

```markdown
# Security Audit: Auth Pattern Analysis

**Dispatch ID**: 20260329-auth-pattern-analysis-A
**Track**: A
**Status**: success

## Findings

### CRITICAL: JWT Secret Hardcoded in Source
- **File**: src/middleware/auth.ts:14
- **Severity**: Critical
- **Description**: JWT signing secret is a string literal, not from environment
- **Remediation**: Move to environment variable, rotate immediately

### HIGH: No Token Refresh Rotation
- **File**: src/services/session.ts:47
- **Severity**: High
- **Description**: Refresh tokens are reusable indefinitely after issuance
- **Remediation**: Implement one-time-use refresh with rotation

### MEDIUM: Missing Rate Limiting on Login
- **File**: src/routes/auth.ts:22
- **Severity**: Medium
- **Description**: No rate limiting on POST /auth/login enables brute force
- **Remediation**: Add express-rate-limit with progressive backoff

## Summary
- Critical: 1
- High: 1
- Medium: 1
- Low: 0
```

## 4. Receipt Trail

The receipt processor converts each report into a structured NDJSON entry:

```json
{
  "event": "task_receipt",
  "track": "A",
  "gate": "review",
  "status": "success",
  "summary": "Auth pattern analysis complete: 1 critical, 1 high, 1 medium finding",
  "report_path": ".vnx-data/unified_reports/20260329-093000-A-auth-pattern-analysis.md"
}
```

## 5. T0 Synthesis

After all three reports arrive, T0 has a complete picture:

```bash
vnx status    # Shows all three tracks completed
```

T0 can now:
- **Merge findings** across tracks into a unified security report
- **Prioritize remediation** based on severity across all three analyses
- **Create follow-up dispatches** for the highest-priority fixes
- **Track resolution** through the existing dispatch → receipt cycle

## 6. Follow-Up: From Research to Action

The security audit findings become input for coding dispatches:

```
T0 creates remediation dispatches:

Track A → "Fix hardcoded JWT secret, add env-based config"
Track B → "Add rate limiting to auth endpoints"
Track C → "Upgrade vulnerable dependencies per CVE list"
```

The audit trail now links: research dispatch → findings report → remediation dispatch → code change → quality gate. Full provenance from discovery to fix.

---

## Headless Execution Note

VNX supports headless execution (no tmux, no interactive terminal) for CI/CD integration and batch research. In headless mode:

- Dispatches execute via CLI without a tmux grid
- Receipts are still emitted to the NDJSON ledger
- Reports follow the same structured format
- Quality gates still apply

This makes VNX suitable for automated audit pipelines where research tasks run on a schedule and results are collected programmatically.

---

## Key Takeaways

| What happened | Why it matters |
|---------------|---------------|
| Research scoped into independent analysis tracks | Each agent focused on one area deeply |
| Structured report format enforced | Findings are queryable, not just prose |
| Receipt trail captured all analysis | Audit of the audit — what was checked, by whom |
| Findings feed directly into remediation dispatches | Research → action pipeline without context loss |
| Works in starter mode (sequential) or operator mode (parallel) | Same governance regardless of mode |
