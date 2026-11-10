# ADR-067 - De leer-routering: vier bakken met elk één home, twee verwijzingsdoelen, een vensterverdrag en een audit die voorstelt en niet beslist

**Status:** Voorgesteld 10-11-2026 (T0, track `learning-capture-fragmentation`, rij PR-0); aangenomen zodra de T0 PR-0 merget.
**Datum:** 2026-11-10
**Besloten door:** de operator voor besluit 2 van de 1.9-sprint (07-11-2026, letterlijk hieronder); het plan `claudedocs/2026-11-07-naar-1-9/plannen/learning-capture-fragmentation.md` (revisie 2, 08-11-2026, plan-gate van 08-11 en de tiebreaker-reden) voor de besluiten 1 tot en met 3, 7, 8 en 13 tot en met 20 van dat plan. Het nummer 067 is door de T0 toegewezen: 064 is `deltareview` DR-0, 065 is loop D0, 066 is dream PR-1 en 068 is reattr L1. ADR-067 was vrij op `origin/main` `beef78644` (10-11-2026).
**Cross-refs:** ADR-007 (samengestelde sleutel over `project_id`; hier gelezen als bindingsplicht, zie besluit 9), ADR-056 (zelfstandigheid als meetbaar begrip), ADR-063 (onbekend is nooit 0), ADR-065 (leren is voorstellen), ADR-066 (dream-liveness: het schema `dream_liveness/1` en zijn drempel; geen overlap met dit spoor).

De operator heeft op 07-11-2026 besloten, onder meer:

> **2.** zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; leren en dreaming blijven voorstellen.

Dit ADR legt vast waartegen de vier code-rijen PR-1 tot en met PR-4 bouwen: welke bak waarvoor dient, hoe een bak naar een andere verwijst, welk venster een meting heeft, hoe een dubbele vastlegging wordt gevonden, welke drie standen een paar kent, hoe de bronnen aan een project gebonden zijn, wat het leesmodel niet mag bevatten en hoe het spoor terug te draaien is. Het bevat geen code; alles erin is een regel over data. Er zit in het hele draaipad geen model. Het vinden van kandidaat-paren is regex, tokenisatie en een vaste drempel. De enige open natuurlijke taal is het oordeel "dit is geen duplicaat", en dat is de handeling van de operator met `dismiss`. De laatste vijftien procent aan kandidaten die een model zou vinden is niet de reden dat dit spoor bestaat.

## Context

Een les landt vandaag in één van vier bakken en niets verbindt ze:

| Bak | Opslag | Schrijver | Lezer in code |
|---|---|---|---|
| Memory | `~/.claude/projects/<slug>/memory/*.md` | operator en het memory-tool van Claude | `scripts/lib/operator_memory_indexer.py` (`fetch_relevant_memories`, `format_memories_section`) |
| Open items | `state/open_items.json` | `scripts/open_items_manager.py`, `scripts/lib/open_items_from_report.py` | `scripts/learning_loop.py`, `scripts/lib/review_cap_aftermath.py` |
| Build-log | `~/Business/Content/build-log-bucket/YYYY-MM.md` | de persoonlijke `/build-log`-skill buiten deze repo | alleen de koppen, in `scripts/lib/t0_rotation_state.py` (`bucket_headings`) |
| Leerlus | `state/quality_intelligence.db`, `pending_rules.json` | `scripts/learning_loop.py` | de operator via `vnx learning review` |

Gemeten op 08-11-2026 op `origin/main` `a1376e5c8`:

- 141 van 448 notities noemen minstens één `OI-<n>`: 228 distinct refs, 0 onopgelost. 109 daarvan wijzen naar een item dat `done`, `deferred` of `wontfix` is, dus een les leest als actueel terwijl het probleem gesloten is. 420 bucket-entries noemen een `OI-<n>`. Geen enkele regel code lost die refs op.
- 428 van 448 notities gebruiken `[[naam]]`. De bucket doet dat drie keer.
- Dubbel vastleggen is meetbaar met de methode van besluit 12: `oi_mem` 15, `oi_nug` 8, `mem_nug` 2 paren. De precisie is niet gemeten, want er is geen gelabelde set.
- De wachtrij van de leerlus bereikt de operator niet. `pending_rules.json` bestaat niet in het grootboek, de voorstellen staan alleen in `learning_loop_shadow_report.json` en `update_terminal_constraints` keert in shadow terug vóór de wachtrij-write.
- Goedkeuren is gevaarlijk voor een ander soort rij. `ingest_approved_rules` zet elke rij met `status == "approved"` in `prevention_rules` als `failure_prevention`, zonder naar `signal` te kijken.
- De bronnen dragen geen projectsleutel en hebben geen gemeenschappelijke klok. Open items hebben geen `project_id` (0 van 2.632) en alle `created_at` zijn naïeve lokale tijd (`open_items_manager.py:254`). Van de 448 notities missen er 175 een `metadata.modified`. Een bucket-entry heeft alleen een datum en het bestand bevat zes projecten.

