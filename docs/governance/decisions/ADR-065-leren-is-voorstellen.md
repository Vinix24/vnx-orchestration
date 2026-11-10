# ADR-065 - Leren is voorstellen: faalklassen, telcontract, besluitrecord, activering en verdict

**Status:** Voorgesteld 10-11-2026 (T0, track `self-learning-loop-reactivate`, rij D0); aangenomen zodra de T0 PR-D0 merget.
**Datum:** 2026-11-10
**Besloten door:** de operator op 07-11-2026 (besluit 2 en 3, hieronder letterlijk) en het plan `claudedocs/2026-11-07-naar-1-9/plannen/self-learning-loop-reactivate.md` (revisie 2, 08-11-2026, plan-gate van 08-11 en de bindende voorwaarde van de tiebreaker) voor de uitwerking. ADR-065 was vrij op `origin/main` `2656026d9` (10-11-2026): de hoogste bestaande is ADR-063, ADR-064 is gereserveerd voor deltareview DR-0 en 066 tot en met 070 voor andere G3-ADR's.
**Cross-refs:** ADR-007 (samengestelde sleutel over `project_id`), ADR-045 (één startregel, één eindregel, één sweep), ADR-046 (één poortmotor), ADR-052 (geen functie boven 200 uitvoerbare regels), ADR-054 (het governance-record van de envelope is geen eindregel), ADR-056 (zelfstandigheid als meetbaar begrip), ADR-063 (dagelijkse doorloopmeting: dezelfde scheiding tussen voorstel, goedkeuring en oordeel).

De operator heeft op 07-11-2026 besloten, onder meer:

> **2.** zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; leren en dreaming blijven voorstellen.
>
> **3.** Learning-tracks ontparkeren zodra ledger-is-falsifiable gemerged is; corpus-quarantaine omkeerbaar (verplaatsen, niet wissen).

Dit ADR legt vast waartegen de tien code-rijen D1 tot en met D9 van dit track bouwen: wat een faalklasse is, hoe één failure per dispatch telt, welke vier toestanden een patroon kent, wie een besluit neemt en waar dat besluit staat, waar een activering woont, en wanneer de lus met een verklaarde reden stopt. Het bevat geen code en geen receipt-formaatwijziging; alles erin is een regel over data. Leren blijft voorstellen: geen enkele regel in dit ADR laat een voorstel zichzelf activeren.

**Deterministisch, en waarom er geen model in zit.** Alles wat dit ADR vastlegt is deterministisch: het klassevocabulaire met zijn geordende tabel, de twee telinvarianten, de vier toestanden, de vorm van het besluitrecord, de tabel `pattern_activation` en het verdict `ledger_verdict/1` met zijn zeven regels. Elk van die delen is een pure functie of een vaste tabel en daarom testbaar en reproduceerbaar op fixtures. Een model heeft hier geen rol: classificatie is een gesloten lijst voorwaarden op velden die andere modules al schrijven, en een verdict is een telling met vaste drempels. Er is geen open natuurlijke taal die om oordeel vraagt. De enige vrije tekst is wat de operator bij een besluit typt (`--reason`, `--effect-criterion`, `--stop-condition`); die wordt opgeslagen en getoond, nooit geïnterpreteerd. De veiligheidsnetten zijn daarom niet "een model dat het goed bedoelt", maar een gesloten vocabulaire waarin een onbekende vorm zichtbaar naar `failed` of `unclassified` valt, en een verdict dat bij elke twijfel `refused` zegt.

## Context

**1. De lus draait elke nacht, maar niets wat hij vindt kan een mens besluiten.** De nachtelijke beacon van 08-11T01:11Z staat op `mode: shadow`, `status: ok`, met 43.025 gelezen receipts, 10.622 na de cutoff, 32 failure-patronen en 5 voorstellen. Die 5 voorstellen staan alleen in `learning_loop_shadow_report.json`; `pending_rules.json` bestaat in 0 van de 5 project-state-mappen. De oorzaak is een ontwerpkeuze: `update_terminal_constraints` (`scripts/learning_loop.py:907`) keert in shadow-modus terug vóór de wachtrij-write (`:915`), `persist_enabled()` is standaard onwaar (`:147-149`) en de nachtelijke wrapper zet de variabele niet (`scripts/conversation_analyzer_nightly.sh:320-325`, `scripts/learning_loop_nightly.py:6`). De code zegt het zelf (`scripts/learning_loop.py:319-322`): de voorsteltier heeft nooit één `pending_rules.json` geproduceerd.

