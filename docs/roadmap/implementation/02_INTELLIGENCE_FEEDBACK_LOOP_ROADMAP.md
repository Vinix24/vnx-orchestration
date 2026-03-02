# Intelligence Feedback Loop — Remaining Work Roadmap

**Datum**: 2026-03-02
**Branch**: `fix/intelligence_DB`
**Auteur**: T-MANAGER
**Status**: In progress — PR #2 impl compleet, openstaande items hieronder
**Cross-ref**: `docs/internal/intelligence_upgrade/INTELLIGENCE_UPGRADE_REPORT.md` (2026-02-28)

---

## Wat is Afgerond (PR #2)

De kernimplementatie van de feedback loop is klaar en CI-groen (9/9 tests):

| Onderdeel | File | Status |
|-----------|------|--------|
| `offered_pattern_hashes` in dispatch output | `gather_intelligence.py` | Done |
| Fallback partial credit bij success zonder hashes | `receipt_processor_v4.sh` | Done |
| `ignored_count` DB persistentie in learning loop | `learning_loop.py` | Done |
| Tag extractie uit completed dispatches | `tag_intelligence.py` | Done |
| Compound tags in `normalize_tags()` | `tag_intelligence.py` | Done |
| Dagelijkse tag refresh in daemon | `intelligence_daemon.py` | Done |
| Dead code opruiming (`pattern_ids`, stale fallback, hash inconsistency) | Meerdere | Done |
| Version bump naar 1.4.0 | `gather_intelligence.py` | Done |

Dit dekt **P0-1** (Fix Feedback Loop) en **P0-2** (Fix Tag Staleness) uit het Upgrade Report.

---

## Openstaande Items

### 1. KRITIEK: Regex Bug in report_parser.py

**File**: `scripts/report_parser.py`, regel 566
**Impact**: Breekt de gehele used_count pipeline — geen enkel pattern wordt als "gebruikt" gedetecteerd
**Urgentie**: Blokkeert de feedback loop die we net gebouwd hebben

