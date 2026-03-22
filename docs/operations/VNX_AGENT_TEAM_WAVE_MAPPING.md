# VNX Digital Agent Team — Wave Mapping

**Source**: VNX_PID.md, VNX_DIGITAL_AGENT_TEAM_VISION.md, VNX_N8N_PROJECTPLAN.md
**Date**: 2026-03-09
**Terminals**: T0 (Opus orchestrator), T1 (Sonnet), T2 (Sonnet), T3 (Opus)
**Total PRs**: 62 (kleine, gefocuste taken)
**Total Waves**: 22 waves over 5 fases

---

## Design Principes

1. **Kleine taken**: Elke PR is 30-90 min Claude Code werk, max 150 regels changed
2. **Single responsibility**: 1 PR = 1 ding. Liever 3 PRs dan 1 multi-step PR
3. **Strikte quality gates**: Elke PR heeft 2-5 executable gate commands met verwachte output
4. **Evidence-based**: T0 accepteert ALLEEN met bewijs (commit hash, test output, gate pass)
5. **No proof = reject**: Geen bewijs in receipt → direct terug naar terminal
6. **Alleen Sonnet + Opus**: T1/T2 = Sonnet (standaard werk), T3 = Opus (complex/architectuur)

---

## Terminal Toewijzing

| Terminal | Model | Wanneer |
|----------|-------|---------|
| T1 | Sonnet | Config files, SQL, standaard workflows, scripts |
| T2 | Sonnet | Config files, SQL, standaard workflows, scripts |
| T3 | Opus | Complexe logica, architectuur, multi-component integratie |

**Regel**: Als een PR alleen config/SQL/template is → T1 of T2. Als een PR routing-logica, error handling, of cross-component integratie bevat → T3.

---

## Quality Gate Standaard

Elke PR MOET bevatten:

```yaml
Quality Gate:
  commands:
    - cmd: "{exact shell command}"
      expect: "exit 0"
    - cmd: "{exact shell command}"
      expect: "exit 0, output contains '{pattern}'"
  evidence:
    - commit_hash: required
    - branch: "feat/PR-{N}-{slug}"
    - changed_files: [list]
    - gate_results: {command → exit_code + output_summary}
```

T0 enforcement:
1. Receipt zonder evidence → **REJECT, terug naar terminal**
2. Evidence met failing gate → **REJECT, terug naar terminal met failure context**
3. Evidence met alle gates PASS → **APPROVE, merge candidate**

---

## Fase 0: Fundament (Week 1-2)

### Wave 0.1 — Infra Basis (parallel, geen deps)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-1 | Docker Compose: n8n + PostgreSQL + Caddy | T1 | `docker compose config --quiet` (exit 0), `grep 'n8nio/n8n' docker-compose.yml` (match) |
| PR-2 | Caddyfile met SSL + rate limiting | T2 | `caddy validate --config Caddyfile` (exit 0), `grep 'rate_limit' Caddyfile` (match) |
| PR-3 | Environment template (.env.example + .env.production.example) | T3 | `test -f .env.example` (exit 0), `grep 'N8N_ENCRYPTION_KEY' .env.example` (match), geen secrets in file (`grep -c 'sk-\|key_' .env.example` = 0) |

### Wave 0.2 — Database Schema (parallel, geen deps)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-4 | Supabase schema: `intelligence` (analytics_snapshots, search_performance, market_signals, content_ideas, lead_events, own_content_performance) | T1 | `psql $DB -c "\dt intelligence.*"` (6 tabellen), SQL syntax check |
| PR-5 | Supabase schema: `crm` (companies, contacts, deals, activities, conversations) | T2 | `psql $DB -c "\dt crm.*"` (5 tabellen), foreign key constraints aanwezig |
| PR-6 | Supabase schema: `content` + `system` (content_items, agent_tasks, agent_runs, system_health, projects, tasks) | T3 | `psql $DB -c "\dt content.*"` (1 tabel), `psql $DB -c "\dt system.*"` (4 tabellen) |

### Wave 0.3 — Bot + Error Handling (deps: Wave 0.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-7 | Telegram bot setup script (BotFather instructies + webhook config template) | T1 | `test -f scripts/setup_telegram_bot.sh` (exit 0), shellcheck pass |
| PR-8 | n8n Error Workflow JSON (global error → Telegram alert + Supabase log) | T2 | `python3 -c "import json; json.load(open('workflows/error_workflow.json'))"` (exit 0, valid JSON), `jq '.nodes | length' workflows/error_workflow.json` (>= 3 nodes) |
| PR-9 | n8n credential templates (Supabase, Anthropic, Telegram placeholders) | T3 | `ls workflows/credentials/` (3+ files), alle JSON valid |

