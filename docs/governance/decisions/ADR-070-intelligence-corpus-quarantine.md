# ADR-070 - Corpus-quarantaine: verplaatsen, nooit wissen, met één bewezen commando terug

**Status:** Accepted (dispatch `dlv-ac8ac9ea8bce`, track `intelligence-corpus-quarantine`, Q8)
**Datum:** 2026-11-10
**Besloten door:** de operator op 07-11-2026. Besluit 3 van die dag, letterlijk: "Learning-tracks ontparkeren zodra ledger-is-falsifiable gemerged is; corpus-quarantaine omkeerbaar (verplaatsen, niet wissen)". De 22 besluiten hieronder staan in het plan `claudedocs/2026-11-07-naar-1-9/plannen/intelligence-corpus-quarantine.md` (revisie 2, 08-11-2026, met de plan-gate-bevindingen B1-B4) en worden hier vastgelegd, niet opnieuw genomen. ADR-070 was vrij op `origin/main` `9803b8976` (11-11-2026); het nummer is door de T0 toegewezen en niet door de bouwer gekozen (besluit 12).
**Cross-refs:** ADR-005 (NDJSON-auditledger), ADR-007 (samengestelde sleutel over `project_id`; hier niet van toepassing, besluit 14), ADR-021 (exception-discipline: een leesfout is een fout), ADR-052 (geen functie boven 200 uitvoerbare regels), ADR-063 (dagelijkse doorloopmeting: meten is geen oordeel).

**Deterministisch, en waarom er geen model in zit.** Alles wat dit ADR vastlegt is determinisme: een gesloten veldenlijst per document, een sha256 over inhoud en opslagklasse per rij, een vaste vergelijking per permit-eis, een transactie met een digest vóór en ná, vaste exitcodes en een NDJSON-regel per stap. Er is geen open natuurlijke taal in de meetweg, geen oordeel onder ambiguïteit en geen synthese over ongestructureerde bronnen; een model heeft hier geen rol en zou de reproduceerbaarheid alleen verzwakken. De veiligheidsnetten zijn daarom gesloten: ontbrekend, `None`, misvormd of onbekend is nooit een pass, en elke weigering noemt de eis die niet houdt.

## Context

**1. Het corpus is groot en het is een verplaatsing, geen verwijdering.** Op 08-11-2026 zijn `code_snippets` en `snippet_metadata` samen 192.495 rijen in een store van 442 MB; de corpus-familie (tabellen, indexen en de FTS5-schaduwtabellen) is circa 415 MB. Besluit 3 van de operator vraagt om het corpus omkeerbaar uit de weg te ruimen: verplaatsen, nooit wissen, en met één commando terug.

**2. De regels bestonden alleen in het plan.** Q1 tot en met Q6 hebben `scripts/vnx_intelligence_corpus_quarantine.py` gebouwd met `baseline`, `quarantine`, `restore`, `verify`, `purge` en `rehearse`, en de module `scripts/lib/intelligence_corpus_quarantine/`. De policy erachter - het manifest, de permit met zijn negen eisen, de sessie-invariant, de exitcodes, de auditregel, het zegel en het fail-closed gedrag - stond nergens waar de vloot hem leest. Een operator die het commando morgen draait, zou de regels uit de code moeten reconstrueren.

**3. De fout die dit voorkomt is een stille halve move.** Een tool die het corpus kopieert maar niet bewijst dat het artefact dezelfde rijen met dezelfde rowids en opslagklassen draagt, of die een delete doet zonder de sessietabel in dezelfde transactie te bewaken, levert een "geslaagde" run op waarvan de terugweg onbewezen is. Daarom is de permit een bewijs en geen aantallencontrole (B1), is de sessie-invariant transactie-gebonden (B2), delen schrijver en lezer één contractmodule (B3), en staat het zegel op de artefactmap (B4).

## Besluit

### Contracten

