# VNX Intelligence Motor — Analyse & Upgrade Rapport

**Datum**: 2026-02-28
**Auteur**: T-MANAGER
**Status**: Analyse compleet, aanbevelingen klaar voor review

---

## 1. Huidige Architectuur

### Data Flow Diagram

```
                    ┌─────────────────────────┐
                    │  code_quality_scanner.py │
                    │  code_snippet_extractor  │
                    └──────────┬──────────────┘
                               │ Dagelijks 18:00
                               ▼
                    ┌─────────────────────────┐
                    │  quality_intelligence.db │
                    │  SQLite FTS5             │
                    │  - code_snippets         │
                    │  - tag_combinations      │
                    │  - prevention_rules      │
                    │  - pattern_usage         │
                    └──────────┬──────────────┘
                               │
                    ┌──────────▼──────────────┐
                    │  gather_intelligence.py  │
                    │  (1.314 LOC)             │
                    │  - Agent validation      │
                    │  - FTS5 keyword search   │
                    │  - Tag extraction        │
                    │  - Relevance scoring     │
                    └──────────┬──────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
    │ cached_      │  │ intelligence │  │ tag_intelligence  │
    │ intelligence │  │ _queries.py  │  │ .py (570 LOC)    │
    │ (569 LOC)    │  │ (560 LOC)    │  │ - Normalisatie   │
    │ - TTLCache   │  │ - CLI API    │  │ - Prevention     │
    │ - Rankings   │  │ - 10 queries │  │   rules          │
    └──────┬───────┘  └──────────────┘  └──────────────────┘
           │
           ▼
    ┌──────────────────────────────────────────┐
    │  userpromptsubmit_intelligence_inject.sh │
    │  (218 LOC, V5)                           │
    │  - SHA256 change detection               │
    │  - JSON additionalContext injection      │
    │  - Brief + tags + quality + recs check   │
    └──────────────────┬───────────────────────┘
                       │
                       ▼
    ┌──────────────────────────────────────────┐
    │  Dispatch Markdown met [INTELLIGENCE_DATA]│
    │  - pattern_count: 5                       │
    │  - prevention_rules: N                    │
    │  - quality_context: {...}                 │
    │  - suggested_patterns: [...]              │
    └──────────────────┬───────────────────────┘
                       │
                       ▼
    ┌──────────────────────────────────────────┐
    │  Terminal (T1/T2/T3) voert taak uit       │
    │  ↓                                        │
    │  learning_loop.py (600 LOC)               │
    │  - Extract used/ignored patterns          │
    │  - Update confidence scores               │
    │  - Archive stale patterns                 │
    │  ↓                                        │
    │  intelligence_daemon.py (819 LOC)         │
    │  - Uurlijkse extractie                    │
    │  - Dagelijkse hygiene (18:00)             │
    │  - Health monitoring                      │
    └──────────────────────────────────────────┘
```

### Componenten Inventaris

| Component | LOC | Functie |
|-----------|-----|---------|
| `gather_intelligence.py` | 1.314 | Core gathering engine, FTS5 queries, agent validation |
| `intelligence_daemon.py` | 819 | Continuous extraction, hourly/daily cycles |
| `generate_t0_recommendations.py` | 821 | Dispatch recommendations vanuit receipts |
| `quality_advisory.py` | 612 | Code quality checks op changed files |
| `learning_loop.py` | 600 | Confidence adjustment, pattern archival |
| `tag_intelligence.py` | 570 | Tag normalisatie, prevention rules |
| `cached_intelligence.py` | 569 | Multi-layer TTL cache, ranking |
| `intelligence_queries.py` | 560 | CLI query API (10 commands) |
| `userpromptsubmit_inject_v5.sh` | 218 | Hook injection met SHA256 change detection |
| `sessionstart_t0_intelligence.sh` | 64 | Session start context injection |
| `conversation_analyzer_stub.py` | 50 | **STUB** — niet geimplementeerd |
| **Totaal** | **6.197** | |