### Wave 0.4 — Morning Brief (deps: Wave 0.2, 0.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-10 | Morning Brief workflow JSON: cron trigger + 4 Supabase queries | T1 | Valid JSON, `jq '.nodes[] | select(.type=="n8n-nodes-base.scheduleTrigger")' workflows/morning_brief.json` (exists) |
| PR-11 | Morning Brief workflow JSON: Anthropic summary node + Telegram output | T2 | `jq '.nodes[] | select(.type | contains("anthropic"))' workflows/morning_brief.json` (exists), `jq '.nodes[] | select(.type | contains("telegram"))' workflows/morning_brief.json` (exists) |
| PR-12 | Morning Brief: agent_runs logging node + test data seed SQL | T3 | Seed SQL executable (`psql $DB -f seeds/morning_brief_test.sql` exit 0), workflow has logging node |

### Wave 0.5 — Idee Capture (deps: Wave 0.2, 0.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-13 | Idee Capture workflow: webhook trigger + input normalisatie | T1 | Valid workflow JSON, webhook node aanwezig, Set node voor normalisatie |
| PR-14 | Idee Capture workflow: Haiku classificatie + intent routing (Switch node) | T2 | Switch node met 4 routes (nu/prio/idee/onzeker), Anthropic node aanwezig |
| PR-15 | Idee Capture workflow: Supabase insert + Notion create + Telegram confirm | T3 | 3 output nodes (Supabase, Notion placeholder, Telegram), error handler aanwezig |

**Fase 0 Totaal: 15 PRs, 5 waves**

---

## Fase 1: Kernworkflows (Week 3-4)

### Wave 1.1 — Telegram Router (deps: Fase 0 compleet)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-16 | Telegram Router: trigger + Switch node voor 9 commands | T3 | Switch node met 9+ routes, Telegram trigger node, valid JSON |
| PR-17 | Telegram Router: unknown command → Anthropic intent classificatie | T2 | Anthropic node met classificatie prompt, confidence threshold 0.7, fallback response |
| PR-18 | Telegram Router: error response + help command output | T1 | `/help` route produces formatted command list, error handler present |

### Wave 1.2 — Gmail Triage (deps: Wave 1.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-19 | Gmail Triage: trigger + filter (skip newsletters/noreply) | T1 | Gmail trigger node, Filter node met skip patterns, valid JSON |
| PR-20 | Gmail Triage: Haiku classificatie (7 categories + priority) | T2 | Anthropic node met 7 categorieen, priority 1-5, summary_nl output |
| PR-21 | Gmail Triage: Switch routing (urgent → label + Telegram, lead → concept reply + CRM) | T3 | Switch node met 4+ routes, Gmail label actions, Telegram alerts voor P1-P2, Supabase lead_event insert |

### Wave 1.3 — Content Pipeline LinkedIn (deps: Wave 1.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-22 | Content Pipeline: trigger (sub-workflow call + cron) + type classificatie | T1 | Sub-workflow trigger, Anthropic type detection node, valid JSON |
| PR-23 | Content Pipeline: SSH → claude -p met linkedin-writer skill + quality gate | T3 | SSH Execute node met claude -p command, Anthropic quality gate node (score 1-10), retry logic (max 2) |
| PR-24 | Content Pipeline: Telegram approve/reject + Supabase insert + Notion sync | T2 | Wait for Webhook node, IF approved/rejected branches, Supabase content_items insert, agent_runs logging |

### Wave 1.4 — Content Fix Pipeline (deps: Wave 1.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-25 | Content Fix: /fix parser (Haiku) + Supabase content lookup | T2 | Anthropic parse node (target, identifier, problem, fix_type), Supabase lookup node |
| PR-26 | Content Fix: SSH → claude -p fix execution + Sonnet quality gate | T3 | SSH Execute node, quality gate met 4 criteria (probleem opgelost, geen nieuwe fouten, tov intact, SEO intact), score threshold 7 |
| PR-27 | Content Fix: approve → git branch + commit + gh pr create + Telegram | T1 | Wait for Webhook, SSH git commands (checkout -b, add, commit, push, gh pr create), Telegram PR URL output |