**Manifest (`baseline`).** `manifest.json`, UTF-8, één object, `sort_keys=True`, geen tijdstip in een veld dat een hash ingaat. Velden: `schema` (1), `track` (`intelligence-corpus-quarantine`), `generated_at` (UTC, ISO-8601 met `Z`), `db` (`path`, `bytes`, `page_size`, `page_count`, `freelist_count`, `user_version`, `journal_mode`, `integrity_check`), en `tables[]` met per tabel `name`, `rows`, `dbstat_bytes`. Met `--snapshot` (of `--reuse-snapshot`) komen daar `snapshot` (`path`, `bytes`, `sha256`, `integrity_check`), `corpus` (`tables`, `rows` per tabel, `dbstat_bytes`, `rowid_range` per tabel, `distinct_file_path`, `file_path_roots`, `languages`, `created_at` min/max, `join` met `orphans` en `duplicate_snippet_rowid`) en `invariants.session_analytics` (`rows`, `max_id`, `sum_id`, `columns` in `pragma_table_info`-volgorde, `digest_sha256`) bij. `file_path_roots` is een deterministische histogram: de sleutel is het pad tot en met de eerste vijf segmenten (of het hele pad als het er minder heeft). `user_version` wordt vastgelegd, niet als poort gebruikt (besluit 5).

**Quarantaine (`quarantine`).** `artifact/quarantine.json`: `schema` (1), `track` (de constante, B3), `generated_at`, `artifact` (bestandsnaam, **relatief** ten opzichte van de map van `quarantine.json`; een absoluut pad, een pad met `..` of een NUL is exit 3), `artifact_bytes`, `artifact_sha256` (gemeten na het schrijven), `source_ddl_sha256` (sha256 over de witruimte-genormaliseerde `sqlite_master.sql` van beide tabellen op de bron), `tables[]` (`name`, `rows`, `row_min`, `row_max`, `columns`, `content_digest`), `restore_proof` (`match_probe`: aantal geprobeerde rowids en `ok: true`) en `seal` (`dir_mode: "0555"`, `file_mode: "0444"`). Het artefact heet `quality_intelligence-corpus-<YYYYMMDD>.db` en draagt de twee corpus-tabellen als `corpus_code_snippets` en `corpus_snippet_metadata`.

**De permit: negen eisen.** `verify` en `purge` gebruiken dezelfde functie (`check_permit`); alle negen moeten waar zijn, anders exit 3 en **is er niets veranderd**:

1. `manifest.json` en `quarantine.json` parsen, `schema == 1`, en **beide** `track` gelijk aan de constante (B3).
2. `artifact_bytes == stat().st_size` en `sha256(artefactbestand) == artifact_sha256`; de artefactnaam is relatief en zonder `..` of NUL.
3. Het zegel is exact wat `quarantine.json` zegt: artefactmap 0555, artefact en `quarantine.json` 0444; het artefact opent read-only en `PRAGMA integrity_check == "ok"`.
4. Per corpus-tabel: de live `table_digest` gelijk aan `tables[].content_digest`, `rows`/`row_min`/`row_max` gelijk, en de live kolomlijst (`pragma_table_info`) gelijk aan `tables[].columns` (DDL-drift). Dezelfde vergelijking wordt op de kopie **in** het artefact gedaan, zodat de drie beschrijvingen het eens moeten zijn.
5. `source_ddl_sha256` gelijk aan de live DDL-hash van beide tabellen.
6. `session_analytics`: live kolomlijst gelijk aan `invariants.session_analytics.columns`; digest over `id <= max_id` gelijk aan `digest_sha256`; `rows` en `sum_id` gelijk. Een ontbrekend id is exit 3 met dat id erbij (B2-drift).
7. De live corpus-aantallen gelijk aan `manifest.corpus.tables[].rows`. Deze eis is strikt: het hervatten van een 0/0-store is geen permit-eis maar een aparte poort in `purge` (besluit 6).
8. `snapshot` aanwezig, een absoluut pad, `bytes == stat().st_size`, `sha256` gelijk (bestand gelezen), `integrity_check == "ok"` en openbaar read-only.
9. `db.bytes > 0` (decoy-poort) en de DB is niet 0 byte.

Ontbrekend, `None`, onparseerbaar of een onbekende waarde is nooit een pass: elk geval is exit 3 met de reden in de auditregel (fail-closed). Eis 5 is de gedragsmatige vervanging van een versiepoort: niet `user_version`, maar de **DDL van de twee tabellen** moet gelijk zijn aan wat het artefact zag.

**Sessie-invariant (B2).** Binnen de delete-transactie, in deze volgorde: `BEGIN IMMEDIATE` -> permit opnieuw op dezelfde connectie -> digest A over `session_analytics` -> `DELETE` van beide corpus-tabellen -> digest B -> **A == B**, anders `ROLLBACK` + exit 3. De implementatie neemt de digest over de hele tabel plus `rows` en `sum(id)`, wat strikter is dan de `id <= max_id`-digest van de permit. Daarna, buiten de transactie: `PRAGMA wal_checkpoint(TRUNCATE)`, `VACUUM`, nameting (0/0, `freelist_count == 0`, geen andere tabel gedaald, sessie-digest gelijk aan de baseline), auditregel. De vergelijking met de baseline (eis 6) gebeurt vóór de transactie; wijkt die af, dan is dat een andere schrijver en weigert de purge met de id's erbij. Het herstel is `baseline --reuse-snapshot`, één commando, geen nieuwe 442 MB-kopie.

