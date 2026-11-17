# ADR-081 - De levenscyclus en synchronisatie van het eventarchief

**Status:** Voorgesteld 17-11-2026 (T0, track `eventarchief-compressie`, levering C0)
**Datum:** 2026-11-17
**Besloten door:** de operator, besluit `B-20261116-a82b8824` (Vincent, 16-11-2026: "Compressor eventarchieven (Recommended)"), en de plan-gate op het plan `claudedocs/specs/eventarchief-compressie/plan.md`: codex gaf op versie 1 REVISE met acht punten, de tiebreaker (deepseek-v4-pro) koos START met één vereiste wijziging (C1 dekt elk archiefschrijverpad, en C4 draait pas als elke schrijver van dat store de versie met het slot draait). Versie 2 van het plan draagt die wijziging en de acht punten; punt 7 vroeg om dit ADR vóór de code.
**Cross-refs:** OI-1944 (acceptatiepunt 1, het comprimeren; de lezers, het verwijderen na M dagen en de salvage-retentie), ADR-007 (geen centrale tabel, dus niet van toepassing), ADR-079 (een bestaand bestand onder `docs/governance/decisions/` verandert niet). Het plan noemt verder geen ADR.

Dit ADR legt vast waartegen C1 (schrijfgrens, slot en lezersterugval), C2a (compressiebibliotheek), C2b (CLI) en C3 (dagelijkse veegronde) van de track bouwen, en waaraan C4 (de inhaalronde) zich houdt. Het bevat geen code en wijzigt geen bestaand ADR. Waar het "moet" of "nooit" zegt, is dat een eis die een test kan controleren.

**Deterministisch of model.** Alles in dit ADR is deterministisch: toestanden, sloten, `stat`-vergelijkingen, byte-vergelijking, budgetten, exitcodes. Er is geen modelpad.

## Context

`events/archive` groeit zonder opruimregel. Op 15-11-2026 om 17:07Z liep de schijf van de Mac mini vol (171 MB vrij) en startte de Mac opnieuw op; daarbij gingen twee bouwers en alle T0-wachtscripts verloren. Het archief is niet de enige schrijver, maar wel de grootste die geen eigen opruiming kent.

**Nulmeting** (16-11-2026 10:56Z, alle stores onder `~/.vnx-data`):

| store | plain | plain ouder dan 14 d | gz | dagelijkse opruimjob |
|---|---|---|---|---|
| vnx-dev | 4.034 bestanden, 10,35 GiB | 2.191, 5,82 GiB | 2.720, 0,69 GiB | ja |
| seocrawler-v2 | 2.084, 1,59 GiB | 1.471, 1,44 GiB | 139, 0,00 GiB | ja |
| mission-control | 846, 1,66 GiB | 531, 0,80 GiB | 1.087, 0,15 GiB | ja |
| sales-copilot | 255, 0,08 GiB | 187, 0,05 GiB | 257, 0,03 GiB | ja |
| pacompany-engine | 95, 0,21 GiB | 95, 0,21 GiB | 162, 0,08 GiB | nee |
| newsflirt, website | 54, 0,12 GiB | 15, 0,00 GiB | 0 | nee |

Samen 8,3 GiB plain ouder dan 14 dagen. De bestaande gz-bestanden komen uit een eenmalige handmatige ronde; hun verhouding (vnx-dev: 2.720 bestanden in 0,69 GiB) wijst op een factor 8 tot 12. Nergens staan plain en gz van hetzelfde id naast elkaar. De dagelijkse opruimjob is `com.vnx.cleanup-reviewed-worktrees.<project>` (04:00 lokaal, via `vnx_pinned_launcher.sh` op een gepinde fabric-versie).

**Wat er vandaag is.**

- OI-1944 C1 bouwde alleen de lezers in `scripts/lib/event_store.py`: `resolve_archive_file`, `open_archive_text`, `list_archive_files` en `find_archive_file` lezen een `.ndjson.gz` even goed als een plain bestand, en plain wint als beide van hetzelfde id bestaan.
- `scripts/lib/vnx_data_footprint_probe.py` meet de achterstand (`VNX_EVENTS_ARCHIVE_COMPRESS_DAYS`, default 14, plus 2 dagen marge, als `plain_archive_files_older_than_window`) en is bewust read-only.
- Geen schrijver comprimeert. Geen schrijver vergrendelt het archief zelf: de schrijvers nemen LOCK_EX op de **live** stream en vervangen het archief daarna via een scratch en `os.replace` (`_replace_from_scratch`).

## Besluit

### 1. De toestanden van een archief-id en wat een lezer krijgt

*(Plan: § Problem, § Paren, § Risks "Een plain die een gz verbergt".)*

Een archief-id `<id>` in een archiefmap heeft precies één van drie toestanden:

- **plain:** alleen `<id>.ndjson`;
- **gz:** alleen `<id>.ndjson.gz`;
- **paar:** `<id>.ndjson` en `<id>.ndjson.gz` naast elkaar.

