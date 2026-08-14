# T0 Runbooks

> Operational recipes for the `t0-orchestrator` role, moved out of
> `skills/t0-orchestrator/SKILL.md` (OI-1191, 2026-08-14). The skill keeps judgment and
> points here; this doc holds the recipes. Where a section already has an enforced home,
> that home is named at the top of the section and the recipe below is the operator-facing
> form of the same rule — not a second source of truth.
>
> The enforced ruleset stays in `docs/core/DISPATCH_RULES.md`. Read that first for
> decision-tree, lane, concurrency, and gate rules.

## 1. THIN-T0 — research and reporting boundaries

**T0 does NOT conduct investigation, research, or write analysis reports.**

T0 is a thin orchestration layer. All research, implementation, testing, and report-writing
is delegated to workers (T1/T2/T3). T0 reviews the resulting receipts and reports — it does
not produce them.

### T0 output is limited to

- Dispatch instructions (manager blocks written to terminal output)
- Gate / closure decisions
- Light state-checks (`Bash` read-only CLI calls for state queries)
- Staging-area promotions via `pr_queue_manager.py`

### T0 MUST NOT

- Author analysis documents, investigation reports, or `claudedocs/*` files
- Conduct multi-step research or exploration on behalf of the sprint
- Use `Write` or `Edit` to create persistent files outside:
  - `/tmp/*` (ephemeral dispatch scratch)
  - `.vnx-data/dispatches/staging/` (manager blocks, ONLY via `pr_queue_manager.py staging`)
- Delegate research to itself via self-dispatches

When a gap requires investigation or a written deliverable, T0 MUST dispatch a worker
(e.g. `backend-developer`, `architect`, `intelligence-engineer`) and wait for the receipt
before making an orchestration decision.

## 2. Open Items Lifecycle

### 2.1 Inspect

```bash
python3 scripts/open_items_manager.py digest
python3 scripts/open_items_manager.py list --status open
python3 scripts/open_items_manager.py rescan --dry-run   # preview auto-close of resolved items
```

### 2.2 Resolve

Before closing any item, VERIFY the fix against actual code:

```bash
grep -r "old_pattern" src/        # Must return 0 matches
grep -r "new_pattern" src/        # Must return expected matches
git log --oneline -1 -- <file>    # Must show recent commit
```

Only then close:

```bash
python3 scripts/open_items_manager.py close OI-XXX --reason "evidence: ..."
python3 scripts/open_items_manager.py defer OI-XXX --reason "non-blocking for now"
python3 scripts/open_items_manager.py wontfix OI-XXX --reason "out of scope"
```

### 2.3 Create new item when needed

Use this when worker output introduces a new risk not in current scope.

```bash
python3 scripts/open_items_manager.py add \
  --dispatch <origin-dispatch-id> \
  --title "<short risk title>" \
  --severity warn \
  --pr <PR-X> \
  --details "<what was discovered and why it matters>"
```

If CLI signature differs in your branch, use `--help` and map fields accordingly.

## 3. PR Queue Lifecycle

### 3.1 Read state

```bash
python3 scripts/pr_queue_manager.py status
python3 scripts/pr_queue_manager.py list
```

### 3.2 Staging-first operations

```bash
python3 scripts/pr_queue_manager.py staging-list
python3 scripts/pr_queue_manager.py show <dispatch-id>
python3 scripts/pr_queue_manager.py promote <dispatch-id>
python3 scripts/pr_queue_manager.py reject <dispatch-id> --reason "..."
```

### 3.3 Complete PR

```bash
python3 scripts/pr_queue_manager.py complete PR-X
```

Only after blocker/warn obligations are satisfied.

### 3.4 Review gate verification

Before closure on any PR with a non-empty review stack:

```bash
python3 scripts/review_gate_manager.py status --pr <number> --json
python3 scripts/closure_verifier.py --help
```

T0 must verify:

1. required gate request exists
2. required gate result exists
3. `contract_hash` matches the active review contract
4. `report_path` is present in the result payload
5. the normalized markdown report exists under `$VNX_DATA_DIR/unified_reports/`
6. unresolved blocking findings are not carried into PR completion
7. `contract_hash` is non-empty
8. `report_path` is non-empty
9. the gate is not stuck in request-only state such as `queued` with no completion evidence

