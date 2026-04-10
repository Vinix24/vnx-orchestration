# FP-B Cutover, Rollback, And Certification

**Feature**: FP-B — Runtime Recovery, tmux Hardening, And Operability
**PR**: PR-5
**Status**: Certified
**Date**: 2026-03-29

---

## 1. Cutover Summary

PR-5 activates the full FP-B recovery stack:
- `vnx recover` reconciles leases, incidents, and tmux bindings from canonical SQLite state
- `vnx doctor` validates runtime health as preflight before recovery
- Workflow supervisor handles incident classification, dead-letter routing, and escalation
- tmux session profiles ensure terminal identity survives pane churn
- Legacy file-based recovery runs as complement/fallback

### Feature Flags (Post-Cutover Defaults)

| Flag | Default | Meaning |
|------|---------|---------|
| `VNX_RUNTIME_PRIMARY` | `1` | Runtime core active (broker + canonical lease) |
| `VNX_BROKER_SHADOW` | `0` | Broker is authoritative (not shadow) |
| `VNX_CANONICAL_LEASE_ACTIVE` | `1` | Canonical lease manager is authority |
| `VNX_TMUX_ADAPTER_ENABLED` | `1` | tmux adapter delivery active |
| `VNX_ADAPTER_PRIMARY` | `1` | load-dispatch primary path |
| `VNX_INCIDENT_SHADOW` | `1` | Incident recording active |

---

## 2. Rollback Procedure

If the FP-B recovery stack causes issues in production:

### Immediate Rollback (< 1 minute)

```bash
# Step 1: Disable runtime core
python scripts/rollback_runtime_core.py rollback

# Step 2: Run legacy-only recovery
vnx recover --legacy

# Step 3: Restart session
vnx start
```

### What Rollback Changes

| Component | Active | Rolled Back |
|-----------|--------|-------------|
| Dispatch coordination | SQLite broker | File-based dispatch dir |
| Lease management | Canonical lease_manager | terminal_state_shadow |
| Recovery command | Runtime + legacy | Legacy only |
| Incident recording | Shadow mode recording | No recording |
| tmux delivery | Adapter primary path | Legacy paste-buffer |

### Rollback Safety

- Rollback does **not** delete canonical state — SQLite database is preserved
- Re-enable is safe: `python scripts/rollback_runtime_core.py enable`
- Legacy paths never read from canonical state, so no conflict
- File-based dispatch artifacts and canonical SQLite are independent

### Re-Enable After Rollback

```bash
python scripts/rollback_runtime_core.py enable
vnx start
```

---

## 3. Recovery Flow (Post-Cutover)

When an operator runs `vnx recover`, the following phases execute in order:

### Phase 1: Preflight (Doctor)
- Run all `vnx doctor` runtime checks
- Abort on hard blockers (missing database, broken schema)
- Warn on soft issues (expired leases, incident pressure)

### Phase 2: Lease Reconciliation
- Expire stale leases (TTL elapsed without heartbeat)
- Recover expired leases to idle
- Release orphan leases (dispatch completed/expired/dead-lettered)
- Project canonical state to terminal_state.json

### Phase 3: Dispatch Reconciliation
- Timeout stuck dispatches (claimed/delivering past threshold)
- Flag recoverable dispatches for operator review
- Do not auto-recover — let operator decide

### Phase 4: Incident Reconciliation
- Generate incident summary from canonical incident_log
- Resolve stale process_crash incidents
- Reset exhausted retry budgets
- Collect pending escalations for report

### Phase 5: tmux Reconciliation
- Verify session profile against live tmux state
- Remap stale pane IDs (workdir-based rediscovery)
- Report missing terminals as remaining blockers

### Phase 6: Cutover Status
- Report runtime core status (active/inactive)
- Include rollback guidance in output

### Legacy Complement
After runtime recovery, legacy cleanup always runs:
- Stale lock cleanup
- Orphan PID cleanup
- Dispatch file cleanup
- Unclean-shutdown marker cleanup
- Payload temp file cleanup

---

## 4. Operator Runbook

### Normal Recovery
```bash
# Check health first
vnx doctor

# Preview recovery
vnx recover --dry-run

# Apply recovery
vnx recover

# Verify health after
vnx doctor
```