**Exitcodes** (uitgegeven door `scripts/vnx_intelligence_corpus_quarantine.py:110-115`, uitgelegd in `docs/EXIT_CODES.md`):

| Code | Betekenis |
|---|---|
| 0 | klaar |
| 1 | onverwachte fout, of een mislukte auditregel in `verify` (daar is niets gecommit); zie stderr |
| 2 | verkeerd gebruik (argumenten; ook een `--out`-map die niet bestaat) |
| 3 | geweigerd: permit, decoy, invariant of manifest klopt niet, of een `ValueError` uit een weigering - **er is niets veranderd** |
| 4 | na de commit of het zegel: de na-controle, de `VACUUM`, een auditregel of een repetitie-verdict dat niet volledig waar is - het corpus is weg (of het artefact is verzegeld) en het artefact is het terugzetpad |

**Auditregel.** Elke stap schrijft één NDJSON-regel met `O_APPEND` + `fsync` naar `<state_dir>/intelligence_corpus_quarantine_audit.ndjson`: `ts`, `stage`, `permit_sha256` (sha256 over de bytes van `manifest.json` gevolgd door die van `quarantine.json`), `status` en de gemeten getallen. De stadia zijn `baseline`, `snapshot`, `quarantined`, `verified`, `deleted`, `vacuumed`, `restored`, `rehearsed`; de statussen `intent`, `ok`, `rolled_back`, `refused`. Invariant: de intent-regel staat vóór de eerste schrijfactie, de uitkomst erna; een lezer telt een stadium alleen op `ok`, en een intent zonder uitkomst voor dezelfde `permit_sha256` is een meting, geen aanname. `baseline` schrijft in de gemergede code geen auditregel (het subcommando heeft geen `--state-dir`); de reeks begint bij `quarantined`. De auditregel staat **nooit** in de verzegelde map (B4); een audit-map binnen de permit-map is exit 3.

**Layout en zegel (B4), zoals de code hem schrijft.**

```
~/.vnx-data/_quarantine/intelligence-corpus-<YYYYMMDD>[-n]/    0755   (permit-map; de tool chmodt hem niet)
  LEESMIJ.md                                                   0444   (het terugzetcommando, na het zegel geschreven)
  manifest.json                                                0600   (atomic_write_text; os.replace bij een her-baseline)
  artifact/                                                    0555   (verzegeld)
    quarantine.json                                            0444
    quality_intelligence-corpus-<YYYYMMDD>.db                  0444
~/.vnx-data/_backups/<UTC>-intelligence-corpus-quarantine/vnx-dev/quality_intelligence.db   (snapshot, 0400)
$VNX_STATE_DIR/intelligence_corpus_quarantine_audit.ndjson                                  (schrijfbaar)
```

Volgorde in `quarantine`: schrijven -> `fsync` -> `chmod 0444` op de bestanden -> `chmod 0555` op `artifact/`. Nooit omgekeerd, en nooit 0555 op de permit-map: een her-baseline moet `manifest.json` kunnen vervangen. `quarantine --out` weigert (exit 3) als `artifact/` al bestaat: een artefact wordt nooit overschreven en nooit verwijderd, met geen enkele glob (besluit 22).

**Fail-closed.** Een leesfout is een fout (exit 1 of 3, nooit "0 rijen"): `measure_baseline` laat I/O- en SQL-fouten door en `Refusal` dekt elke permit-weigering. Een mislukte auditregel **na** de commit is exit 4 met de mededeling dat de regel niet geschreven is. De tool neemt het fail-open patroon van `vnx_db_maintenance` (`_write_audit_record` slikt een schrijffout, `_count_prunable` geeft `0` bij een `OperationalError`) niet over.

### De 22 besluiten

