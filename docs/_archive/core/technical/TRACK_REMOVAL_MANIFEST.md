# Track A/B/C Removal — Complete File Manifest

**Doel**: Verwijder de Track A/B/C abstractielaag. Gebruik alleen T1/T2/T3 als terminal identifiers.
**Aangemaakt**: 2026-03-04
**Status**: Analyse compleet, nog niet uitgevoerd

---

## Samenvatting

| Categorie | Bestanden | Occurrences | Type wijziging |
|---|---|---|---|
| Dispatcher & routing | 3 | ~50 | Functionele code wijziging |
| Smart tap & receipt processing | 3 | ~20 | Parser update |
| Manager block template | 4 | ~100 | Template herstructurering |
| Commands (track-init, track-status) | 2 | ~40 | Hernoemen of verwijderen |
| Terminal CLAUDE.md identiteit | 4 | ~30 | Tekst aanpassing |
| State files (JSON) | 4 | ~30 | Schema wijziging |
| Python contracts & config | 3 | ~15 | Method hernoemen |
| VNX intelligence scripts | 5 | ~25 | Variable/field hernoemen |
| Feature planning docs | 3 | ~100 | Find-replace |
| Architecture docs & ADRs | 4 | ~80 | Herschrijven |
| Test files | 6 | ~40 | Comments/naming |
| Overige docs & blogs | 15+ | ~200 | Find-replace |
| Multi-provider duplicates (.gemini, .agents) | 6 | ~130 | Sync na .claude wijzigingen |
| Archived dispatches | 156 | ~1500 | NIET WIJZIGEN (historisch) |
| **Totaal actief** | **~60** | **~860** | |

---

## Phase 1: Functionele kern (MOET EERST)

### 1.1 Dispatcher routing

**`.claude/vnx-system/scripts/dispatcher_v8_minimal.sh`**
- `track_to_terminal()` functie (regels ~350-360) → verwijderen
- `[[TARGET:A]]` parsing → accepteer `[[TARGET:T1]]`
- Dispatch filename suffix `-A.md` → `-T1.md`
- Track routing in `process_dispatches()` → direct terminal ID gebruiken
- ~50 regels geraakt

**`.claude/vnx-system/scripts/lib/dispatch_metadata.sh`**
- `vnx_dispatch_extract_track()` → hernoemen naar terminal extractie
- Track-gerelateerde helper functies → aanpassen
- ~15 regels geraakt

### 1.2 Smart tap parser

**`.claude/vnx-system/scripts/smart_tap_v7_json_translator.sh`**
- Parseert `Track: A` uit Manager Blocks → verwijder Track field parsing
- Bouwt `[[TARGET:A]]` → verwacht `[[TARGET:T1]]`
- Track extractie in JSON output → terminal ID direct
- ~20 regels geraakt

### 1.3 Receipt processor

**`.claude/vnx-system/scripts/receipt_processor_v4.sh`**
- Track veld in receipt metadata → verwijder of negeer
- Gebruikt al terminal ID intern voor routing
- ~10 regels geraakt

### 1.4 State files

**`.claude/vnx-system/.vnx-data/state/panes.json`**
```json
// VOOR:
"T1": { "pane_id": "%114", "track": "A", "model": "sonnet" }
// NA:
"T1": { "pane_id": "%114", "model": "sonnet" }
```
- Verwijder `"track"` key uit elke terminal entry

**`.claude/vnx-system/.vnx-data/state/pr_queue_state.json`**
```json
// VOOR:
{ "id": "PR-1", "track": "A", ... }
// NA:
{ "id": "PR-1", "terminal": "T1", ... }
```

**`.claude/vnx-system/.vnx-data/state/progress.yaml`**
- Track A/B/C secties → T1/T2/T3 secties

**`.claude/vnx-system/.vnx-data/state/t0_brief.json`** (gegenereerd)
- `tracks.A` / `tracks.B` / `tracks.C` → `terminals.T1` / `terminals.T2` / `terminals.T3`

---

## Phase 2: Templates & commands

### 2.1 Manager block templates

**`.claude/skills/t0-orchestrator/template.md`**
```markdown
// VOOR:
[[TARGET:A]]
Manager Block
Role: backend-developer
Track: A
Terminal: T1

// NA:
[[TARGET:T1]]
Manager Block
Role: backend-developer
Terminal: T1
```
- Verwijder `Track:` veld volledig
- Wijzig `[[TARGET:A]]` → `[[TARGET:T1]]`

