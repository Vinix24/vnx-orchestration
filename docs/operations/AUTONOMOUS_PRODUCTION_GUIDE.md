# VNX Autonomous Production Guide

**Version**: 2.1
**Date**: 2026-04-01
**Status**: Active reference with historical sections
**Source**: VNX_PID.md, VNX_DIGITAL_AGENT_TEAM_VISION.md, VNX_N8N_PROJECTPLAN.md
**Prerequisite**: [AUTONOMOUS_EXECUTION_PLAN.md](../plans/AUTONOMOUS_EXECUTION_PLAN.md)
**Wave Mapping**: [VNX_AGENT_TEAM_WAVE_MAPPING.md](VNX_AGENT_TEAM_WAVE_MAPPING.md)

---

## Current State Delta (2026-04-01)

This guide still contains older execution examples, but the current production reality is:

- the default model is **one feature worktree per feature/fix**, not per-terminal worktrees
- the operator surface is now complemented by the delivered **SEOcrawler Command Center**
- FP29, FP30, and FP31 have now provided real proving-ground evidence for dashboard operations, paid delivery lifecycle, and production validation
- interactive defaults remain Sonnet/Opus driven, but optional **Gemini/Codex headless review gates** are now part of the broader VNX evolution and should not be read as forbidden by older examples in this document

Use this guide as the production protocol reference, but prefer:
- `../PROJECT_STATUS.md` for current milestone truth
- `../core/00_GETTING_STARTED.md` for current startup/worktree flow
- `../manifesto/ROADMAP.md` for what is next

---

## Inhoudsopgave