### Wave 1.5 — Apple Shortcuts + Calendar Sync (deps: Wave 1.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-28 | Apple Shortcuts config docs (VNX Idee, VNX Nu, VNX Prio) | T1 | 3 markdown docs met webhook URLs, headers, body format. `test -f docs/shortcuts/vnx_idee.md` etc. |
| PR-29 | Content Calendar Sync workflow: cron 06:30 + SSH read kalender + Anthropic parse | T2 | Cron trigger 06:30, SSH cat command, Anthropic parse node (date, time, type, topic, platform) |
| PR-30 | Content Calendar Sync: Supabase upsert + Notion sync | T3 | Supabase upsert node (WHERE planned_date = today), Notion create/update node, valid JSON |

### Wave 1.6 — Kill Switch + Execution Logging (deps: Wave 1.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-31 | Kill Switch workflow: /stop + /start via n8n API | T1 | n8n API GET workflows node, PATCH active:false/true node, Telegram confirmation |
| PR-32 | Execution logging utility workflow (reusable sub-workflow voor agent_runs insert) | T2 | Sub-workflow met Supabase INSERT agent_runs, input: agent, trigger, duration, tokens, status, metadata |

**Fase 1 Totaal: 17 PRs, 6 waves**

---

## Fase 2: Migratie (Week 5-7)

