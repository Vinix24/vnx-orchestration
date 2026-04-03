# T0 Orchestrator Output Template

## ⚠️ CRITICAL: Output Method
**TERMINAL ONLY** - Print Manager Block directly to terminal console
**NO FILES** - Smart tap monitors terminal and picks up blocks automatically
**NO REPORTS** - T0 never writes reports, only dispatches

## Manager Block V2 Output Format

Output exactly ONE Manager Block between `[[TARGET:X]]` and `[[DONE]]` markers:

```markdown
[[TARGET:A|B|C]]
Manager Block

Role: <skill-name>  # See .claude/skills/skills.yaml
Track: <A|B|C>
Terminal: <T1|T2|T3>
Priority: <P0|P1|P2>
Cognition: <normal|deep>
Requires-Model: <opus|sonnet>
ClearContext: <true|false>
Risk-Class: <low|medium|high>
Merge-Policy: <human|conditional_auto>
Review-Stack: <gemini_review,codex_gate,claude_github_optional>
Dispatch-ID: <YYYYMMDD-HHMMSS-descriptor-track>
Parent-Dispatch: <dispatch-id or "none">
PR-ID: <PR-X or "none">
Reason: <1-line justification>

Context: [[@path1]] [[@path2]] [[@path3]]

Instruction:
- <Concrete requirement 1>
- <Concrete requirement 2>
- <Success criteria>

[[DONE]]
```

## Receipt Analysis Output

When analyzing receipts, output your decision as plain text BEFORE any Manager Block:

```
=== ORCHESTRATION DECISION ===
Status: WAIT|DISPATCH|ESCALATE|COMPLETE
Reason: <brief explanation>

Terminal States: T1=idle T2=working T3=idle
Queue: pending=0 active=1 conflicts=0
Next Gates: A=testing B=review C=planning
================================
```

Then if DISPATCH, output the Manager Block below.

## Decision Types

### WAIT Decision
Output when terminals busy or queue active:
```
=== ORCHESTRATION DECISION ===
Status: WAIT
Reason: T2 currently working on authentication feature

Terminal States: T1=idle T2=working(5m) T3=idle
Queue: pending=2 active=1 conflicts=0
================================
```

### DISPATCH Decision
Output when ready to create new dispatch:
```
=== ORCHESTRATION DECISION ===
Status: DISPATCH
Reason: All terminals idle, proceeding with next priority task

Terminal States: T1=idle T2=idle T3=idle
Queue: pending=0 active=0 conflicts=0
Next Gates: A=implementation B=testing C=idle
================================

[[TARGET:A]]
[... Manager Block content ...]
[[DONE]]
```

### ESCALATE Decision
Output when intervention needed:
```
=== ORCHESTRATION DECISION ===
Status: ESCALATE
Reason: T3 blocked for >30min, needs manual intervention

Terminal States: T1=idle T2=idle T3=blocked(35m)
Queue: pending=5 active=0 conflicts=2
================================
```

### COMPLETE Decision
Output when program/feature complete:
```
=== ORCHESTRATION DECISION ===
Status: COMPLETE
Reason: Authentication feature fully implemented and tested

Terminal States: T1=idle T2=idle T3=idle
Queue: pending=0 active=0 conflicts=0
Program: auth_feature_2025 - COMPLETE
================================
```

## Field Validation Reminders

### Required Fields
Every Manager Block MUST have:
- `Role`: Skill name from `.claude/skills/skills.yaml`
  - Examples: `backend-developer`, `frontend-developer`, `security-engineer`, `planner`, `architect`
- `Track`: A (implementation), B (storage/testing), C (architecture/research)
- `Terminal`: T1, T2, or T3
- `Priority`: P0 (critical), P1 (high), P2 (normal)
- `Cognition`: normal or deep
- `ClearContext`: true or false (default: true)
- `Dispatch-ID`: Unique ID with format YYYYMMDD-HHMMSS-descriptor-track
- `Parent-Dispatch`: Parent dispatch ID or "none"
- `PR-ID`: PR-X format (e.g., PR-4) or "none" for non-PR work
- `Reason`: One-line justification
- `Instruction`: Clear tasks with success criteria

### Optional Fields (Use When Needed)
- `Gate`: investigation, planning, implementation, review, testing, integration (mode selection only)
- `Requires-Model`: opus or sonnet (force specific model)
- `Risk-Class`: low, medium, or high
- `Merge-Policy`: human or conditional_auto
- `Review-Stack`: comma-separated reviewer gates
- `Mode`: planning, thinking, normal (terminal mode)
- `ForceNormalMode`: true/false (reset mode first)

## Common Patterns

### After Success Receipt
1. Analyze what was completed
2. Check if more work needed
3. Progress to next gate if applicable
4. Output DISPATCH or COMPLETE decision

### After Failure Receipt
1. Analyze failure reason
2. Determine if retry needed
3. Consider deep cognition
4. Output DISPATCH with higher priority

### After Blocked Receipt
1. Analyze blocker type
2. Check if other tracks can help
3. Consider escalation
4. Output ESCALATE or reassign

## Example Manager Block (V2 Spec)

```markdown
[[TARGET:A]]
Manager Block

Role: backend-developer
Track: A
Terminal: T1
Priority: P1
Cognition: normal
Requires-Model: sonnet
ClearContext: true
Risk-Class: medium
Merge-Policy: human
Review-Stack: gemini_review,codex_gate,claude_github_optional
Dispatch-ID: 20260203-153000-storage-fix-A
Parent-Dispatch: none
PR-ID: PR-4
Reason: Implement storage persistence fix from architecture decision

Context: [[@src/storage/rag_storage_client.py]] [[@docs/requirements.md]]

Instruction:
- Implement RAGStorageClient.store_page() integration
- Update quickscan_controller to use unified pipeline
- Write unit tests with 80%+ coverage
- Success: Tests pass, no linter errors, storage working

[[DONE]]
```

## Available Skills Reference

Check `.claude/skills/skills.yaml` for current list. Common skills:
- `planner` - Feature planning and breakdown
- `architect` - System design and architecture
- `backend-developer` - Server-side implementation
- `frontend-developer` - UI/UX implementation
- `security-engineer` - Security audits and hardening
- `quality-engineer` - Comprehensive testing
- `test-engineer` - TDD and test creation
- `reviewer` - Code review and validation
- `debugger` - Problem investigation

## Output Rules

1. **One Block Only**: Never output multiple Manager Blocks
2. **Terminal Output**: Print directly to console
3. **No File Writing**: Never use Write tool for dispatches
4. **Smart Tap Ready**: Use exact format for parser
5. **Clear Decisions**: Always explain orchestration reasoning
6. **V8 UPDATE**: Dispatcher uses skills from `.claude/skills/`, not agent templates
7. **Required Fields**: All V2 fields must be present (Role, Dispatch-ID, Parent-Dispatch, PR-ID, Reason)
8. **Context Format**: Use `[[@path]]` format for context files
9. **Governance Metadata**: Set Risk-Class, Merge-Policy, and Review-Stack explicitly for any PR-backed dispatch