1. [Preflight Checklist](#1-preflight-checklist)
2. [FEATURE_PLAN Format](#2-feature_plan-format)
3. [Quality Gate Standaard](#3-quality-gate-standaard)
4. [T0 Quality Enforcement](#4-t0-quality-enforcement)
5. [Wave-Based Execution Protocol](#5-wave-based-execution-protocol)
6. [Git Automation](#6-git-automation)
7. [Autonome Safeguards](#7-autonome-safeguards)
8. [Volledige Run: 5 Fases, 24 Waves, 70 PRs](#8-volledige-run-5-fases-24-waves-70-prs)

---

## Kernprincipes

| Principe | Regel |
|----------|-------|
| **Klein en gefocust** | 1 PR = 1 ding. 30-90 min. Max 150 regels changed. Liever 50 kleine PRs dan 10 grote |
| **Geen bewijs = reject** | Receipt zonder evidence → direct terug naar terminal |
| **T0 verifieert zelf** | T0 runt gate commands zelf, vertrouwt NIET op self-reported resultaten |
| **Interactive default, optional headless review** | T1/T2 = Sonnet (standaard), T3 = Opus (complex). Headless Gemini/Codex review gates are optional and policy-driven, not the default execution path |
| **Quality gates overal** | In dispatch (worker moet uitvoeren) + door T0 (onafhankelijk herhaald) |
| **Single responsibility** | Elke PR doet precies 1 ding. Multi-step = opsplitsen in meerdere PRs |

---

## 1. Preflight Checklist

Script: `vnx_preflight.sh`
Output: `$VNX_STATE_DIR/preflight_report.json`

Draait VOOR de eerste dispatch van een autonome sessie. Elke check levert PASS/FAIL/WARN.

### 1.1 Environment

| Check | Command | Criterium |
|-------|---------|-----------|
| Claude CLI | `claude --version` | Exit 0, versie >= 1.0 |
| GitHub CLI | `gh --version` | Exit 0 |
| Git | `git --version` | Exit 0, versie >= 2.30 |
| Python 3 | `python3 --version` | Exit 0, versie >= 3.11 |
| jq | `jq --version` | Exit 0 |
| tmux | `tmux -V` | Exit 0 |
| Node.js | `node --version` | Exit 0, versie >= 18 |
| .venv | `test -d .venv && .venv/bin/python --version` | Exit 0 |
| Disk | `df -g . \| awk 'NR==2{print $4}'` | >= 5 GB vrij |
| RAM | `sysctl hw.memsize` | >= 4 GB |
| Network | `curl -sI https://api.anthropic.com` | HTTP 200/401 |

### 1.2 Credentials

| Check | Validatie | Criterium |
|-------|-----------|-----------|
| ANTHROPIC_API_KEY | `test -n "$ANTHROPIC_API_KEY"` | Non-empty |
| GitHub auth | `gh auth status` | Authenticated |
| Supabase URL | `test -n "$SUPABASE_URL"` | Non-empty |
| Supabase KEY | `test -n "$SUPABASE_ANON_KEY"` | Non-empty |
| .env completeness | Vergelijk `.env` tegen `.env.example` | Alle keys aanwezig |

### 1.3 VNX Health

| Check | Command | Criterium |
|-------|---------|-----------|
| VNX Doctor | `bash scripts/vnx_doctor.sh` | Exit 0 |
| Processen alive | Supervisor status check | 9/9 processen running |
| Terminal state | Parse `terminal_state.json` | Alle terminals `idle` |
| Dashboard fresh | `stat dashboard_status.json` mtime | < 30s oud |

### 1.4 Worktrees

| Check | Command | Criterium |
|-------|---------|-----------|
| Active feature worktree | `git rev-parse --show-toplevel` | Resolved and expected |
| Isolated runtime state | `test -d .vnx-data && test -d .vnx-data/state` | Exists in active worktree |
| Git clean | `git status --porcelain` | Empty before kickoff/merge boundary |
| Branch sync | `git rev-parse HEAD` vs `origin/main` | Up-to-date or intentionally branched |
| Session/worktree mapping | `vnx status` or operator surface | Correct worktree/session ownership visible |

### 1.5 FEATURE_PLAN

| Check | Command | Criterium |
|-------|---------|-----------|
| Parseable | `python3 validate_feature_plan.py FEATURE_PLAN.md` | Exit 0 |
| DAG acyclisch | Dependency graph validation | Geen cycles |
| Skills bestaan | Cross-check met `skills/skills.yaml` | Alle skills geldig |
| Quality gates | Parse gate commands per PR | Alle commands uitvoerbaar |
| Wave assignments | Wave metadata aanwezig | Elke PR heeft wave |

### 1.6 Token Budget

| Check | Criterium |
|-------|-----------|
| `VNX_TOKEN_STOP_PER_PHASE` | Geconfigureerd en > 0 |
| Max Plan actief | `claude --version` + subscription check |
| cost_tracker.py | Responsive (exit 0 op `--tokens`) |

### Preflight Report Schema

```json
{
  "timestamp": "2026-03-09T14:00:00Z",
  "overall": "PASS|FAIL",
  "sections": {
    "environment": { "status": "PASS", "checks": [] },
    "credentials": { "status": "PASS", "checks": [] },
    "vnx_health":  { "status": "PASS", "checks": [] },
    "worktrees":   { "status": "PASS", "checks": [] },
    "feature_plan":{ "status": "PASS", "checks": [] },
    "budget":      { "status": "PASS", "checks": [] }
  },
  "blockers": [],
  "warnings": []
}
```

**Gate rule**: ANY section `FAIL` → autonome sessie start NIET.

---

## 2. FEATURE_PLAN Format

Enhanced format voor autonome executie. Backwards-compatible met `validate_feature_plan.py`.

### 2.1 Wave Definitie (bovenaan plan)

```markdown
## Waves

| Wave | PRs | Gate | Merge Strategy | Sync |
|------|-----|------|----------------|------|
| 0.1 | PR-1, PR-2, PR-3 | All gates pass | squash per PR | Full worktree sync |
| 0.2 | PR-4, PR-5, PR-6 | All gates pass | squash per PR | Full worktree sync |

### Dependency DAG
PR-1 → []
PR-2 → []
PR-3 → []
PR-4 → [PR-1]
PR-5 → [PR-2]
PR-6 → [PR-3]
```

Machine-parseable: `PR-{N} → [{deps}]` formaat, 1 per regel.

### 2.2 PR Definitie (klein en gefocust)

```markdown
## PR-{N}: {Titel}

**Track**: A|B|C
**Skill**: @{skill-name}
**Wave**: {fase.wave}
**Model**: Sonnet|Opus

Dependencies: [{PR-X}, ...]

### Taak
{Eén duidelijke zin: wat er gebouwd wordt}

### Files
- `path/to/file` (~{N} lines) — {purpose}

### Quality Gate
```bash
# Check 1: {wat wordt getest}
{exact command}
# Verwacht: exit 0

# Check 2: {wat wordt getest}
{exact command}
# Verwacht: exit 0, output bevat "{pattern}"
```

### Evidence (verplicht in receipt)
```json
{
  "pr_id": "PR-{N}",
  "commit_hash": "{sha}",
  "branch": "feat/PR-{N}-{slug}",
  "changed_files": [],
  "gate_results": {
    "check_1": { "exit_code": 0, "output": "" },
    "check_2": { "exit_code": 0, "output": "" }
  }
}
```
```

**Regels**:
- Max 150 regels changed per PR
- Max 3 bestanden gewijzigd per PR
- 1 taak = 1 verantwoordelijkheid
- Als een taak meerdere stappen heeft → opsplitsen in meerdere PRs

### 2.3 Validatie Extensies

`validate_feature_plan.py` wordt uitgebreid met:
- **Wave parsing**: Elke PR moet `Wave:` field hebben
- **Size check**: Max 150 regels per PR (geschat via Files sectie)
- **Gate command check**: Minstens 2 gate commands per PR
- **Evidence template check**: Alle PRs hebben evidence template
- **DAG + Wave consistency**: PR in wave N mag alleen dependen op PRs in wave < N
- **Model check**: Sonnet of Opus, geen andere modellen

---

## 3. Quality Gate Standaard

Quality gates worden op **twee plekken** afgedwongen: in de dispatch naar de terminal, en door T0 na ontvangst van de receipt.

### 3.1 In de Dispatch (worker-facing)

Elke dispatch bevat een verplichte quality gate sectie die de worker MOET uitvoeren:

```markdown
---
## Quality Gate (VERPLICHT)

Je werk is PAS klaar als ALLE checks hieronder slagen.
Voer ze ZELF uit en rapporteer de resultaten in je receipt.

### Checks
1. `{command}` → verwacht: exit 0
2. `{command}` → verwacht: exit 0, output bevat "{pattern}"
3. `{command}` → verwacht: exit 0

### Evidence (VERPLICHT in receipt)
Stuur in je afsluitende receipt:
- Commit hash (git rev-parse HEAD)
- Branch naam (feat/PR-{N}-{slug})
- Lijst van gewijzigde bestanden
- Per gate check: exit code + relevante output

ZONDER EVIDENCE WORDT JE WERK AFGEWEZEN.
Als een check faalt: fix het eerst. Als je het niet kunt fixen: meld exact wat er faalt.
---
```

### 3.2 T0 Enforcement (onafhankelijke verificatie)

T0 voert na elke receipt een onafhankelijke verificatie uit. Dit is de **enige** manier waarop werk goedgekeurd wordt.

```
Receipt ontvangen
  │
  ├─ Heeft evidence sectie? ─── NEE → REJECT "Geen evidence"
  │                                    Re-dispatch met instructie:
  │                                    "Voer quality gate checks uit en
  │                                     lever evidence in je receipt"
  │
  ├─ Commit hash verificatie ── NEE → REJECT "Commit hash mismatch"
  │   git rev-parse HEAD ==
  │   receipt.commit_hash?
  │
  ├─ Branch naam correct? ──── NEE → REJECT "Branch naming violation"
  │   feat/PR-{N}-{slug}?
  │
  ├─ Changed files in scope? ── NEE → REJECT "Out-of-scope changes"
  │   subset van FEATURE_PLAN
  │   Files sectie?
  │
  ├─ T0 runt gate commands ─── FAIL → REJECT met failure output
  │   in worker's worktree          Re-dispatch met failure context:
  │                                  "Gate {N} failed: {output}"
  │
  └─ Alle checks PASS ─────── APPROVE → merge candidate
```

### 3.3 Gate Command Categorieeen

| Type | Voorbeeld | Wanneer |
|------|-----------|---------|
| **Syntax check** | `python3 -c "import json; json.load(open('file.json'))"` | JSON/YAML files |
| **Linting** | `shellcheck script.sh`, `ruff check file.py` | Scripts |
| **Schema check** | `psql $DB -c "\dt schema.*"` (N tabellen) | Database |
| **Config validity** | `docker compose config --quiet` | Docker |
| **Pattern match** | `grep 'expected_pattern' file` | Config correctness |
| **File existence** | `test -f path/to/expected/file` | Deliverable check |
| **Node count** | `jq '.nodes \| length' workflow.json` (>= N) | n8n workflows |
| **Import test** | `python3 -c "from module import Class"` | Python modules |

### 3.4 Minimum Gate Requirements

| PR Type | Minimum gates |
|---------|---------------|
| Config/template file | 2: syntax valid + expected content present |
| SQL migration | 3: syntax valid + tables created + constraints present |
| n8n workflow JSON | 3: valid JSON + required nodes present + error handler |
| Shell script | 2: shellcheck pass + expected behavior |
| Python script | 3: import succeeds + lint pass + basic execution |
| Documentation | 2: file exists + required sections present |

---

## 4. T0 Quality Enforcement

Script: `t0_evidence_validator.py`
Aangeroepen door T0 na elke worker completion receipt.

### 4.1 Validatie Pipeline

```
Receipt ontvangen
  ├→ 1. Evidence Presence Check
  ├→ 2. Git Evidence Verification
  ├→ 3. Gate Command Re-execution
  ├→ 4. Scope Verification
  └→ 5. Decision
       ├→ ALL PASS → Approve (merge candidate)
       └→ ANY FAIL → Reject (re-dispatch)
```

### 4.2 Evidence Presence Check

Eerste check: heeft de receipt alle verplichte velden?

```python
REQUIRED_EVIDENCE = [
    "commit_hash",    # git SHA
    "branch",         # feat/PR-{N}-{slug}
    "changed_files",  # list of paths
    "gate_results",   # dict of check → result
]

def check_evidence_presence(receipt: dict) -> bool:
    evidence = receipt.get("evidence", {})
    missing = [f for f in REQUIRED_EVIDENCE if f not in evidence]
    if missing:
        return False, f"Ontbrekende evidence velden: {missing}"
    return True, ""
```

**Als evidence ontbreekt**: REJECT met instructie om gate checks uit te voeren.
Dit is de meest voorkomende rejection — workers vergeten soms de evidence.

### 4.3 Git Evidence Verification

```bash
# Verify branch exists and matches pattern
BRANCH=$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD)
[[ "$BRANCH" == feat/PR-${PR_ID}-* ]] || FAIL "Branch pattern mismatch"

# Verify commit hash matches receipt
ACTUAL=$(git -C "$WORKTREE" rev-parse HEAD)
test "$ACTUAL" = "$RECEIPT_HASH" || FAIL "Commit hash mismatch"

# Verify changed files
CHANGED=$(git -C "$WORKTREE" diff --name-only origin/main...HEAD)
# Cross-check: alle changed files moeten in FEATURE_PLAN Files sectie staan
```

### 4.4 Gate Command Re-execution

T0 runt ZELF alle gate commands in de worker's worktree:

```bash
cd "$WORKTREE"

# Execute each gate command from FEATURE_PLAN
for cmd in "${GATE_COMMANDS[@]}"; do
    output=$(eval "$cmd" 2>&1)
    exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        FAIL "Gate command failed: $cmd\nOutput: $output"
    fi
done
```

**Kritiek**: T0 vertrouwt NIET op de worker's self-reported gate results.
T0 runt de commands zelf. Als de worker zegt "alle gates pass" maar T0's
re-execution faalt → REJECT.

**Hook-compatibiliteit**: `t0-readonly-enforcer.sh` moet gate-gerelateerde
commands whitelisten voor T0 wanneer `VNX_AUTONOMOUS=true`:
- `python3 -c "..."` (import/syntax checks)
- `jq` (JSON validation)
- `shellcheck` (linting)
- `grep` (pattern matching)
- `docker compose config` (Docker validation)
- `psql` (read-only queries)

### 4.5 Decision Matrix

| Evidence | Git | Gates | Scope | Decision |
|----------|-----|-------|-------|----------|
| Present | PASS | PASS | PASS | **APPROVE** |
| Missing | * | * | * | **REJECT** — "Lever evidence" |
| Present | FAIL | * | * | **REJECT** — "Git evidence mismatch" |
| Present | PASS | FAIL | * | **REJECT** — "Gate {N} failed: {output}" |
| Present | PASS | PASS | FAIL | **REJECT** — "Out-of-scope changes" |

### 4.6 Rejection Protocol

Bij REJECT:

**Poging 1** (severity: warn):
1. Open item aanmaken: tag `quality-gate-failure`, severity `warn`
2. Re-dispatch naar DEZELFDE terminal met:
   - Originele dispatch als context
   - Failure reden + output
   - Instructie: "Fix {specifiek probleem} en lever evidence"
3. Role blijft originele skill (niet debugger)

**Poging 2** (severity: blocker):
1. Open item severity escaleren naar `blocker`
2. Re-dispatch met `debugger` role
3. Uitgebreide failure context + stack trace als beschikbaar

**Na 2 failures**:
1. PR → `blocked` in queue state
2. T0 gaat door met andere PRs in wave (als onafhankelijk)
3. Wave kan NIET completen totdat blocked PR resolved
4. Escaleer naar Vincent

---

## 5. Wave-Based Execution Protocol

### 5.1 Lifecycle

```
PREFLIGHT → FASE_INIT → WAVE_INIT → DISPATCH → EXECUTE → COLLECT → VALIDATE → MERGE → SYNC → NEXT_WAVE → ... → FASE_GATE → NEXT_FASE
```

### 5.2 Fase Lifecycle

Elke fase heeft:
- Meerdere waves (2-6 per fase)
- Een go/no-go gate voor de volgende fase
- Een budget limiet

```
FASE_INIT
  ├→ Parse alle waves in deze fase
  ├→ Check fase dependencies (vorige fase compleet?)
  ├→ Check budget ($30/fase default)
  └→ Start eerste wave

WAVE_INIT
  ├→ Parse PRs voor deze wave (max 3, een per terminal)
  ├→ Verifieer wave dependencies (vorige wave gemerged)
  ├→ Terminal assignment (T1=Sonnet, T2=Sonnet, T3=Opus)
  └→ Budget check ($15/wave default)

DISPATCH
  ├→ Auto-accept: VNX_AUTO_ACCEPT=true bypass popup
  ├→ Alle PRs tegelijk naar T1/T2/T3
  ├→ Dispatch bevat: taak + quality gate + evidence template
  └→ Git instructies in footer

EXECUTE
  ├→ Workers parallel in eigen worktrees
  ├→ Timeout: 30 min per PR
  └→ Context rotation bij 65% threshold

COLLECT
  ├→ T0 wacht op receipts
  ├→ Timeout monitoring (heartbeat_ack_monitor.py)
  └→ Partial completion: valideer klare PRs, re-dispatch timeouts

VALIDATE
  ├→ Per PR: t0_evidence_validator.py
  ├→ Evidence check → Git check → Gate re-execution → Scope check
  └→ APPROVE of REJECT (max 2 retries)

MERGE (alleen als alle PRs in wave APPROVED)
  ├→ Per PR sequentieel: gh pr merge --squash --delete-branch
  ├→ Bij merge conflict: stop, escaleer
  └→ Update PR queue state

SYNC
  ├→ vnx_worktree_setup.sh sync
  ├→ Verifieer alle worktrees op zelfde main HEAD
  └→ Dashboard update

NEXT_WAVE of FASE_GATE
  ├→ Meer waves in fase? → WAVE_INIT
  ├→ Fase compleet? → Go/no-go check
  └→ Alle criteria pass? → NEXT_FASE
```

### 5.3 State Tracking

`$VNX_STATE_DIR/wave_state.json`:

```json
{
  "session_id": "auto-2026-03-09-001",
  "current_phase": 0,
  "current_wave": "0.3",
  "phases": {
    "0": {
      "status": "executing",
      "budget_used_usd": 12.40,
      "waves": {
        "0.1": {
          "status": "merged",
          "prs": {
            "PR-1": { "status": "merged", "commit": "abc123", "retries": 0 },
            "PR-2": { "status": "merged", "commit": "def456", "retries": 0 },
            "PR-3": { "status": "merged", "commit": "ghi789", "retries": 1 }
          }
        },
        "0.2": { "status": "merged" },
        "0.3": { "status": "executing" }
      }
    }
  },
  "total_cost_usd": 12.40,
  "total_dispatches": 9,
  "total_retries": 1,
  "total_rejections": 1
}
```

---

## 6. Git Automation

### 6.1 Worker-Side (in dispatch footer)

```bash
# 1. Branch vanuit track branch
git checkout -b "feat/PR-${PR_ID}-${SLUG}" "track/${TRACK}"

# 2. Implementeer (1 taak, klein en gefocust)

# 3. Commit met metadata
git add -A
git commit -m "$(cat <<'EOF'
feat(${scope}): ${title}

Dispatch-ID: ${DISPATCH_ID}
PR-ID: ${PR_ID}
Wave: ${WAVE}
Terminal: ${TERMINAL}

Co-Authored-By: Claude ${MODEL} <noreply@anthropic.com>
EOF
)"

# 4. Push
git push -u origin "feat/PR-${PR_ID}-${SLUG}"

# 5. PR aanmaken
gh pr create \
  --title "PR-${PR_ID}: ${TITLE}" \
  --body "$(cat <<'EOF'
## Changes
${CHANGE_SUMMARY}

## Quality Gate Results
${GATE_RESULTS}

## Evidence
- Commit: ${COMMIT_HASH}
- Branch: feat/PR-${PR_ID}-${SLUG}
- Files: ${CHANGED_FILES}
EOF
)"  --base main
```

### 6.2 T0-Side (na validatie)

```bash
# 1. Merge
gh pr merge "feat/PR-${PR_ID}-${SLUG}" --squash --delete-branch

# 2. Sync
bash .claude/vnx-system/scripts/vnx_worktree_setup.sh sync

# 3. State update
python3 .claude/vnx-system/scripts/pr_queue_manager.py complete "PR-${PR_ID}"
```

### 6.3 Conventies

| Item | Format | Voorbeeld |
|------|--------|-----------|
| Feature branch | `feat/PR-{N}-{slug}` | `feat/PR-10-morning-brief-cron` |
| Track branch | `track/{A\|B\|C}` | `track/A` |
| Commit type | `feat\|fix\|refactor\|docs\|chore` | `feat(n8n): add morning brief cron trigger` |
| PR title | `PR-{N}: {Titel}` | `PR-10: Morning Brief cron trigger + Supabase queries` |

---

## 7. Autonome Safeguards

### 7.1 Token Guardrails (Max Plan)

Alle terminals draaien op Claude Max Plan — geen per-token kosten. De limiet is **tokens per tijdseenheid** (rate limit), niet budget. Guardrails zijn daarom token-based:

| Threshold | Actie |
|-----------|-------|
| 500K tokens per dispatch | WARNING: taak mogelijk te groot, opsplitsen |
| 1M tokens per wave (3 dispatches) | AUTO-PAUSE: review of dispatches efficient zijn |
| 5M tokens per fase | HARD STOP: fase review, mogelijke context bloat |

```bash
export VNX_TOKEN_WARN_PER_DISPATCH=500000    # Warning per dispatch
export VNX_TOKEN_PAUSE_PER_WAVE=1000000      # Pause per wave
export VNX_TOKEN_STOP_PER_PHASE=5000000      # Stop per fase
```

**Rate limit strategie**: Met 3 terminals parallel is de Max Plan rate limit de bottleneck. Bij rate limiting:
- T0 vertraagt dispatches (5 min cooldown)
- Waves worden sequentieel i.p.v. parallel
- Terminal met laagste token usage krijgt prioriteit

Token tracking via `cost_tracker.py --tokens` (leest uit receipts).

### 7.2 Quality Gates (samenvatting)

- **In dispatch**: Worker krijgt gate commands + evidence template
- **T0 re-execution**: T0 runt gate commands zelf in worktree
- **Geen bewijs = reject**: Altijd, zonder uitzondering
- **Max 2 retries**: Daarna PR blocked + escalatie

### 7.3 Rollback

Bij downstream wave failure traceerbaar naar eerder gemerged PR:

```bash
MERGE_COMMIT=$(gh pr view "PR-${N}" --json mergeCommit -q '.mergeCommit.oid')
git revert --no-edit "$MERGE_COMMIT"
git push origin main
bash .claude/vnx-system/scripts/vnx_worktree_setup.sh sync
```

Alleen als:
- Failure direct traceerbaar naar specifiek PR
- Revert geen cascade-effect op andere PRs
- Anders: escaleer naar Vincent

### 7.4 Deadlock Detectie

| Conditie | Timeout | Actie |
|----------|---------|-------|
| Geen heartbeat change | 10 min | WARNING |
| Geen receipt na dispatch | 30 min | Force lease clear + re-dispatch |
| Terminal crashed | Heartbeat stopt | Restart + re-dispatch |

### 7.5 Loop Preventie

| Limiet | Waarde | Bij overschrijding |
|--------|--------|-------------------|
| Retries per PR | 2 | PR blocked, escaleer |
| Waves per sessie | 6 | Pauze voor review |
| Dispatches per sessie | 25 | Pauze voor review |
| Failed gates per wave | 3 | Wave blocked, escaleer |
| Totaal blocked PRs | 5 | Sessie stop, review |

### 7.6 Context Rotation

Ongewijzigd bestaand mechanisme:
- `vnx_context_monitor.sh` monitort context usage
- Bij 65% threshold: handover + rotate
- Worker krijgt context summary van vorige sessie

---

## 8. Volledige Run: 5 Fases, 24 Waves, 70 PRs

Project: VNX Digital Agent Team
Source: VNX_PID.md (Fase 0-4, 4 Tracks, 12 weken, 23 workflows)
Terminals: T0 (Opus) + T1 (Sonnet) + T2 (Sonnet) + T3 (Opus)
Mode: `--dangerously-skip-permissions`

Gedetailleerde PR-niveau breakdown: zie [VNX_AGENT_TEAM_WAVE_MAPPING.md](VNX_AGENT_TEAM_WAVE_MAPPING.md)

### Fase 0: Fundament (Week 1-2) — 15 PRs, 5 Waves

**Doel**: Infra live, database schema compleet, eerste 2 workflows importeerbaar.

| Wave | PRs | Focus | T1 | T2 | T3 |
|------|-----|-------|----|----|-----|
| 0.1 | 3 | Docker + Caddy + env template | Docker Compose | Caddyfile | .env templates |
| 0.2 | 3 | Database schemas | intelligence schema | crm schema | content+system schema |
| 0.3 | 3 | Bot + error + credentials | Telegram setup | Error workflow | Credential templates |
| 0.4 | 3 | Morning Brief workflow | Cron + queries | Summary + output | Logging + test data |
| 0.5 | 3 | Idee Capture workflow | Webhook + normalize | Classify + route | Store + confirm |

**Go/No-Go → Fase 1**:
- [ ] Docker Compose valid en deployable
- [ ] Alle 4 Supabase schemas met tabellen
- [ ] Morning Brief + Idee Capture workflows als valid JSON
- [ ] Error workflow met Telegram alert
- [ ] Credential templates compleet

### Fase 1: Kernworkflows (Week 3-4) — 17 PRs, 6 Waves

**Doel**: Telegram Router + Gmail + Content Pipeline + Fix Pipeline + Calendar Sync operationeel.

| Wave | PRs | Focus | T1 | T2 | T3 |
|------|-----|-------|----|----|-----|
| 1.1 | 3 | Telegram Router | Help command | Intent classify | Switch 9 routes |
| 1.2 | 3 | Gmail Triage | Trigger + filter | Haiku classify | Switch routing |
| 1.3 | 3 | Content Pipeline LinkedIn | Trigger + type detect | Approve flow | SSH + quality gate |
| 1.4 | 3 | Content Fix Pipeline | /fix parser + lookup | SSH fix + gate | Approve → git PR |
| 1.5 | 3 | Shortcuts + Calendar | Shortcut docs | Calendar parse | Calendar store |
| 1.6 | 2 | Kill Switch + Logging | Kill switch | Execution logging | — |

**Go/No-Go → Fase 2**:
- [ ] Router routeert 9 commands
- [ ] Gmail classificeert 7 categorieen
- [ ] Content Pipeline met quality gate
- [ ] Fix Pipeline produceert PR
- [ ] Calendar Sync + Kill Switch werken

### Fase 2: Migratie (Week 5-7) — 18 PRs, 6 Waves

**Doel**: Monitoring, leads, dev task, blog, SEOcrawler, backup allemaal operationeel.

| Wave | PRs | Focus | T1 | T2 | T3 |
|------|-----|-------|----|----|-----|
| 2.1 | 3 | Website Monitoring | HTTP checks | Alert + log | Daily digest |
| 2.2 | 3 | Lead Management | PhantomBuster import | Alert flow | Lead scoring |
| 2.3 | 3 | Dev Task (/bouw) | Confirm flow | Parser + routing | SSH execution |
| 2.4 | 3 | Blog Pipeline | Git + publish | Quality gate | SSH blog-writer |
| 2.5 | 3 | SEOcrawler | /scan trigger | Result + PDF | Email + health |
| 2.6 | 3 | Backup + Fallback | pg_dump script | Workflow export | SSH queue fallback |

**Go/No-Go → Fase 3**:
- [ ] 5 endpoints gemonitord
- [ ] Lead scoring operationeel
- [ ] /bouw produceert PR
- [ ] Blog pipeline met approval
- [ ] Backup strategie actief

### Fase 3: Intelligence (Week 8-10) — 11 PRs, 4 Waves

**Doel**: Evening digest, repurposer, Notion sync, LinkedIn scan, Ollama classificatie.

| Wave | PRs | Focus | T1 | T2 | T3 |
|------|-----|-------|----|----|-----|
| 3.1 | 3 | Digest + Repurposer | Digest output | Digest data | Repurposer SSH |
| 3.2 | 3 | Cross-post + Notion | Dev.to API | n8n → Notion | Notion → Supabase |
| 3.3 | 3 | LinkedIn + Triage | Lead alerts | LinkedIn scan | Wekelijkse triage |
| 3.4 | 2 | Ollama Setup | Setup script | HTTP template | — |

**Go/No-Go → Fase 4**:
- [ ] Evening digest draait
- [ ] Content repurposing werkt
- [ ] Notion sync bidirectioneel
- [ ] LinkedIn scan levert alerts

### Fase 4: Geavanceerd (Week 11-12) — 9 PRs, 3 Waves

**Doel**: Lead automation, meeting prep, performance tracking, compliance, docs.

| Wave | PRs | Focus | T1 | T2 | T3 |
|------|-----|-------|----|----|-----|
| 4.1 | 3 | Lead + Meeting Prep | Meeting output | Meeting data | Lead automation |
| 4.2 | 3 | Performance + Formula | GA4/GSC tracking | Notion KPIs | Succesformule |
| 4.3 | 3 | Compliance + Docs | Runbook | Model router | AVG cleanup |

**Eindcriteria**:
- [ ] 23 workflows als valid JSON
- [ ] Lead scoring automation
- [ ] Performance tracking
- [ ] AVG compliance
- [ ] Complete runbook
- [ ] Model router configured

### Totaaloverzicht

| Fase | Waves | PRs | Tokens (schatting) | Doorlooptijd |
|------|-------|-----|-------------------|-------------|
| 0 | 5 | 15 | ~800K | ~3-4 uur |
| 1 | 6 | 17 | ~1.2M | ~4-5 uur |
| 2 | 6 | 18 | ~1.5M | ~5-6 uur |
| 3 | 4 | 11 | ~700K | ~3 uur |
| 4 | 3 | 9 | ~500K | ~2-3 uur |
| **Totaal** | **24** | **70** | **~4.7M** | **~17-21 uur** |

NB: Doorlooptijd is executietijd (niet kalendertijd). Met pauzes en reviews is het realistische schema 12 weken zoals het PID beschrijft.

### Vincent's Handmatige Taken (buiten autonome executie)

| Taak | Wanneer | Blokkeert |
|------|---------|-----------|
| BotFather: Telegram bot aanmaken | Voor Wave 0.3 | PR-7 |
| DNS: auto.vincentvandeth.nl CNAME | Voor deployment | Alle |
| Google OAuth consent screen | Voor Wave 1.2 | PR-19 |
| 1Password CLI setup | Voor Wave 0.3 | PR-9 |
| Notion workspace aanmaken | Voor Wave 0.5 | PR-15 |
| Apple Shortcuts installeren | Na Wave 1.5 | Geen |
| GCP VM aanmaken + SSH key | Voor Wave 0.1 deployment | Alle |
| Supabase credentials | Voor Wave 0.2 | PR-4/5/6 |
| Tailscale (VPS + MacBook) | Voor Wave 2.3 | PR-40 |

### Configuratie

```bash
# Environment
export VNX_AUTO_ACCEPT=true
export VNX_AUTONOMOUS=true
export VNX_TOKEN_WARN_PER_DISPATCH=500000
export VNX_TOKEN_PAUSE_PER_WAVE=1000000
export VNX_TOKEN_STOP_PER_PHASE=5000000
export VNX_DISPATCH_TIMEOUT_MIN=30
export VNX_MAX_RETRIES=2
export VNX_MAX_WAVES_PER_SESSION=6
export VNX_MAX_DISPATCHES_PER_SESSION=25

# Launch (per fase)
bash .claude/vnx-system/scripts/vnx_preflight.sh
# Verify PASS → Start T0 met wave execution protocol
```

---

## Verificatie

### Showstoppers (uit AUTONOMOUS_EXECUTION_PLAN.md)

| # | Showstopper | Geadresseerd in |
|---|-------------|-----------------|
| S1 | Geen bestaande codebase | Sectie 1 (preflight), Sectie 8 (wave 0.1 = bootstrap) |
| S2 | Geen credentials | Sectie 1.2, Vincent's handmatige taken |
| S3 | Geen tooling validatie | Sectie 1.1 (environment checks) |
| S4 | Queue handmatige acceptatie | Sectie 5.2 (VNX_AUTO_ACCEPT) |
| S5 | Geen git/PR workflow | Sectie 6 (git automation) |

### Safeguards (uit AUTONOMOUS_EXECUTION_PLAN.md)

| # | Safeguard | Geadresseerd in |
|---|-----------|-----------------|
| G1 | Cost guardrails | Sectie 7.1 (70/90/100% per wave/fase/sessie) |
| G2 | Quality gate enforcement | Sectie 3 + 4 (dual enforcement: dispatch + T0) |
| G3 | Wave-based execution | Sectie 5 (24 waves, 5 fases) |
| G4 | External API mocking | Niet in scope (n8n workflows gebruiken --dry-run) |
| G5 | Rollback capability | Sectie 7.3 (git revert protocol) |

---

## Te Implementeren Bestanden

| Bestand | Type | Beschrijving |
|---------|------|--------------|
| `scripts/vnx_preflight.sh` | NIEUW | Preflight validatie (sectie 1) |
| `scripts/t0_evidence_validator.py` | NIEUW | Evidence verificatie (sectie 4) |
| `scripts/queue_popup_watcher.sh` | EDIT | VNX_AUTO_ACCEPT bypass |
| `scripts/dispatcher_v8_minimal.sh` | EDIT | Quality gate + evidence in dispatch footer |
| `scripts/cost_tracker.py` | EDIT | Budget threshold per wave/fase/sessie |
| `scripts/validate_feature_plan.py` | EDIT | Wave + size + gate + model validatie |
| `scripts/heartbeat_ack_monitor.py` | EDIT | Deadlock detectie autonome modus |
| `.claude/hooks/t0-readonly-enforcer.sh` | EDIT | Gate commands whitelist voor T0 |
| `.claude/terminals/T0/CLAUDE.md` | EDIT | Wave protocol + evidence enforcement |

---

## VNX Upgrade: New Operational Capabilities

The following capabilities are available after the VNX upgrade (one-command worktree lifecycle + deterministic gates).

### Feature Worktree Lifecycle

The primary worktree flow replaces per-terminal worktrees:

```
vnx new-worktree <feature-name>   # Create isolated feature worktree
  → work in worktree              # All terminals share one worktree
vnx merge-preflight <name>        # GO/NO-GO from runtime state
vnx finish-worktree <name>        # Governance-aware closure
```

**Deprecated**: `vnx start` auto-creating per-terminal worktrees (`-wt-T1/T2/T3`). Legacy mode via `VNX_WORKTREES=true` still works but is not recommended.

### Session Recovery

After crash or unclean shutdown:
```bash
vnx recover              # Standard: clear locks, reset claims, restart processes
vnx recover --aggressive # Force-clean all stale state
vnx recover --dry-run    # Preview without changes
```

### Stale State Cleanup

Remove orphan PIDs and stale locks without affecting live sessions:
```bash
vnx cleanup --dry-run    # Preview
vnx cleanup              # Execute
```

### Settings Management

VNX now patches only its owned keys in `settings.json`:
```bash
vnx regen-settings --merge  # Update VNX keys, preserve project config
vnx regen-settings --full   # Full regeneration (first-time init)
```

### Deterministic Gates

Contract blocks in dispatches enable automated verification:
- **Receipt-time** (lightweight): `verify_claims.py` checks file existence, git changes, patterns
- **Pre-merge** (heavy): `vnx gate-check --pr <PR-ID>` runs pytest, AST, artifacts, CQS

This reduces T0 review scope to semantic review for PRs covered by deterministic gates.

---

## Referenties

### Source Documenten
- `VNX_PID.md` — Project Initiation Document (5 fases, 4 tracks, go/no-go criteria)
- `VNX_DIGITAL_AGENT_TEAM_VISION.md` — Architectuur, agent teams, data schema, fasering
- `VNX_N8N_PROJECTPLAN.md` — 23 workflows, Docker setup, governance

### VNX System
- [AUTONOMOUS_EXECUTION_PLAN.md](../plans/AUTONOMOUS_EXECUTION_PLAN.md) — S1-S5, R1-R5, G1-G5
- [00_VNX_ARCHITECTURE.md](../core/00_VNX_ARCHITECTURE.md) — V11.0 architectuur
- [VNX_AGENT_TEAM_WAVE_MAPPING.md](VNX_AGENT_TEAM_WAVE_MAPPING.md) — Gedetailleerde PR mapping
- [RECEIPT_PIPELINE.md](RECEIPT_PIPELINE.md) — Receipt processing flow
- `terminals/T0/CLAUDE.md` — T0 richtlijnen
- `scripts/vnx_worktree_setup.sh` — Worktree management
- `scripts/vnx_paths.sh` — Pad-conventies