### Wave 2.1 — Website Monitoring (deps: Fase 1 compleet)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-33 | Website Monitoring: cron 5min + HTTP GET 5 endpoints | T1 | Cron trigger, 5 HTTP Request nodes, valid JSON |
| PR-34 | Website Monitoring: IF status != 200 → Telegram alert + Supabase system_health log | T2 | IF node, Telegram alert node, Supabase insert system_health, daily digest sub-workflow call |
| PR-35 | Website Monitoring: daily digest (cron 22:00 → aggregate → Telegram) | T1 | Cron 22:00, Supabase query (today's health records), Anthropic summary, Telegram output |

### Wave 2.2 — Lead Management (deps: Fase 1, B1 CRM data)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-36 | Lead Management: PhantomBuster API → parse → Supabase contacts upsert | T2 | HTTP Request to PhantomBuster, JSON parse node, Supabase upsert contacts |
| PR-37 | Lead Scoring: Supabase trigger (new lead_events) → recalculate function call | T3 | Supabase trigger node, Function node calling recalculate_lead_score(), Supabase update contacts.lead_score |
| PR-38 | Lead Alert: Supabase trigger (lead_score >= 60) → Telegram alert + outreach suggestie | T1 | Supabase trigger, Anthropic outreach suggestion node, Telegram formatted alert |

### Wave 2.3 — Dev Task Workflow (deps: Wave 1.1, SSH setup)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-39 | Dev Task: /bouw parser + project routing table (Switch node) | T2 | Anthropic parse node (project, task, complexity), Switch node met 4 project routes, lookup table |
| PR-40 | Dev Task: SSH → claude -p execution + PR URL extraction | T3 | SSH Execute node met claude -p, output parse for PR URL, timeout handling (30 min) |
| PR-41 | Dev Task: Telegram confirmation flow (/go, /cancel) + agent_runs logging | T1 | Wait for Webhook, IF go/cancel, Supabase agent_runs insert, error handler |

### Wave 2.4 — Blog Pipeline (deps: Wave 1.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-42 | Blog Pipeline: trigger + SSH → claude -p met blog-writer skill | T3 | SSH Execute node met blog-writer skill, timeout 45 min, output capture |
| PR-43 | Blog Pipeline: Anthropic quality gate + retry logic (max 2) | T2 | Quality gate node (SEO, lengte, structuur, feiten), IF score < 7 → retry, retry counter |
| PR-44 | Blog Pipeline: approve → git + gh pr + Telegram + cross-posting trigger | T1 | Wait for Webhook, git branch/commit/push/PR commands, sub-workflow trigger for cross-posting |

### Wave 2.5 — SEOcrawler Integration (deps: Wave 2.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-45 | SEOcrawler Scan: /scan trigger + HTTP request naar SEOcrawler API | T1 | Telegram trigger for /scan, HTTP Request node with URL param, SSE listener setup |
| PR-46 | SEOcrawler Scan: result → Supabase opslaan + PDF generatie trigger | T2 | Supabase insert scan results, SSH claude -p for PDF, result file check |
| PR-47 | SEOcrawler Scan: email rapport via Gmail node + health check workflow | T3 | Gmail send node with attachment, cron health check (5 min), Telegram alert on failure |

### Wave 2.6 — SSH Fallback + Backup (deps: Wave 2.3)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-48 | SSH queue fallback: Supabase task queue + launchd agent template | T3 | Supabase tasks table query, launchd plist template, fallback logic in dev task workflow (IF SSH timeout → queue insert) |
| PR-49 | Backup strategy: pg_dump cron script + Google Drive upload | T1 | Bash script with pg_dump, rclone/gws upload, retention (7 daily + 4 weekly), `shellcheck` pass |
| PR-50 | Workflow versioning: weekly n8n export → git repo script | T2 | Export script (n8n CLI), git add/commit/push, `shellcheck` pass |

**Fase 2 Totaal: 18 PRs, 6 waves**

---

## Fase 3: Intelligence (Week 8-10)

### Wave 3.1 — Evening Digest + Repurposer (deps: Fase 2 compleet)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-51 | Evening Digest: cron 18:00 + data aggregation (done today, missed, options tomorrow) | T2 | Cron 18:00, 4 Supabase queries (completed tasks, missed items, tomorrow's plan, metrics) |
| PR-52 | Evening Digest: Anthropic summary + Telegram keuzemenu | T1 | Anthropic node, Telegram output met inline keyboard buttons, agent_runs logging |
| PR-53 | Content Repurposer: trigger (post-blog-merge) + SSH claude -p repurpose skill | T3 | Sub-workflow trigger, SSH Execute met repurpose prompt, output: LinkedIn + Dev.to + Twitter variants |

### Wave 3.2 — Cross-posting + Notion Sync (deps: Wave 3.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-54 | Cross-posting: Dev.to API node + content_items tracking | T1 | HTTP Request to Dev.to API, Supabase insert content_items (parent_id link), error handler |
| PR-55 | Notion bidirectioneel sync: n8n → Notion (content tracker, projects) | T2 | Notion API create/update nodes, Supabase query nodes, mapping logic |
| PR-56 | Notion sync: Notion → Supabase (status updates, manual edits) | T3 | Notion trigger (webhook of polling), Supabase upsert, conflict resolution logic |

### Wave 3.3 — LinkedIn Scan + Triage (deps: Wave 3.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-57 | LinkedIn Scan: cron 12:00 + PhantomBuster watchlist + engagement kansen | T2 | Cron trigger, PhantomBuster API call, Anthropic analyse node (engagement opportunities) |
| PR-58 | LinkedIn Scan: lead alerts + Supabase lead_events insert | T1 | Supabase insert lead_events, Telegram formatted alerts, agent_runs logging |
| PR-59 | Wekelijkse Triage: maandag 07:00 + idee categorisatie + prioritering | T3 | Cron maandag 07:00, Supabase query ideas (status=inbox), Anthropic categorisatie, Telegram rapport met keuzes |

### Wave 3.4 — Ollama Setup (deps: geen, kan parallel)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-60 | Ollama setup script + model pull (Kimi 2.5, Llama) | T1 | `test -f scripts/setup_ollama.sh`, shellcheck pass, model list command |
| PR-61 | n8n HTTP Request template voor Ollama calls + classificatie prompt | T2 | HTTP Request node template (localhost:11434), test prompt voor email classificatie |

**Fase 3 Totaal: 11 PRs, 4 waves**

---

## Fase 4: Geavanceerd (Week 11-12)

### Wave 4.1 — Lead Scoring + Meeting Prep (deps: Fase 3 compleet)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-62 | Lead Scoring Automation: composite score berekening + auto-update triggers | T3 | Supabase function recalculate_lead_score(), trigger on lead_events INSERT, score range 0-100 |
| PR-63 | Meeting Prep Agent: trigger (calendar event T-1h) + briefing generation | T2 | Cron of calendar trigger, Supabase query (contact + company + activities + conversations), Anthropic briefing |
| PR-64 | Meeting Prep: Telegram briefing output + email history lookup | T1 | Gmail search node (contact email), Telegram formatted briefing, agent_runs logging |

### Wave 4.2 — Performance + Succesformule (deps: Wave 4.1)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-65 | Content Performance Tracking: weekly cron + GA4/GSC data aggregation | T1 | Cron weekly, Google Analytics node, Supabase upsert own_content_performance |
| PR-66 | Succesformule Engine: Apify competitor scrape + Anthropic analyse | T3 | HTTP Request Apify, Anthropic analysis node (what works in niche), Supabase market_signals insert |
| PR-67 | Performance Dashboard: Notion page update met KPIs | T2 | Notion update nodes, Supabase aggregate queries, formatted output |

### Wave 4.3 — Compliance + Docs (deps: Wave 4.2)

| PR | Titel | T | Gate Commands |
|----|-------|---|---------------|
| PR-68 | AVG Compliance: auto-cleanup workflow (contacts > 2yr inactive) | T3 | Supabase DELETE query met datum filter, Telegram confirmation before delete, audit log |
| PR-69 | Runbook: per-workflow documentation (23 workflow summaries) | T1 | `test -f docs/runbook.md`, alle 23 workflows beschreven, rollback instructies per workflow |
| PR-70 | Model Router config: task → model mapping tabel + n8n Switch logic | T2 | Switch node met 5 model routes (Opus/Sonnet/GPT-4o/Kimi/Ollama), fallback logic, valid JSON |

**Fase 4 Totaal: 9 PRs, 3 waves**

---

## Totaaloverzicht

| Fase | Waves | PRs | Weken | Focus |
|------|-------|-----|-------|-------|
| 0 | 5 | 15 | 1-2 | Infra, schema, bot, morning brief, idee capture |
| 1 | 6 | 17 | 3-4 | Router, Gmail, content pipeline, fix pipeline, calendar |
| 2 | 6 | 18 | 5-7 | Monitoring, leads, dev task, blog, SEOcrawler, backup |
| 3 | 4 | 11 | 8-10 | Digest, repurposer, Notion sync, LinkedIn scan, Ollama |
| 4 | 3 | 9 | 11-12 | Lead scoring, meeting prep, performance, compliance |
| **Totaal** | **24** | **70** | **12** | **23 workflows + infra + docs** |

### Dependency Chain (fase-niveau)

```
Fase 0 (fundament)
  └→ Fase 1 (kernworkflows)
       └→ Fase 2 (migratie + integratie)
            └→ Fase 3 (intelligence + Ollama)
                 └→ Fase 4 (geavanceerd + compliance)
```

### Wave Parallellisatie per Fase

```
Fase 0:  [W0.1] ─→ [W0.2] ─→ [W0.3] ─→ [W0.4 | W0.5]
Fase 1:  [W1.1] ─→ [W1.2 | W1.3 | W1.6] ─→ [W1.4 | W1.5]
Fase 2:  [W2.1 | W2.2 | W2.3] ─→ [W2.4 | W2.5] ─→ [W2.6]
Fase 3:  [W3.1 | W3.4] ─→ [W3.2 | W3.3]
Fase 4:  [W4.1] ─→ [W4.2] ─→ [W4.3]
```

### Vincent's Handmatige Taken (NIET in waves)

Deze taken vereisen menselijke actie en worden buiten het autonome systeem afgehandeld:

| Taak | Wanneer | Blokkeert |
|------|---------|-----------|
| BotFather: Telegram bot aanmaken + token | Voor Wave 0.3 | PR-7 |
| DNS: auto.vincentvandeth.nl CNAME instellen | Voor Wave 0.1 deploy | Deployment |
| Google OAuth: consent screen + scopes | Voor Wave 1.2 | PR-19 |
| 1Password CLI setup + secrets migratie | Voor Wave 0.3 | PR-9 |
| Notion workspace + databases aanmaken | Voor Wave 0.5 | PR-15 |
| Apple Shortcuts installeren op iPhone | Na Wave 1.5 | Geen (docs alleen) |
| GCP VM aanmaken + SSH key | Voor Wave 0.1 deploy | Deployment |
| Supabase project selectie + credentials | Voor Wave 0.2 | PR-4/5/6 |
| Tailscale installatie (VPS + MacBook) | Voor Wave 2.3 | PR-40 |

### Infra Credentials (vastgelegd)

| Service | Account / Project | Ref |
|---------|-------------------|-----|
| GCP | `vinixmarketingaigency@gmail.com` | Project: `project-fe083502-8c46-44e4-bbe`, VM: `vnx-n8n`, Zone: `europe-west4-a`, IP: `34.7.174.17` |
| Supabase | linkedin_engine DB | Project-ref: `unzlbspzklhpuxbjoyvw` |

---

## Quality Gate Enforcement Protocol

### In Dispatch (meegestuurd naar terminal)

Elke dispatch bevat een `## Quality Gate` sectie:

```markdown
## Quality Gate

Je werk is PAS klaar als ALLE checks hieronder slagen.
Voer ze uit en rapporteer de resultaten in je receipt.

### Gate Checks
1. `{command}` → verwacht: {expected}
2. `{command}` → verwacht: {expected}
3. `{command}` → verwacht: {expected}

### Evidence (verplicht in receipt)
- Commit hash: {sha}
- Branch: feat/PR-{N}-{slug}
- Changed files: [exact list]
- Gate results: per check → pass/fail + output snippet

BELANGRIJK: Stuur GEEN receipt zonder alle gate checks uitgevoerd.
Als een check faalt: fix het. Als je het niet kunt fixen: meld wat er faalt.
```

### T0 Enforcement (na receipt)

```
Receipt ontvangen
  │
  ├─ Heeft evidence sectie? ──── NEE → REJECT "Geen evidence in receipt"
  │
  ├─ Commit hash klopt? ──────── NEE → REJECT "Commit hash mismatch"
  │
  ├─ T0 runt gate commands ──── FAIL → REJECT "Gate {N} failed: {output}"
  │
  └─ Alle gates PASS ─────────── APPROVE → merge candidate
```

**Max retries**: 2 per PR. Na 2 failures → PR blocked, escaleer naar Vincent.

---

## Token Schatting per Fase (Max Plan)

Alle terminals draaien op Claude Max Plan — geen per-token kosten.
Schattingen zijn voor rate limit planning en context efficiency.

| Fase | PRs | Waves | Geschatte tokens | Toelichting |
|------|-----|-------|-----------------|-------------|
| 0 | 15 | 5 | ~800K | Veel config/SQL, korte contexten |
| 1 | 17 | 6 | ~1.2M | Meer Opus voor routing logica |
| 2 | 18 | 6 | ~1.5M | SSH integratie = grotere contexten |
| 3 | 11 | 4 | ~700K | Standaard workflows |
| 4 | 9 | 3 | ~500K | Config + docs |
| **Totaal** | **70** | **24** | **~4.7M** | |

Per dispatch: ~50-100K tokens (klein en gefocust).
Per wave (3 dispatches): ~150-300K tokens.
Rate limit buffer: 5 min cooldown tussen waves als nodig.

---

## Go/No-Go Criteria per Fase

### Fase 0 → Fase 1

- [ ] Docker Compose valid en deployable
- [ ] Alle 4 Supabase schemas met tabellen aangemaakt
- [ ] Morning Brief workflow importeerbaar in n8n
- [ ] Idee Capture workflow importeerbaar in n8n
- [ ] Error workflow configured
- [ ] Alle credential templates aanwezig

### Fase 1 → Fase 2

- [ ] Telegram Router routeert 9 commands correct
- [ ] Gmail Triage classificeert naar 7 categorieen
- [ ] Content Pipeline produceert LinkedIn draft met quality gate
- [ ] Content Fix Pipeline produceert PR via git
- [ ] Calendar Sync parseert contentkalender
- [ ] Kill Switch en execution logging werken

### Fase 2 → Fase 3

- [ ] 5 endpoints gemonitord met alerting
- [ ] Lead scoring berekent composite score
- [ ] /bouw produceert PR via SSH → claude -p
- [ ] Blog pipeline met quality gate en approval flow
- [ ] SEOcrawler scan triggerable via Telegram
- [ ] Backup strategie operationeel

### Fase 3 → Fase 4

- [ ] Evening digest draait om 18:00
- [ ] Content repurposing werkt (blog → LinkedIn + Dev.to)
- [ ] Notion sync bidirectioneel
- [ ] LinkedIn watchlist scan levert lead alerts
- [ ] Ollama classificatie operationeel (of skippable)

### Fase 4 Eindcriteria

- [ ] 23 workflows als JSON beschikbaar
- [ ] Lead scoring automation met triggers
- [ ] Performance tracking met KPIs
- [ ] AVG compliance workflow
- [ ] Volledige runbook documentatie
- [ ] Model router configured