1. **Verplaatsen, niet wissen; het artefact is een corpus-artefact, geen kloon van de hele store.** Een volledige kloon als "move" zou bij terugzetten ook `session_analytics` terugdraaien en dat is verlies in de andere richting.
2. **Het artefact bevat de twee tabellen en hun rijen, zonder enige index.** De vier `idx_snippet*`-indexen worden bij het terugzetten door de doeltabel onderhouden; ze meenemen kost circa 38 MB voor niets. De FTS5-index valt onder besluit 19.
3. **De back-up van clausule 1 is een verse `VACUUM INTO`-snapshot, niet een van de vier bestaande kopieën.** `baseline` maakt hem zelf en legt `bytes`, `sha256` en `integrity_check` van de snapshot vast; de bestaande kopieën zijn `copy2` op een WAL-store en hun consistentie is nooit bewezen.
4. **Rijen, nooit tabellen.** De structuurcontrole kijkt naar namen en de monitor eist de tabellen en kolommen; rijen verwijderen houdt al die controles groen.
5. **De tool weigert op bewijs, niet op een versienummer.** `user_version` wordt vastgelegd en niet als poort gebruikt, zodat de volgende migratie (D5 zet `HIGHEST_QI_VERSION` op 35) de tool niet om een niet-fout weigert; eis 5 (DDL-hash) is de gedragsmatige poort.
6. **Twee opgenomen fasen in één commando, hervatbaar alleen op bewijs.** De delete is één `BEGIN IMMEDIATE` ... `COMMIT`; daarna checkpoint en `VACUUM`. Een tweede run die 0/0 aantreft rondt de `VACUUM` af **als en alleen als** de auditregel `deleted`/`ok` met dezelfde `permit_sha256` bestaat; zonder die regel is 0/0 een corpus dat iemand anders heeft leeggemaakt en is het exit 3.
7. **De sessie-invariant staat in de transactie, niet op de wandklok.** Een digest zonder transactie flaket op een levende schrijver en een `COUNT(*)` ziet een in-place edit niet.
8. **De proef-terugzetting draait op een kopie van de live store, met dezelfde codepaden.** Een fixture van drie rijen bewijst de code, niet de data; `rehearse` doorloopt baseline, quarantine, verify, purge en restore met dezelfde functies.
9. **De leeg-corpusfix is één functie, en de fix liegt niet.** `_verify_pattern_quality` benoemt de lege stand in plaats van `0,0 avg quality` te printen, want `AVG` is NULL op een lege tabel en een verzonnen meting zou de dagelijkse digest voeden.
10. **`check_intelligence_health.py` blijft rood (exit 11) en dat wordt opgeschreven, niet gerepareerd.** Bij 0 patronen is `unhealthy` de waarheid; de gezondheidscheck aan een quarantainemanifest koppelen geeft hem een tweede bron van waarheid, en er is geen scheduler die hem draait.
11. **Repopulatie wordt benoemd, niet afgedwongen.** `VNX_DAILY_INTEL_REFRESH` (default `1`) plus `_refresh_quality_intelligence` kunnen het corpus opnieuw vullen; er is vandaag geen geplande run, dus de `pattern_count=0`-meting wordt in de na-meting van de purge zelf genomen.
12. **Het ADR-nummer wordt bij de bouw genomen, niet in het plan.** De bouwer leest `git ls-tree origin/main docs/governance/decisions/`; `tests/test_adr_numbers_unique.py` en `scripts/lib/merge_preflight_adr_check.py` bewaken de botsing.
13. **Geen `bin/vnx`-subcommando.** De tool is operatorgereedschap; `bin/vnx` wordt door zes open plannen van deze sprint aangeraakt. Precedent: `scripts/vnx_db_maintenance.py`.
14. **Geen schemawijziging; ADR-007 bindt niet.** Er komt geen tabel bij in de centrale DB; de `project_id`-drift in 16 live tabellen is een andere track en blijft een OI.
15. **De permit bewijst exacte gelijkheid, niet aantallen (B1).** De inhoudsdigest over `rowid` plus alle kolommen, met dezelfde functie aan beide kanten en de bronzijde herberekend **binnen** `BEGIN IMMEDIATE`, sluit verwisselde of beschadigde rijen uit. Het artefact draagt geen index, dus bewijst `quarantine` het terugzetpad door de rijen in een wegwerp-FTS5 opnieuw te indexeren en te zoeken (`restore_proof.match_probe`); `restore` herhaalt dat op de doeltabel.
16. **De sessie-invariant dekt alle kolommen en is transactie-gebonden (B2).** Een digest over `id|session_date` ziet een gewijzigde `deep_analysis_json` of `project_id` niet; de kolomlijst gaat mee in het manifest, zodat een schemawijziging als "kolommen gewijzigd" weigert en niet als "data gewijzigd" liegt.
17. **Eén contractmodule voor schrijver en lezer (B3).** Twee modellen van hetzelfde document veroorzaakten de schrijver/lezer-drift; één module met één schema en één validator is de fix, en de test schrijft met de schrijver en leest met de lezer.
18. **Zegel 0555 op de artefactmap, 0444 op de bestanden, permit-map 0755 (B4).** Een 0444-map geeft `PermissionError` op `open()` en `os.stat()`; 0555 leest en somt wel en is niet schrijfbaar. Het zegel op de permit-map zou een her-baseline onmogelijk maken, en dat is juist het herstel voor B2-drift.
19. **Het artefact draagt inhoud, geen index.** `PRAGMA integrity_check` en de FTS5-`integrity-check` melden een verwijderde `_data`- of `_idx`-rij niet, dus een index in het artefact is een belofte die de check niet kan nakomen. De tabellen hebben dezelfde kolomnamen **zonder gedeclareerd type** (affiniteit BLOB), zodat elke waarde zijn opslagklasse houdt (gemeten: `TEXT` maakt van `42` de tekst `'42'`); het artefact is daarmee circa 330 MB in plaats van circa 407 MB.
20. **`restore` bewijst zichzelf en draait terug bij twijfel.** Na het invoegen herberekent `restore` de digest op de doeltabel en vergelijkt hem met `quarantine.json`; ongelijk -> `ROLLBACK`, exit 3, niets veranderd. Daarna een `MATCH`-probe op een vaste steekproef (de eerste 20 rowids, de laatste 20 en de rijen waar `rowid % 9973 == 0`), want de index is bij het invoegen opnieuw opgebouwd en moet ook echt vinden.
21. **`verify` is een eigen subcommando en de groene knop vóór de purge.** De operator ziet het permit-bewijs zonder de delete in de buurt; `purge` roept dezelfde functie aan, dus er is geen tweede implementatie en geen "vergeten te verifiëren".
22. **Een tweede quarantaine krijgt een nieuwe map; niets wordt ooit overschreven of verwijderd.** Het artefact is na de purge het enige terugzetpad (de nachtelijke rotatie ruimt de vier grote kopieën binnen drie nachten op); `quarantine --out` weigert op een bestaande `artifact/` en geen codepad verwijdert een artefact.