## Besluit

### 1. Welke bak waarvoor (de routeringstabel)

| Recordtype | Home | Wanneer |
|---|---|---|
| Open item | `state/open_items.json` | Een probleem dat de sessie overleeft en een status nodig heeft. |
| Memory-notitie | `~/.claude/projects/<slug>/memory/<naam>.md` | Een duurzame les die toekomstig gedrag verandert. |
| Build-log-nugget | `build-log-bucket/YYYY-MM.md` | Een publiceerbare sessie-observatie. |
| Leerlus | `state/quality_intelligence.db` | Een uit bewijs afgeleid patroon. Alleen voorstel, nooit een bron. |

Bij een gelijke vondst in twee bakken wint de volgorde open item, dan notitie, dan nugget. De leerlus staat buiten die volgorde, want zij is een afgeleide en geen bron. De tabel krijgt in PR-1 een code-vorm, `BIN_ROUTING: dict[str, dict]` met per recordtype `{home, why}`, en die code leest deze tabel. De code mag de tabel niet inhalen: een wijziging van de routering is een wijziging van dit ADR.

### 2. Eén home per record

Een record heeft precies één home-bak. De andere bakken verwijzen alleen. Een paar is *geadopteerd* wanneer het niet-home record de sleutel van het home record draagt. Dit is de afdwingbare vorm van "welke bak waarvoor".

### 3. Twee verwijzingsdoelen en de grammatica

Een verwijzing wijst naar een open item of naar een geheugennotitie. Een nugget is nooit een doel, alleen een bron die verwijst. De grammatica:

- Open item: `OI-<n>`, gelezen met `OI-[1-9][0-9]*`. Sleutel van het record: `OI-<n>`.
- Notitie: `[[naam]]`, zonder alias-deel. Sleutel van het record: de bestandsnaam zonder `.md`. Een `[[naam]]` telt bij een notitie waarvan de bestandsnaam of `name:` gelijk is aan `naam`, want 5 van 448 notities hebben een `name:` ongelijk aan de bestandsnaam.
- Nugget: een locator-hash van de kop. Een nugget heeft geen verwijsbare sleutel.

Reden: 228 en 411 bestaande OI-refs vragen geen migratie en het geheugen gebruikt `[[naam]]` al in 428 notities. Een notitie of nugget zonder OI heeft een eigen sleutel en hoeft er geen te krijgen. Er komt geen universele OI-identiteit, want die botst met geheugen-only en nugget-only homes.

### 4. De koppeling is een read-only resolver

`scripts/lib/lesson_refs.py` (PR-1) leest en schrijft niets in de bronnen. De refs bestaan al; de resolver voegt leesbaarheid toe.

### 5. Memory toont de live status van de OI die de les noemt

`fetch_relevant_memories` hangt per geselecteerde notitie de status van elke genoemde OI aan het `OperatorMemory`-object, vóór de budgetlus. `format_memories_section(memories)` houdt zijn handtekening en rendert `- OI-<n>: <status> (gesloten <datum>, <pr>)`. Alleen verwijzingen in de tekst tellen: 0 notities gebruiken een `open_item:`-sleutel in de frontmatter.

### 6. Onbekend is nooit open

Een ontbrekende, onleesbare of niet-gebonden store geeft `onbekend (<reden>)`, nooit `open` en nooit een stille weglating. Een verwijzing die niet in de store staat geeft `onbekend (niet in de store)`. De redenen zijn `store_unavailable`, `not_in_store` en `project_unknown`. Dit volgt ADR-063 besluit 2.

### 7. De handoff blijft ongewijzigd: geen handoff-sleutel en geen sidecar

Er komt geen `wrap.nugget_refs` in het `wrap:`-blok, geen sleutel in `handoff.md` en geen sidecar. Gemeten redenen:

1. `check_wrap_block`, `_check_nuggets` en `render_wrap_block` in `scripts/lib/t0_rotation_state.py` lezen benoemde sleutels en eisen een strikte round-trip. Een nieuwe sleutel is dus een schrijver- en lezercontract in een script dat de spawner (`scripts/t0_rotate_spawn.sh`, exit 6 bij een slecht bewijs) en elke gepinde consumer draait.
2. Er is geen lezer van een `nugget_refs`-sleutel.
3. `daily-log/handoff.md` is niet getrackt en wordt per wrap overschreven. Het is geen duurzaam record, en een sidecar zou alleen een kopie van tijdstempels zijn.
4. Het venster dat de audit nodig heeft is tijd (besluit 10), en het `wrap:`-blok levert `started_at` en `wrapped_at` al als expliciete bron voor de Done-meting.

Er is niets om terug te draaien, want geen levering raakt `t0_rotation_state.py`. Mocht een toekomstig spoor een sleutel willen toevoegen: een oude lezer negeert een extra sleutel, en een nieuwe lezer mag de sleutel nooit verplicht stellen, want afwezigheid is geen bewijs voor een poort.

### 8. De audit is een afgeleid leesmodel, geen leerlusvoorstel en geen rij in `pending_rules.json`

Redenen: de wachtrij is leeg en onleesbaar voor de operator in shadow; een rij van deze soort past niet in het regelschema (`pattern`, `prevention`, `confidence`, `occurrence_count`); en goedkeuren zou via `ingest_approved_rules` een preventieregel in dispatches zetten. De audit schrijft `state/capture_audit.json` (atomair overschreven, herbouwbaar) en leest en schrijft `state/capture_dispositions.ndjson` (append-only). `learning_loop.py`, `pending_rules.json`, `ingest_approved_rules` en `vnx_cli/commands/learning.py` blijven onaangeroerd. De audit stelt voor en beslist niet: een paar wordt alleen `linked` doordat het record zelf de verwijzing draagt, of `dismissed` door een operatorregel met reden. Niets schrijft naar de leerdatabase of naar `prevention_rules`.

### 9. Expliciete projectbinding en fail-closed bronnen

De audit leest alleen via een bindingsbestand `state/capture_sources.json` met de velden `schema`, `project_id`, `memory_dir`, `bucket_dir`, `bucket_slug` en `bound_at`.

- Het staat in de store van het project en `project_id` moet gelijk zijn aan `project_id_from_state_dir(state_dir)` (`scripts/lib/vnx_paths.py`). Een afwijking of een lege afleiding geeft staat `unbound`.
- `memory_dir` moet na `resolve()` onder `~/.claude/projects/` liggen.
- De bucket wordt gelezen als `bucket_dir/YYYY-MM.md`. Alleen entries met een kopslug gelijk aan `bucket_slug` komen in de audit. De andere slugs worden geteld als `foreign_slug_skipped` en nooit gelezen in de uitvoer.
- Open items worden gebonden door de plek van de store, want de items dragen geen `project_id`. Een item mét een ander `project_id` wordt overgeslagen en geteld, zoals de leerlus doet.
- Voor de injectie in dispatches (besluit 5) komt de OI-store uit `resolve_paths()["VNX_STATE_DIR"]` van het proces zelf. De status wordt alleen getoond als `project_id_from_state_dir` een geldig id geeft; anders `onbekend (project onbekend)`.
- Elke bak heeft een staat `ok`, `absent`, `unbound` of `error`. `open_pairs` is `null` zodra één bak niet `ok` is. Een afwezige `open_items.json` is `absent` en geen lege lijst: `read_items` geeft bij een ontbrekend bestand stilzwijgend een lege store, dus de audit controleert eerst of het bestand bestaat. Een niet-gebonden bucket is `unbound`, nooit een gegokt pad.
- `python3 scripts/capture_audit.py bind` schrijft het bindingsbestand na controle dat de paden bestaan. Zonder bindingsbestand is de audit `unbound`.

Dit is de lezing van ADR-007 voor bronnen zonder projectsleutel: de binding ligt bij de plek en wordt expliciet vastgelegd, nooit afgeleid uit het huidige pad.

### 10. Het vensterverdrag

Een venster is `(project_id, since, until)` in UTC. Een record is *nieuw in het venster* wanneer zijn eigen tijdstempel erin valt:

- Open item: `created_at`, naïef gelezen als lokale tijd van de host (alle 2.632 zijn naïef) en omgezet naar UTC.
- Notitie: `metadata.modified` indien aanwezig, anders de mtime van het bestand. Het veld `modified_source` (`frontmatter` of `mtime`) staat in het leesmodel. Een gewijzigde notitie telt als nieuw: dat is de voorzichtige kant.
- Nugget: de datum in de kop op dagniveau. De entry telt als nieuw wanneer die datum tussen `since.date` en `until.date` ligt. Dat kan een oudere nugget van dezelfde dag meenemen, wat een extra paar kan geven dat met `dismiss` af te handelen is.