### Opslag

| Bron | Type | Status |
|------|------|--------|
| `quality_intelligence.db` | SQLite FTS5 | **0 bytes — LEEG** |
| `t0_intelligence.ndjson` | Rolling NDJSON | **0 regels** |
| `t0_receipts.ndjson` | Receipt log | 50 entries |
| `t0_recommendations.json` | JSON | **Niet aanwezig** |
| `dispatches/completed/*.md` | Dispatch bestanden | 156 bestanden |

---

## 2. Analyse: Intelligence bij Recente Dispatches

### Kernmetrieken

| Metriek | Waarde |
|---------|--------|
| Totaal completed dispatches | 156 |
| Dispatches met `[INTELLIGENCE_DATA]` | 64 (41%) |
| Dispatches zonder intelligence | 92 (59%) |
| Dispatch periode | 2026-01-28 t/m vandaag |
| Pattern count per dispatch | Constant 5 |
| Quality scores | 85.0 (92%) en 95.0 (8%) |
| Usage count | **0 overal** |
| Relevance scores range | 0.43 — 1.01 |

### Observaties

**1. Intelligence dekking: 41%, niet 47%**
Van de 156 completed dispatches bevatten er slechts 64 een `[INTELLIGENCE_DATA]` blok. 92 dispatches (59%) kregen geen intelligence mee. Dit zijn waarschijnlijk de dispatches van voor het intelligence systeem online kwam (pre 2026-01-28), plus dispatches waar de database niet bereikbaar was.

**2. Quality scores zijn bijna uniform**
Vrijwel alle snippets krijgen quality_score 85.0. Slechts enkele krijgen 95.0. Dit wijst op een threshold-effect: de quality scanner kent een smal scorebereik toe, waardoor differentiatie afwezig is.

**3. Usage count = 0 overal**
Geen enkel pattern is ooit als "gebruikt" geregistreerd. De `pattern_usage` tabel bestaat als schema in `learning_loop.py` maar de database zelf is **0 bytes** — tabellen zijn nooit aangemaakt. De feedback loop is volledig broken.

**4. Relevance scores zijn FTS5 rank-gebaseerd**
De relevance_score (0.43-1.01) komt uit SQLite FTS5 rank, gecombineerd met een `compute_relevance_score()` functie die 4 factoren weegt:
- `quality_score * 0.3` — altijd ~85 → bijdrage ~25.5
- `usage_count * 0.2` — altijd 0 → bijdrage 0
- `confidence * 0.3` — default 1.0 → bijdrage ~0.3
- `recency * 0.2` — meeste data stale → bijdrage ~0

**Effectief is ranking = puur quality_score + FTS5 keyword match.**

**5. Pattern count altijd 5**
Elke dispatch krijgt precies 5 patterns, ongeacht context. Dit is de hardcoded `limit=5` in `query_relevant_patterns()`. Er is geen dynamische selectie gebaseerd op taakcomplexiteit.

### Conclusie

> "We schieten receptkaarten af maar meten niet of de kok ze leest."

Het systeem injecteert intelligence, maar:
- Geen meting of patterns daadwerkelijk gebruikt worden
- Geen feedback loop (DB leeg, pattern_usage nooit gevuld)
- Geen correlatie tussen dispatch outcome en intelligence kwaliteit
- Ranking is effectief random (alle scores gelijk)

---

## 3. Tag Systeem Evaluatie

### Huidige Taxonomie

De tag normalisatie in `tag_intelligence.py:106-152` definieert 5 categorieën:

| Categorie | Tags | Voorbeeld mapping |
|-----------|------|-------------------|
| **Fases** | 4 | `design`, `planning`, `architecture` → `design-phase` |
| **Componenten** | 3 | `crawler`, `storage`, `api` → `*-component` |
| **Issues** | 4 | `validation-error`, `performance-issue`, `memory-problem`, `race-condition` |
| **Prioriteit** | 3 | `critical-blocker`, `high-priority`, `medium-impact` |
| **Acties** | 3 | `needs-refactor`, `needs-validation`, `needs-retry-logic` |
| **Totaal** | **17 gedefinieerde mappings** | Onbekende tags worden as-is bewaard |

