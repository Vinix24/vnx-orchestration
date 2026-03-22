# Claude Code New Features — Research & Implementation Guide

**Datum**: 2026-02-28
**Auteur**: T-MANAGER (VNX Orchestration Expert)
**Doel**: Analyse van nieuwe Claude Code features en concrete implementatie-instructies voor SEOcrawler V2
**Status**: Research document — geen configuratie-wijzigingen

---

## Inhoudsopgave

1. [Executive Summary](#1-executive-summary)
2. [Huidige Staat (Inventarisatie)](#2-huidige-staat-inventarisatie)
3. [Feature 1: `.claude/rules/` Directory](#3-feature-1-clauderules-directory)
4. [Feature 2: Project CLAUDE.md Slimming](#4-feature-2-project-claudemd-slimming)
5. [Feature 3: Auto Memory (MEMORY.md)](#5-feature-3-auto-memory-memorymd)
6. [Feature 4: `@import` Syntax](#6-feature-4-import-syntax)
7. [Feature 5: Global Rules Directory](#7-feature-5-global-rules-directory)
8. [Feature 6: Agent Teams Evaluatie](#8-feature-6-agent-teams-evaluatie)
9. [Feature 7: Skills @import Upgrade](#9-feature-7-skills-import-upgrade)
10. [Vergelijkingstabel: Huidig vs. Nieuw](#10-vergelijkingstabel-huidig-vs-nieuw)
11. [Prioriteiten & Aanbevelingen](#11-prioriteiten--aanbevelingen)
12. [Implementatie Volgorde](#12-implementatie-volgorde)

---

## 1. Executive Summary

### Probleem

De project CLAUDE.md is **210 regels** en bevat alles: project status, roadmap fasen, monitoring dashboard details, browser pool configuratie, stress test resultaten, SME B2B targets, en test instructies met curl commando's. Alles wordt geladen in elke sessie, ongeacht of die sessie met de API, crawler, storage, of monitoring werkt.

### Oplossing

Claude Code biedt vier features die dit oplossen:

| Feature | Impact | Effort | Prioriteit |
|---------|--------|--------|------------|
| `.claude/rules/` | Hoog — context-specifieke instructies | Laag | **1** |
| CLAUDE.md slimming | Hoog — 210→~70 regels universeel | Laag | **2** |
| Auto Memory (MEMORY.md) | Medium — cross-sessie leren | Laag | **3** |
| `@import` syntax | Medium — documentatie-linking | Laag | **4** |
| Global rules | Laag — al compacte global CLAUDE.md | Laag | **5** |
| Skills @import | Medium — 4 core skills profiteren | Medium | **6** |
| Agent Teams | Niet aanbevolen — experimenteel | N/A | **Niet** |

### Verwacht Resultaat

- **CLAUDE.md**: 210 → ~70 regels (67% reductie)
- **Context-relevantie**: Van "alles altijd" naar "alleen wat relevant is per werkpad"
- **Cross-sessie geheugen**: Terminals leren patronen over sessies heen
- **Documentatie-loading**: Automatisch via @import i.p.v. handmatig openen

---

## 2. Huidige Staat (Inventarisatie)

### 2.1 CLAUDE.md Bestanden

| Bestand | Regels | Locatie | Inhoud |
|---------|--------|---------|--------|
| Global CLAUDE.md | 21 | `~/.claude/CLAUDE.md` | Implementation Standards, Compressed Communication, File Organization |
| Project CLAUDE.md | **210** | `./CLAUDE.md` | **Alles** — status, constraints, roadmap, monitoring, browser pool, testing, SME targets, curl commands |
| T-MANAGER CLAUDE.md | 117 | `.claude/terminals/T-MANAGER/CLAUDE.md` | VNX orchestration expert, two-repo rule, CI/CD |
| T0 CLAUDE.md | 80 | `.claude/terminals/T0/CLAUDE.md` | Master orchestrator, brain-not-hands, dispatch format |
| T1 CLAUDE.md | 113 | `.claude/terminals/T1/CLAUDE.md` | Track A worker, Sonnet model, receipt protocol |
| T2 CLAUDE.md | 142 | `.claude/terminals/T2/CLAUDE.md` | Track B worker, storage/optimization, quality gate |
| T3 CLAUDE.md | 185 | `.claude/terminals/T3/CLAUDE.md` | Track C deep analysis, Opus, MCP-enabled |
| **Totaal** | **868** | | |

**Extra**: 11 sample/template CLAUDE.md bestanden onder skills directories (niet actief geladen).

### 2.2 Ontbrekende Features

| Feature | Status | Locatie |
|---------|--------|---------|
| `.claude/rules/` | **Bestaat niet** | — |
| `~/.claude/rules/` | **Bestaat niet** | — |
| MEMORY.md (alle terminals) | **Geen enkel bestand** | — |
| `@import` in project CLAUDE.md | **Niet gebruikt** | — |
| `@import` in terminal CLAUDE.md | **Deels** — alleen T-MANAGER (3 imports) | T-MANAGER |

### 2.3 @import Gebruik (Huidige Staat)

**Enige gebruik**: T-MANAGER CLAUDE.md bevat 3 imports:
```
@.claude/vnx-system/docs/core/00_VNX_ARCHITECTURE.md
@.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md
@.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md
```

**Alle andere terminals** (T0, T1, T2, T3): Geen @import syntax.

### 2.4 Skills Referentie-Infrastructuur

| Categorie | Aantal | Patroon |
|-----------|--------|---------|
| Skills met `references/` | 58 (40 project + 18 VNX) | |
| Symlinks naar SEOCRAWLER_DOCS | 30+ skills | `references/X.md → ../../../../SEOCRAWLER_DOCS/Y.md` |
| Directe bestanden in references | ~25 skills | Email templates, PostgreSQL guides, etc. |
| VNX _MAPPING.md | 18 skills | VNX-specifieke referentie-mapping |

**Referentie-methode in SKILL.md**: Alle skills gebruiken backtick path-referenties:
```markdown
- `references/implementation.md` — Core implementation architecture
```
Geen enkele skill gebruikt `@import` syntax in SKILL.md.

### 2.5 Settings Configuratie

**Auto Memory**: Niet expliciet uitgeschakeld. Geen `CLAUDE_CODE_DISABLE_AUTO_MEMORY` in env. Auto memory is **impliciet ingeschakeld** maar er zijn geen MEMORY.md bestanden aangemaakt.

**Project settings.json highlights**:
- `PYTHONPATH`, `SEOCRAWLER_VENV`, `VIRTUAL_ENV` geconfigureerd
- `VNX_CONTEXT_ROTATION_ENABLED=1`
- 5 hook types actief (SessionStart, PreToolUse, PostToolUse, Stop, UserPromptSubmit)
- `additionalDirectories`: T0-T3, vnx-system, ./

---

## 3. Feature 1: `.claude/rules/` Directory

### 3.1 Hoe Het Werkt

**Mechanisme**: Plaats `.md` bestanden in `.claude/rules/`. Claude Code laadt deze automatisch als aanvullende instructies met dezelfde prioriteit als CLAUDE.md.

**Twee typen regels**:

1. **Onvoorwaardelijke regels** (geen frontmatter): Worden altijd geladen
2. **Pad-specifieke regels** (met YAML frontmatter): Worden alleen geladen wanneer Claude werkt met bestanden die matchen

**YAML Frontmatter**:
```yaml
---
paths:
  - "src/api/**/*.py"
  - "tests/e2e/**/*.py"
---
```

- `paths` is het enige ondersteunde frontmatter-veld
- Standaard glob-patronen: `**/*.py`, `src/**/*`, `*.md`
- Brace expansion: `src/**/*.{ts,tsx}`, `{src,lib}/**/*.py`

**Scope**:
- **Project rules** (`.claude/rules/`): In version control, gedeeld met team
- **User rules** (`~/.claude/rules/`): Persoonlijk, over alle projecten
- User rules laden eerst, project rules overschrijven

**Beperkingen**:
- `@import` syntax werkt **NIET** in rules bestanden — alleen in CLAUDE.md
- Subdirectories worden recursief gescand
- Symlinks ondersteund (circulaire detectie ingebouwd)

### 3.2 Voorgestelde Rules Structuur

```
.claude/rules/
├── python-backend.md      # Python/FastAPI conventies (onvoorwaardelijk)
├── api-endpoints.md       # API regels (pad: src/api/**)
├── crawler.md             # Crawler constraints (pad: src/services/crawl*, browser*)
├── storage.md             # Storage pipeline (pad: src/services/storage/*, src/storage/**)
├── testing.md             # Test conventies (pad: tests/**)
├── dutch-compliance.md    # KvK/BTW regels (pad: src/services/extractors/dutch*)
└── monitoring.md          # Dashboard regels (pad: src/api/monitoring/*)
```

### 3.3 Concrete Inhoud Per Rule

#### `api-endpoints.md`
```markdown
---
paths:
  - "src/api/**/*.py"
  - "tests/e2e/**/*.py"
---

# API Development Rules

## Endpoints
- SSE Stream: `GET /api/quickscan/stream?url=X`
- Email Capture: `POST /api/quickscan/email-capture?email=X&scan_id=Y`
- Browser Pool Stats: `GET /api/browser/pool-stats`
- Health Check: `GET /health`

## Standards
- Ports: 8077 (development), 8000 (production)
- Response time target: <10s (current: 4-8s)
- SSE streaming: 12 progressive events
- Backpressure: HTTP 429 at concurrency limit (3 concurrent)

## Testing
- Dev server: `uvicorn src.api.main:app --host 0.0.0.0 --port 8077 --reload`
- Prod server: `uvicorn src.api.main:app --host 0.0.0.0 --port 8000`
```

#### `crawler.md`
```markdown
---
paths:
  - "src/services/crawl*"
  - "src/services/browser*"
  - "src/crawler/**/*.py"
---

# Crawler Constraints

## Framework
- Crawl4AI 0.7.4 native features — no custom wrappers unless explicitly instructed
- VNX Hybrid Crawler: 26 extractors active

## Memory Budgets
- Python process: <150MB (current: 130-140MB)
- Chromium per crawl: <680MB (marginal — monitor closely)
- Total system budget: <500MB Python-side

## Browser Pool
- Capacity: 3 concurrent crawls (4GB production server)
- Config: POOL_MIN_SIZE=2, POOL_MAX_SIZE=3, POOL_MAX_CONCURRENT=3
- Zero zombie tolerance after cleanup
- Pool stats: GET /api/browser/pool-stats

## Server Requirement
- Minimum: 4GB RAM
- At 3 concurrent: 2040MB Chromium (51% of 4GB), leaves headroom for Python + OS
```

#### `storage.md`
```markdown
---
paths:
  - "src/services/storage/**/*.py"
  - "src/storage/**/*.py"
  - "src/services/pdf/**/*.py"
---

# Storage Pipeline Rules

## Performance Targets
- Query p95: ≤50ms (current: 21ms)
- RAG pipeline: Chunking + embeddings active

## Infrastructure
- Supabase: Schema compliance required
- Storage query optimization: Maintain p95 target when modifying queries
```

#### `testing.md`
```markdown
---
paths:
  - "tests/**/*.py"
---

# Testing Conventions

## Environment
- Use project .venv for all Python commands
- Run tests from repo root after activating .venv
- Current: 405 tests passing

## Targets
- Success rate: ≥90% for E2E
- Response time: <10s
- Storage query: <50ms p95
- Memory cleanup: zero zombie browsers after tests

## Stress Testing
- Current results: 93.3% success (14/15 requests)
- Concurrent capacity: 3 simultaneous
- Memory under load: ~2040MB peak (3 concurrent)
```

#### `dutch-compliance.md`
```markdown
---
paths:
  - "src/services/extractors/dutch*"
  - "src/services/extractors/kvk*"
  - "src/services/extractors/btw*"
  - "tests/**/test_dutch*"
  - "tests/**/test_kvk*"
---

# Dutch Market Compliance

## Required Validations
- KvK (Kamer van Koophandel) number extraction and validation
- BTW (belastingnummer) extraction and validation
- Dutch decimal format support (comma as decimal separator)

## Context
- Target market: Dutch MKB (SME B2B)
- KvK visibility: Only ~5% of sites display prominently
- Test coverage: 11 primary targets + 20 research pool
```

#### `monitoring.md`
```markdown
---
paths:
  - "src/api/monitoring/**/*.py"
  - "src/api/routes/monitoring*"
  - "templates/monitoring*"
---

# Monitoring Dashboard Rules

## Access
- Dev: http://localhost:8077/monitoring
- Prod: http://localhost:8000/monitoring

## Features (7 cards)
1. Active Scans — concurrent capacity tracking
2. Browser Pool — live instance stats
3. Performance Metrics — Python + Chromium memory
4. Memory Trends — dual-line chart
5. URL Testing — live SSE stream testing
6. SSE Event Stream — 12 filterable event types
7. Error Log — severity-based tracking

## Status
- Production Ready v1.1.0
- 100% test coverage (8/8 validation tests)
```

#### `python-backend.md` (onvoorwaardelijk — geen paths)
```markdown
# Python Backend Conventions

## Framework
- FastAPI with async/await
- Python 3.11+, type hints required

## File Management
- No duplicate files (*_v2, *_fixed, *_new)
- Edit originals; split by responsibility if refactor is large
- Reports → claudedocs/ | Tests → tests/ | Scripts → scripts/

## Reporting Tags
- Use specific compound tags (sse-streaming, browser-pool, kvk-validation)
- Avoid general-only tags
```

### 3.4 Wat Verhuist Uit CLAUDE.md

| CLAUDE.md Sectie | Regels | Verhuist naar |
|------------------|--------|---------------|
| Production Monitoring Dashboard (r48-77) | 30 | `monitoring.md` |
| Phase 5 Testing Requirements (r93-161) | 69 | `testing.md` + `api-endpoints.md` + `crawler.md` |
| Testing Instructions met curl (r163-195) | 33 | `api-endpoints.md` |
| Browser Pool details (r122-140) | 19 | `crawler.md` |
| SME B2B Test Coverage (r142-147) | 6 | `testing.md` |
| File Management + Reporting Tags (r79-91) | 13 | `python-backend.md` |
| **Totaal verhuisd** | **~170** | |

---

## 4. Feature 2: Project CLAUDE.md Slimming

### 4.1 Principe

CLAUDE.md bevat alleen wat **elke sessie** moet weten, ongeacht werkcontext. Details gaan naar rules.

### 4.2 Voorgestelde Nieuwe CLAUDE.md (~70 regels)

```markdown
# SEOcrawler V2

## Project Status (2026-02-28)
- Production ready | Phase 5 E2E Testing IN PROGRESS
- 405 tests passing | Dutch market supported
- Memory: <500MB target achieved (~140MB typical)
- Storage: p95 ≤50ms achieved (21ms)

## Core Constraints
- Crawl4AI 0.7.4 native features
- Memory budget: <500MB Python-side; Chromium <680MB per crawl
- Storage query p95: ≤50ms
- Dutch compliance: KvK/BTW validation, decimal formats
- Server minimum: 4GB RAM

## Active Phase
Phase 5: E2E Testing & Production Validation
- Stress testing: 93.3% success rate, 3 concurrent capacity
- Browser pool: operational (PR-3 complete)
- Pending: 24h continuous stress test, 10+ additional SME B2B sites

## File Management
- No duplicate files (*_v2, *_fixed, *_new)
- Edit originals; split by responsibility
- Reports → claudedocs/ | Tests → tests/ | Scripts → scripts/

## Testing
- Use project .venv for all Python commands
- Run tests from repo root

## Key Docs
@FEATURE_PLAN.md
@SEOCRAWLER_DOCS/roadmap.md
@SEOCRAWLER_DOCS/API_QUICKSCAN.md
@SEOCRAWLER_DOCS/API_REFERENCE.md
@SEOCRAWLER_DOCS/E2E_TESTING_CENTRAL.md
@SEOCRAWLER_DOCS/41_BROWSER_POOL.md
@SEOCRAWLER_DOCS/PREVIEW_SYSTEM_ARCHITECTURE.md
```

### 4.3 Reductie-Analyse

| Aspect | Oud | Nieuw | Verschil |
|--------|-----|-------|----------|
| Totaal regels | 210 | ~65 | **-69%** |
| Roadmap Phases 1-4 (compleet) | 16 regels | 0 | Verwijderd (historisch) |
| Monitoring details | 30 regels | 0 | → `monitoring.md` rule |
| Browser Pool details | 19 regels | 1 regel | → `crawler.md` rule |
| Testing Requirements | 69 regels | 2 regels | → `testing.md` rule |
| Curl commands | 33 regels | 0 | → `api-endpoints.md` rule |
| Key Docs | 14 regels | 7 regels | Compacter + @import |

### 4.4 Wat Bewaard Blijft in CLAUDE.md

Alleen **universele project-identiteit**:
- Project naam en status
- Core constraints (altijd relevant)
- Actieve fase (1 paragraaf)
- File management regels
- Basis test instructie
- Key docs via @import

---

## 5. Feature 3: Auto Memory (MEMORY.md)

### 5.1 Hoe Het Werkt

**Auto Memory** is Claude Code's persistent geheugen dat cross-sessie leert.

**Opslaglocatie**: `~/.claude/projects/<project-hash>/memory/`

De `<project-hash>` is afgeleid van het absolute pad van de working directory. Voor terminals met aparte working directories krijgt elke terminal een eigen memory:

```
~/.claude/projects/
├── -Users-...-SEOcrawler-v2--claude-terminals-T-MANAGER/memory/MEMORY.md
├── -Users-...-SEOcrawler-v2--claude-terminals-T0/memory/MEMORY.md
├── -Users-...-SEOcrawler-v2--claude-terminals-T1/memory/MEMORY.md
├── -Users-...-SEOcrawler-v2--claude-terminals-T2/memory/MEMORY.md
└── -Users-...-SEOcrawler-v2--claude-terminals-T3/memory/MEMORY.md
```

**Gedrag**:
- Eerste 200 regels van `MEMORY.md` worden automatisch geladen in elke sessie
- Regels na 200 worden afgekapt — daarom kort houden
- Topic-bestanden (bijv. `debugging.md`, `patterns.md`) worden NIET automatisch geladen
- Claude leest topic-bestanden on-demand wanneer relevant
- Claude schrijft zelf naar memory bestanden tijdens sessies

**Standaard**: Ingeschakeld. Uitschakelen via `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in env.

### 5.2 Huidige Staat

**Nul MEMORY.md bestanden gevonden** in het hele systeem. De memory directories bestaan:
- T-MANAGER: `~/.claude/projects/-Users-vincentvandeth-Development-SEOcrawler-v2--claude-terminals-T-MANAGER/` (139 bestanden)
- T0 t/m T3: Directories bestaan ook

Maar geen enkele bevat een `memory/MEMORY.md`.

### 5.3 Seed MEMORY.md Per Terminal

Het plan is om per terminal een **seed** MEMORY.md aan te maken die Claude een startpunt geeft. Claude vult dit vervolgens automatisch aan.

#### T-MANAGER MEMORY.md
```markdown
# T-MANAGER Memory

## Rol
VNX Orchestration System Expert — beheer vnx-system infrastructure

## Twee-Repo Regel
- VNX code → commit in .claude/vnx-system/ (vnx-orchestration.git)
- SEOcrawler code → commit in project root (SEOcrawler_v2.git)

## Veelgebruikte Paden
- VNX docs: .claude/vnx-system/docs/
- VNX scripts: .claude/vnx-system/scripts/
- Project status: .claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md
- Skills registry: .claude/vnx-system/skills/skills.yaml
- CI config: .claude/vnx-system/.github/workflows/vnx-ci.yml

## CI/CD Quick Reference
- Profile A: vnx doctor + 3 pytest suites (push to main, all PRs)
- Profile B: PR recommendation engine (after Profile A passes)
- Public CI: Secret scan + vnx doctor
- Forbidden paths: no .claude/vnx-system or /Users/ literals in scripts

## Geleerde Patronen
(wordt automatisch aangevuld door Claude)
```

#### T0 MEMORY.md
```markdown
# T0 Memory

## Rol
VNX Master Orchestrator — brain, not hands. Coördineer T1-T3, dispatch taken.

## Kernregels
- NOOIT zelf code schrijven (Write/Edit/Task denied)
- Altijd state verifiëren voor dispatch
- Wacht op dependencies voor gate approval

## Veelgebruikte Paden
- Dispatch queue: .claude/vnx-system/queue/
- Terminal states: .claude/vnx-system/state/
- Feature plan: FEATURE_PLAN.md

## Geleerde Patronen
(wordt automatisch aangevuld door Claude)
```

#### T1 MEMORY.md
```markdown
# T1 Memory

## Rol
Track A Implementation Worker (Sonnet) — focused execution, markdown reports

## Report Format
- Bestandsnaam: {YYYYMMDD-HHMMSS}-T1-{TYPE}-{topic-slug}.md
- Typen: IMPL, TEST, PERF, REVIEW
- PR-ID verplicht in metadata

## Veelgebruikte Paden
- Source: src/
- Tests: tests/
- Reports: .claude/vnx-system/receipts/

## Geleerde Patronen
(wordt automatisch aangevuld door Claude)
```

#### T2 MEMORY.md
```markdown
# T2 Memory

## Rol
Track B Implementation Worker (Sonnet) — storage, data, optimization

## Specialisaties
- Storage pipeline (Supabase)
- Data processing
- Quality gate verification

## Veelgebruikte Paden
- Storage: src/services/storage/
- Supabase: src/storage/
- Quality workflows: .claude/vnx-system/docs/QUALITY_REVIEWER_WORKFLOW.md

## Geleerde Patronen
(wordt automatisch aangevuld door Claude)
```

#### T3 MEMORY.md
```markdown
# T3 Memory

## Rol
Track C Deep Analysis Worker (Opus) — complex investigations, architecture, MCP-enabled

## MCP Servers
- Context7: documentation lookup
- Playwright: browser automation
- Sequential Thinking: complex reasoning
- Memory budget: 300MB (210MB MCP + 90MB overhead)

## Veelgebruikte Paden
- Architecture docs: SEOCRAWLER_DOCS/10_ARCHITECTURE.md
- Crawler arch: SEOCRAWLER_DOCS/11_CRAWLER_ARCHITECTURE.md
- Preview system: SEOCRAWLER_DOCS/PREVIEW_SYSTEM_ARCHITECTURE.md

## Geleerde Patronen
(wordt automatisch aangevuld door Claude)
```

### 5.4 Topic Bestanden

Naast MEMORY.md kunnen topic-bestanden details bevatten die niet in de 200-regellimiet passen:

```
memory/
├── MEMORY.md           # Index (max 200 regels effectief)
├── debugging.md        # Debugging patronen en oplossingen
├── patterns.md         # Codebase patronen en conventies
└── gotchas.md          # Bekende valkuilen en workarounds
```

**Aanbeveling**: Begin alleen met MEMORY.md. Claude maakt zelf topic-bestanden aan wanneer nodig.

---

## 6. Feature 4: `@import` Syntax

### 6.1 Hoe Het Werkt

**Syntax**: `@pad/naar/bestand` in CLAUDE.md bestanden

```markdown
## Key Docs
@SEOCRAWLER_DOCS/roadmap.md
@FEATURE_PLAN.md
```

**Resolutie**: Relatief t.o.v. het bestand dat de import bevat (niet de working directory).

**Beveiligingsmodel**:
- Eerste keer per project: goedkeuringsdialoog met lijst van bestanden
- Eenmalige beslissing — verschijnt niet opnieuw
- Bij weigering blijven imports uitgeschakeld

**Recursie**: Maximaal 5 niveaus diep. Geïmporteerde bestanden kunnen zelf ook imports bevatten.

**Scope**: Werkt **alleen** in CLAUDE.md bestanden. **NIET** in `.claude/rules/*.md`.

**Code-uitsluiting**: @-referenties binnen code spans (`` `@decorator` ``) en codeblokken worden genegeerd.

### 6.2 Huidig Gebruik

| Bestand | @imports | Status |
|---------|----------|--------|
| T-MANAGER CLAUDE.md | 3 | Actief |
| T0 CLAUDE.md | 0 | Niet gebruikt |
| T1 CLAUDE.md | 0 | Niet gebruikt |
| T2 CLAUDE.md | 0 | Niet gebruikt |
| T3 CLAUDE.md | 0 | Niet gebruikt |
| Project CLAUDE.md | 0 | Niet gebruikt |

### 6.3 Implementatie-Plan

**Project CLAUDE.md** — Key Docs sectie upgraden:
```markdown
## Key Docs
@FEATURE_PLAN.md
@SEOCRAWLER_DOCS/roadmap.md
@SEOCRAWLER_DOCS/API_QUICKSCAN.md
@SEOCRAWLER_DOCS/API_REFERENCE.md
@SEOCRAWLER_DOCS/E2E_TESTING_CENTRAL.md
@SEOCRAWLER_DOCS/41_BROWSER_POOL.md
@SEOCRAWLER_DOCS/PREVIEW_SYSTEM_ARCHITECTURE.md
```

**Terminal CLAUDE.md bestanden** — Geen extra imports nodig. T0-T3 zijn gespecialiseerd; hun relevante docs worden geladen via skills, niet CLAUDE.md.

### 6.4 Overwegingen

**Voordelen**:
- Documentatie altijd actueel (single source of truth)
- Geen copy-paste veroudering
- Key docs worden automatisch context bij sessiestart

**Nadelen**:
- Elke @import vergroot de initiële context
- 7 docs importeren kan 1000+ regels toevoegen aan de startcontext
- Goedkeuringsdialoog bij eerste gebruik per project

**Aanbeveling**: Selectief importeren — alleen docs die ELKE sessie nodig heeft. De roadmap en feature plan zijn universeel; API-specifieke docs zijn dat niet.

**Selectieve import** (aanbevolen):
```markdown
## Key Docs
@FEATURE_PLAN.md
@SEOCRAWLER_DOCS/roadmap.md

## Referenties (handmatig raadplegen)
- SEOCRAWLER_DOCS/API_QUICKSCAN.md
- SEOCRAWLER_DOCS/API_REFERENCE.md
- SEOCRAWLER_DOCS/E2E_TESTING_CENTRAL.md
- SEOCRAWLER_DOCS/41_BROWSER_POOL.md
- SEOCRAWLER_DOCS/PREVIEW_SYSTEM_ARCHITECTURE.md
```

---

## 7. Feature 5: Global Rules Directory

### 7.1 Hoe Het Werkt

`~/.claude/rules/` bevat persoonlijke regels die over **alle projecten** gelden.

- Laadt vóór project rules
- Zelfde formaat als project rules (markdown + optionele YAML frontmatter)
- Niet in version control (persoonlijk)

### 7.2 Huidige Staat

**Bestaat niet**. De huidige `~/.claude/CLAUDE.md` is 21 regels en bevat:
1. Implementation Standards (no TODOs, no mocks, no partial features)
2. Compressed Communication (symbool-shorthand)
3. File Organization (reports/tests/scripts directories)

### 7.3 Migratie-Analyse

De global CLAUDE.md is al compact (21 regels). Opsplitsen naar rules biedt **minimale meerwaarde** — alles past ruim binnen de context.

**Optionele migratie**: De "Compressed Communication" sectie is context-afhankelijk (alleen bij hoge context-druk). Dit zou een voorwaardelijke rule kunnen worden, maar er is geen pad-gebaseerde trigger voor "context pressure".

**Aanbeveling**: **Niet prioriteren**. De global CLAUDE.md is effectief zoals het is. Pas overwegen wanneer er meer persoonlijke regels bijkomen.

### 7.4 Als Toch Gewenst

```
~/.claude/rules/
├── code-style.md          # Implementation Standards (onvoorwaardelijk)
└── communication.md       # Compressed Communication (onvoorwaardelijk)
```

Maar dit verplaatst alleen inhoud — het bespaart geen context.

---

## 8. Feature 6: Agent Teams Evaluatie

### 8.1 Status

**Experimenteel** — disabled by default. Activering:

```json
// settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

### 8.2 Architectuur

| Component | Beschrijving |
|-----------|-------------|
| Team lead | Hoofdsessie die team aanstuurt |
| Teammates | Onafhankelijke Claude-instanties |
| Task list | Gedeelde takenlijst met dependencies |
| Mailbox | Direct messaging tussen agents |
| Display | In-process (default) of split panes (tmux/iTerm2) |

### 8.3 Vergelijking met VNX Orchestration

| Aspect | VNX Orchestration | Agent Teams |
|--------|-------------------|-------------|
| **Coördinatie** | Custom dispatcher + smart tap + receipt processor | Native task list |
| **Communicatie** | Receipt-based async (file-based) | Direct mailbox messaging |
| **State** | File-based persistence (cross-sessie) | In-memory (verloren bij sessie-einde) |
| **Stabiliteit** | Bewezen in productie | Experimenteel — bekende beperkingen |
| **Terminal isolatie** | Volledige onafhankelijkheid per terminal | Gedeeld proces |
| **Model per agent** | T0/T3=Opus, T1/T2=Sonnet (vrij configureerbaar) | Niet gedocumenteerd |
| **MCP per agent** | T3 exclusief MCP (memory-efficiënt) | Niet gedocumenteerd |
| **Hooks** | Per-terminal hooks (SessionStart, PreToolUse, etc.) | Geen hook-ondersteuning |
| **Cost tracking** | Session resolver per terminal | Geen |
| **Quality gates** | Dispatch → Implement → Verify → Approve pipeline | Geen formeel gate systeem |

### 8.4 Bekende Beperkingen Agent Teams

1. **Geen session resumption**: `/resume` en `/rewind` herstellen teammates niet
2. **Task status lag**: Dependent tasks kunnen vast lijken te zitten
3. **Langzame shutdown**: Bekend probleem
4. **Één team per sessie**: Geen geneste teams
5. **Fixed leader**: Leiderschap niet overdraagbaar
6. **Geen persistentie**: State verloren bij sessie-einde

### 8.5 Conclusie

**Niet activeren**. Redenen:

1. **VNX is volwassener**: File-based persistence, cross-sessie state, formele quality gates
2. **Agent Teams mist kritieke features**: Geen hooks, geen MCP-per-agent, geen cost tracking
3. **Experimenteel**: Bekende bugs rond session management en shutdown
4. **Risico**: Activering kan conflicteren met VNX hooks en terminal-isolatie

**Herevalueren**: Wanneer Agent Teams uit experimental komt EN persistentie ondersteunt. Kijk specifiek naar:
- Cross-sessie state support
- Per-agent model configuratie
- Hook integratie
- MCP per teammate

---

## 9. Feature 7: Skills @import Upgrade

### 9.1 Huidig Patroon

Skills refereren documentatie via backtick pad-notatie in hun SKILL.md:

```markdown
## Project References
Consult these project-specific references for domain context:
- `references/implementation.md` — Core implementation architecture
- `references/services.md` — Service layer architecture
```

**Probleem**: Deze worden NIET automatisch geladen. Claude moet ze handmatig openen met de Read tool. Dit kost een tool-call per referentie en vergroot de latency.

### 9.2 @import in SKILL.md

**Vraag**: Werkt @import in SKILL.md bestanden?

**Antwoord**: @import is gedocumenteerd voor CLAUDE.md bestanden. Skills worden geladen als system prompts wanneer geactiveerd. Of @import syntax werkt in SKILL.md is **niet bevestigd** in de documentatie. Dit moet **getest worden** voordat een brede uitrol plaatsvindt.

### 9.3 High-Impact Skills voor Upgrade

De volgende 4 core development skills zouden het meest profiteren:

#### 1. `backend-developer`
**Referenties**: 4 symlinks
- `references/implementation.md` → SEOCRAWLER_DOCS/20_IMPLEMENTATION.md
- `references/services.md` → SEOCRAWLER_DOCS/25_SERVICES.md
- `references/extractors.md` → SEOCRAWLER_DOCS/30_EXTRACTORS.md
- `references/plugins.md` → SEOCRAWLER_DOCS/35_PLUGINS.md

**Impact**: Hoog — implementation.md is altijd relevant voor backend werk.

**Upgrade**:
```markdown
## Project References
@references/implementation.md
@references/services.md

Consult when needed:
- `references/extractors.md` — 26 data extractors documentation
- `references/plugins.md` — Plugin system design and usage
```

**Rationale**: implementation.md en services.md altijd laden; extractors en plugins alleen on-demand.

#### 2. `api-developer`
**Referenties**: 4 symlinks
- `references/api-quickscan.md` → SEOCRAWLER_DOCS/21_API_QUICKSCAN.md
- `references/api-reference.md` → SEOCRAWLER_DOCS/22_API_REFERENCE.md
- `references/sse-contract.md` → SEOCRAWLER_DOCS/SSE_EVENT_CONTRACT_V2.md
- `references/email-api.md` → SEOCRAWLER_DOCS/23_EMAIL_SERVICE_API.md

**Impact**: Hoog — SSE contract is kritiek voor API werk.

**Upgrade**:
```markdown
## Project References
@references/api-quickscan.md
@references/sse-contract.md

Consult when needed:
- `references/api-reference.md` — Complete REST API specification
- `references/email-api.md` — Email service API documentation
```

#### 3. `debugger`
**Referenties**: 2 symlinks
- `references/operations.md` → SEOCRAWLER_DOCS/60_OPERATIONS.md
- `references/gotchas.md` → SEOCRAWLER_DOCS/99_LINE_RANGE_TRAP.md

**Impact**: Medium — gotchas.md voorkomt herhaalde fouten.

**Upgrade**:
```markdown
## Project References
@references/gotchas.md

Consult when needed:
- `references/operations.md` — Troubleshooting and operational guide
```

#### 4. `test-engineer`
**Referenties**: 3 symlinks
- `references/testing-setup.md` → SEOCRAWLER_DOCS/65_TESTING_DESIGN_SETUP.md
- `references/testing-strategy.md` → SEOCRAWLER_DOCS/66_TESTING_STRATEGY_PROMPT.md
- `references/coverage.md` → SEOCRAWLER_DOCS/64_TEST_COVERAGE_REPORT.md

**Impact**: Medium — testing strategy als standaard context.

**Upgrade**:
```markdown
## Project References
@references/testing-strategy.md

Consult when needed:
- `references/testing-setup.md` — Test infrastructure and design
- `references/coverage.md` — Test coverage metrics and report
```

### 9.4 Trade-offs

| Pro | Con |
|-----|-----|
| Elimineer tool-calls voor referenties | Vergroot SKILL.md context footprint |
| Altijd actuele docs (symlinks) | Elke @import laadt volledig document |
| Snellere skill-activatie | @import in SKILL.md onbevestigd |
| Consistent met CLAUDE.md patroon | Sommige docs zijn 200+ regels |

### 9.5 Aanbeveling

1. **Test eerst**: Verifieer dat @import werkt in SKILL.md bestanden
2. **Selectief**: Alleen docs die de skill ALTIJD nodig heeft als @import
3. **Hybride aanpak**: 1-2 kritieke docs als @import, rest als backtick-referentie
4. **Begin met 1 skill**: Test met `api-developer` (SSE contract is compact en altijd relevant)

---

## 10. Vergelijkingstabel: Huidig vs. Nieuw

### Context-Loading Vergelijking

| Scenario | Huidig (210-regel CLAUDE.md) | Met Rules + Slim CLAUDE.md |
|----------|------------------------------|---------------------------|
| **API werk** | 210 regels geladen (monitoring, crawler, SME targets mee) | ~65 CLAUDE.md + ~30 api-endpoints.md = **~95 regels** |
| **Crawler werk** | 210 regels geladen | ~65 CLAUDE.md + ~25 crawler.md = **~90 regels** |
| **Storage werk** | 210 regels geladen | ~65 CLAUDE.md + ~15 storage.md = **~80 regels** |
| **Test schrijven** | 210 regels geladen | ~65 CLAUDE.md + ~25 testing.md = **~90 regels** |
| **Monitoring** | 210 regels geladen | ~65 CLAUDE.md + ~25 monitoring.md = **~90 regels** |
| **Algemeen** | 210 regels geladen | ~65 CLAUDE.md + ~15 python-backend.md = **~80 regels** |

**Gemiddelde reductie**: 210 → ~87 regels (**59% minder** irrelevante context per sessie).

### Feature Adoption Matrix

| Feature | Huidige staat | Na implementatie |
|---------|--------------|-----------------|
| Project CLAUDE.md | 210 regels, monolithisch | ~65 regels, universeel |
| Rules directory | Bestaat niet | 7 pad-specifieke rule bestanden |
| Auto Memory | Niet geïnitialiseerd | 5 seed MEMORY.md bestanden |
| @import | Alleen T-MANAGER (3) | Project CLAUDE.md + T-MANAGER |
| Global rules | N/A | Niet nodig (CLAUDE.md al compact) |
| Skills @import | 0 skills | 4 core skills (na test) |
| Agent Teams | N/A | Niet geactiveerd |

---

## 11. Prioriteiten & Aanbevelingen

### Prioriteit 1: Rules Directory + CLAUDE.md Slimming (SAMEN)

**Waarom samen**: Rules zonder slimming is zinloos (dubbele informatie). Slimming zonder rules verliest informatie.

**Impact**: Hoogste — lost het kernprobleem op (alles-altijd-geladen)
**Effort**: ~30 minuten
**Risico**: Laag — bestaande CLAUDE.md content wordt verplaatst, niet verwijderd

### Prioriteit 2: Auto Memory Seeds

**Waarom**: Kost 5 minuten, terminals beginnen direct te leren
**Impact**: Medium — waarde groeit over tijd
**Effort**: ~10 minuten
**Risico**: Nul — additief, breekt niets

### Prioriteit 3: @import in Project CLAUDE.md

**Waarom**: Key docs als automatische context elimineert handmatig openen
**Impact**: Medium — afhankelijk van hoeveel docs geïmporteerd worden
**Effort**: ~5 minuten
**Risico**: Laag — goedkeuringsdialoog bij eerste gebruik

### Prioriteit 4: Skills @import (experimenteel)

**Waarom**: Vereist eerst test of @import werkt in SKILL.md
**Impact**: Medium — maar alleen als @import werkt in SKILL.md
**Effort**: ~15 minuten (test + 4 skills updaten)
**Risico**: Medium — kan onverwacht gedrag geven als SKILL.md @import niet ondersteunt

### Niet Prioriteren

- **Global rules**: CLAUDE.md is al 21 regels
- **Agent Teams**: Experimenteel, VNX is superieur voor onze use case

---

## 12. Implementatie Volgorde

### Fase 1: Foundation (samen uitvoeren)
1. `.claude/rules/` directory aanmaken met 7 rule bestanden
2. CLAUDE.md slanken naar ~65 regels
3. Valideren: Start sessie in API-context → controleer dat api-endpoints.md geladen wordt

### Fase 2: Memory
4. Seed MEMORY.md aanmaken voor T-MANAGER, T0, T1, T2, T3
5. Valideren: Start sessie → controleer dat MEMORY.md in context verschijnt

### Fase 3: Imports
6. @import toevoegen aan geslankte CLAUDE.md (selectief: FEATURE_PLAN + roadmap)
7. Goedkeuringsdialoog accepteren bij eerste gebruik
8. Valideren: Start sessie → controleer dat geïmporteerde docs in context zitten

### Fase 4: Skills (experimenteel)
9. Test: Voeg @import toe aan `api-developer` SKILL.md
10. Activeer skill → controleer of referentie automatisch geladen wordt
11. Bij succes: Upgrade `backend-developer`, `debugger`, `test-engineer`
12. Bij falen: Documenteer en wacht op Claude Code update

### Post-Implementatie
- Monitor context-gebruik over 5 sessies
- Laat Claude memory bestanden aanvullen
- Evalueer of selectieve @imports voldoende docs laden

---

*Einde research document. Geen configuratie-wijzigingen zijn uitgevoerd.*