Een venster komt uit drie bronnen, vastgelegd in `window.source`:

| `window.source` | Betekenis |
|---|---|
| `explicit` | `--since` en `--until`, voor de Done-meting, te nemen uit `started_at` en `wrapped_at` van het `wrap:`-blok |
| `chain` | de nachtelijke run: `since` is de `until` van de vorige geslaagde audit |
| `first24h` | de eerste run: 24 uur terug |

De keten is gat-vrij, want elke run sluit aan op de vorige.

### 11. De ankerregel, achterstand apart, geen lege nul

Een paar telt voor een venster wanneer minstens één van zijn twee records nieuw is in dat venster. De partner mag elke leeftijd hebben. `open_pairs` telt alleen die paren die nog `open` zijn. `backlog_open_pairs` telt de open paren zonder nieuw record en staat apart, want het corpus-aantal is geen Done-maat. `anchors_total` is het aantal nieuwe records over de drie bakken. Een meting met `anchors_total == 0` is leeg: de meting is `null` en nooit `0`. Een lege meting is geen lege nul die als geslaagd leest. Dit sluit de bevinding dat een Done-meting het corpus scande in plaats van nieuwe vastleggingen.

### 12. Paarsoorten, paarsleutel en methode

Drie soorten: `oi_mem`, `oi_nug` en `mem_nug`. De laatste is de OI-vrije kruisbak-soort. `oi_oi` valt buiten het spoor omdat `open_items_manager._find_by_dedup_key` die dekt. `mem_mem` valt erbuiten omdat dat geheugenonderhoud is, en `nug_nug` omdat een bak met alleen narratie geen tweede home heeft.

- Paarsleutel: `sha256(project_id | soort | sleutelA | sleutelB)`, ingekort tot 16 tekens.
- Methode, als constanten in de code: `TOKEN_MIN_LEN = 4`, `JACCARD_MIN = 0.34`, `SHARED_MIN = 2`.
- Tokens zijn letters-alleen en kleine letters. Vergeleken tekst is de OI-titel, de notitie `name` plus `description`, en de nugget-titel met koppeltekens als spaties.
- `normalize_open_item_title` uit de leerlus wordt niet hergebruikt en niet verhuisd: het is een klassesleutel die cijfers laat vallen en op 6 woorden afkapt, en Jaccard vraagt tokensets.
- Basislijn op 08-11-2026: `oi_mem` 15, `oi_nug` 8, `mem_nug` 2.

Waarom deterministisch: de methode vindt kandidaten, de operator beslist. Een model zou meer kandidaten vinden, maar levert een niet-reproduceerbare telling op en een uitvoer die niet te falsifiëren is.

### 13. De dispositie van een paar: drie standen

| Stand | Wanneer |
|---|---|
| `linked` | Het niet-home record draagt de sleutel van het home record (besluit 2 en 3). De audit ziet dat zelf, de operator hoeft niets te schrijven. |
| `dismissed` | De operator zegt dat het geen duplicaat is, als een regel `{pair_id, reason, at, by}` in `capture_dispositions.ndjson`. Een dismissal blijft staan voor die `pair_id`. |
| `open` | Geen van beide. |

Adoptie is dus het toevoegen van `OI-<n>` of `[[naam]]` aan het niet-home record, in een bestand dat de operator al bezit. Het commando is `python3 scripts/capture_audit.py dismiss --pair <id> --reason <tekst>`; een lege reden en een onbekende `pair_id` worden geweigerd. Een kapotte ledgerregel telt niet als dismissal: het paar blijft `open`.

### 14. Het operatorpad, zonder shadow-afhankelijkheid

De audit kent geen shadow-stand: hij rekent altijd en schrijft alleen zijn eigen leesmodel. Dormant betekent hier schrijven uit en rekenen aan. De audit draait in de nachtelijke fase en hangt niet af van `VNX_LEARNING_LOOP_ENABLED` of van de uitkomst van de lus. Zichtbaar op drie plekken:

1. de nachtelijke beacon (`duplicate_capture_open`, `capture_audit_bins`);
2. het leesmodel `state/capture_audit.json`;
3. `python3 scripts/capture_audit.py report`, met `--show` om namen lokaal op te lossen voor de terminal.