Een lezer die het id resolvet moet bij plain de plain krijgen, bij gz de gz, en bij een paar de plain: plain wint (`resolve_archive_file`). Dat blijft zo en verandert niet voor de lezers. Een paar is een overgangstoestand die alleen kan ontstaan door een onderbroken compressie, een schrijver die een plain verlengde na een crash, of een corrupte gz. Een paar moet volgens besluit 5 worden opgelost en mag nooit door een schrijver of de compressor worden aangemaakt.

Testbaar: `resolve_archive_file` geeft per toestand het genoemde bestand; bij een paar de plain.

### 2. Eén schrijfgrens voor het archief

*(Plan: § Eén schrijfgrens voor het archief.)*

Elk pad dat onder `archive_dir(...)` schrijft moet door één functie, de schrijfgrens. Die functie:

1. moet `<archive_dir>/.archive.lock` nemen (`fcntl.flock`, LOCK_EX), altijd **ná** het slot op de live stream. De volgorde is vast: live, dan archief. De compressor neemt alleen het archiefslot, dus er is geen deadlock;
2. moet het bestaande archief resolven via `resolve_archive_file`. Bestaat alleen een gz, dan moet die een nieuw gzip-lid krijgen met de nieuwe regels (ontdubbeling zoals `_extend_archive`). Er mag nooit een nieuwe plain naast een gz ontstaan;
3. moet schrijven via de bestaande scratch-plus-replace (`_replace_from_scratch`: `mkstemp` in dezelfde map, `fsync`, `os.replace`).

De schrijverpaden die het plan op `f8582cde` las zijn `_copy_to_archive`, `_archive_run_stream`, `_append_late_to_pinned_archive` en de tak van `archive_completed_stream` die `_move_to_archive` direct aanroept. C1 moet beginnen met een volledige inventaris van elke aanroep die onder `archive_dir(...)` schrijft, ook buiten `event_store.py`, en die in zijn rapport zetten. Een pad dat de inventaris vindt en dat hier niet staat, gaat ook door de schrijfgrens.

Een structurele toets moet dit vasthouden: in `event_store.py` roept niets buiten de schrijfgrens `_replace_from_scratch` of `os.replace` aan op een pad onder een archiefmap.

Testbaar: per schrijverpad een id dat alleen als gz bestaat wordt na archivering één gz met alle regels in volgorde en geen plain; een tweede proces dat het archiefslot vasthoudt laat de schrijver wachten; de structurele toets.

### 3. De leesgarantie

*(Plan: § Lezers.)*

Een lezer die een plain resolvet en opent nadat de compressor hem heeft weggehaald, krijgt anders `FileNotFoundError`. Een geopende descriptor blijft na een unlink leesbaar (POSIX); alleen het gat tussen resolven en openen telt. `open_archive_text` moet daarom bij een `FileNotFoundError` op `<id>.ndjson` één keer `<id>.ndjson.gz` van hetzelfde id proberen, en pas daarna de fout doorgeven. Een lezer die eerst lijst en later opent, gaat door `open_archive_text` en valt daaronder.

Testbaar: plain resolven, de plain vervangen door een gz, openen leest de gz.

### 4. Het compressieprotocol met het korte slot

*(Plan: § Het compressieprotocol per bestand, met een kort slot.)*

Per plain `<dir>/<id>.ndjson` ouder dan N dagen, oudste eerst, moet de compressor:

1. **Zonder slot:** `stat` (inode, grootte, mtime), comprimeren naar een scratch (`mkstemp` in dezelfde map), `fsync`, de scratch decomprimeren en byte voor byte vergelijken met wat gelezen is. Wijkt het af: scratch weg, melden, volgende.
2. **Met `.archive.lock`, niet-blokkerend:** is het slot bezet, dan de scratch weg en het bestand overslaan (volgende ronde). Anders `stat` opnieuw; is inode, grootte of mtime veranderd, dan de scratch weg en overslaan. Is er intussen een gz van dat id, dan geldt besluit 5.
3. **Nog onder het slot:** mode en mtime van de plain op de scratch zetten, `os.replace(scratch, <id>.ndjson.gz)`, `fsync` van de map, `unlink` van de plain, slot los.

Het slot moet alleen `stat`, replace en unlink dekken: milliseconden, ook bij een groot bestand. Een schrijver wacht nooit op compressie.

Testbaar: verliesloos, ook bij meerdere gzip-leden en een bestand zonder eindnewline; mode en mtime gaan mee; een plain die tussen stap 1 en 2 verandert blijft staan; een bezet slot betekent overslaan.

### 5. Herstel per soort paar

*(Plan: § Paren (plain en gz van hetzelfde id), onder het slot.)*

Onder het archiefslot moet de compressor een paar per soort behandelen:

- **Gelijk** (de gz decomprimeert byte-gelijk aan de plain): een onderbroken ronde. De plain gaat weg.
- **gz is een prefix van de plain** (een schrijver verlengde de plain na een crash): de plain opnieuw comprimeren volgens besluit 4, de gz vervangen, de plain weg.
- **Divergent, of de gz is onleesbaar:** er mag nooit iets worden weggehaald. De ronde meldt het paar als `divergent` met beide paden en groottes, de CLI eindigt met een eigen exitcode (3), en het paar blijft staan tot een mens kiest. Plain wint voor de lezers, zoals nu.

Testbaar: elk van de drie soorten paar; bij divergent blijven beide bestanden byte-gelijk aan vóór de ronde en is de exitcode 3.

### 6. De budgetten

*(Plan: § Budgetten.)*

Een automatische ronde moet stoppen bij wat het eerst komt: 2 GiB gelezen, 10 minuten, of 500 bestanden. Een bestand groter dan 1 GiB moet de automatische ronde overslaan en melden; de CLI met de hand neemt het wel. De CLI moet een bestand weigeren als de vrije ruimte kleiner is dan twee keer zijn grootte.

Testbaar: elk budget stopt de ronde; een bestand boven 1 GiB wordt automatisch overgeslagen en gemeld; de CLI weigert onder twee keer de grootte vrij.

### 7. Wanneer hij draait en de uitrolvoorwaarde

*(Plan: § Wanneer hij draait, § Uitrolvoorwaarde voor C4.)*

- **Eenmalige handronde per store** (C4) om de achterstand weg te werken, pas nadat de uitrolvoorwaarde voor dat store is gemeten. Het slot is advisory: een proces dat een oude versie draait neemt het niet. De ronde draait dus pas als elk proces dat in dat store archiveert de versie met C1 draait. Voor vnx-dev: de hoofd-checkout staat op een commit met C1 en `live_work` in `t0_index.json` toont geen dispatch die vóór die fast-forward begon. Voor de andere projecten: hun fabric (`~/.vnx-system/current` of hun pin) bevat C1 en hun `live_work` is leeg of jonger dan die versie. De T0 meet dit per store en zet het in het C4-rapport. Zonder die meting draait de ronde niet voor dat store.
- **Daarna dagelijks, los van activiteit,** binnen de bestaande opruimjob `com.vnx.cleanup-reviewed-worktrees.<project>`: die draait na zijn eigen werk een veegronde over `events/archive` van zijn store, binnen de budgetten van besluit 6. Er komt geen nieuwe launchd-unit of plist bij. De job draait op een gepinde fabric-versie, dus de dagelijkse ronde begint pas met de eerste release-pin die C3 draagt; tot die tijd meldt de probe `degraded` als er achterstand groeit.
- **Fail-open voor de opruimer:** een fout van de compressor verandert de exitcode en het werk van de opruimer niet, alleen een regel in zijn log.

Testbaar: de opruimer draait de ronde; een exception in de compressor laat de opruimer slagen.

### 8. Wat het niet doet

*(Plan: § Wat het niet doet.)*

Dit ADR en de track verwijderen niets na M dagen (OI-1944 acceptatie 1, tweede helft), leggen geen salvage-retentie vast (acceptatie 2) en ruimen geen worktrees op. Dat blijft op OI-1944. ADR-007 is niet van toepassing: er is geen centrale tabel.

## Gevolgen

- **Het slot is advisory.** Een oud proces zonder het archiefslot kan nog steeds schrijven terwijl de compressor loopt. Daarom bestaat de uitrolvoorwaarde van besluit 7 en draait C4 niet zonder die meting per store. De dubbele `stat` onder het slot en de vergelijking na decompressie vangen een schrijver die toch tussen stap 1 en 2 in het bestand schreef; een schrijver die na stap 3 een plain naast de gz aanmaakt, geeft een paar dat besluit 5 oplost.
- **Onveranderd voor de lezers:** `resolve_archive_file`, `list_archive_files` en `find_archive_file` houden hun contract (plain wint bij een paar); alleen `open_archive_text` krijgt de terugval van besluit 3. De probe blijft read-only en blijft het signaal.
- **Drie stores zonder opruimjob** (pacompany-engine, newsflirt, website) krijgen alleen de handronde. Een opruimjob voor hen is een nieuwe launchd-unit, dus operatorwerk met een akkoord van Vincent, en valt buiten deze track; hun archief is samen 0,33 GiB. Dit staat als bekende grens in de klaar-criteria.
- **Een divergent paar** blijft staan tot een mens kiest; de nameting meldt het.
- **Schijfruimte tijdens de inhaalronde:** per bestand even plain plus gz, vandaar de ruimteregel van besluit 6. De handronde draait niet tijdens een Time Machine-backup: een lokale snapshot houdt de blokken vast tot de backup klaar is.
- Dit ADR wijzigt geen bestaand ADR (ADR-079).