### Problemen

**1. Flat structuur — geen relaties**
Tags zijn een platte lijst zonder hierarchie. Er is geen concept van:
- Component → subcomponent relaties (crawler → browser-pool → page-extraction)
- Fase → gate koppeling (implementation-phase → gate_pr4_implementation)
- Terminal → component affiniteit (T1 doet altijd storage, T2 altijd crawler)

**2. Stale data**
De `tag_combinations` tabel in de database is leeg (DB = 0 bytes). De in-code taxonomie is statisch gedefinieerd — er is geen automatische verrijking vanuit dispatches of receipts.

**3. Geen terminal-specifieke regels**
Alle terminals krijgen dezelfde intelligence. T1 (storage specialist) krijgt crawler patterns, T3 (infra) krijgt API patterns. Er is geen filtering op basis van terminal capabilities.

**4. Beperkte dekking**
Met slechts 17 mappings en 5 categorieën dekt het systeem een fractie van de werkelijke taakdiversiteit. Compound tags uit dispatches (bijv. `sse-streaming`, `browser-pool`, `kvk-validation`) worden niet genormaliseerd.

### Aanbeveling

- **Terminal-scoped tags**: Filter patterns per terminal op basis van track/component affiniteit
- **Component-gekoppelde filtering**: Koppel tags aan CLAUDE.md component definities
- **Automatische tag refresh**: Extract tags uit recente dispatches/receipts, dagelijks

---

## 4. Zoekalgoritme Evaluatie

### Huidig Algoritme

```python
# Stap 1: Keyword extractie uit task description
keywords = extract_keywords(task_description)  # simple regex, stopword removal

# Stap 2: FTS5 query
SELECT ... FROM code_snippets
WHERE code_snippets MATCH '"keyword1" OR "keyword2" OR ...'
AND quality_score >= 85
ORDER BY rank, quality_score DESC
LIMIT 15  # (limit * 3)

# Stap 3: Relevance scoring
score = quality * 0.3 + usage * 0.2 + confidence * 0.3 + recency * 0.2

# Stap 4: Return top 5
```

### Ranking Formule Analyse

| Factor | Gewicht | Werkelijke waarde | Bijdrage |
|--------|---------|-------------------|----------|
| `quality_score / 100` | 0.3 | ~0.85 | 0.255 |
| `usage_count / 10` | 0.2 | 0 (altijd) | **0.000** |
| `confidence` | 0.3 | 1.0 (default) | 0.300 |
| `recency` | 0.2 | ~0 (data stale) | **~0.000** |
| **Totaal** | | | **~0.555** |

Door de lege `pattern_usage` tabel en stale data is de effectieve ranking:
**`ranking ≈ 0.255 + 0 + 0.3 + 0 = 0.555`** voor alle patterns.

Differentiatie komt alleen van FTS5 `rank` (BM25 keyword frequency), niet van de composite score.

### Vergelijking met State-of-Art

| Methode | VNX Huidig | Industrie Best Practice |
|---------|------------|------------------------|
| **Zoektype** | FTS5 keyword match | Semantic embedding search |
| **Ranking** | BM25 + flat quality | Citation-based + usage decay |
| **Context** | Geen terminal/gate filtering | Scope-aware retrieval |
| **Feedback** | Geen (usage=0) | Implicit feedback loop |
| **Compressie** | Geen | Observer/Reflector (3-6x) |
| **Kosten** | Alles via Opus | Tiered (deterministic → Haiku → Opus) |

---

## 5. Alternatieven voor Intelligence Bepaling

### Pattern A: Observational Memory (Mastra-stijl)

**Concept**: Observer/Reflector compressie van conversation logs.

