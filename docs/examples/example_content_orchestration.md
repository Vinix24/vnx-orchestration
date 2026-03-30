# Example: Content and Documentation Orchestration

> Rewrite API documentation, generate migration guides, and produce changelog — all in parallel.

This walkthrough shows VNX orchestrating non-coding work: documentation updates, content generation, and technical writing tasks that benefit from the same governance and audit trail as code.

---

## Prerequisites

- VNX installed (`vnx init --starter` or `--operator`)
- `vnx doctor` passes cleanly
- At least one AI CLI installed

---

## Why VNX for Content?

Documentation and content tasks share the same problems as coding:
- Multiple writers (human or AI) editing overlapping files create conflicts
- No audit trail for who wrote what and whether it was reviewed
- Quality varies without structured review gates
- Large documentation rewrites exhaust context windows

VNX doesn't care whether the dispatch produces code or prose — it tracks dispatches, enforces gates, and maintains provenance either way.

---

## The Task

Your API has reached v3.0. You need:
1. Updated API reference docs reflecting new endpoints
2. A migration guide from v2 to v3
3. A user-facing changelog summarizing what changed and why

Each deliverable is independent and can be produced by a separate agent.

## 1. Feature Plan

T0 breaks the documentation task into tracked work:

```markdown
## PR-1: API Reference Update (Track A)
Gate: review | Priority: P1 | Dependencies: none
Scope: Update docs/api/ with new v3 endpoints, deprecation notices, request/response schemas

## PR-2: Migration Guide v2 → v3 (Track B)
Gate: review | Priority: P1 | Dependencies: none
Scope: Step-by-step migration instructions, breaking changes, compatibility notes

## PR-3: Public Changelog (Track C)
Gate: review | Priority: P2 | Dependencies: PR-1, PR-2
Scope: User-facing changelog synthesized from API changes and migration notes
```

PR-3 depends on PR-1 and PR-2 because the changelog should reflect the final documented state.

## 2. Dispatch and Execute

### Operator Mode (Parallel)

```bash
vnx start
# Approve PR-1 and PR-2 dispatches via Ctrl+G
# T1 and T2 work simultaneously
# PR-3 dispatches after both complete
```

### Starter Mode (Sequential)

```bash
vnx promote <pr1-dispatch-id>    # API reference first
# Wait for completion
vnx promote <pr2-dispatch-id>    # Migration guide second
# Wait for completion
vnx promote <pr3-dispatch-id>    # Changelog last (has context from both)
```

## 3. Scoped Dispatches

Each dispatch is precise about what to produce:

```markdown
## Dispatch: API Reference Update (Track A)

Objective: Update API reference documentation for v3.0 release.

Instructions:
- Read src/routes/*.ts to identify all v3 endpoint changes
- Update docs/api/endpoints.md with new routes, parameters, response schemas
- Add deprecation notices for removed v2 endpoints
- Include request/response examples for each new endpoint
- Mark breaking changes with a clear "BREAKING" label

Deliverable: Updated docs/api/endpoints.md
Do NOT modify source code.

## Contract
- file_changed: docs/api/endpoints.md
- pattern_match: "v3" in docs/api/endpoints.md
- pattern_match: "BREAKING" in docs/api/endpoints.md
```

The contract block enables automated verification — the receipt processor checks that the file was actually changed and contains the expected markers.

## 4. Quality Gates for Content

Quality gates work for documentation too. The gate checks:

- **File exists**: Did the agent produce the expected deliverable?
- **Contract assertions**: Does the output contain required sections?
- **File size**: Is the document substantive (not a stub)?
- **No open blockers**: Did the agent flag unresolved issues?

```bash
vnx gate-check --pr PR-1
# Verdict: APPROVE — file changed, contract assertions pass, no blockers
```

## 5. Context Rotation for Large Docs

API reference updates can be large. If the agent hits context limits:

```
T1 context at 65%
  → Writes ROTATION-HANDOVER.md:
    "Completed endpoints A-M. Remaining: N-Z.
     See docs/api/endpoints.md for current state."
  → VNX clears and resumes
  → Fresh session picks up at endpoint N
```

The handover preserves exactly where the agent left off. The receipt chain links both sessions.

## 6. Synthesizing Results

After PR-1 and PR-2 complete, T0 dispatches PR-3 (changelog) with context from both:

```markdown
## Dispatch: Public Changelog (Track C)

Objective: Write user-facing changelog for v3.0 release.

Context:
- API reference changes: see docs/api/endpoints.md (updated by PR-1)
- Migration guide: see docs/migration-v2-to-v3.md (produced by PR-2)

Instructions:
- Summarize all breaking changes from the API reference
- Reference migration steps for each breaking change
- Group changes by category: New, Changed, Deprecated, Removed
- Write for end users, not internal developers
- Keep entries concise — one line per change with a link to details

Deliverable: CHANGELOG.md (v3.0 section)
```

The agent has both prior deliverables as context, producing a changelog that's consistent with the reference and migration guide.

## 7. Audit Trail

The receipt ledger captures the full content production pipeline:

```bash
cat .vnx-data/state/t0_receipts.ndjson | jq 'select(.gate == "review")'
```

Each entry shows: what was dispatched, which agent produced it, what files changed, and whether the gate passed. If the changelog contradicts the API reference six months later, you can trace exactly what happened.

---

## Applicable Content Tasks

This pattern works for any structured content orchestration:

| Task | Track A | Track B | Track C |
|------|---------|---------|---------|
| **Release docs** | API reference | Migration guide | Changelog |
| **Architecture review** | System analysis | Risk assessment | Recommendation report |
| **Onboarding material** | Setup guide | Tutorial series | FAQ compilation |
| **Compliance documentation** | Policy drafting | Control mapping | Evidence collection |
| **Incident postmortem** | Timeline reconstruction | Root cause analysis | Action items |

---

## Key Takeaways

| What happened | Why it matters |
|---------------|---------------|
| Documentation tasks got the same governance as code | Content quality is tracked, not assumed |
| Contract assertions verified content completeness | Automated checks caught missing sections |
| Dependencies ensured correct synthesis order | Changelog reflected final API state |
| Context rotation handled large documents | No lost progress on extensive rewrites |
| Receipt trail covers content provenance | Auditable record of who wrote what |