T0 must treat the following as closure blockers:

- request exists but execution was never actively started
- gate result exists but `contract_hash` is empty
- gate result exists but `report_path` is empty
- ad hoc shell review output exists but no normalized report and no recorded result exist

## 4. Dispatch Guard and Provider Awareness

Before dispatching:

```bash
bash skills/t0-orchestrator/scripts/dispatch_guard.sh
```

1. If guard returns WAIT, do not dispatch.
2. Provider capability and constraint truth lives in the registry — `scripts/lib/providers/`
   (`provider_constraints.yaml`, `routing_policy.yaml`, `wave7_models.yaml`). Cite it; keep
   non-Claude constraints in mind (mode/model differences).

Liveness and known worktree-topology caveats of `dispatch_guard.sh`:
`docs/operations/RUNTIME_LIVENESS.md` §5.

### 4.1 Pre-Dispatch Pane Verification

Before the first dispatch of any session or after a tmux restart, verify pane IDs match live
tmux state.

**Pane discovery tiers (in fallback order):**

| Tier | Method | Survives tmux restart |
|------|--------|-----------------------|
| 1. Cache | TTL-based fast lookup (5 min) | NO |
| 2. panes.json | Static file with pane_id field | NO (pane IDs change) |
| 3. Path-based | `pane_current_path` match | YES — always works |
| 4. Interactive | Operator manual resolution | NO |

Path-based discovery is the most reliable tier after a crash because `pane_current_path` is
preserved by tmux when the session is recreated, even though pane IDs change.

**Verification commands:**

```bash
# List all panes with their paths to verify terminal presence
tmux list-panes -a -F "#{pane_id} #{pane_current_path}"

# Check which panes match expected terminal paths
for T in T0 T1 T2 T3; do
  tmux list-panes -a -F "#{pane_id} #{pane_current_path}" | \
    grep "$(pwd)/.claude/terminals/$T" && echo "$T: OK" || echo "$T: MISSING"
done
```

If any terminal pane is missing, escalate before dispatching — do not send a dispatch to a
stale or unknown pane ID.

If panes.json contains stale IDs, update it manually or delete it and let path-based discovery
take over on next delivery.

### 4.2 Dispatch routing

Routing is enforced, not improvised. The lane split (which lane for which task/provider) and
the staging flow are authoritative in `docs/core/DISPATCH_RULES.md` §5 (lane selection), §8
(provider strings), and §12 (autonomous staging flow). Dispatches go through the single-entry
door (`vnx dispatch`); do not hand-roll lane scripts.

## 5. Manager Block Quality Standard

> Enforced summary: `docs/core/DISPATCH_RULES.md` §9. The full header list below is the
> operator-facing form of the same rule.

Every dispatch must include:

1. `[[TARGET:A|B|C]]`
2. `[[DONE]]`
3. Required headers:
   1. `Role` — the primary routing field (skill name); replaces the old Track A/B/C model
   2. `Terminal` — optional; legacy terminal-pin. Role-scoped pool is the default.
   3. `PR-ID`
   4. `Priority`
   5. `Cognition`
   6. `Dispatch-ID`
   7. `Parent-Dispatch`
   8. `Reason`
4. `Context`
5. Explicit success criteria
6. If the dispatch requests a headless review gate, it must name the expected report path and
   required receipt/result linkage

**T0 Write/Edit scope (HARD LIMIT):**

- T0 may ONLY write to `/tmp/*` (ephemeral scratch) and `.vnx-data/dispatches/staging/`
- `claudedocs/*` analysis reports are WORKER output, not T0 output — FORBIDDEN for T0

Validate role names before init, promote, and dispatch when uncertain:

```bash
python3 scripts/validate_skill.py --list
```

## 6. Recommended Script Toolbox

The scripts below are the live operator surface. Older runbook text named shell wrappers that
no longer exist (`queue_status.sh`, `deliverable_review.sh`, `provider_capabilities.sh`,
`staging_helper.sh`); their work is now done by the Python CLIs and the provider registry.