De volgorde van de adoptie staat in `docs/core/LEARNING_CAPTURE_ROUTING.md`. Het commando is een script en geen nieuw `bin/vnx`-subcommando, want twee golf-3-sporen bezetten `bin/vnx`. De exitcode is geen bewijs: de Done-meting leest het JSON-bestand.

### 15. Privacy van het leesmodel

`capture_audit.json` bevat alleen: `project_id`, het venster, staat en tellingen per bak, en per paar `{pair_id, soort, oi_id, locators, anker_bak, dispositie, in_venster}`. Een locator is `sha256(project_id | bak | sleutel)`, ingekort. Geen titel, geen kop, geen body, geen bestandsnaam. `report --show` leest de bronnen opnieuw en print namen alleen naar de terminal, en slaat ze nooit op.

### 16. Geen consolidatie van bakken

De records zijn complementair en drie van de vier stores zijn operator-bezit buiten de repo. De overlap is klein: 25 paren op 2.632 items, 448 notities en 1.615 nuggets van dit project. Er komt geen nieuwe store voor lessen.

### 17. De volgorde van de bouw en de bouwer

PR-0 (dit ADR) eerst, want de code leest de tabel hieruit. PR-1 (resolver en geheugen) en PR-2 (bronnen en binding) kunnen tegelijk. PR-3 (de audit-engine) volgt op beide en PR-4 (rapport, dismiss en beacon) op PR-3. PR-2, PR-3 en PR-4 raken allemaal `scripts/capture_audit.py` en worden nooit gelijktijdig gebouwd. PR-3 wordt met `VNX_STAGE_MODEL=opus` gebouwd, de andere rijen met sonnet: de fouten in PR-3 zijn stil (een venster dat tijdzones verkeerd leest, een paar dat als `linked` telt zonder verwijzing, een nul bij `anchors_total == 0`) en een vloer die alleen de stoel bindt repareert niets, want de omgekeerde conditie wordt door de bouwer geschreven. Elke PR gaat door de deur; geen levering raakt deur, merge, permit-check, receiptformaat of een onomkeerbaar pad.

## Consequenties

- De code van PR-1 tot en met PR-4 heeft één bron voor de routering, de grammatica, het venster en de standen. Een afwijking van dit ADR is een reviewbevinding.
- Een notitie toont voortaan of het probleem dat ze noemt nog open is. 109 notities die nu als actueel lezen krijgen een zichtbare gesloten-status.
- De operator krijgt een leesbaar overzicht van dubbele vastleggingen dat niet van shadow-modus of van een lege wachtrij afhangt.
- Het spoor raakt `learning_loop.py` niet. Dat haalt de bestandsoverlap weg met `learning-d3-provider-filter-drop`, `self-learning-loop-reactivate` en `learning-signal-reattribution`.
- De precisie van de paarmethode is niet gemeten. Zolang er geen gelabelde set is, zegt `open_pairs` hoeveel kandidaten er zijn en niet hoeveel er echt dubbel zijn.
- Een nugget van dezelfde dag wordt meegenomen op de randdag van een venster. Dat kan één extra paar geven.

## Terugdraaien

De audit uitzetten is de nachtelijke aanroep weghalen of het bindingsbestand `state/capture_sources.json` verwijderen. Zonder bindingsbestand is elke bak `unbound` en is `open_pairs` `null`. Er is geen datamigratie: de bronnen zijn nooit gewijzigd door dit spoor, `capture_audit.json` is herbouwbaar en `capture_dispositions.ndjson` is een eigen append-only bestand dat bij een heraanzetten blijft gelden. De geheugenindexer (PR-1) is terug te draaien door de statusregel niet meer te renderen; de handtekeningen van `fetch_relevant_memories` en `format_memories_section` zijn niet veranderd, dus er is geen aanroeper om aan te passen.

## Wat dit ADR niet doet

- Het zet de leerlus niet aan en wijzigt `VNX_LEARNING_LOOP_ENABLED` niet.
- Het schrijft niets naar de leerdatabase, naar `prevention_rules` of naar `pending_rules.json`.
- Het wijzigt de handoff, het `wrap:`-blok of `t0_rotation_state.py` niet (besluit 7).
- Het verplaatst of wijzigt geen bestaande notitie, open item of nugget. Adoptie is het toevoegen van een verwijzing door de operator.
- Het legt geen precisie van de paarmethode vast. De drempels zijn constanten die met een gelabelde set herzien kunnen worden, in een volgend ADR.