**Het probleem**:
```python
# HUIDIGE CODE (BROKEN):
list_match = re.search(r'used_pattern_hashes\s*:\s*\\[(.*?)\\]', content, re.IGNORECASE | re.DOTALL)

# `\\[` in een raw string matcht letterlijk backslash+bracket, niet `[`
# Agent reports bevatten: used_pattern_hashes: [abc123, def456]
# De regex matcht dit NIET, dus used_count wordt nooit geïncrementeerd
```

**Fix** (1 regel):
```python
list_match = re.search(r'used_pattern_hashes\s*:\s*\[(.*?)\]', content, re.IGNORECASE | re.DOTALL)
```

**Effort**: 5 minuten
**Risico als we dit vergeten**: De hele feedback loop (offered → used → confidence decay) doet niets

---

### 2. KRITIEK: Agent Templates Missen used_pattern_hashes Instructie

**Locatie**: `skills/templates/*.md` (agent dispatch templates)
**Impact**: Agents weten niet dat ze `used_pattern_hashes` moeten rapporteren in hun report
**Urgentie**: Zonder deze instructie zullen agents nooit hashes terugsturen, en valt alles terug op de partial-credit fallback

**Het probleem**:
De intelligence inject stuurt `offered_pattern_hashes` mee in het `[INTELLIGENCE_DATA]` blok. Maar nergens in de agent templates staat een instructie als:

```markdown
## Intelligence Feedback
Als je patterns uit [INTELLIGENCE_DATA] hebt gebruikt, vermeld dan in je report:
used_pattern_hashes: [hash1, hash2, ...]
```

Zonder deze instructie is de sterke feedback loop (expliciete hash-rapportage) niet actief en leunt alles op de zwakke fallback (success-based partial credit).

**Fix**: Voeg een kort `## Intelligence Feedback` blok toe aan de relevante agent templates. Zoek welke templates een `[INTELLIGENCE_DATA]` sectie verwachten en voeg de instructie toe.

**Effort**: 30 minuten (audit templates + toevoegen instructie)
**Risico als we dit vergeten**: Feedback loop werkt alleen via partial credit (zwak signaal), nooit via directe hash-rapportage (sterk signaal)

---

### ~~3. HOOG: SEOCRAWLER_DOCS Ingestion~~ DONE (2026-03-02)

**Status**: Afgerond — `doc_section_extractor.py` geïmplementeerd en CI-groen (13/13 tests)

**Oplossing geïmplementeerd**:
- Nieuw script: `scripts/doc_section_extractor.py` — apart van code extractor
- Configureerbaar via `VNX_DOCS_DIRS` env var (geen hardcoded paden)
- Splitst markdown op `##` headings, scoort secties, categoriseert op bestandsnaam-prefix
- Slaat op in bestaande `code_snippets` FTS5 tabel met `language="markdown"`
- `framework` kolom hergebruikt voor doc categorie (architecture, api, operations, etc.)
- Idempotent: skipt ongewijzigde bestanden via git commit hash check
- Intelligence daemon roept extractor automatisch aan in dagelijkse hygiene
- `gather_intelligence.py` uitgebreid met `_get_preferred_language()` voor taal-aware filtering
- FTS5 queries filteren nu op `language` kolom: doc-taken krijgen markdown, code-taken krijgen Python

**Files gewijzigd/aangemaakt**:
| File | Actie |
|------|-------|
| `scripts/doc_section_extractor.py` | Nieuw — markdown parser + FTS5 opslag |
| `scripts/gather_intelligence.py` | Gewijzigd — `_get_preferred_language()`, language filter in FTS5 query |
| `scripts/intelligence_daemon.py` | Gewijzigd — roept doc extractor aan in `_refresh_quality_intelligence()` |
| `tests/test_doc_section_extractor.py` | Nieuw — 13 CI tests |

---

### ~~4. MEDIUM: _track_pattern_usage O(n) Full Table Scan~~ DONE (2026-03-02)

**Status**: Afgerond — `pattern_hash` kolom toegevoegd aan `snippet_metadata` met index

**Oplossing geïmplementeerd**:
- `snippet_metadata` tabel: `pattern_hash TEXT` kolom + `idx_snippet_pattern_hash` index
- `code_snippet_extractor.py` en `doc_section_extractor.py`: berekenen `SHA1(title|file_path|line_range)` bij extractie
- `receipt_processor_v4.sh`: `_track_pattern_usage()` query via `WHERE sm.pattern_hash IN (...)` i.p.v. O(n) full table scan
- `quality_db_init.py`: migratie voor bestaande databases (ALTER TABLE + CREATE INDEX)

**Files gewijzigd**:
| File | Actie |
|------|-------|
| `schemas/quality_intelligence.sql` | `pattern_hash` kolom + index |
| `scripts/code_snippet_extractor.py` | Hash berekening bij extractie |
| `scripts/doc_section_extractor.py` | Hash berekening bij extractie |
| `scripts/receipt_processor_v4.sh` | O(1) indexed lookup i.p.v. O(n) scan |
| `scripts/quality_db_init.py` | Migratie voor bestaande databases |

---

### ~~5. MEDIUM: Stale Documentatie-Referenties~~ DONE (2026-03-02)

**Status**: Afgerond — alle `pattern_ids` referenties vervangen door `offered_pattern_hashes`

**Oplossing geïmplementeerd**:
- `docs/core/00_VNX_ARCHITECTURE.md`: `pattern_ids` → `offered_pattern_hashes`, versie 1.0.0 → 1.4.0
- `docs/core/technical/INTELLIGENCE_SYSTEM.md`: `pattern_ids` → `offered_pattern_hashes`, versie 2.0.0 → 3.0.0
- `docs/core/technical/DISPATCHER_SYSTEM.md`: `pattern_ids` → `offered_pattern_hashes`, versie 1.0.0 → 1.4.0
- `docs/internal/intelligence_upgrade/INTELLIGENCE_UPGRADE_REPORT.md`: P0-1 en P0-2 gemarkeerd als DONE

---

### 6. LAAG: conversation_analyzer_stub.py

**File**: `scripts/learning/conversation_analyzer_stub.py`
**Impact**: Conversation log mining is niet geïmplementeerd
**Urgentie**: P3 in het Upgrade Report — backlog item

Dit is een 50-regels stub met `pass` bodies:
```python
class ConversationAnalyzer:
    def analyze_period(self, days=3):
        print(f"[STUB] Would analyze {days} days of conversation logs")
    def detect_inefficiency_patterns(self, log_content):
        pass
    def generate_triggers(self):
        pass
```

**Oplossing**: Implementeer de volledige SELECT → EXTRACT → COMPRESS → STORE pipeline zoals beschreven in sectie 7 van het Upgrade Report. Vereist Haiku integration voor log analyse.

**Effort**: 3 dagen
**Cross-ref**: Upgrade Report P3-1 (Conversation Log Mining Pipeline)

---

## Prioriteit Volgorde

| # | Item | Urgentie | Effort | Blokkeert |
|---|------|----------|--------|-----------|
| 1 | ~~Regex bug report_parser.py:566~~ | DONE | 5 min | ~~Hele used_count pipeline~~ |
| 2 | ~~Agent template instructie~~ | DONE | 30 min | ~~Sterke feedback signalen~~ |
| 3 | ~~SEOCRAWLER_DOCS ingestion~~ | DONE (2026-03-02) | ~4 uur | ~~Non-code intelligence~~ |
| 4 | ~~_track_pattern_usage O(n) fix~~ | DONE (2026-03-02) | 1 uur | ~~Performance bij DB groei~~ |
| 5 | ~~Stale docs update~~ | DONE (2026-03-02) | 30 min | ~~Documentatie consistency~~ |
| 6 | conversation_analyzer_stub.py | LAAG | 3 dagen | Conversation log mining |

**Status**: Items 1-5 afgerond. Item 6 is een apart project.

---

## Cross-Reference met Upgrade Report Prioriteiten

| Upgrade Report | Status | Onze Dekking |
|----------------|--------|--------------|
| **P0-1**: Fix Feedback Loop | **Afgerond** (PR #2) | offered_pattern_hashes, fallback, ignored_count |
| **P0-2**: Fix Tag Staleness | **Afgerond** (PR #2) | extract_tags_from_dispatch, compound tags, daemon refresh |
| **P1-1**: Citation-Based Memory | Niet gestart | Niet in scope deze roadmap (apart project) |
| **P1-2**: Terminal-Scoped Filtering | Niet gestart | Niet in scope deze roadmap (apart project) |
| **P2-1**: Observational Memory | Niet gestart | Backlog |
| **P2-2**: Haiku Curation Layer | Niet gestart | Backlog |
| **P3-1**: Conversation Log Mining | Niet gestart | Item 6 in deze roadmap |
| **P3-2**: Stable-Prefix Dispatch | Niet gestart | Backlog |

**Nieuw ontdekt** (niet in Upgrade Report):
- ~~Regex bug in report_parser.py (item 1)~~ DONE
- ~~Agent template instructie gap (item 2)~~ DONE
- ~~SEOCRAWLER_DOCS ingestion gap (item 3)~~ DONE — `doc_section_extractor.py` + language-aware filtering
- ~~_track_pattern_usage O(n) scan (item 4)~~ DONE — `pattern_hash` kolom + indexed lookup

---

*Laatst bijgewerkt: 2026-03-02 door T-MANAGER*