```
Raw conversation log (50K tokens)
    → Observer: extract feiten, beslissingen, patterns
    → Reflector: synthetiseer tot bruikbare memory
    → Compressed memory (8-15K tokens, 3-6x reductie)
```

**Toepassing VNX**: Receipts en dispatch resultaten comprimeren tot herbruikbare knowledge units. In plaats van 30K+ ruwe snippets, bewaar ~5K gecomprimeerde patterns met context.

**Voordeel**: Dramatische token reductie, betere signaal/ruis ratio
**Nadeel**: Vereist LLM-pass voor compressie (Haiku: ~$0.25/MTok)
**Effort**: Medium (2-3 dagen implementatie)

### Pattern B: Citation-Based Memory (GitHub Copilot-stijl)

**Concept**: Elke intelligence reference bevat verifieerbare bronverwijzing.

```json
{
  "pattern": "Browser pool cleanup after crawl failure",
  "citation": {
    "file": "src/services/browser_pool.py",
    "lines": "145-167",
    "commit": "38c1db1",
    "verified_at": "2026-02-28"
  }
}
```

**Toepassing VNX**: Patterns koppelen aan exact file:line_range met commit hash. Terminal kan verwijzing verifiëren en stale patterns detecteren.

**Voordeel**: Verifieerbaar, detecteert automatisch stale intelligence
**Nadeel**: Meer storage, needs git integration
**Effort**: Laag (1-2 dagen, bestaande file_path/line_range velden uitbreiden)

### Pattern C: Stable-Prefix Dispatch (Manus-stijl)

**Concept**: Dispatch format optimaliseren voor LLM KV-cache hergebruik.

```
[STABLE PREFIX - zelfde voor alle dispatches]
System prompt + agent template + constraints

[VARIABLE SUFFIX - uniek per dispatch]
Task description + intelligence data + context
```

**Toepassing VNX**: Door het dispatch format te standaardiseren met een vast prefix, kan de LLM provider KV-cache hergebruiken. Bij Anthropic levert prompt caching tot 90% input token korting.

**Voordeel**: Tot 10x cost reductie op input tokens
**Nadeel**: Vereist strikte format discipline
**Effort**: Laag (1 dag, dispatch template herstructureren)

### Pattern D: Directory-Scoped Context (HAM-stijl)

**Concept**: Intelligence filteren op basis van welke directories de taak raakt.

```
Task raakt: src/services/browser_pool.py
    → Filter intelligence: alleen patterns uit src/services/
    → Exclude: patterns uit tests/, scripts/, docs/
    → Resultaat: 50% token reductie
```

**Toepassing VNX**: De bestaande `task_paths` parameter in `gather_intelligence` wordt al doorgegeven maar nauwelijks benut. Door file_path filtering te versterken halveer je irrelevante intelligence.

**Voordeel**: 50%+ token reductie, hogere relevantie
**Nadeel**: Kan cross-cutting patterns missen
**Effort**: Laag (0.5 dag, bestaande logica versterken)

### Pattern E: Deterministic-First, LLM-Second (JetBrains-stijl)

**Concept**: Gebruik deterministische regels als eerste filter, LLM alleen voor edge cases.

```
Stap 1 (deterministisch): file_path match, tag filter, component scope
    → 90% van queries beantwoord zonder LLM
Stap 2 (Haiku): Complexe pattern selectie waar regels niet volstaan
    → 8% van queries
Stap 3 (Opus): Alleen voor architectural decisions
    → 2% van queries
```

**Toepassing VNX**: Het huidige systeem is al grotendeels deterministisch (FTS5 + rules). Door een Haiku-laag toe te voegen voor complexe selectie bespaar je Opus tokens.

**Voordeel**: 52% goedkoper dan full-LLM, voorspelbaar, testbaar
**Nadeel**: Vereist goede deterministische regels als basis
**Effort**: Medium (2 dagen voor Haiku integration)

---

## 6. T0 als Context Curator vs Deterministisch Systeem

### Huidige Situatie