**`.claude/skills/t0-orchestrator/references/example-workflows.md`**
- Alle voorbeelddispatches: Track verwijderen, TARGET wijzigen
- ~80 occurrences

**`.claude/terminals/library/templates/t0/manager_block.md`**
- Template definitie: Track veld verwijderen
- Voorbeelden bijwerken

**`.claude/terminals/library/templates/t0/manager_block_v2.md`**
- Idem als hierboven

**`.claude/terminals/library/templates/dispatches/quality_gate_rejection.md`**
- `[[TARGET:A|B|C]]` → `[[TARGET:T1|T2|T3]]`

**`.claude/terminals/library/templates/dispatches/quality_gate_verification.md`**
- Idem

### 2.2 Commands

**`.claude/commands/track-init.md`**
- Hernoemen naar `terminal-init.md` of argument wijzigen
- `track-init A` → `terminal-init T1` (of `/track-init T1` als backward compat)
- NDJSON event: `"track": "A"` → `"terminal": "T1"`
- Track-specifieke context secties (TRACK_A_CRAWLER_PLAN etc.) → terminal-gebaseerd

**`.claude/commands/track-status.md`**
- `/track-status A` → `/track-status T1`
- Output: `Track A: ✅ COMPLETE` → `T1: ✅ COMPLETE`

**`.claude/commands/orchestrate.md`**
- Documentatie update: `Track A → T1` mapping tekst verwijderen
- Commando zelf accepteert al terminal names

**`.claude/commands/seo-sprint.md`**
- Track A/B/C sprint breakdown → T1/T2/T3

**`.claude/commands/pm-next.md`**
- Track referenties in suggesties → terminal IDs

---

## Phase 3: Intelligence & briefing scripts

**`.claude/vnx-system/scripts/generate_t0_brief.sh`**
- Brief structuur: `tracks.A/B/C` → `terminals.T1/T2/T3`
- Track-gebaseerde health aggregatie → terminal-gebaseerd

**`.claude/vnx-system/scripts/generate_t0_recommendations.py`**
- Aanbevelingen per track → per terminal
- `"track": "A"` in output → `"terminal": "T1"`

**`.claude/vnx-system/scripts/build_t0_tags_digest.py`**
- Track referenties in tag analyse

**`.claude/vnx-system/scripts/gather_intelligence.py`**
- `track` parameter in `gather_for_dispatch()` → `terminal`
- Intelligence output: `"track"` veld → verwijderen (al terminal in apart veld)

**`.claude/vnx-system/scripts/userpromptsubmit_intelligence_inject_v5.sh`**
- Brief parsing: `tracks.A` → `terminals.T1`

---

## Phase 4: Terminal identiteit

**`.claude/terminals/T1/CLAUDE.md`**
```markdown
// VOOR:
# T1 - Track A Implementation (Sonnet)
You are T1, responsible for Track A implementation

// NA:
# T1 - Implementation Terminal (Sonnet)
You are T1, a worker terminal for implementation tasks
```

**`.claude/terminals/T2/CLAUDE.md`**
- Idem: "Track B" → generiek worker

**`.claude/terminals/T3/CLAUDE.md`**
- Idem: "Track C" → generiek deep analysis terminal
- MCP configuratie blijft ongewijzigd (gerelateerd aan T3, niet aan Track C)

**`.claude/terminals/T3/bootstrap.md`**
- Track C referenties → T3

---

## Phase 5: Python source code

**`src/shared/contracts_v2.py`**
```python
# VOOR:
def to_track_b_format(self) -> Dict[str, Any]:
def validate_for_track_b(output: CrawlerOutput) -> Tuple[bool, List[str]]:

# NA:
def to_storage_format(self) -> Dict[str, Any]:
def validate_for_storage(output: CrawlerOutput) -> Tuple[bool, List[str]]:
```

**`src/infrastructure/config/utils.py`**
```python
# VOOR:
def get_track_config(track: str) -> Dict[str, Any]:
    track_configs = { "A": {...}, "B": {...}, "C": {...} }

# NA:
def get_terminal_config(terminal: str) -> Dict[str, Any]:
    terminal_configs = { "T1": {...}, "T2": {...}, "T3": {...} }
```