**2. De meeteenheid is fout, dus het doel is onmeetbaar.** Er is geen faalklasse. `extract_failure_patterns` (`scripts/learning_loop.py:572`) projecteert alleen `task`/`terminal`/`agent`/`error`/`timestamp` (`:689`), `_failure_error_message` valt terug op `"Unknown error"` (`:819`) en `generate_prevention_rules` groepeert op `(error[:50], terminal, agent or 'none')` (`:865`) met drempel `len(failures) >= 2` (`:870`). Gemeten op het echte grootboek (zonder project- of provenance-filter, vanaf 2026-11-06T01:13:09): 67 failure-receipts over 42 dispatch-id's. **25 dispatches hebben twee receipts**: `dispatch_finished` met status `failure` en `subprocess_completion` met status `failed`, en in **25 van 25** paren staat de foutentekst alleen op de `subprocess_completion`-regel. Een regel "komt 2x voor" kan dus één dispatch zijn, en de eindregel alleen kiezen levert elke keer `Unknown error`. De meest voorkomende klasse is `PHANTOM:` (25 van 42 dispatches: status `success` terwijl de worktree-diff leeg is), daarna `kimi-cli: [quota_or_auth]` (4 receipts).

**3. Er is geen operator-oppervlak en geen activeerbare identiteit.** `bin/vnx` heeft geen `learning)`-arm (`suggest)` staat op `:2693`, `dream)` op `:2777`), dus `vnx learning status` eindigt op "Unknown command". `_cmd_status` (`vnx_cli/commands/learning.py:59`) toont zes tellers die in deze state nul zijn, en `approve`/`dismiss` (`:636`) werken alleen op `pending_archival.json`. Voor `pending_rules.json` bestaat geen werkwoord. `ingest_approved_rules` (`scripts/learning_loop.py:1064`) schrijft goedgekeurde regels rechtstreeks in `prevention_rules`, zonder `activation_id` (`:1106`: de `INSERT` noemt geen `project_id`, terwijl dat kolom `NOT NULL` is), zonder nulmeting, zonder stopconditie en zonder herstelpad.

