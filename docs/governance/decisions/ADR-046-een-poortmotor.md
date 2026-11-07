# ADR-046 - Eén poortmotor: één uitkomst, één schrijver, drie adaptersoorten

**Status:** Accepted (dispatch dlv-0ffac24a3e57, track gate-engine-consolidation)
**Date:** 2026-10-24
**Amended:** 2026-11-07, zie Amendement 1 onderaan (dispatch dlv-d051cefd0f8c, track provider-agnostic-review-lane, P0)
**Decided by:** T0, onder het mandaat van de operator voor golf 1-3 van 24-10. De plan-gate van 24-10 gaf START met één vereiste wijziging: GE-6 loopt ná GE-4b, omdat het `gate_recorder.py` en `gate_executor.py` met serie A deelt.
**Cross-refs:** plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/gate-engine-consolidation.md` (revisie 2); inventaris `20261024-inv-poortmotor`; ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-035 (receipt v2); ADR-036 (modelidentiteit uit het register); ADR-037 (uitval is afwezigheid, geen tegenstem); ADR-041 (tiebreaker bij de review-cap); ADR-044 (de vorm van dit besluit); ADR-045 (het grootboek, concept van 15-10, nog niet op main).

## Context

Elke poort heeft vandaag zijn eigen weg naar buiten, en voor de harness-lanes is dat een eigen script. Op main (`3e55e06d`) telt de poortlaag 33.515 regels over 46 bestanden; op 3 september waren dat 19.918 regels over 37. `glm_gate.py` is 1.100 regels, `kimi_gate.py` 1.012 en `codex_final_gate.py` 826. De laag groeide dus, maar de vorm bleef: één motor zou het moeten zijn, met dunne adapters eromheen.

Het register (`GATE_PROVIDERS`, `scripts/lib/gate_recorder.py:95`) kent negen poorten, elk met een eigen weg naar buiten:

| Poort | Register | Weg van vandaag |
|---|---|---|
| `codex_gate` | `path_binary` `codex` | `GateRunner.run`, prompt op stdin |
| `gemini_review` | `path_binary` `gemini` | `GateRunner.run`, optioneel via Vertex |
| `claude_github_optional` | `path_binary` `gh` | `GateRunner.run` |
| `ci_gate` | `path_binary` `gh` | inline in `gate_executor.execute_gate` |
| `wiring_gate` | `path_binary` `gh` | `GateRunner.run` |
| `kimi_gate` | `harness_lane` `kimi` | `_make_default_dispatcher` via `provider_dispatch` |
| `glm_gate` | `harness_lane` `glm-harness` | idem |
| `deepseek_gate` | `harness_lane` `deepseek-harness` | idem |
| `claude_gate` | `harness_lane` `claude` | `claude_gate_lane`, niet `provider_dispatch` |

Het verschil tussen glm en kimi is groter dan een tabel met strings. Van de 13 gedeelde functienamen zijn er 2 byte-gelijk (`_get_diff` en `_frontmatter_duration_seconds`). De andere 11 wijken af, waaronder `_build_prompt`, `_extract_verdict` en `main`. Een mechanische samenvoeging levert geen shim op. De adapter moet die verschillen dragen en per provider één regel in het register hebben.

Eén poortuitkomst wordt op veel plekken geschreven. De inventaris telt 26 verschillende schrijfplekken over vijf artefacten:

| Artefact | Schrijvers | Waarvan onder een slot |
|---|---|---|
| result-record | 11 (8 bewaakt, 3 raw) | 8 |
| aanvraag | 5 (1 atomair) | 0 |
| `review_gate_result` | 3 emitters | append-lock alleen |
| verplichting | 1 | 0 |
| forge check-run | 1 | 0, bewust |

De drie ruwe result-schrijvers staan op `scripts/lib/gate_result_parser.py:97`, `scripts/lib/gate_result_parser.py:225` en `scripts/lib/gate_request_handler.py:2373`. Aan de aanvraagkant schrijven `scripts/lib/gate_recorder.py:545` (`persist_request`) en `scripts/lib/gate_request_handler.py:2248`, `:2361` en `:2889` met een kale `write_text`; `:1077` schrijft atomair maar zonder slot. De drie `review_gate_result`-emitters zijn `_emit_terminal_result_receipt` (`scripts/lib/gate_recorder.py:1704`), `_emit_claude_github_receipt` (`scripts/lib/gate_result_parser.py:112`) en `_emit_result_receipt` (`:234`). Na GE-4c-1 schrijven beide parser-schrijvers via `commit_gate_outcome` (`scripts/lib/gate_result_parser.py:128` en `:271`) en bestaan de twee parser-emitters niet meer: hun eigen velden gaan als `receipt_fields` mee met `_emit_terminal_result_receipt` (`scripts/lib/gate_recorder.py:1746`). De verplichting kent 1 schrijver, `scripts/lib/gate_obligations.py:270`.

Alleen de result-records hebben een slot en een overschrijfwacht. `write_result_guarded` (`scripts/lib/gate_recorder.py:1921`) houdt `slot_lock` (`scripts/lib/atomic_io.py:46`) en `_check_overwrite_guard` (`scripts/lib/gate_recorder.py:1085`). De test die een nieuwe ruwe schrijver moet vangen, kent 3 modules (`tests/test_oi1472_residual_guard_writers.py:357`); `gate_result_parser.py` en `gate_request_handler.py` staan er niet in. De drie ruwe schrijvers leveren bovendien geen `review_gate_result` op, dus een uitkomst die langs die weg binnenkomt staat niet in het grootboek.

De idempotentiesleutel draagt sinds #1767 (4 september) `status` en `commit_sha` (`scripts/lib/append_receipt_internals/idempotency.py:64` en `:65`). Wat open is, is de andere kant: de enige poort-emitter stempelt `head_sha` (`scripts/lib/gate_recorder.py:1781`) en geen enkele plek geeft `commit_sha` mee. De commit-as van de sleutel staat dus leeg, en twee uitkomsten met dezelfde status op twee commits vallen samen. Dat is geen sleutelprobleem meer maar een schrijversprobleem.

`codex_final_gate.py` heeft één productiefunctie: `enforce_codex_gate` (`scripts/codex_final_gate.py:176`), met één aanroeper (`scripts/closure_verifier.py:27`). De 569 regels daarna (258 tot 826) zijn alleen vanuit tests bereikbaar. Sinds GE-5b zijn `glm_gate.py` en `kimi_gate.py` shims van 77 regels die hun `main` aan `governed_lane.run_gate_head` geven; de dispatcher wordt daar gebouwd met `governed_lane.review_dispatcher`, dat de fabriek als argument krijgt. Sinds GE-5a staat `_make_default_dispatcher` in `scripts/lib/governed_lane.py:240` (INTERIM-markering op `:247`, daarvoor `scripts/lib/plan_gate_panel.py:1659`/`:1666`). `claude_gate` staat als harness-lane in het register maar loopt via `scripts/lib/claude_gate_lane.py:159`, niet via `provider_dispatch`, dat claude weigert. `ci_gate` en `wiring_gate` stonden als `path_binary` op `gh`; sinds GE-6 staan ze als `deterministic` in het register (`scripts/lib/gate_recorder.py:103`), met de `gh`-lezing in de aanroeper.

ADR-037 legde de leeskant al vast: een poort die niet gedraaid heeft is afwezigheid, geen tegenstem. Op vijf PR's met een volledig glm-PASS kon de merge niet door omdat een andere poort `not_executable` was. De schrijfkant heeft die twee soorten afwezigheid nog niet als aparte uitkomsten. Daar gaat dit ADR over.

## Besluit

Er komt één schrijver van een poortuitkomst, met één gesloten set uitkomsten, en per adaptersoort één vorm. De motor bezit wanneer en wat er geboekt wordt; de adapters bezitten alleen hoe een provider wordt aangeroepen en gelezen.

### 1. `GateOutcome` kent vier statussen

| Uitkomst | Betekenis | Terminaal | Uitspraak |
|---|---|---|---|
| `pass` | de poort keek en keurde goed | ja | ja |
| `fail` | de poort keek en keurde af | ja | ja |
| `unavailable` | de poort kon niet kijken: storing, quota, auth, lege uitvoer | nee | nee |
| `not_executable` | de poort kan hier niet draaien: niet geïnstalleerd, verkeerde omgeving | ja | nee |

Vier en niet twee, omdat twee statussen een storing als tegenstem moeten boeken. ADR-037 splitste dat al in twee assen: is de poging voorbij, en is er een uitspraak gedaan. `not_executable` is terminaal zonder uitspraak. `unavailable` is niet terminaal, want een herkansing op een gezonde provider kan nog beslissen. Alleen `pass` en `fail` zijn uitspraken.

De lezer `scripts/lib/gate_status.py` kent deze categorieën al (`PASS_STATES`, `FAIL_STATES`, `UNAVAILABLE_STATES`, `PARTIAL_REVIEW_STATES`, `is_terminal`). `GateOutcome` is de schrijversnaam van dezelfde vier; het is geen tweede lijst. Een lezer verandert dus niet mee.

Twee bestaande neerwaarderingen blijven binnen die vier:

- Een degenerate `pass` boekt de schrijver als `unavailable` (`scripts/lib/gate_recorder.py:2081`).
- Een `pass` op een afgekapte diff boekt hij als `partial_review` (`:2098`).

De aanroeper kiest dus altijd een van de vier. De schrijver verlaagt een `pass` als het bewijs dat eist, met een status die de lezer al kent. Dat is geen vijfde uitkomst in `GateOutcome`, maar een herclassificatie op de grens van het record.

### 2. `commit_gate_outcome()` is de enige schrijver

Eén aanroep neemt het slot per `(pr, gate)`, via `slot_lock` op het result-pad (`scripts/lib/atomic_io.py:46`, gebruikt op `scripts/lib/gate_recorder.py:2118`). Onder dat slot doet dezelfde aanroep vier dingen in deze volgorde:

1. de overschrijfwacht (`_check_overwrite_guard`, `scripts/lib/gate_recorder.py:1085`);
2. het rondegrootboek (`_book_round`, `:874`);
3. het record atomair (`_write_result_atomic`, `:1374`);
4. één `review_gate_result`-receipt (`_emit_terminal_result_receipt`, `:1704`).

Vanaf GE-4b boekt dezelfde aanroep ook de aanvraag en de verplichting. Eén aanroep levert van elk precies één. Een geweigerde schrijfactie levert er nul. De forge-publicatie (`publish_forge_check_run`, `:1507`) blijft buiten het slot, zoals vandaag: een publicatiefout mag een geland record niet terugdraaien.

De reden is meting, geen smaak. Drie emitters betekent dat elk nieuw veld drie keer geschreven wordt of uiteenloopt. Elf result-schrijvers waarvan drie raw betekent dat een uitkomst langs een weg binnenkomt die geen slot, geen wachter en geen grootboekregel kent. Vijf aanvraagschrijvers zonder slot betekent dat een lezer een half JSON kan zien, de klasse van OI-1486. Eén schrijver maakt de uitkomst atomair waar dat telt en laat één plek over voor een veld.

### 3. Contract voor deelfalen en poging-identiteit

Dit contract staat vóór de code van GE-4a vast.

- **Volgorde:** eerst het record, atomair (tijdelijk bestand plus rename, `_write_result_atomic`), dan het receipt.
- **Deelfout:** faalt het receipt, dan blijft het record staan met `receipt_pending: true`.
- **Herstel:** de volgende `commit_gate_outcome` op hetzelfde slot boekt het ontbrekende receipt eerst. Dat is idempotent via de bestaande sleutel, die `commit_sha` en `status` al draagt (`scripts/lib/append_receipt_internals/idempotency.py:64` en `:65`), dus een herhaling na een crash levert geen tweede regel.
- **Poging-identiteit:** `(pr, gate, commit_sha, attempt_seq)`. `attempt_seq` is nieuw en telt binnen het slot op.
- **Weigering:** een geweigerde schrijfactie laat geen record en geen receipt achter, en zet dus ook geen `receipt_pending`.

Het record gaat eerst omdat elke lezer en de merge-deur het record lezen; het receipt is de grootboekregel. Een verloren grootboekregel is uit het record te herstellen met de volgende aanroep. Een verloren record maakt de run onzichtbaar voor iedereen behalve het grootboek, en dat is de verkeerde kant om blind te zijn. `receipt_pending` wordt alleen gezet als het record werkelijk landde.

### 4. Drie adaptersoorten, één eigen transport, en een kleinere codex-module

| Soort | Poorten | Wat het is |
|---|---|---|
| `GovernedLane` | `glm_gate`, `kimi_gate`, `deepseek_gate` | één gedeelde adapter en parser onder `scripts/lib/`; glm en kimi worden shims van ongeveer 60 regels met dezelfde CLI |
| `Deterministic` | de lokale helft van `ci_gate` en `wiring_gate` | de lokale checks als uitvoerder; de `gh`-lezing van GitHub-checks blijft |
| `PathBinary` | `codex_gate`, `gemini_review`, `claude_github_optional` | een binary met stall-detectie, zoals vandaag |

- **`GovernedLane`.** De gedeelde parser (`_extract_verdict`, `_read_report`) en de dispatcher-bedrading verhuizen uit `plan_gate_panel._make_default_dispatcher` (na GE-5a: `scripts/lib/governed_lane.py:148`; daarvoor `scripts/lib/plan_gate_panel.py:1659`). De plan-gate importeert ze terug en verandert zelf niet. De CLI van beide shims blijft `--pr --diff-file --data-dir --model --timeout --json --reprocess`.
- **Waar fase 1-2 woont (GE-5b).** Sinds GE-5b staan ook de argumenten, het aanvraagrecord en de diff- en rapportlezing in de adapter: `governed_lane.run_gate_head` doet wat elke `main` tot dan zelf deed, met de registerregel als enige bron van de provider-verschillen (`cli_description`, `model_pin`, `lane_provider`, `dispatch_id_prefix`, `gate`). De scripts zijn shims van 77 regels: de CLI, de namen die lezers en tests aanroepen, en een `GateSeams` die bij elke aanroep uit de eigen module-globals wordt gebouwd, zodat een monkeypatch op `glm_gate._get_diff` en de kimi-tweeling de aanroep blijft bereiken. De fasen 3-6 zaten sinds GE-5b-2 al in `run_gate_tail`; samen is dit één motor met één pad: `run_gate_head` → `GateRunState` → `run_gate_tail`, en geen tweede per provider.
- **`Deterministic`.** Alleen de lokale helft van `ci_gate` en `wiring_gate` krijgt deze vorm. De `gh`-lezing van GitHub-checks blijft een subproces. Dit is een gedragswijziging, geen etiket: vandaag staan beide poorten als `path_binary` op `gh`.
- **`PathBinary`.** `codex_gate` houdt zijn binary en stall-detectie. `gemini_review` en `claude_github_optional` blijven in de stapel en houden dezelfde vorm.
- **`codex_final_gate.py` is geen adapter maar een controle.** Het bestand houdt `enforce_codex_gate` (`scripts/codex_final_gate.py:176`) en de helpers die die functie gebruikt. De 569 dode regels (258 tot 826) gaan in GE-2a en GE-2b weg. De aanroeper `scripts/closure_verifier.py:27` verandert niet.
- **`claude_gate` houdt een eigen transport.** Het staat als harness-lane in het register maar loopt via `claude_gate_lane.make_claude_gate_dispatcher` (`scripts/lib/claude_gate_lane.py:159`). `provider_dispatch` weigert claude, dus dit is een vierde transport en geen `GovernedLane`. Het register noemt hem apart.

Een nieuwe provider is na dit besluit een registerregel plus een parser. De motor verandert niet mee.

### 5. De naad met ADR-045

Dit ADR bezit wanneer en wat er geboekt wordt: `GateOutcome`, `commit_gate_outcome` en het terugbrengen van drie `review_gate_result`-emitters naar één. ADR-045 bezit wat elke grootboekregel draagt: het identiteitsblok en de schemaversie in de append-laag (`scripts/lib/append_receipt_internals/payload.py:416` en `:455`).

Het veld heet `commit_sha`, precies zoals punt 4 van ADR-045 het definieert. GE-1 schrijft het op het receipt en houdt `head_sha` als alias. Geen van beide tracks bouwt een tweede `review_gate_result`-schrijver. Deze volgorde is bewust: eerst de drie emitters naar één, dan het identiteitsblok van ADR-045 erop.

### 6. Wat dit ADR niet verandert

- **De stoelregel en de merge-deur.** Welke stoelen de deur eist is `scripts/lib/review_seat_evaluation.py` (R3a #2191, R3b #2193) en track `deterministische-testverificatie`.
- **De standaardstapel.** `gemini_review` en `claude_github_optional` blijven staan. Of ze eruit gaan is een apart besluit van de operator, niet een code-PR van deze track.
- **De idempotentiesleutel.** Die draagt `commit_sha` en `status` al sinds #1767. Dit ADR wijzigt hem niet.

## Gevolgen

- **De schrijverwachter dekt elke module die een result- of request-record schrijft.** `tests/test_oi1472_residual_guard_writers.py` breidt zijn lijst op `:357` uit; een nieuwe ruwe `write_text` op zo'n record laat een test falen. Vandaag kent de wachter 3 modules en passeren de drie raw schrijvers.
- **Het rondegrootboek valt onder hetzelfde slot.** `_book_round` (`scripts/lib/gate_recorder.py:874`) boekt de ronde in dezelfde aanroep als het record, zodat de review-cap niet anders kan tellen dan het record zegt.
- **Eén receipt-emitter.** Een veld toevoegen gebeurt op één plek. Dat is de voorwaarde waaronder ADR-045 zijn identiteitsblok kan opleggen zonder drie schrijvers bij te houden.
- **Geen migratie.** Het receipt krijgt een veld erbij en `head_sha` blijft als alias staan, dus oude lezers blijven werken. Terugdraaien is `git revert` per PR.
- **Consumers merken het pas na een release.** Een project op een oudere engine houdt het oude gedrag. De release is aan de operator.
- **De vier statussen zijn geen nieuwe lezerslijst.** `scripts/lib/gate_status.py` kent ze al. Een lezer die vandaag `unavailable` als afwezigheid behandelt, blijft dat doen.

**Uitrol.** Elke PR is een squash-commit op main en apart terug te draaien. De volgorde volgt uit de gedeelde bestanden:

1. GE-1 schrijft `commit_sha` op het receipt en houdt `head_sha` als alias.
2. GE-3 breidt de schrijverwachter uit en leidt de drie ruwe result-schrijvers door `write_result_guarded`.
3. GE-4a voert `GateOutcome` en `commit_gate_outcome` in en leidt `record_terminal_result` en `record_not_executable` erdoorheen.
4. GE-4b boekt ook de aanvraag en de verplichting in dezelfde aanroep.
5. GE-5a en GE-5b maken glm en kimi shims boven één `GovernedLane`-adapter.
6. GE-2a en GE-2b verwijderen de dode helft van `codex_final_gate.py`.
7. GE-6 doet de lokale helft van `ci_gate` en `wiring_gate`, ná GE-4b.

**Hoe we meten dat het gehaald is.** Na GE-4b: twintig poortruns op verschillende PR's, met per run precies één record, één aanvraag, één `review_gate_result` met `commit_sha` en één verplichting. Daarnaast `grep -rn 'write_text' scripts/*gate*.py scripts/lib/*gate*.py`, gekruist met de wachterlijst: geen ruwe schrijver van een result- of request-record buiten `commit_gate_outcome`. Tot slot `wc -l scripts/glm_gate.py scripts/kimi_gate.py scripts/codex_final_gate.py` voor en na, in het eindrapport van de track.

## Alternatieven die zijn afgewezen

**(a) Eén motor per provider houden en alleen de wachter verbreden.**
Afgewezen. De wachter uitbreiden dekt de drie ruwe result-schrijvers en de aanvraagkant, maar laat vijf artefacten en drie `review_gate_result`-emitters staan. De inventaris telt 26 verschillende schrijfplekken over die vijf artefacten. De laag groeide in zeven weken van 19.918 over 37 naar 33.515 over 46, dus per-provider-motoren blijven groeien. De wachter maakt één record veilig, niet één uitkomst atomair over alle artefacten.

**(b) `claude_gate` in de `GovernedLane`-adapter vouwen.**
Afgewezen. `claude_gate` loopt via `claude_gate_lane.make_claude_gate_dispatcher` (`scripts/lib/claude_gate_lane.py:159`) en `provider_dispatch` weigert claude. Vouwen brengt die weigering terug en zet de claude-stoel op een lane die hem niet kan draaien. Daarom een eigen transport, apart in het register.

**(c) De idempotentiesleutel wijzigen.**
Afgewezen. Sinds #1767 (4 september) draagt de sleutel `status` (`scripts/lib/append_receipt_internals/idempotency.py:64`) en `commit_sha` (`:65`). Een wijziging zou de dedupe van bestaande receipts ongeldig maken zonder iets te winnen. Wat open stond is de schrijfkant: de emitter stempelt `head_sha` (`scripts/lib/gate_recorder.py:1781`) en geeft geen `commit_sha` mee. GE-1 schrijft het veld; de sleutel blijft.

**(d) De drie emitters laten staan en het identiteitsblok van ADR-045 op alle drie zetten.**
Afgewezen. Drie emitters is er al één te veel. Elk veld dat ADR-045 toevoegt zou dan drie keer geschreven moeten worden, met drie kansen om uiteen te lopen. Eerst naar één emitter, dan het veld.

## Amendement 1 (2026-11-07): de dispatchtabel, de rij als enige registratie en toelating tot closure op soort

**Status:** Accepted (dispatch dlv-d051cefd0f8c, track provider-agnostic-review-lane, levering P0)
**Decided by:** T0, onder het mandaat van de operator voor golf 1-3. Het plan (`claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/provider-agnostic-review-lane.md`) passeerde de plan-gate van 28-10 (codex REVISE op `ee307b0e54a6`, tiebreaker START). Dit amendement is de eis uit die gate: de contracten liggen vast vóór de code van P1, P2, P2b, P4 en P6.
**Gemeten op:** `origin/main` `532813fce` (1.8.0). De regelnummers hieronder zijn die van die commit.
**Wijzigt:** de slotzin van punt 4 ("Een nieuwe provider is na dit besluit een registerregel plus een parser. De motor verandert niet mee.") van belofte naar contract met een toets. Punt 1 tot en met 3 en 5 en 6 blijven staan.

### A1. Aanleiding

Punt 4 beloofde dat een nieuwe provider een registerregel plus een parser kost. Dat is op 28-10 nagemeten en klopt niet. De laatst toegevoegde provider (`claude_gate`, #2172) raakte 21 productiebestanden en `scripts/gate_runner.py` kreeg +28 regels. Achttien bestanden onder `scripts/` dragen een letterlijke poortnaamtabel.

Drie plekken houden de belofte tegen. Ze zijn hier de reden dat het amendement bestaat:

- **De motor kent een provider bij naam.** `GateRunner._build_harness_lane_dispatcher` (`scripts/gate_runner.py:382`) vergelijkt op `:409` de registerlane met `CLAUDE_LANE_PROVIDER` en haalt anders op `:421` `_make_default_dispatcher` via `plan_gate_panel`. Een tweede eigen transport betekent daar een tweede `if`.
- **Eén poort staat op vijf plekken.** `GATE_PROVIDERS` (`scripts/lib/gate_recorder.py:95`), `GATE_BILLING` (`scripts/lib/gate_billing_table.py:39`), vier tabellen in `scripts/lib/gate_lane_contract.py` (`:44`, `:81`, `:126`, `:171`), de `Gate`-enum (`scripts/lib/dispatch_spec.py:38`) en `closure_verifier._GATE_HANDLERS` (`scripts/closure_verifier.py:1880`). Een rij die op één van die plekken ontbreekt valt pas om bij het gebruik. In de closure-handler is dat een `KeyError`.
- **`vnx pr-ready` leest de verkeerde bron voor "van de deur".** Gemeten 28-10: 8 van de 20 nieuwste PR's printen een tweede zetel als `[off the door: no obligation]`, terwijl de deur die zetel zelf aanvroeg en uitvoerde.

### A2. Dispatchtabel (P1)

Er komt één module `scripts/lib/gate_dispatch_table.py`, alleen stdlib, in dezelfde vorm als `gate_billing_table.py`: de deur leest haar met alleen `scripts/lib` op het zoekpad.

- **Wat de tabel is.** Een afbeelding van een *dispatchersleutel* naar een fabriek van een dispatcher. Vandaag zijn er twee sleutels. `governed` is de fabriek van `_make_default_dispatcher` (`scripts/lib/governed_lane.py:299`) en loopt via `provider_dispatch`. `claude_lane` is `claude_gate_lane.make_claude_gate_dispatcher` (`scripts/lib/claude_gate_lane.py:159`). Dat zijn de twee transporten die punt 4 al kent.
- **Wat de rij zegt.** Een `harness_lane`-rij draagt de dispatchersleutel als kolom (zie A3). `GateRunner` kiest de fabriek uit de tabel op die kolom. De vergelijking met `CLAUDE_LANE_PROVIDER` op `:409` en de hop via `plan_gate_panel` op `:421` verdwijnen. `gate_runner.py` noemt na P1 geen providernaam meer.
- **Eén fabriekshandtekening.** `(data_dir, base_ref, timeouts, project_root) -> dispatcher`. `timeouts` is wat `_harness_lane_dispatcher_timeouts` vandaag levert (`timeout_seconds` en `total_deadline_seconds`). Een fabriek die een argument niet nodig heeft negeert het; de handtekening verschilt niet per provider.
- **Faalt dicht.** Een rij met een dispatchersleutel die niet in de tabel staat, en een rij zonder sleutel, geven geen standaarddispatcher. De run boekt `not_executable` met `gate_runner_missing` en noemt de sleutel in `reason_detail`. Vandaag valt elke niet-claude lane stil terug op `governed`. Die terugval is precies de eigenschap die een tikfout in een nieuwe rij onzichtbaar maakt, dus hij gaat weg.
- **Wat de tabel niet doet.** Ze kiest het transport, niet de provider-specifieke parser van het antwoord. Die zit in `governed_lane` en blijft daar. Ze verandert ook `provider_dispatch` niet en raakt `subprocess_adapter.py` niet; dat is de reden dat de keuze in een tabel staat en niet in de claude-lane zelf.

### A3. De rij is de enige registratie (P2 en P2b)

Voor elke `harness_lane`-poort bestaat één rij. Alles wat nu op vijf plekken staat, volgt uit die rij.

| Kolom | Vandaag op | Wordt |
|---|---|---|
| `gate` | de sleutel van alle tabellen | de sleutel van de rij |
| `lane` | `GATE_PROVIDERS[gate][1]` (`kimi`, `glm-harness`, `deepseek-harness`, `claude`) | kolom, `GATE_PROVIDERS` is er een afgeleide van |
| `request_provider` | letterlijk in `_request_kimi`, `_request_glm`, `_request_harness_seat` (`kimi`, `glm`, `deepseek`, `claude`) | expliciete kolom (A5) |
| `dispatcher` | de `if` op `scripts/gate_runner.py:409` | dispatchersleutel voor de tabel van A2 |
| `billing` | `GATE_BILLING` | kolom, `gate_billing()` leest de rij |
| `model_env` en `model_default` | `MODEL_DEFAULTS` | kolommen |
| `single_shot` | `SINGLE_SHOT_GATES` | kolom |
| `timeout` en `diff_chars` | `TIMEOUT_CONFIG` en `DIFF_CHAR_CONFIG` | kolommen |

Vier regels leggen vast hoe dat werkt.

1. **Eén `GATE_LANE_ROWS`, de oude namen blijven.** `MODEL_DEFAULTS`, `SINGLE_SHOT_GATES`, `TIMEOUT_CONFIG` en `DIFF_CHAR_CONFIG` blijven bestaan als afgeleide views, zodat geen lezer meeverhuist (P2). `GATE_BILLING` en de `Gate`-enum lezen dezelfde rij (P2b). `SINGLE_SHOT_GATES` is vandaag een `frozenset` zonder kolom en is daarmee de view die een nieuwe rij het eerst vergeet.
2. **De rij leeft in een module zonder zware imports.** `dispatch_spec.py` en `gate_billing_table.py` zijn stdlib-only en moeten dat blijven. `gate_lane_contract.py` importeert `config_runtime` op `:25` en kan dus niet door hen geïmporteerd worden. De rij en de rijtype staan daarom in een module die alleen stdlib importeert; `gate_lane_contract`, `gate_billing_table`, `dispatch_spec` en `gate_recorder` lezen uit die module, nooit andersom.
3. **De enum volgt de rij voor de harness-lanes.** De vier niet-harness-leden (`codex_gate`, `claude_github_optional`, `ci_gate`, `wiring_gate`) blijven letterlijk staan; de leden van de harness-lanes komen uit de rijen. Het lid heet de poortnaam in hoofdletters (`kimi_gate` wordt `KIMI_GATE`), zodat elke bestaande `Gate.KIMI_GATE` blijft werken. `RETIRED_GATE_NAMES` (`:96`) en `GATES_OUTSIDE_ENUM` (`:76`) veranderen niet. `REGISTERED_GATE_NAMES` (`:87`) blijft de lijst van wat gedeclareerd mag worden.
4. **Een ontbrekende plek faalt met haar naam.** Een test loopt voor elke rij langs billing, enum, de vier views, de dispatchtabel en de aanvraag, en faalt met de naam van de plek die ontbreekt. Er komt geen plek bij die stil een standaardwaarde aanneemt. Een rij met een onbekende `billing`, een lege `lane` of een `dispatcher` buiten de tabel wordt bij het laden geweigerd, niet bij de eerste aanroep.

Wat dit niet doet: de rijen van `path_binary` en `deterministic` blijven in `GATE_PROVIDERS` en `GATE_BILLING` staan. Hun uitvoering, dus binary, argumenten en stall-detectie, is geen tabelwerk maar de adapter zelf (punt 4). De belofte van dit amendement geldt voor de harness-lanes, waar de laatste vier toevoegingen vandaan kwamen.

### A4. Toelating tot closure op soort (P6)

`closure_verifier._check_single_gate` (`scripts/closure_verifier.py:1892`) zoekt vandaag een handler in `_GATE_HANDLERS` (`:1880`) en geeft een `KeyError` als een bekende poort er niet in staat. Vier handlers (`_gate_kimi_gate`, `_gate_glm_gate`, `_gate_deepseek_gate`, `_gate_claude_gate`, `:1840` tot `:1873`) zijn identiek op de poortnaam na en roepen allemaal `_gate_full_evidence_pass` aan (`:1795`).

**De regel.** Volgorde van beslissen in `_check_single_gate`:

1. Een poort buiten `_KNOWN_GATES` blijft `UNVERIFIED`, ook mét resultaatrecord. Dit is OI-1093 en verandert niet.
2. Een poort met een eigen handler in `_GATE_HANDLERS` gebruikt die handler (`codex_gate`, `claude_github_optional`, `gemini_review`, `ci_gate`).
3. Een poort zonder eigen handler waarvan de rij `harness_lane` is, krijgt de generieke handler: `_gate_full_evidence_pass(gate, result)`. De vier wrappers vervallen.
4. Elke andere bekende poort zonder handler geeft `UNVERIFIED` met de reden dat er voor haar soort geen generieke handler bestaat. Nooit een `KeyError`, nooit een `PASS`.

Toelating op soort geldt dus alleen voor `harness_lane`. `path_binary` en `deterministic` blijven expliciet: `ci_gate` leest CI-checks en `codex_gate` heeft een risicogebonden "required"-beleid. Wat bij die soorten hoort is geen gemeenschappelijke handler.

**De uitsluitingen blijven staan.** Wat de drifttest `tests/test_closure_verifier_gate_enum_drift.py` vandaag uitsluit blijft uitgesloten, ook als de soort toelating zou geven:

- `_GATES_NOT_IMPLEMENTED_BY_CLOSURE` (`scripts/closure_verifier.py:82`, nu `wiring_gate`) blijft buiten `_KNOWN_GATES`. De uitsluiting wint van de soort, dus stap 1 vóór stap 3. Een nieuwe uitsluiting vraagt nog steeds een reden.
- `_NON_REVIEW_SIGNER_REASONS` (`:122`) blijft de bron van wie geen peer-signer is. Toelating tot closure maakt een poort geen peer-signer. Een nieuwe harness-poort is dat pas door de afleiding uit de enum en de drifttest van OI-1645, zoals vandaag.

**Dezelfde bewijsinvarianten.** De generieke handler is geen lichtere handler. Hij roept dezelfde functie aan als de vier wrappers, dus er is geen tweede code die kan afwijken. De zeven invarianten van de track zijn: het record bestaat, het is terminaal, `contract_hash` is gevuld, `report_path` is gevuld, het rapport staat op schijf, het verdict spreekt zichzelf niet tegen en de `commit_sha` is de kop. Ze zijn verdeeld over `gate_status.has_complete_evidence` (`scripts/lib/gate_status.py:269`, waar terminaliteit, gedeeltelijke review, `contract_hash` en `report_path` samen in zitten) en de controles van de merge-deur in `_merge_door_record_verdict` (`scripts/closure_verifier.py:741`). Een wijziging die één van de zeven in de generieke handler weglaat is een afwijzing in de review.

**De drifttest schuift mee, in dezelfde PR.** Niet verzwakken, wel verleggen:

- "elke enum-lid is geïmplementeerd of uitgesloten met een reden" blijft zoals hij is.
- "elke geïmplementeerde poort heeft een handler" wordt "elke geïmplementeerde poort heeft een eigen handler of een `harness_lane`-rij". Een poort met geen van beide faalt.
- Nieuw: "elke geregistreerde poort heeft een soort" (`resolve_gate_provider` geeft geen `None`).
- Lukt dat niet onder 400 regels, dan vervalt P6 en blijft doelpunt 5 van de track open met een OI. Dit is besluit 7 van het plan, hier vastgelegd.

**Fail-closed toets.** Alles wat vrijgeeft heeft een geval waarin het veld ontbreekt, `None` is, misvormd is of een onbekende waarde heeft, en dat geval blijft blokkeren. Voor de generieke handler: geen resultaatrecord, een niet-terminale status (`unavailable`, `executing`), een onbekende statuswaarde, ontbrekende of leeg-geplaatste `contract_hash` en `report_path`, een `not_executable`-record (OI-1400) en een uitgesloten poort. Geen van die gevallen geeft `PASS`.

### A5. Het `provider`-label op de aanvraag

De labels `kimi`, `glm`, `deepseek` en `claude` op een poortaanvraag blijven byte-gelijk. `scripts/lib/stop_conditions.py:846` en `provider_costs` matchen erop. De registerlane heet anders (`glm-harness`, `deepseek-harness`), dus het label mag nooit uit de lane afgeleid worden. Het is de kolom `request_provider` uit A3, met één test die de vier waarden vastpint. Hetzelfde geldt voor `report_path` op een aanvraag: de vorm is voor alle vier de harness-zetels gelijk (vandaag zet `_request_kimi` het onvoorwaardelijk, `scripts/lib/gate_request_handler.py:2609`, en `_request_glm` alleen bij een beschikbare poort, `:2645`). Dat laatste is werk van P3b, niet van P0.

### A6. Wat een aanvraag "van de deur" maakt (P4)

`vnx pr-ready` bepaalt of een poort "off the door" liep via `declared_set`, en die komt alleen uit de verplichtingenstore (`scripts/lib/pr_readiness.py:442`, gebruikt op `:456` en `:465`). De deur declareert per dispatch één verplichting, de primaire zetel. Elke tweede zetel van dezelfde stack is wel aangevraagd en uitgevoerd, maar heet daarmee onterecht buiten de deur.

**Gemeten voor het besluit.** Draagt een aanvraagrecord zijn herkomst? Nee. Geen van de schrijvers van een aanvraag (`scripts/lib/gate_request_handler.py`, `gate_executor.py`, `gate_recorder.py`, `gate_outcome.py`) zet een veld `origin`. Daarom is "aanvraagrecord op schijf" op zichzelf geen bewijs van de deur: `governed_lane.run_gate_head` schrijft ook een aanvraag wanneer een shim als `kimi_gate.py` direct gedraaid wordt (`scripts/lib/governed_lane.py:780`).

**Het besluit.**

- De deur zet `origin: "door"` op elke aanvraag die ze schrijft. De plek is `_write_request_record` (`scripts/lib/gate_request_handler.py:1048`), de enige schrijver van aanvragen van die module (GE-4c-2). Eén plek, niet vier.
- `persist_request` (`scripts/lib/gate_recorder.py:612`) herschrijft een bestaand record en neemt zijn velden mee. Het veld moet die ronde overleven. P4 pint dat met een test op `commit_gate_outcome`, de uitvoerder en de herhaalde schrijfactie.
- `declared_set` is: de verplichtingen vereniging de poorten met een aanvraagrecord waarvan `origin` gelijk is aan `"door"` voor deze PR.
- Het veld wordt exact vergeleken (`== "door"`). Een record zonder het veld, met een andere waarde, met een waarde die geen tekst is, of een record dat niet leesbaar is, telt als buiten de deur. Het label blijft dan staan; het verdwijnt alleen op positief bewijs.
- Een los geschreven aanvraagrecord voor `kimi_gate` houdt `[off the door`. Een resultaat dat de deur nooit aanvroeg houdt hem ook.
- Er komt geen terugwerkende kracht. Aanvragen van vóór P4 hebben het veld niet en houden hun label. Een bestaand record herschrijven om het label te laten verdwijnen is het achteraf verbouwen van bewijs. Daarom meet de T0-nameting van doelpunt 4 op PR's waarvan de aanvraag ná P4 is geschreven.
- P4 controleert dat geen lezer het volledige aanvraagrecord vergelijkt of hasht, want het veld is een nieuwe sleutel op een bestaand record. Vindt hij zo'n lezer, dan is dat een bevinding met een OI.

### A7. Deterministisch of model

Alles in dit amendement is deterministisch: een rij, een tabel, een soort, een exacte vergelijking op een veld. Er is geen model nodig voor de keuze van een dispatcher, de afleiding van een enum of de toelating tot closure. Het model zit alleen in de reviews zelf, en daar verandert niets: de uitkomst van een review gaat nog steeds door `commit_gate_outcome` en de zeven invarianten voordat iets als `PASS` telt.

### A8. Wat dit amendement niet verandert

- De stoelregel (`review_seat_evaluation.seats_for_risk`) en de standaard overnameketen `codex_gate,kimi_gate,claude_gate,deepseek_gate`.
- `GateOutcome`, `commit_gate_outcome` en de ene emitter van `review_gate_result` (punt 1 tot en met 3 en 5). Geen PR van deze track raakt die emitter.
- De drie adaptersoorten van punt 4. P5 haalt de dode vierde constante `GATE_PROVIDER_SCRIPT_RUNNER` weg (`scripts/lib/gate_recorder.py:93`, de tak op `scripts/gate_runner.py:845` en de leestak op `scripts/lib/gate_result_parser.py:335`), zodat de code zegt wat punt 4 al zegt. Dat is een opruiming en geen nieuw besluit.
- De rollback-routes `VNX_DISPATCH_LEGACY=1` en `VNX_SINGLE_ENTRY_DISPATCH=0`.

### A9. Gevolgen en toets

- **De belofte krijgt een toets.** P8 (`tests/test_new_provider_conformance.py`) laat een synthetische rij door billing, lane-config, aanvraag, beschikbaarheid, closure en `pr_readiness` lopen zonder codewijziging. Het aantal bestanden met een letterlijke poortnaamtabel (18 op 28-10) mag niet groeien. Een plek die na P2b en P3b nog een poortnaam letterlijk kent is een bevinding met een OI, geen stille uitzondering.
- **Volgorde.** P1 en P2 raken geen gedeeld bestand. P3a leest beide. P2b volgt P2. P5 volgt P1 omdat beide `gate_runner.py` raken. P6 en P4 staan los.
- **Terugdraaien.** Elke PR is een squash-commit en apart te reverten. Er is geen migratie en geen record dat verandert, behalve het nieuwe veld `origin` op nieuwe aanvragen. Oude lezers negeren het.
- **Meetbaar af** blijft het criterium van de track: een echte PR op vnx-dev krijgt een `kimi_gate`-verplichting, de deur vervult hem, en het bewijs komt door de zeven invarianten. De T0 meet dat na de bouw. Komt er geen kimi-verplichting, dan is dat een bevinding met een OI en is de track niet klaar.

### A10. Afgewezen alternatieven

**(e) De generieke closure-handler voor elke geregistreerde poort.** Afgewezen. `ci_gate` verifieert CI-checks en is bewust geen peer-signer (OI-1645). Toelating op elke soort zou een poort met een ander bewijsmodel de review-bewijsketen binnenlaten. Alleen `harness_lane` deelt het bewijsmodel van `_gate_full_evidence_pass`.

**(f) `origin` afleiden uit het `dispatch_id`.** Afgewezen. Beide ingangen (de deur en een direct gedraaide shim) maken dezelfde vorm `<provider>-gate-pr<nr>-<ts>` aan, bewust (OI-1725). De vorm onderscheidt ze niet, en een regel die dat toch probeert leest een gok als bewijs.

**(g) De tweede zetel ook als verplichting declareren in plaats van `origin` te stempelen.** Niet hier. Dat verandert wat de deur declareert en raakt het verplichtingenmodel. Als de rode run van P7 laat zien dat de deur geen `kimi_gate`-verplichting declareert voor een aangevraagde kimi-stoel, is dat P7a (plan, eis 4) en geen onderdeel van dit amendement.

**(h) De dispatchersleutel afleiden uit de lane-naam.** Afgewezen om dezelfde reden als A5: de lane (`glm-harness`) en het wezenlijke transport zijn verschillende dingen, en een afleiding wordt weer een `if` op een naam.