**`src/infrastructure/config/cli.py`**
- `cmd_track()` → `cmd_terminal()` of verwijderen als ongebruikt

---

## Phase 6: Test files

**`tests/integration/test_cross_track_integration.py`**
- Docstring: "Track A → Track B integration" → "T1 → T2 integration"
- Functioneel ongewijzigd (test business logic, niet track routing)

**`tests/integration/test_track_ab_real_integration.py`**
- Hernoemen naar `test_t1_t2_real_integration.py`
- Interne referenties bijwerken

**`tests/utils/run_track_a_tests.py`**
- Hernoemen naar `run_t1_tests.py` of verwijderen als ongebruikt

**`tests/production_blockers/TRACK_B_COORDINATION.md`**
- "Track A" → "T1", "Track B" → "T2" (336 regels, ~80 occurrences)

**`.claude/terminals/T1/test_unified_mapping_integration.py`**
- `validate_for_track_b` import → `validate_for_storage`

**`.claude/terminals/T2/test_e2e_interface_validation.py`**
- Idem

---

## Phase 7: Architectuur docs

**`SEOCRAWLER_DOCS/10_ARCHITECTURE.md`** (1422 regels)
- Diagram herschrijven: "Track A (T1)" → "T1 — Crawler"
- Beschrijving: Track-gebaseerde topology → terminal-gebaseerde topology
- Grootste doc-wijziging

**`SEOCRAWLER_DOCS/80_ADRs/001-multi-track-architecture.md`**
- Amendment toevoegen: "Track A/B/C alias layer removed, terminals T1/T2/T3 used directly"
- Originele decision behouden als historische context

**`SEOCRAWLER_DOCS/05_RAID_LOG.md`**
- "Track A Lead" → "T1" etc.

**`SEOCRAWLER_DOCS/06_REQUIREMENTS.md`**
- "Track A: 100% COMPLETE" → "T1: 100% COMPLETE"

---

## Phase 8: Feature planning & queue

**`FEATURE_PLAN.md`**
```markdown
// VOOR:
**Track**: A
**Priority**: P0

// NA:
**Terminal**: T1
**Priority**: P0
```
- 9 PRs bijwerken

**`PR_QUEUE.md`**
- Track referenties → terminal IDs

**`.claude/vnx-system/scripts/pr_queue_manager.py`**
- `"track"` veld in PR schema → `"terminal"`

**`.claude/vnx-system/scripts/lib/pr_queue_state_snapshot.py`**
- Track referenties in snapshot logic

---

## Phase 9: Multi-provider duplicates (SYNC NA .claude wijzigingen)

**`.gemini/skills/t0-orchestrator/`** (3 bestanden)
- template.md, references/example-workflows.md, SKILL.md
- Synchroniseer met .claude versies

**`.agents/skills/t0-orchestrator/`** (3 bestanden)
- Idem

---

## NIET WIJZIGEN (historisch archief)

- `.claude/vnx-system/dispatches/completed/` — 156 bestanden met Track suffix
- `.claude/vnx-system/dispatches/rejected/` — 858 bestanden
- `.claude/vnx-system/demo/dry-run/evidence/` — demo bewijs
- `.claude/vnx-system/archive/` — legacy scripts
- `.claude/vnx-system/docs/orchestration/AS-*.md` — AI Scrutiny changelogs

---

## SQL Migrations

**`migrations/v2/006_create_webvitals_enrichment_rpc.sql`**
- Alleen commentaar: `-- Track: B (Storage)` → verwijderen of updaten
- Geen functionele impact

---

## Hooks

**`.claude/hooks/sessionstart_worker.sh`**
- Track C referentie in prose → verwijderen
- Terminal detectie logica (case A/B/C → T1/T2/T3) → aanpassen of verwijderen

---

## Verificatie na wijziging

1. `cd .claude/vnx-system && bash scripts/vnx_doctor.sh`
2. `python3 -m pytest tests/test_cli_json_output.py tests/test_validate_template_tokens.py tests/test_receipt_ci_guard.py -q`
3. Maak test dispatch met `[[TARGET:T1]]` en verifieer delivery
4. Grep controle: `grep -r "Track: [ABC]" .claude/ --include="*.md" --include="*.sh" --include="*.py" | grep -v archive | grep -v completed | grep -v rejected`
5. Volledige dispatch cyclus: create → deliver → receipt voor T1