T0 (Opus) wordt momenteel **niet** gebruikt voor intelligence selectie. Het systeem is volledig deterministisch:
1. `userpromptsubmit` hook detecteert SHA256 change
2. `gather_intelligence.py` doet FTS5 query + ranking
3. Resultaat wordt als `[INTELLIGENCE_DATA]` in dispatch geïnjecteerd
4. Geen LLM betrokken bij selectie

### Pro/Con Analyse

| Aspect | Deterministisch (huidig) | T0 (Opus) Curation | Hybrid |
|--------|--------------------------|---------------------|--------|
| **Kosten** | $0 per query | ~$15/MTok | ~$0.25/MTok (Haiku) |
| **Voorspelbaarheid** | Hoog | Laag | Medium |
| **Testbaarheid** | Unit testbaar | Moeilijk | Partially testbaar |
| **Relevantie** | Laag (keyword match) | Hoog (reasoning) | Medium-Hoog |
| **Latency** | <100ms | 5-30s | 1-3s (Haiku) |
| **Token overhead** | 0 | 2-5K per selectie | 0.5-1K per selectie |

### Hybrid Voorstel

```
Laag 1: Deterministisch (alle dispatches)
    - File path matching
    - Component tag filtering
    - Terminal affinity rules
    - Gate-specifieke patronen
    Kost: $0, <100ms

Laag 2: Haiku Curation (complexe taken)
    - Wanneer: >3 candidate patterns, ambigue task description
    - Taak: Selecteer top-3 uit 10+ candidates
    - Context: Task + candidates (500-1K tokens)
    Kost: ~$0.25/MTok input, $1.25/MTok output
    Budget: ~20 calls/dag × 1K tokens = 20K tokens/dag = $0.005/dag

Laag 3: Opus Decision (architectuur/escalatie)
    - Wanneer: Cross-component dependencies, nieuwe patterns nodig
    - Taak: Evalueer of intelligence upgrade nodig is
    - Alleen via T0 in orchestration context
    Kost: Bestaand T0 budget
```

### Token Budget Analyse

| Laag | Calls/dag | Tokens/call | Dagkosten |
|------|-----------|-------------|-----------|
| Deterministisch | ~50 | 0 | $0.00 |
| Haiku curation | ~20 | 1.000 | $0.005 |
| Opus escalatie | ~2 | 2.000 | ~$0.06 |
| **Totaal** | | | **~$0.07/dag** |

### Background Model Optie

Claude Code ondersteunt `claude -p` met model selectie. Een background Haiku process kan context verrijken:

```bash
# Haiku curation call
echo "$CANDIDATES_JSON" | claude -p \
  --model claude-haiku-4-5-20251001 \
  "Select the 3 most relevant patterns for this task: $TASK_DESC"
```

Dit houdt T0 (Opus) vrij voor orchestration decisions terwijl Haiku de intelligence selectie doet.

---

## 7. Conversation Log Hygiene Pipeline

### Huidige Status

`learning/conversation_analyzer_stub.py` is een **stub van 50 regels**:

```python
class ConversationAnalyzer:
    def analyze_period(self, days=3):
        print(f"[STUB] Would analyze {days} days of conversation logs")

    def detect_inefficiency_patterns(self, log_content):
        pass  # Future: LLM-based pattern detection

    def generate_triggers(self):
        pass  # Future: Create deterministic trigger algorithms
```

Er is **niets geïmplementeerd**. Conversation logs worden niet geanalyseerd, patterns niet geëxtraheerd.

### Voorgestelde Pipeline

