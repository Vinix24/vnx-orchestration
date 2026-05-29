# Skill Frontmatter Audit — 2026-05-29

**Dispatch:** 20260529-cc-hardening-batch-1 (A-4)
**Scope:** all 22 skills in `.claude/skills/`

## Summary

All 22 skills now have `paths:` and `name:` + `description:` fields.
Three critical skills received `disable-model-invocation: true` to prevent worker self-promotion.

---

## Before/After Table

| Skill | paths: before | paths: after | disable-model-invocation added |
|---|---|---|---|
| api-developer | missing | `["scripts/**", "tests/**"]` | no |
| architect | missing | `["claudedocs/**"]` | **yes** |
| backend-developer | missing | `["scripts/**", "tests/**"]` | no |
| control-centre | missing | `["claudedocs/**"]` | **yes** |
| data-analyst | missing | `["claudedocs/**"]` | no |
| database-engineer | missing | `["schemas/**", "scripts/**", "tests/**"]` | no |
| debugger | missing | `["claudedocs/**", "scripts/**"]` | no |
| excel-reporter | missing | `["claudedocs/**"]` | no |
| featureplan-kickoff | missing | `["FEATURE_PLAN.md", ".vnx-data/**"]` | no |
| frontend-developer | missing | `["dashboard/**", "tests/**"]` | no |
| intelligence-engineer | missing | `["schemas/**", "scripts/**", "tests/**"]` | no |
| monitoring-specialist | missing | `["claudedocs/**", "scripts/**"]` | no |
| performance-profiler | missing | `["claudedocs/**"]` | no |
| planner | missing | `["FEATURE_PLAN.md", "claudedocs/**"]` | no |
| python-optimizer | missing | `["scripts/**", "tests/**"]` | no |
| quality-engineer | missing | `["tests/**", "claudedocs/**"]` | no |
| reviewer | missing | `["claudedocs/**"]` | no |
| security-engineer | missing | `["claudedocs/**"]` | no |
| supabase-expert | missing | `["schemas/**", "scripts/**"]` | no |
| t0-orchestrator | missing | `[".vnx-data/**", "claudedocs/**"]` | **yes** |
| test-engineer | missing | `["tests/**"]` | no |
| vnx-manager | missing | `["scripts/**", ".vnx/**", "claudedocs/**"]` | no |

---

## Rationale for disable-model-invocation

| Skill | Reason |
|---|---|
| `t0-orchestrator` | CRITICAL — workers must not self-escalate to T0 role. Worker calling T0 skill defeats the human-gate architecture. |
| `architect` | Only T0 dispatches architecture reviews. A worker invoking architect skill creates unauthorized planning scope. |
| `control-centre` | Operator-only. A T1/T2/T3 worker invoking control-centre would attempt multi-project supervision it has no context for. |

---

## Paths rationale

Paths are scoped to the write surfaces each skill actually needs:

- **Analysis/review skills** (`reviewer`, `architect`, `data-analyst`, `security-engineer`, `performance-profiler`, `excel-reporter`, `quality-engineer`) → `claudedocs/**` only. They produce reports, not code.
- **Implementation skills** (`backend-developer`, `api-developer`, `frontend-developer`) → `scripts/**` + `tests/**`. Frontend also writes to `dashboard/**`.
- **Database/schema skills** (`database-engineer`, `intelligence-engineer`, `supabase-expert`) → `schemas/**` + `scripts/**` + `tests/**`.
- **Infrastructure skills** (`vnx-manager`, `monitoring-specialist`, `debugger`) → `scripts/**` + `.vnx/**` + `claudedocs/**` where appropriate.
- **Orchestration skills** (`t0-orchestrator`, `featureplan-kickoff`) → `.vnx-data/**` (runtime state) + `claudedocs/**`.
- **Planning skills** (`planner`) → `FEATURE_PLAN.md` + `claudedocs/**`.

---

## Validation

```
python3 scripts/validate_skill.py --list → 22/22 [OK]
SKIP_WHEEL=1 bash scripts/local-ci.sh → adr-003-no-sdk [pass], wheel [skip], all gates passed
```