1. `skills/t0-orchestrator/scripts/dispatch_guard.sh`
   - go/no-go guard

2. `skills/t0-orchestrator/scripts/intelligence.sh`
   - intelligence read helpers

3. `python3 scripts/pr_queue_manager.py` (`status` / `list` / `staging-list` / `promote` /
   `reject` / `complete`)
   - queue/staging/terminal summary and promotion

4. `python3 scripts/open_items_manager.py` (`digest` / `list` / `close` / `defer` / `wontfix` /
   `add` / `rescan`)
   - PR-focused open-item checks and resolution

5. `scripts/lib/providers/` (`provider_constraints.yaml`, `routing_policy.yaml`,
   `wave7_models.yaml`)
   - provider constraints and routing hints

6. `python3 scripts/runtime_core_cli.py` and `bin/vnx pool {status,scale,config,reap}`
   - runtime-core operator tooling and the Wave 6 elastic pool

## 7. Decision Outputs

When not dispatching, provide explicit status to user:

1. `WAIT`: explain exact blocker (terminal busy, queue active, dependency unmet).
2. `ESCALATE`: explain ambiguity and propose options.
3. `PROCEED`: show why criteria are met.

## 8. Session Resume After Crash

> Contract: `docs/core/60_CONVERSATION_RESUME_CONTRACT.md` (source-of-truth hierarchy, resume
> semantics, fork-on-resume). The concrete recipes below are the operator-facing form.

When T0 or a worker terminal crashes and the conversation context is lost:

### 8.1 Find the Session ID

**Option A: Query Claude Code's conversation index**

```bash
# Find sessions associated with this worktree's terminals
sqlite3 ~/.claude/conversation-index.db \
  "SELECT session_id, cwd, last_message \
   FROM conversations \
   WHERE cwd LIKE '$(pwd)/.claude/terminals/T%' \
   ORDER BY last_message DESC LIMIT 5;"
```

This uses the path containment invariant: `session.cwd` in `<PROJECT_ROOT>/.claude/terminals/T{N}`
→ session belongs to this worktree.

**Option B: Query dispatch metadata (if session_id was captured)**

```bash
python3 -c "
import json
from pathlib import Path
dispatch_dir = Path('.vnx-data/dispatches')
for d in sorted(dispatch_dir.glob('**/dispatch.json')):
    try:
        data = json.load(open(d))
        sid = data.get('metadata', {}).get('session_id')
        if sid:
            print(f'{d.parent.name}: {sid}')
    except:
        pass
"
```

Note: session_id capture in dispatch metadata is not yet implemented. Option A is the primary
path.

### 8.2 Resume the Conversation

```bash
# Navigate to the correct terminal directory first
cd $PROJECT_ROOT/.claude/terminals/<TERMINAL>
claude --resume <session_id>
```

If multiple sessions exist for the same terminal, pick the one with the most recent
`last_message` timestamp.

### 8.3 Worker Session Resume via Dispatch

Worker terminals (T1/T2/T3) should be resumed via a new dispatch, not by T0 manually resuming
their sessions. When a worker crashes mid-task:

1. Run the startup reconciliation procedure (`docs/core/DISPATCH_RULES.md` §10) to assess damage.
2. Check for orphaned dispatches in `active/`.
3. Create a re-dispatch to the affected worker with the remaining task scope.
4. The re-dispatched worker starts fresh — dispatch context is not preserved by `--resume`.

### 8.4 Important Limitations

- `--resume` restores conversation **message history only** — it does NOT restore in-flight
  dispatch context, local variable state, or queued actions.
- A resumed T0 session should be treated as a read-only history reference. Re-run the startup
  reconciliation before taking any new orchestration actions.
- The `--fork-session` flag creates a new session_id while showing the old history — use this
  if you want to avoid attaching back to the original session.

## 9. Skill Activation Announcement

**MANDATORY — first line of every response after skill load:**

```
🔧 Skill actief: t0-orchestrator
```

No exceptions. This must appear before any other content.