```
┌─────────────────────────────────────────────────┐
│  Stap 1: SELECT — Welke logs analyseren?        │
│  Criteria:                                       │
│  - Failures (dispatch outcome = error)           │
│  - Long duration (>30 min voor simpele taak)     │
│  - Re-dispatches (zelfde taak herhaald)          │
│  - Low confidence receipts (<0.7)                │
│  Max: 20 logs/dag                                │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  Stap 2: EXTRACT — Wat eruit halen?             │
│  Per geselecteerde log:                          │
│  - Error patterns en root causes                 │
│  - Succesvolle oplossingsstrategieën             │
│  - Herhaalde commando-sequenties                 │
│  - File paths die vaak geraakt worden            │
│  Tool: Haiku met extraction prompt (~5K tokens)  │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  Stap 3: COMPRESS — Comprimeer tot patterns     │
│  - Deduplicatie tegen bestaande patterns         │
│  - Citation toevoegen (dispatch_id, timestamp)   │
│  - Confidence score initieel 0.5                 │
│  - Tags extraheren en normaliseren               │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│  Stap 4: STORE — Opslaan in pattern DB          │
│  - Insert in code_snippets met source="log"      │
│  - Update prevention_rules indien failure pattern │
│  - Log extraction in intelligence_usage.ndjson    │
└─────────────────────────────────────────────────┘
```

### Frequentie en Budget

| Parameter | Waarde |
|-----------|--------|
| Frequentie | Dagelijks 18:00 (samen met learning_loop) |
| Max logs/dag | 20 |
| Tokens per log analyse | ~5K (Haiku) |
| Dagelijks token budget | 100K tokens |
| Dagelijkse kosten | ~$0.025 (Haiku input) + ~$0.125 (output) = **~$0.15/dag** |

---

## 8. Aanbevelingen met Prioriteit

### P0 — Kritiek (deze week) — AFGEROND

#### P0-1: Fix Feedback Loop (pattern_usage tracking) — DONE (2026-03-02, PR #2)

Geïmplementeerd: `offered_pattern_hashes` in dispatch output, fallback partial credit bij success, `ignored_count` DB persistentie, usage tracking via indexed `pattern_hash` kolom in `snippet_metadata` (O(1) lookup, was O(n) full table scan).

#### P0-2: Fix Tag Staleness (daily refresh) — DONE (2026-03-02, PR #2)

Geïmplementeerd: `extract_tags_from_dispatch()` in `tag_intelligence.py`, compound tags in `normalize_tags()`, dagelijkse tag refresh in `intelligence_daemon.py`.

### P1 — Belangrijk (volgende sprint)

#### P1-1: Citation-Based Memory

**Probleem**: Patterns bevatten code snippets zonder verifieerbare bronverwijzing. Stale patterns (file verwijderd/gewijzigd) worden niet gedetecteerd.

**Oplossing**:
1. Voeg `commit_hash` en `verified_at` toe aan code_snippets schema
2. Bij dagelijkse hygiene: verificeer of file_path + line_range nog klopt
3. Markeer stale patterns (file gewijzigd sinds extraction) met lagere confidence
4. Terminal kan citation verifiëren voor gebruik

**Effort**: 1-2 dagen
**Impact**: Hoog — elimineert stale intelligence probleem

#### P1-2: Terminal-Scoped Context Filtering

**Probleem**: Alle terminals krijgen dezelfde intelligence. T1 krijgt crawler patterns terwijl het storage doet.

**Oplossing**:
1. Definieer terminal → component mapping in skills.yaml of separate config
2. Filter patterns op basis van terminal's track/component affiniteit
3. Gate-specifieke pattern selectie (investigation → debugging patterns, implementation → code patterns)

**Effort**: 1 dag
**Impact**: Medium-Hoog — reduceert irrelevante intelligence met ~60%

### P2 — Verbetering (komende 2 sprints)

#### P2-1: Observational Memory voor Receipt Compressie

**Oplossing**: Implementeer Observer/Reflector pattern op receipts en dispatch resultaten. Comprimeer 50K token receipts tot 8-15K bruikbare knowledge units.

**Effort**: 2-3 dagen
**Impact**: Medium — betere kennisretentie, minder token verspilling

#### P2-2: Haiku Curation Layer

**Oplossing**: Voeg Haiku als tussenstap toe voor complexe pattern selectie. Deterministisch voor 90% van queries, Haiku voor ambigue cases.