### Aggressive Recovery
```bash
# Force-kill all VNX processes, then recover
vnx recover --aggressive
```

### JSON Output (For Automation)
```bash
vnx recover --json
```

### Legacy-Only Recovery
```bash
vnx recover --legacy
```

---

## 5. Certification Evidence

### PR-0: Incident Taxonomy, Recovery Contracts
- All 7 incident classes defined with non-overlapping boundaries
- Recovery contracts specify retry budgets, cooldowns, escalation rules
- tmux identity invariants documented
- Certification matrix covers all in-scope scenarios
- **Evidence**: `test_incident_taxonomy.py` — all tests pass

### PR-1: Durable Incident Log, Retry Budgets
- Incident records are durable (SQLite-backed)
- Retry budgets persist across process restarts
- Shadow mode records without changing supervisor behavior
- Repeated failure detection works
- **Evidence**: `test_incident_log.py` — all tests pass

### PR-2: Workflow Supervisor, Dead-Letter, Escalation
- Process and workflow incidents handled by different logic paths
- Budget exhaustion triggers dead-letter transition
- Escalation events are durable and explicit
- Resume validation checks dispatch state and halt flags
- **Evidence**: `test_workflow_supervisor.py` — all tests pass

### PR-3: tmux Session Profiles, Remap, Operator Shell
- Session profile model separates identity from pane mechanics
- Remap updates adapter state, not runtime state
- Reheal rediscovers panes by workdir
- Home layout preserved during recovery
- **Evidence**: `test_tmux_session_profile.py`, `test_tmux_adapter.py` — all tests pass

### PR-4: Doctor Hardening, Recovery Preflight
- Doctor reads canonical runtime state
- Output distinguishes pass/warn/fail with concrete reasons
- Recovery preflight identifies blockers
- Doctor is read-only and idempotent
- **Evidence**: `test_vnx_doctor_runtime.py` — all tests pass

### PR-5: Recover Operator Flow, Cutover, Certification
- Recovery reconciles leases, incidents, tmux bindings before resume
- Output includes summary, escalation items, remaining blockers
- Legacy recovery marked as fallback when runtime core is active
- Cutover has rollback guidance and certification evidence
- Recovery is idempotent — second run is clean
- **Evidence**: `test_vnx_recover_runtime.py` — all tests pass

---

## 6. Certification Matrix Verification

Rows from `22_FPB_CERTIFICATION_MATRIX.md`:

| Row | Scenario | Status | Evidence |
|-----|----------|--------|----------|
| 1.1-1.3 | Process crash recovery | PASS | test_incident_log, test_workflow_supervisor |
| 2.1-2.3 | Terminal unresponsive recovery | PASS | test_incident_log, test_workflow_supervisor |
| 3.1-3.3 | Delivery failure recovery | PASS | test_incident_log, test_workflow_supervisor |
| 4.1-4.2 | ACK timeout recovery | PASS | test_incident_log, test_workflow_supervisor |
| 5.1-5.2 | Lease conflict recovery | PASS | test_incident_log |
| 6.1-6.2 | Resume failed recovery | PASS | test_workflow_supervisor |
| 7.1-7.2 | Repeated failure loop | PASS | test_incident_log, test_workflow_supervisor |
| 8.1-8.3 | tmux identity invariants | PASS | test_tmux_session_profile |
| 9.1-9.3 | Doctor integrity | PASS | test_vnx_doctor_runtime |
| 9.4-9.6 | Recover operator flow | PASS | test_vnx_recover_runtime |

---

## 7. Residual Risks

| Risk | Mitigation | Status |
|------|-----------|--------|
| Incident class boundaries may need tuning | Configurable thresholds; monitor first week | Open |
| Retry budgets may be too aggressive/conservative | Adjustable via ReconcilerConfig | Open |
| tmux crash during recovery could compound incidents | Recovery is idempotent; safe to re-run | Mitigated |
| Dead-letter accumulation without operator attention | `vnx doctor` surfaces count; dashboard shows queue | Mitigated |
| Rollback path not tested under load | Documented procedure; manual testing sufficient for now | Accepted |

---

## 8. FP-B Status

**FP-B is CERTIFIED** — all quality gates pass, all certification matrix rows verified.

FP-C (execution mode expansion) is unblocked pending this certification.