## Consequenties

- **Het corpus is omkeerbaar weggezet, niet gewist.** Na de purge staan dezelfde 192.495 rijen met dezelfde rowids en opslagklassen in het artefact; `restore` zet ze in één transactie terug en bewijst dat met een digest en een `MATCH`-probe vóór de commit.
- **Een verkeerde run verandert niets.** Elke weigering is exit 3 vóór de eerste schrijfactie; de enige weg naar een gewijzigde store loopt door de permit, en de sessietabel wordt binnen dezelfde transactie bewaakt.
- **Na de commit is er geen twijfel meer over de terugweg.** Exit 4 betekent: het corpus is weg en het artefact in de genoemde map is het terugzetpad. De operator ruimt het artefact nooit op in code en nooit met een glob.
- **De gezondheidscheck blijft rood na de purge.** Dat is de verwachte, gedocumenteerde toestand (besluit 10) en geen fout van de quarantaine; de runbook noemt het expliciet.
- **Het corpus kan opnieuw groeien.** `VNX_DAILY_INTEL_REFRESH` staat default aan (besluit 11); een tweede quarantaine is een nieuwe map (besluit 22) en de meting van de eerste blijft staan.
- **De documentatie is de bron voor de operator, de code voor het bewijs.** Waar de gemergede code van het plan afwijkt (de `baseline`-auditregel, de bestandsmodi van manifest en snapshot, de strikte eis 7, de status- en verdictvelden, de `--state-dir`- en `--reuse-snapshot`-vlaggen), beschrijft dit ADR de code; de afwijkingen staan in het dispatchrapport van Q8.

## Wat dit ADR niet doet

Het wijzigt geen code en geen store. Het draait de quarantaine niet: dat is de operatorstap in `docs/operations/INTELLIGENCE_CORPUS_QUARANTINE.md`. Het brengt `check_intelligence_health.py` niet in orde, koppelt hem niet aan een manifest, en voegt geen `bin/vnx`-subcommando, geen schemawijziging en geen vierde opslagvorm toe. De andere vier projectstores blijven buiten acceptatie.