**Effort**: 2 dagen
**Impact**: Medium — betere relevantie voor complexe taken
**Kosten**: ~$0.07/dag extra

### P3 — Toekomstig (backlog)

#### P3-1: Conversation Log Mining Pipeline

**Oplossing**: Implementeer `conversation_analyzer_stub.py` volledig. Select → Extract → Compress → Store pipeline met Haiku.

**Effort**: 3 dagen
**Impact**: Medium — leert van failures en successen
**Kosten**: ~$0.15/dag

#### P3-2: Stable-Prefix Dispatch Format

**Oplossing**: Herstructureer dispatch format voor KV-cache optimalisatie. Vast prefix met system prompt + agent template, variabel suffix met taak + intelligence.

**Effort**: 1 dag
**Impact**: Medium — tot 90% input token korting via prompt caching

### Samenvattende Roadmap

| Prio | Item | Effort | Impact | Kosten/dag |
|------|------|--------|--------|------------|
| P0 | Fix feedback loop | 1d | Hoog | $0 |
| P0 | Fix tag staleness | 0.5d | Medium | $0 |
| P1 | Citation-based memory | 1-2d | Hoog | $0 |
| P1 | Terminal-scoped filtering | 1d | Medium-Hoog | $0 |
| P2 | Observational memory | 2-3d | Medium | ~$0.025 |
| P2 | Haiku curation layer | 2d | Medium | ~$0.07 |
| P3 | Conversation log mining | 3d | Medium | ~$0.15 |
| P3 | Stable-prefix dispatch | 1d | Medium | $0 (bespaart) |

---

## 9. Externe Bronnen

### Industrie Onderzoek

| Bron | Key Insight | Toepassing VNX |
|------|-------------|----------------|
| **Anthropic Prompt Caching** | Stable prefix + variable suffix → 90% input korting | Pattern C: Stable-prefix dispatch |
| **Manus AI Architecture** | KV-cache aware prompt design, 10x cost reductie | Dispatch format optimalisatie |
| **GitHub Copilot Memory** | Citation-based patterns met file:line verwijzing | Pattern B: Verifieerbare intelligence |
| **Mastra Observational Memory** | Observer/Reflector compressie, 3-6x token reductie | Pattern A: Receipt compressie |
| **JetBrains AI Assistant** | Deterministic-first, LLM-second architectuur, 52% goedkoper | Pattern E: Tiered intelligence |
| **HAM (Hierarchical Agentic Memory)** | Directory-scoped context filtering, 50% token reductie | Pattern D: Path-based filtering |

### Relevante URLs

- Anthropic Prompt Caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- Anthropic Extended Thinking: https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
- Claude Code Hooks: https://docs.anthropic.com/en/docs/claude-code/hooks
- SQLite FTS5: https://www.sqlite.org/fts5.html
- Mastra Memory Architecture: https://mastra.ai/docs/agents/agent-memory
- JetBrains AI Approach: https://blog.jetbrains.com/ai/

### Key Metrics Samenvatting

| Metriek | Huidige waarde | Doel na P0 | Doel na P1 |
|---------|----------------|------------|------------|
| Intelligence dekking | 41% | 80%+ | 95%+ |
| Pattern usage tracking | 0% | 100% | 100% |
| Quality score differentiatie | 2 waarden (85/95) | Continu 70-100 | Continu 70-100 |
| Ranking effectiviteit | ~uniform | Score-gedifferentieerd | Usage+citation based |
| Tag staleness | N/A (DB leeg) | <24h | <24h |
| Terminal relevantie | 0% filtering | 60%+ filtering | 80%+ filtering |
| Feedback loop cycle time | ∞ (broken) | 24h | 24h |
| Intelligence kosten | $0 | $0 | ~$0.07/dag |

---

*Dit rapport is gebaseerd op directe code-analyse van 6.197 regels intelligence code, 156 completed dispatches, en database inspectie op 2026-02-28.*
