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

### 3. HOOG: SEOCRAWLER_DOCS Ingestion

**Files**:
- `scripts/code_snippet_extractor.py` — verwerkt alleen `*.py` bestanden
- `scripts/code_quality_scanner.py` — scant alleen `*.py` bestanden

**Impact**: 79 SEOCRAWLER_DOCS markdown bestanden (marketing, sales, architectuur, API specs, deployment, security) zijn **volledig onzichtbaar** voor het intelligence systeem
**Urgentie**: Blokkeert intelligence voor niet-code projecten (marketing, sales, business)

**Het probleem**:
```
SEOCRAWLER_DOCS/
├── 01_ARCHITECTURE_OVERVIEW.md
├── 02_CRAWLER_ARCHITECTURE.md
├── API_QUICKSCAN.md
├── API_REFERENCE.md
├── PREVIEW_SYSTEM_ARCHITECTURE.md
├── 41_BROWSER_POOL.md
├── 50_ACTIVECAMPAIGN_AUTOMATION_SETUP.md
├── ... (79 bestanden totaal)
```

`code_snippet_extractor.py` zoekt alleen naar Python files:
```python
for py_file in source_dir.rglob("*.py"):
    # ... extract snippets
```

Markdown docs met architectuurbeslissingen, API contracten, deployment procedures, en business logica worden nooit geïndexeerd in `quality_intelligence.db`.

**Oplossing**:
1. Voeg een `MarkdownDocExtractor` toe aan `code_snippet_extractor.py` (of als apart script)
2. Parse markdown files op heading-structuur (## sections als individuele "snippets")
3. Extraheer metadata: titel, sectie, tags (uit bestandsnaam en heading keywords)
4. Sla op in `code_snippets` tabel met `source_type="documentation"`
5. Update `code_quality_scanner.py` om docs te scoren op basis van:
   - Volledigheid (heeft het een example? heeft het een schema?)
   - Staleness (wanneer laatst gewijzigd vs laatst gecontroleerd)
   - Relevance keywords

**Effort**: 1-2 dagen
**Risico als we dit vergeten**: Intelligence systeem is blind voor 79 docs met cruciale project kennis. Agents die marketing/sales/deployment taken krijgen, krijgen geen relevante intelligence mee.

**Cross-ref**: Niet expliciet in het Upgrade Report gedocumenteerd, maar het is een directe consequentie van het feit dat de code_snippet_extractor alleen `*.py` verwerkt.

---

### 4. MEDIUM: _track_pattern_usage O(n) Full Table Scan

**File**: `scripts/receipt_processor_v4.sh`, functie `_track_pattern_usage()` (regel 331-372)
**Impact**: Bij elke receipt wordt de hele `code_snippets` tabel gescand om SHA1 hashes te matchen
**Urgentie**: Wordt een bottleneck zodra de DB groeit (nu 1.143 patterns, bij SEOCRAWLER_DOCS ingestion 2.000+)

**Het probleem**:
```python
# In de inline Python van _track_pattern_usage():
rows = conn.execute('SELECT id, title, file_path, line_range FROM code_snippets').fetchall()
for row in rows:
    hash_val = hashlib.sha1(f"{row[1]}|{row[2]}|{row[3]}".encode()).hexdigest()
    if hash_val in used_hashes:
        # update pattern_usage
```

Dit berekent SHA1 voor **elke rij** bij elke receipt. O(n) met n = aantal patterns.

**Oplossing**: Voeg een `pattern_hash` kolom toe aan `code_snippets` tabel:
```sql
ALTER TABLE code_snippets ADD COLUMN pattern_hash TEXT;
CREATE INDEX idx_pattern_hash ON code_snippets(pattern_hash);
```

Vul bij extractie (in `code_snippet_extractor.py`) en query dan direct:
```sql
SELECT id FROM code_snippets WHERE pattern_hash IN (?, ?, ...)
```

**Effort**: 1 uur
**Risico als we dit vergeten**: Trage receipt processing bij DB groei, maar functioneel correct

---

### 5. MEDIUM: Stale Documentatie-Referenties

**Files met verouderde `pattern_ids` referenties**:
- `docs/core/00_VNX_ARCHITECTURE.md` — verwijst naar `pattern_ids` in intelligence flow
- `docs/intelligence/` — diverse docs met oude veldnamen
- `docs/internal/intelligence_upgrade/INTELLIGENCE_UPGRADE_REPORT.md` — P0-1 en P0-2 zijn nu afgerond, status moet ge-update

**Impact**: Verwarrend voor toekomstige ontwikkelaars die de docs lezen
**Urgentie**: Niet blokkerend maar nodig voor onderhoud

**Fix**:
1. Zoek alle referenties naar `pattern_ids` in docs en vervang door `offered_pattern_hashes`
2. Update het Upgrade Report: P0-1 en P0-2 → "Afgerond in PR #2 (2026-03-02)"
3. Update intelligence versie referenties naar 1.4.0

**Effort**: 30 minuten
**Risico als we dit vergeten**: Documentatie-drift, verwarring bij onboarding

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
| 3 | SEOCRAWLER_DOCS ingestion | HOOG | 1-2 dagen | Non-code intelligence |
| 4 | _track_pattern_usage O(n) fix | MEDIUM | 1 uur | Performance bij DB groei |
| 5 | Stale docs update | MEDIUM | 30 min | Documentatie consistency |
| 6 | conversation_analyzer_stub.py | LAAG | 3 dagen | Conversation log mining |

**Aanbevolen aanpak**: Items 1-2 eerst (samen <1 uur, deblokkeren de feedback loop die we net gebouwd hebben). Dan item 3 als volgende PR. Items 4-5 kunnen mee in een cleanup PR. Item 6 is een apart project.

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
- Regex bug in report_parser.py (item 1)
- Agent template instructie gap (item 2)
- SEOCRAWLER_DOCS ingestion gap (item 3)
- _track_pattern_usage O(n) scan (item 4)

---

*Laatst bijgewerkt: 2026-03-02 door T-MANAGER*