**4. De toets bestaat grotendeels, maar wordt door niemand afgedwongen.** `attempt_verdict` (`scripts/lib/ledger_attempt_state.py:95`) kent de soorten `legacy`/`ended`/`live`/`unknown`/`dead_no_end` op sleutel `(project_id, dispatch_id, attempt)`, en `compute_lifecycle_count` (`scripts/lib/lifecycle_count.py:554`) telt vijf defecten over een cohort via `activation_commit`. Er bestaat geen `ledger_source_check` die daaruit één getypeerd verdict maakt; `ledger-is-falsifiable` sloot zonder hem (#2469, #2473, #2478). Vandaag geeft het cohort `all_v3` 3 einde-zonder-start en 1 `projection_conflict`; op cohort `7418c877b` is het totaal 0. Zonder verdict leert de lus dus door op een grootboek dat zichzelf tegenspreekt.

**5. Driehonderd lezers, één beetje waarheid.** `prevention_rules` wordt op negen plekken gelezen die inhoud teruggeven. Alleen `failure_prevention.py:189` filtert op `valid_until`; de andere acht zien elke rij. Er is geen veld dat zegt of een patroon is geactiveerd en geen enkele lezer gehoorzaamt een deactivering.

**6. De oude regels zijn niet stabiel.** De 20 bestaande `prevention_rules`-rijen zijn alle `source='behavior_analysis'`, `project_id='vnx-dev'`, `valid_until` NULL, en `scripts/lib/pattern_extractor.py:301` wist en herschrijft ze elke nacht. Een activeringsrij op zo'n `id` zou na één nacht naar een andere regel wijzen.

## Besluit

### 1. Faalklasse is een eersteklas veld uit één geordende tabel

Reden: zonder klasse is het doel onmeetbaar (Context 2). De classificatie is deterministisch en heeft geen model nodig. Het contract is één geordende tabel in `scripts/lib/learning_failure_class.py` (nieuw in D1), toegepast op de samengevoegde dispatch-groep van besluit 2, niet op een los receipt. Gesloten vocabulaire `FAILURE_CLASSES`: `contract_invalid`, `contract_violations_empty`, `phantom`, `timeout`, `provider_quota`, `review_failed`, `budget`, `failed`, `unclassified`. De rijen, bovenaan eerst:

| rang | voorwaarde | klasse |
|---|---|---|
| 1 | `is_contract_invalid` (`scripts/lib/contract_invalid_window.py:40-46`) op een receipt van de groep: een niet-lege `contract_violations`-lijst | `contract_invalid` |
| 1 | idem, maar de lijst is leeg | `contract_violations_empty` |
| 2 | `failure_reason` begint met `PHANTOM:` | `phantom` |
| 3 | `status == "timeout"` of `failure_reason` begint met `deadline exceeded` | `timeout` |
| 4 | `failure_reason` bevat `[quota_or_auth]` | `provider_quota` |
| 5 | `event_type == "review_gate_result"` | `review_failed` |
| 6 | `failure_reason` bevat `budget` (kleine letters) | `budget` |
| 7 | `status` in `_FAILURE_STATUSES` (`scripts/learning_loop.py:48`) | `failed` |
| 8 | anders | `unclassified` |

Een ontbrekend, `None`, onleesbaar of onbekend veld valt door naar een lagere rij en sluit nooit een receipt uit; de uitkomst is altijd een waarde uit `FAILURE_CLASSES`. De prefix- en substringregels hangen aan tekst die andere modules schrijven, dus een nieuwe tekstvorm valt naar `failed` of `unclassified` en blijft als dekking zichtbaar. Elk `receipt_stats`-record draagt `class_counts` met alle negen klassen, ook de nul.

Eligibiliteit is een tweede, even gesloten tabel: `ELIGIBLE_CLASSES` = `contract_invalid`, `phantom`, `timeout`, `provider_quota`, `review_failed`, `budget`, `failed`. `contract_violations_empty` (3.326 legacy-rijen; een voorstel kan daar niets aan verhelpen) en `unclassified` (geen inhoud) worden geteld en nooit een regel. Een groep met één dispatch is geen voorstel.

### 2. Eén failure per dispatch, met twee telinvarianten

Reden: 25 van 25 paren dragen de tekst alleen op `subprocess_completion` (Context 2). De groepeersleutel is `dispatch_id` alleen. Per groep zijn de identiteitsvelden (`task`, `terminal`, `agent`, `timestamp`) die van de receipt met de hoogste rang (`dispatch_finished` boven `subprocess_completion` boven `task_complete` boven `review_gate_result`); de bewijsvelden (`failure_reason`, `error`, `message`, `detail`, `terminal_response`, `contract_violations`) zijn per veld de eerste niet-lege waarde in diezelfde rangorde. Een receipt zonder `dispatch_id` blijft een eigen groep met sleutel `receipt:<regelnummer>` en telt apart. De dedupe hoort in de extractie, niet in de presentatie.

`receipt_stats` krijgt vijf velden: `failure_receipts` (receipts die alle bestaande filters passeren), `dispatches` (distincte `dispatch_id`'s daaronder), `without_dispatch_id`, `merged_duplicates` en `class_counts`. Twee invarianten gelden samen en altijd:

- `len(failure_patterns) == dispatches + without_dispatch_id`
- `failure_receipts == len(failure_patterns) + merged_duplicates`

`generate_prevention_rules` groepeert op `(failure_class, error[:50], terminal, agent or 'none')`, dus de drempel `>= 2` telt twee **dispatches**. `occurrence_count` is dat aantal; het voorstel draagt `failure_class` en `source_dispatch_ids` (gesorteerd, maximaal 20). De nulmeting van een voorstel is één definitie: `baseline_metric = "dispatches_with_class:<failure_class>"` en `baseline_value = occurrence_count` op het moment van het voorstel. Er is geen tweede meetpad. Rust op `scripts/learning_loop.py:572` (extractie), `:645` (statusfilter), `:689` (projectie), `:819` (fallback) en `:865`/`:870` (groepering en drempel).

### 3. Vier toestanden, en een voorstel activeert zichzelf nooit

Een patroon kent precies vier toestanden: **voorstel**, **goedgekeurd**, **geactiveerd**, **gedeactiveerd**. De wachtrij kent daarnaast de eindtoestand `rejected` voor een voorstel dat is afgewezen; dat is geen toestand van een patroon maar het einde van een voorstel. De projectie in `pending_rules.json` gebruikt `pending` voor voorstel, `approved` voor goedgekeurd, `ingested` voor geactiveerd en `deactivated` voor gedeactiveerd; het besluitrecord (besluit 4) is de waarheid en de projectie volgt.

De overgangen zijn eenrichting: voorstel → goedgekeurd of afgewezen; goedgekeurd → geactiveerd; geactiveerd → gedeactiveerd. Alleen een nieuw besluit `approved` met een nieuwe `approval_id` zet een gedeactiveerd patroon weer aan. Een besliste regel keert nooit naar `pending` terug; alleen de bewijsvelden van een nog open voorstel groeien.

**Een voorstel activeert zichzelf nooit.** Geen code-pad schrijft een databaserij op grond van de wachtrij alleen. `update_terminal_constraints` (`scripts/learning_loop.py:907`) blijft in shadow terugkeren vóór elke write (`:915`), en de nieuwe queue mode (`VNX_LEARNING_LOOP_QUEUE=1`) is shadow plus de operator-wachtrij: hij schrijft `pending_rules.json` en nooit een databaserij. Zonder de variabele is het gedrag byte-identiek aan vandaag, zodat de twee bestaande tests die het shadow-contract vasthouden ongewijzigd groen blijven. De nachtelijke run herstelt niets: hij schrijft geen besluit en zet geen `active`-vlag. Het commentaar in `scripts/learning_loop_nightly.py:6` ("persists nothing") is met queue mode verouderd; die tekstcorrectie rijdt mee met `learning-capture-fragmentation` PR-3, dat het bestand al bezit.

### 4. Een besluit is een record met een geminte `approval_id`

Reden: de bindende voorwaarde van de tiebreaker. Het werkwoord heet `vnx learning decide-rule` (niet `approve`, dat bestaat al voor archivering met een door de operator gegeven token, `vnx_cli/commands/learning.py:636`). Het werkt op één of meer herhaalbare `--rule-id` in één handeling, met `--decision approved|rejected`, `--reason` en `--by`. Elke andere waarde van `--decision` is exit 2. Bij `approved` zijn `--effect-criterion` en `--stop-condition` verplicht (exit 2 als ze ontbreken). De deur krijgt een `learning)`-arm in `bin/vnx` (naast `suggest)` op `:2693` en `dream)` op `:2777`); de subparser staat al (`vnx_cli/main.py:1416`).

Het werkwoord schrijft per regel één regel in `state/prevention_rule_decisions.ndjson` (append-only) met: `decision_id`, `approval_id`, `bundle_ref` (alleen bij `approved`), `rule_id`, `proposal_hash`, `decision`, `reason`, `decided_by`, `decided_at`, `project_id`, `rollout_scope`, `failure_class`, `baseline_metric`, `baseline_value`, `effect_criterion`, `stop_condition`, `recovery_path`. `approval_id` is `apr-` plus de eerste 12 hex van `sha256(project_id|rule_id|decision|decided_at|decided_by)`; `bundle_ref` is `bundle-` plus de eerste 12 hex van `sha256` over de gesorteerde `rule_id`'s van die ene aanroep en `decided_at`. Eén handeling is dus één bundel. `recovery_path` is de vaste tekst `scripts/deactivate_pattern_activation.py --activation-id <id> --reason <tekst>`.

**De store is de enige producent en de enige schrijver.** `scripts/lib/prevention_rule_store.py` (nieuw in D2, uitgebreid in D3) mint `approval_id` en `bundle_ref` in `mint_approval_id()` en `decide()`; een test greept dat geen andere module een `apr-`- of `bundle-`-string formatteert. D5 en D9 lezen uitsluitend dit record via `approved_decision(...)` en vragen geen metadata die het record niet draagt. Het protocol: alle schrijfacties onder `fcntl.flock` op `state/.prevention_rules.lock`; `pending_rules.json` atomair (tijdelijk bestand, `os.replace`); `rule_id` is `rule-` plus 8 hex van `sha1(pattern|terminal|failure_class)`; het record is de waarheid en de `status` in het JSON-bestand een projectie; een besluit wordt eerst als regel toegevoegd en gefsynct en daarna in de projectie gezet; valt het proces ertussen om, dan zet `store.reconcile()` de projectie bij aan het begin van elke lees- of schrijfroute. Een regel met `status: "approved"` zonder bijbehorend besluit wordt niet geïngest en krijgt de reden `approved_without_decision`.

### 5. Een activering woont in `pattern_activation`, met PK `(project_id, activation_id)`

Reden: golf-gat 7 (opgeslagen patronen krijgen een identiteit die in één commando deactiveert; een scheduler uitzetten is geen herstel). Eén nieuwe centrale tabel in `quality_intelligence.db`, met ADR-007 als bindende regel: samengestelde PK `(project_id, activation_id)`. Er zijn vijf van die databases in de vloot, één per project.

Kolommen: `project_id`, `activation_id`, `rule_row_id` (de `prevention_rules.id`), `rule_id` (de wachtrij-id), `decision_id`, `approval_id`, `bundle_ref`, `rollout_scope`, `baseline_metric`, `baseline_value`, `effect_criterion`, `stop_condition`, `recovery_path`, `activated_at`, `active` (0 of 1, met CHECK), `deactivated_at`, `deactivation_reason`, `deactivation_approval_id`. Index op `(project_id, rule_row_id, active)`.

`activation_id` is `act-` plus de eerste 12 hex van `sha256(project_id|approval_id|rule_id)`: deterministisch, dus een herhaalde ingest raakt dezelfde PK en maakt geen tweede rij. De migratie is `_migrate_v35`, additief (`CREATE TABLE IF NOT EXISTS`) en volgt het patroon van `_migrate_v34` (`scripts/quality_db_init.py:1698`) en de `MIGRATIONS`-tabel (`:1727`); `HIGHEST_QI_VERSION` gaat van 34 (`:34`) naar 35 en de tabel komt in `verify_database_structure`. De migratie staat in `scripts/quality_db_init.py` en niet in `schemas/` (een `schemas/`-wijziging tilt de PR naar risico `high` met twee stoelen).

Het activeringsregime geldt alleen voor rijen met `source='learning_loop'`, die D9 schrijft. De 20 bestaande `behavior_analysis`-rijen hebben geen activeringsrij, blijven zichtbaar en veranderen niet; `scripts/lib/pattern_extractor.py:301` wist en herschrijft ze elke nacht, dus een activeringsrij op hun `id` zou na één nacht naar een andere regel wijzen. `deactivate_pattern_activation.py` op een legacy-regel is exit 1 met de verwijzing naar het bestaande archiveringspad. `project_id` wordt gestempeld via `resolve_stamp_project_id` (`scripts/lib/project_scope.py:80`, faalt dicht bij een onoplosbare tenant); `rollout_scope` is vast `project` en `validate_activation` weigert elke andere waarde. De lezerspredicate koppelt op `prevention_rules.project_id`, dus een activering in project A is onzichtbaar in B.

**Deactiveren is één commando en blijft.** `scripts/deactivate_pattern_activation.py --activation-id X --reason "..."` zet `active=0` met tijd en reden en schrijft via de store een besluitregel `decision: "deactivated"` met een eigen `approval_id`. Durable betekent: (i) een herhaalde ingest raakt dezelfde `activation_id` en activeert niet opnieuw; (ii) de wachtrij-regel krijgt status `deactivated` en komt niet terug als `pending`; (iii) alleen een nieuw `decide-rule --decision approved` (nieuwe `approval_id`, dus nieuwe `activation_id`) zet het patroon weer aan; (iv) de nachtelijke run herstelt niets.

### 6. Het verdictcontract `ledger_verdict/1`, en de lus stopt op een afgewezen verdict

Reden: de lus mag niet leren op een grootboek dat zichzelf tegenspreekt (Context 4), en het golfplan wees `ledger_source_check` aan dit track toe. De toets is een dunne, getypeerde projectie van `attempt_verdict` (`scripts/lib/ledger_attempt_state.py:95`) en `compute_lifecycle_count` (`scripts/lib/lifecycle_count.py:554`), geen tweede lezer. Uitvoer: `{schema: "ledger_verdict/1", evaluated_at, project_id, cohort, status, reason_code, reason, counts}` met `status` precies `ok`, `pending` of `refused`. De regels, bovenaan eerst:

1. `compute_lifecycle_count.status != "measured"`: `refused`, reden `unmeasured`.
2. `end_without_start > 0`, `double_end > 0`, `projection_conflict > 0` of `provenance_unknown > 0`: `refused` met die reden.
3. sweep-hartslag onbekend of ouder dan 600 s: `refused`, reden `processor_stalled`.
4. `source=pytest`-ruis (via `receipt_outcome.noise_reason`) onder de receipts die de lus als leermateriaal telt: `refused`, reden `noise_in_cohort`.
5. nul `ended`-pogingen in het cohort: `refused`, reden `empty_cohort`.
6. `start_without_end > 0` waarvan elke poging `live` of `unknown` is en jonger dan 24 uur: `pending`. Een `dead_no_end` of een poging ouder dan 24 uur: `refused`.
7. anders: `ok`.

Een onbekende waarde, ontbrekend veld of onleesbare input op welke plek ook geeft `refused` met de reden, nooit `ok`. Dispatches zonder `v3`-startregel (`legacy`) tellen niet mee als bewijs en zijn nooit een reden voor `refused`. Het cohort is `VNX_LEDGER_ACTIVATION_COMMIT`, hetzelfde begrip als `ledger_health.py --activation-commit` (`scripts/ledger_health.py:729`); zonder die variabele is het cohort `all_v3`. De producent (`scripts/lib/learning_ledger_gate.py`, nieuw in D6) schrijft `state/learning_ledger_verdict.json` atomair; `read_verdict` geeft bij elk gebrek een `refused`-dict met `reason_code: "verdict_unreadable"` terug in plaats van een uitzondering.

**De lus stopt op een afgewezen verdict.** In queue mode en in persist mode roept `daily_learning_cycle` (`scripts/learning_loop.py:1404`) het verdict aan vóór de extractie. `refused` geeft `{"status": "refused", "reason": "ledger_source_check: <reason_code>"}` plus een beacon-hartslag met `status: "degraded"` en `details.reason`; `vnx_doctor` (`scripts/vnx_doctor.py:945`) waarschuwt op `degraded` en faalt niet. `pending` loopt door, maar de lopende dispatches tellen niet mee als bewijs of als voorstel. Het grootboek wordt bij elke run opnieuw gelezen, dus herstel naar een groen verdict is automatisch en vraagt geen handwerk. Shadow zonder queue mode blijft ongewijzigd, omdat die niets schrijft. Een `refused` verdict blokkeert ook de ingest (D9): activeren zonder groen of `pending` verdict is niet toegestaan.

**De lezers gehoorzamen dezelfde toestand.** `scripts/lib/pattern_activation.py` biedt `injectable_clause(conn, state_dir, now) -> (sql, params)`. Een rij met `source IS NOT 'learning_loop'` is altijd leesbaar (legacy). Een rij met `source = 'learning_loop'` is alleen leesbaar als (a) er een activeringsrij is met `active = 1` voor `(project_id, rule_row_id)` én (b) het verdictbestand bestaat, geldig is, jonger dan 36 uur en `status` precies `ok` of `pending` heeft. Ontbrekend, onleesbaar, verouderd, `refused` of een onbekende waarde sluit de loop-regels uit en laat legacy staan; ontbreekt de tabel `pattern_activation` (oudere database), dan sluit de predicate loop-regels uit. Dat is de fail-closed regel van dit ADR: geen bewijs betekent geen injectie. Leesplekken die de predicate krijgen: `scripts/lib/intelligence_sources/failure_prevention.py:189`, `scripts/cached_intelligence.py:299`, `scripts/gather_intelligence.py:2118`, `scripts/build_t0_quality_digest.py:273`, `scripts/tag_intelligence.py:617` en `:637`. Bewust zonder predicate, met de reden in de census-allowlist: `scripts/lib/dispatch_enricher.py:342` (de literal `source='behavior_analysis'` sluit loop-regels al uit), `scripts/check_intelligence_health.py:201` (`COUNT(*)`), `scripts/lib/intelligence_dashboard_data.py:145` (operatorweergave toont alle rijen), `scripts/learning_loop.py:1099` en `:1362` (interne dedupe en archiveringskandidaten, geen injectie). Een census-test scant de repo op `FROM prevention_rules` en faalt voor elke plek die niet in de allowlist staat en de predicate-markering mist.

### Wat niet verandert

- **Geen receipt-formaatwijziging.** `scripts/lib/report_to_receipt_converter.py` en `scripts/lib/receipt_outcome.py` zijn `gate-merge`-paden (`scripts/lib/smart_router.py:1039` en `:1043`). Dit track importeert `receipt_outcome` alleen en raakt die paden niet aan.
- **`dispatch_governed` blijft geen eindregel** (ADR-054); de envelope-recordvorm verandert niet.
- **`prevention_rules` krijgt geen schemawijziging.** De enige nieuwe tabel is `pattern_activation`; de 20 legacy-rijen blijven byte-identiek en elke lezer geeft voor hen dezelfde uitvoer vóór en na D8.
- **Geen model, geen nieuwe poortmotor.** Er komt geen LLM-aanroep bij, geen tweede poortmotor (ADR-046) en geen functie boven 200 uitvoerbare regels (ADR-052).

## Consequenties

- **Positief:** één contract voor tien rijen, met de telinvarianten in het type in plaats van in een renderer. Een ontbrekende of onbekende vorm wordt luid (`unclassified` in `class_counts`) in plaats van stil "Unknown error".
- **Positief:** leren blijft structureel voorstellen. De enige weg naar een actieve rij loopt door een besluitrecord met een geminte `approval_id`, en deactiveren is één commando dat een volgende nacht niet terugdraait.
- **Positief:** de lus stopt met een verklaarde reden op een grootboek dat zichzelf tegenspreekt, en hervat automatisch zodra het verdict groen is. Elke lezer faalt dicht op een ontbrekend, verouderd of afgewezen verdict.
- **Negatief, en aanvaard:** de fail-closed predicate is streng. Een onleesbaar of verouderd verdictbestand onderdrukt alle `learning_loop`-regels in de hele vloot tot iemand kijkt; dat is de bedoelde prijs, maar het vraagt dat de doctor-waarschuwing op `degraded` niet wordt genegeerd.
- **Negatief, en aanvaard:** de migratie `_migrate_v35` is onomkeerbaar op de levende centrale database van 442 MB. Ze is additief (geen kolom-, geen rijwijziging) en wordt daarom op een kopie en met een back-up gedraaid; de terugweg is de bestaande back-uprotatie.
- **Negatief, en aanvaard:** de classificatie hangt aan tekst die andere modules schrijven. Een nieuwe tekstvorm valt naar `failed` of `unclassified`; dat is zichtbaar in de dekking, maar vraagt onderhoud van de tabel, niet van een model.
- **Negatief, en aanvaard:** de goedkeuringspoort is structureel, geen beveiligingsgrens — dezelfde grens als ADR-063 §6. Wie onder dezelfde OS-gebruiker in het operatorpaneel kan typen, kan een besluit nemen. Gevolg van misbruik is een geactiveerd patroon dat in één commando weer uitgaat.
- **Volgorde:** niets van dit alles is actief voordat D1 tot en met D9 gemerged zijn. D9, de eerste rij die iets activeert, hangt van D7 (verdict in de lus) en D8 (lezersguard) af; de lineaire keten op `scripts/learning_loop.py` staat in het plan.

## Wat dit ADR niet doet

Het lost de lege doelpopulatie `contract_invalid` niet op (die blijft een meetpunt in D4), het repareert de 20 legacy-regels niet, het kiest de cohort-epoch niet (dat is een operatorstap) en het bouwt geen code. Het bevat geen receipt-formaatwijziging en geen model. De tien rijen van het plan bouwen ertegen.
