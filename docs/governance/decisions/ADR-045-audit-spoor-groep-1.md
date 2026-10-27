# ADR-045 - Het grootboek legt elke dispatch vast: één startregel, één eindregel, één sweep

**Status:** Aangenomen 27-10-2026 (T0, op het autonomie-akkoord van Vincent van 27-10), op basis van het concept dat Vincent op 15-10 goedkeurde. Vervangt niets.
**Datum:** 2026-10-27
**Besloten door:** T0, onder het mandaat van het draaiboek van 27-10 (sectie 0: "weer STOP: splits de track"). Dit ADR beslist groep 1 van het concept van 15-10. Groep 2 tot en met 4 en de correcties blijven bij een later ADR op de track `audit-spoor-volledig`.
**Cross-refs:** concept `claudedocs/specs/audit-spoor-volledig/adr-concept.md` (goedgekeurd 15-10; daar nog ADR-041 geheten, het nummer 045 is definitief); plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/audit-spoor-kern.md` (revisie 2, met de eisen uit de plan-gate van 27-10 ronde 2); metingen `claudedocs/2026-10-07-terminal-legacy-en-voorstel-goal-4.md` §3.5, §3.6 en §6b; ADR-005 (eerst het grootboek, dan de projectie); ADR-007 (`project_id` en samengestelde sleutels); ADR-038 (de identiteit van een receipt-uitkomst); ADR-046 (de poortmotor: wie schrijft wanneer en wat, en waar de naad met dit besluit ligt); ADR-049 (de vorm van dit besluit).

## Context

Het grootboek `t0_receipts.ndjson` moet de bron zijn waarop een besluit kan leunen. Op 9 september is de intelligence-laag geparkeerd tot dat zo is. Vandaag is het grootboek dat niet. De metingen hieronder komen uit twee bronnen: het rapport van 7-10 (secties 3.5, 3.6 en 6b) en een telling door T0 op 14-10 over de 240 dispatches met een `task_complete` sinds 1 oktober in de vnx-dev-store.

| Eis | Stand | Bron |
|---|---|---|
| Vastleggen vóór de actie | Niet gehaald. Het grootboek kent alleen een eindregel. De start staat in de tabel `dispatches` en in het besluitenlog. | 7-10, §3.5 |
| Een run die sneuvelt laat een spoor na | Niet gehaald. Nul `task_started` of `task_timeout` in het hele bestand (OI-1995, OI-1999). De standaardlane heeft geen vangnet als de orchestrator sterft vóór `_govern` (OI-1932). | 7-10, §3.5 |
| Eén uitkomst per dispatch | Niet gehaald. Elke dispatch krijgt twee schrijvers: het eind-receipt van de lane en een receipt dat de converter uit het rapport maakt. 471 van 1.001 dispatches hadden twee of drie eindregels. | 7-10, §3.5; 14-10 |
| `project_id` op elke regel (ADR-007) | Niet gehaald. Het lane-receipt draagt hem nooit (0 van 230). Het converter-receipt wel (250 van 251). | 14-10 |
| Bij welke track hoort het werk | Niet gehaald. De horizon-track staat in `dispatches.track`, maar in 0% van de receipts. Het receipt-veld `track` bevat `plan-gate` of `unknown`: een andere betekenis onder dezelfde naam. | 14-10 |
| Op welke commit slaat het | Niet gehaald. 0% van de `task_complete`- en 0 van 104 `review_gate_result`-receipts dragen een commit. Het result-bestand van de poort heeft hem wel, maar dat bestand is laatste-schrijver-wint (OI-2017). | 14-10 |
| Eenduidige modelnaam | Deels. `model` is altijd gevuld, maar niet uit het register: 46 converter-receipts zeggen alleen `deepseek`, en `model_resolved` is leeg op alle DeepSeek-regels. | 14-10 |
| Het tool-spoor hoort bij de dispatch | Niet gehaald. De eventbuffer is gesleuteld op het naamvak `ephemeral`, dat alle headless bouwers delen. Parallelle dispatches archiveren en legen elkaars stroom (OI-1869, OI-1930, OI-1032). Zes bouwers die op 7-10 binnen vier minuten startten, hielden bestanden over van 1, 1, 1, 94, 16 en 109 regels; van P3 stonden 13 regels over drie bestanden tegen 109 in zijn transcript. | 7-10, §3.6 |
| Onwijzigbaar en bewaakt | Deels. De hash-keten bestaat maar staat uit (`unchained`). De freshness-monitor schrijft sinds 5 oktober naar de verkeerde store (OI-2042). | ledger_health 14-10 |

Het concept van 15-10 noemt zelf een volgorde in vier groepen: (1) startregel, eindregel en sweep plus de lezers van `dispatches/active/`; (2) het identiteitsblok en de modelnaam uit het register plus `report_ingested`; (3) de eventstroom per dispatch; (4) de hash-keten en de dagelijkse telling.

**Waarom dit ADR alleen groep 1 beslist.** Het plan van `audit-spoor-volledig` probeerde alle zeven punten van het concept in één track te leveren. Revisie 4 haalde de plan-gate niet: na zeven stoelrondes gaf de tiebreaker STOP (`plan-tiebreak-audit-spoor-volledig-456d2502`), met zes open structurele gaten (OI-2352 tot en met OI-2357). Groep 1 is klein en dicht juist de gaten waar OI-1995, OI-1999, OI-1931 en OI-1932 uit komen. Daarom staat groep 1 hier, en blijven groep 2 tot en met 4 plus de correcties bij `audit-spoor-volledig` (OI-2352, OI-2353 en OI-2355 blijven daar open). Het plan van deze track is op 27-10 in ronde 2 door de plan-gate gekomen (START, op `5e613d617b5d`) met zes eisen; elke eis is hieronder als benoemde wijziging opgenomen. Waar een eis een eerdere regel uit het plan verandert, gaat de eis voor.

**Wat de code vandaag al doet, en waarom dat de vorm bepaalt.**

- De namen bestaan al in een ander register: de dispatch-register emit mapt `task_started`/`task_start`/`dispatch_start` naar `dispatch_started` (`scripts/lib/append_receipt_internals/register_emit.py:71-75`) en `dispatch_started` staat al in de lijst die een state-rebuild afvuurt (`scripts/lib/append_receipt_internals/payload.py:816-820`). De startregel van dit ADR is dus een grootboekregel van een type dat het systeem al kent, niet een nieuwe naam voor een oude gebeurtenis.
- Twee schrijvers leveren vandaag een einde: de converter zet `event_type: task_complete` (`scripts/lib/report_to_receipt_converter.py:1294`) en de rapportparser doet hetzelfde (`scripts/report_parser.py:844`). `task_complete` is een completion-type (`scripts/lib/event_outcome_semantics.py:107`) en telt als rapport-type (`scripts/lib/receipt_outcome.py:89`). Precies daarom is "één eindregel" geen leesregel maar een schrijfregel.
- De toestandsmachine kent geen `abandoned` en de overgang leest en schrijft op `dispatch_id` alleen (`scripts/lib/runtime_state_machine.py:152`). De toestandsverzameling en de overgangstabel staan in `scripts/lib/coordination_db.py:40-71`. De tabel `dispatches` draagt al `UNIQUE(dispatch_id, project_id)` (`schemas/migrations/0022_track_layer.sql:133`), dus een per-project-lezing vraagt geen schemawijziging.
- Het bezettingslot per worktree bestaat al (`scripts/lib/dispatch_worktree_isolation.py:584`, vrijgegeven op `:616`). Dat is de liveness die de sweep kan lezen zonder nieuwe daemon.
- Code en documentatie gebruiken dit nummer al voordat dit bestand bestond: `commit_sha` heet daar "ADR-045 point 4" (`scripts/lib/append_receipt_internals/idempotency.py:61`, `scripts/lib/gate_recorder.py:1855`, `docs/core/11_RECEIPT_FORMAT.md:243`). Punt 4 van het concept is groep 2. Dit ADR neemt die naam niet over en wijzigt hem niet; het legt alleen vast dat de verwijzing naar punt 4 naar het latere ADR wijst.

## Besluit

**Elke dispatch krijgt in het grootboek precies één startregel en precies één eindregel per poging. Elke regel draagt dezelfde sleutel. Het grootboek is de bron; de tabel `dispatches`, `dispatches/active/` en de statusbestanden zijn projecties ervan.** Groep 1 doet dat voor de levenscyclus: start, einde, sweep, de lezers van `active/` en de converter die het rapport boekt. Het identiteitsblok, de eventstroom en de keten volgen in groep 2 tot en met 4.

Er komt geen tabel en geen kolom bij. `lifecycle_mode` gaat in `dispatches.metadata_json`, gelezen op `(project_id, dispatch_id)`; die tabel draagt de samengestelde sleutel al (ADR-007). De intenties zijn bestanden per `project_id` in de projectstore.

### 1. De grens van een dispatch is de overdracht aan de lane

Wat de deur vóór de overdracht weigert, is een geweigerde aanvraag en blijft geboekt zoals vandaag: een reject, geen regel, geen rij. Vanaf de overdracht geldt de levenscyclus. De overdracht is de aanroep van de lane in `_execute_claude_headless` (`scripts/lib/dispatch_cli.py:2860`) respectievelijk de provider-lane; de lane-start zelf is `run_envelope_headless_plan` (`scripts/lib/dispatch_envelope.py:828`) en voor claude is dat de enige claude-lane (`scripts/lib/dispatch_plan.py:32`).

- **Volgorde, en die is bindend:** eerst `dispatch_started` in het grootboek (ADR-005), dan `lifecycle_mode` in `dispatches.metadata_json`, dan de spawn. Mislukt de startregel, dan volgt geen DB-schrijfactie en geen spawn.
- **Herstel na een crash tussen startregel en DB:** de startregel is de bron. Finalizer, sweep en lezers lezen `lifecycle_mode` van de startregel; de sweep zet de DB-waarde alsnog, idempotent.
- **Elke uitgang vóór de spawn wordt een eindregel:** `end_state: failed` met `reason` `slot_timeout` (het bezettingslot kwam niet vrij) of `pre_spawn_error` (elke andere uitgang).
- **Fail-closed:** staat de instelling aan en mislukt het schrijven van de startregel, dan start de worker niet. Dat is bewust: een volle schijf of een kapot grootboek stopt het dispatchen liever dan een run zonder spoor te laten lopen.

### 2. Drie regeltypen, uit een gesloten lijst

| `event_type` | Wat het is | Telt als uitkomst |
|---|---|---|
| `dispatch_started` | de deur begint de overdracht | nee |
| `dispatch_finished` | het ene einde van deze poging | ja |
| `report_ingested` | het rapport is binnen en aan de dispatch gekoppeld | nee, nooit |

- **`dispatch_finished` draagt `end_state`** uit {`completed`, `failed`, `timed_out`, `abandoned`}. Niet meer en niet minder.
- **`report_ingested` draagt `dispatch_id`** en `end_outcome_id` als de eindregel er al is, anders `end_pending: true`. De regel wordt nooit herschreven. Een lezer koppelt per `(project_id, dispatch_id)`: een `report_ingested` zonder eindregel betekent "rapport binnen, dispatch niet geëindigd" en telt nooit als uitkomst. De eindregel noemt geen rapport; de koppeling is altijd via de sleutel.
- **Voor een `v3`-dispatch schrijft de converter geen `task_complete` meer.** Hij kiest zijn pad op `lifecycle_mode` van de dispatch-rij, en die keuze blijft staan zolang er één `v3`-rij bestaat. `task_complete` blijft bestaan voor `legacy`-dispatches en voor de geschiedenis.
- **Lezers lezen `dispatch_finished` naast `task_complete`,** via één leeshulp `end_line(project_id, dispatch_id)`. Een ledger met de nieuwe typen en een legacy-ledger geven per lezer hetzelfde beeld. Elke lezer houdt de legacy-tak.

### 3. Precies één eindregel per `(project_id, dispatch_id, attempt)`

De sleutel van de startregel, de eindregel en de intentie is `(project_id, dispatch_id, attempt)`. `attempt` komt uit `dispatches.attempt_count` op het moment van de overdracht en staat op beide regels.

- **Eén eindregel per poging.** Een refire (`--refire`) is een nieuwe poging met een nieuwe intentie. De deur weigert een refire zolang de vorige poging een start zonder einde heeft én liveness heeft (punt 5). De telling en de lezers koppelen per poging.
- **De intentie is exclusief.** Het eindbesluit met de volledige canonieke eindregel en zijn `sha256` wordt `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json`, geschreven via een tijdelijk bestand plus `os.link`. Precies één aanroep wint, ook tussen processen. Wie verliest leest de intentie van de winnaar en schrijft niets.
- **Publicatieslot.** Een exclusieve flock op `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.lock` tot en met de publicatie. Onder het slot wordt de intentie opnieuw gelezen: staat `published_at` er al, dan stopt de aanroep.
- **De drie stappen onder het slot:** het grootboek (staat er vanaf de byte-offset uit de intentie al een `dispatch_finished` met deze sleutel, dan niets schrijven), dan de projectie (punt 4), dan `published_at` in de intentie via een tijdelijk bestand plus `rename`. Een intentie zonder `published_at` herstelt de sweep via dezelfde stappen, onder hetzelfde slot; ook als de regel al in het grootboek staat, past het herstel de projectie opnieuw toe.
- **Geen correcties in groep 1.** Deze track schrijft geen `dispatch_correction` en kent geen `supersedes`-keten voor dit type. Een handmatige correctie door de T0 en de keten van correcties horen bij `audit-spoor-volledig` (OI-2352, OI-2353).

**De benoemde uitzondering op ADR-038.** ADR-038 staat een correctie toe als nieuwe regel met `supersedes`. Voor `dispatch_finished` geldt in de kern het omgekeerde: de append-laag accepteert een `dispatch_finished` alleen als er een intentie voor die sleutel bestaat **en** de canonieke vorm van de regel byte-gelijk is aan de regel in die intentie (inclusief de `sha256` daarvan). Elke andere `dispatch_finished` voor die sleutel wordt geweigerd, gepubliceerd of niet. Een gelijke `outcome_id` is dus niet genoeg: die laat een andere `end_state` toe. Wie een einde wil rechtzetten heeft geen regel maar een nieuwe poging, of de T0 met het rapport als bewijs.

### 4. Eindtoestand naar projectie, zonder nieuwe DB-toestand

Gemeten op `scripts/lib/coordination_db.py:40-71`: er is geen toestand `abandoned`, en die komt er niet. De projectie gebruikt deze tabel; elke stap is een eigen overgang met event (`actor`: `lifecycle` of `sweep`).

| `end_state` | receipt-`status` | `dispatches.state` (doel) | pad vanuit de huidige rij (alleen overgangen uit `DISPATCH_TRANSITIONS`) |
|---|---|---|---|
| `completed` | `success` | `completed` | `running` -> `completed`; vanuit `accepted` via `running`; vanuit `delivering` via `accepted`, `running` |
| `failed` | `failure` | `failed_delivery` | vanuit `delivering` of `running` direct; vanuit `claimed` via `delivering`; vanuit `accepted` via `running` |
| `timed_out` | `timeout` | `timed_out` | vanuit `delivering`, `accepted` of `running` direct; vanuit `claimed` via `delivering` |
| `abandoned` | `timeout` | `timed_out`, met `metadata_json.end_state = abandoned` | als `timed_out` |

- Staat de rij al in de doeltoestand, dan gebeurt er niets. Staat ze in een andere terminale toestand, of is er geen pad, dan blijft de rij staan en meldt de telling een **projectieconflict**. Een projectie wordt nooit stilzwijgend overschreven.
- `abandoned` is dus een feit in het grootboek en een `timed_out` met een markering in de projectie. Zo blijft de terminale verzameling één verzameling (ADR-049) en hoeft geen enkele lezer een nieuwe toestand te kennen.
- De overgang leest, wijzigt en geeft de rij terug op `(dispatch_id, project_id)`, en het event draagt hetzelfde `project_id`. Zonder `project_id` blijft het oude gedrag voor bestaande aanroepers staan. Twee projecten met hetzelfde `dispatch_id` raken elkaar niet.
- `failed_delivery` en `timed_out` blijven open uitkomsten voor de T0 (`scripts/lib/open_outcomes.py:831`), net als vandaag. `abandoned` en `timed_out` zijn geen dead letter.

### 5. Liveness: het slot, het deurproces en het kind

Een dispatch leeft als **één** van deze drie waar is:

1. **het bezettingslot is gehouden** — de occupancy-flock van de dispatch-worktree (`scripts/lib/dispatch_worktree_isolation.py:584`);
2. **het deurproces leeft** — `door_pid` met dezelfde `door_started_at` als op de startregel;
3. **het kind leeft** — `child_pid` met dezelfde `child_started_at` als in het spawn-bestand.

- **De startregel draagt `door_pid` en `door_started_at`.** Dat proces leeft van de overdracht tot na de publicatie van de eindregel: de deur wacht op de lane. Het venster tussen de startregel en het pakken van de flock is daarmee gedekt.
- **Direct na de spawn schrijft de deur `child_pid` en `child_started_at`** naast de intentie, atomisch: `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.spawn.json`.
- **Een `pid` alleen is geen bewijs.** De sweep vergelijkt de starttijd van het proces met de vastgelegde starttijd. Een hergebruikt `pid` met een andere starttijd telt als dood.
- **De lane houdt haar liveness vast tot na de publicatie van haar eindregel.** De finalizer wordt aangeroepen vóór `_release_occupancy` (`scripts/lib/dispatch_worktree_isolation.py:616`) en vóór het deurproces eindigt. Daarmee kan een levende lane nooit `abandoned` worden, en een dode lane kan niet meer eindigen. Dat vervangt de correctie die revisie 4 nodig had.
- **Bekend restrisico, benoemd en niet weggeschreven:** valt de deur weg in de enkele milliseconden tussen de spawn en het schrijven van het spawn-bestand, dan is het kind niet aantoonbaar levend en kan de sweep `abandoned` boeken terwijl de lane loopt. Groep 1 repareert dat niet (geen correcties, geen tweede schrijver). Het rapport van die lane staat in `unified_reports/` en de T0 beslist met dat bewijs. Dit venster is de prijs van "geen nieuwe daemon".

### 6. De sweep: 30 minuten na de deadline, op drie bestaande tikken

- **Drempel: de deadline van de dispatch plus 30 minuten** (besluit van Vincent, 15-10). Een headless worker krijgt tot 7.200 seconden. Er wordt geen marge bijopgeteld en geen marge afgetrokken.
- **De sweep draait op drie plekken, en nergens anders:** (1) in de tik van de gate-obligation-runner, `StartInterval` 900 (`scripts/launchd/com.vnx.gate-obligation-runner.plist:95`); (2) bij elke dispatch via de deur, vóór de overdracht, begrensd op 2 seconden; (3) bij elke `build_t0_state` (`scripts/build_t0_state.py:2608`). **Geen nieuwe LaunchAgent**: een nieuwe daemon is machinestaat en lost niets op dat drie bestaande tikken niet oplossen.
- **Wat de sweep doet:** open intenties herstellen (de stappen uit punt 3), en `abandoned` boeken voor een startregel zonder eindregel waarvan de drempel verstreken is **en** waarvoor geen liveness geldt. Hetzelfde geldt voor een ontbrekende `lifecycle_mode` in de DB: de sweep zet hem uit de startregel.
- **Bewust gevolg, en het doel is daarop aangepast:** met een runner-interval van 900 s staat `abandoned` er uiterlijk op deadline plus 45 minuten. Het doel zegt daarom "bij de eerste sweep na deadline plus 30 minuten". De hartslag van de sweep staat met zijn leeftijd in de SessionStart-briefing, zodat een stille sweep zichtbaar is, en de acceptatie (K-meet) meet de werkelijke vertraging in plaats van hem aan te nemen.

### 7. De lezers van `dispatches/active/` beslissen op grootboek plus liveness

`check_active_drain` (`scripts/check_active_drain.py`), `orphan_sweep` (`scripts/lib/orphan_sweep.py`), `crash_recovery_sweep` (`scripts/lib/crash_recovery_sweep.py`) en `open_outcomes` (`scripts/lib/open_outcomes.py:831`) beantwoorden "loopt dit nog" vanaf de startregel plus de liveness uit punt 5, niet vanaf de map. Dat is de voorwaarde uit het besluit van 8 oktober.

- Een dispatch met een eindregel en zonder map geldt als klaar.
- Een dispatch met een map, zonder eindregel en zonder liveness is een `abandoned`-kandidaat.
- `abandoned` en `timed_out` staan daarna in `open_outcomes`, niet in `dead_letter/`.

### 8. Schemaversie, herkomst en wat er niet verandert

- **`schema_version: 3`** op de drie nieuwe typen. Lezers accepteren 2 en 3. Geen lezer weigert op de versie.
- **`writer_sha`** wordt samen met de schemaversie op één plek in de append-laag gezet, voor de drie nieuwe typen. Het cohort van een telling is: `git merge-base --is-ancestor <activeringscommit> <writer_sha>` waar in de repo van de telling. Een `writer_sha` die de repo niet kent, telt als "herkomst onbekend". Koppelen gebeurt per `(project_id, dispatch_id)` en per `lifecycle_mode`; alleen `v3`-dispatches worden gepaard.
- **Geen backfill.** De geschiedenis blijft wat ze was. Regels zonder `writer_sha` zijn historie, en een telling over een periode die beide versies omvat zegt dat erbij.
- **Het bestaande receiptveld `track` blijft** met zijn huidige betekenis (lane-label). `track_id`, `parent_dispatch`, de modelnaam uit het register en `commit_sha` op poortresultaten zijn groep 2. Dit ADR hernoemt of schrapt `track` niet, en wijzigt de identiteitsblok-regel van ADR-046 niet.
- **Wat de startregel in groep 1 al draagt**, omdat het doel dat eist: `project_id`, `dispatch_id`, `attempt`, `role`, `writer_sha`, `schema_version`, `base_sha`, `spec_sha256`, `requested_by`, `door_pid`, `door_started_at` en `lifecycle_mode`. Het einde draagt `head_sha` of een lege `head_sha` met `no_push:<reden>`. Het volledige identiteitsblok (met `track_id` en de register-modelnaam) is groep 2 en wordt niet hier afgedwongen.

### 9. De instelling en de modus per dispatch

- **Alles landt uit, achter één instelling:** `ledger_lifecycle` in het register (`scripts/lib/config_registry.py:83`), standaard uit. Tot de activeringslevering (K-act) merkt geen dispatch iets. De instelling uitzetten is de eerste stap terug en de noodrem.
- **De modus staat per dispatch vast:** `lifecycle_mode: v3` of `legacy`, vastgelegd bij de overdracht, op de startregel en in `dispatches.metadata_json`. Finalizer, sweep en lezers kiezen hun pad op die waarde, **nooit** op de huidige stand van de instelling. Een dispatch die in `v3` startte, eindigt in `v3`.

### 10. Uitrol en terugdraaien

- **Schrijvers gaan samen terug:** eerst de startregel en de finalizer-aanroepen in de lanes, daarna de finalizer-module en de sweep, en alleen als het telcommando nul `v3`-dispatches in vlucht en nul open intenties meldt. De volgorde is die van het plan.
- **Lezers, vocabulaire en de converter gaan niet terug** zodra er één regel van een nieuw type of één `v3`-rij bestaat: fix-forward. Elke lezer houdt de legacy-tak, dus een oude ledger blijft leesbaar.
- **De instelling is altijd de eerste stap.** Zij stopt alle nieuwe schrijvers tegelijk zonder code te reverten.
- Consumers merken pas iets na een release, en een release is aan Vincent.

## Wat hier deterministisch is, en wat niet

Groep 1 is volledig deterministisch. Dat is geen observatie maar een eis van dit besluit.

- **Deterministisch, en dus testbaar en reproduceerbaar:** de drie regeltypen en hun gesloten lijst; de sleutel `(project_id, dispatch_id, attempt)`; de exclusieve intentie via `os.link` en het publicatieslot; de byte-gelijke-acceptatie in de append-laag; de projectietabel met zijn overgangen; de liveness-regel met proces-starttijden; de drempel deadline plus 30 minuten; de drie tikken waarop de sweep draait; `schema_version` en `writer_sha`; de instelling en de modus per dispatch.
- **Niets in dit besluit vraagt een model.** Er is geen natural-language oordeel, geen synthese en geen ambiguïteit in het schrijfpad, de sweep of de projectie. Een model dat "even beslist" of een dispatch klaar is, zou de enige eigenschap wegnemen die het grootboek bruikbaar maakt: dezelfde invoer geeft altijd dezelfde regel.
- **Waar een model wél voorkomt, is het een lezer en geen schrijver.** De intelligence-laag en de learning-loop mogen het grootboek straks interpreteren; zij schrijven er geen regel in en hun uitkomst telt nooit als uitkomst van een dispatch. Dat is dezelfde grens als in ADR-037: een oordeel is een oordeel, een meting is een meting.

## Gevolgen

**Wat het kost.**

- **Twee extra regels per dispatch:** `dispatch_started` en `report_ingested`. Dat is de prijs van "vastleggen vóór de actie" en van "het rapport is geen tweede einde".
- **De deur wordt fail-closed op de startregel.** Een volle schijf of een kapot grootboek stopt het dispatchen. Dat is bewust; de fout noemt pad en reden, en de instelling uitzetten is de noodrem.
- **De deur wacht op de lane** zolang die loopt, en de startregel is één extra schrijfactie per dispatch.
- **Elke lezer van `task_complete` moet om.** Vóór de bouw komt er een inventaris van die lezers; het plan telt ze in drie groepen (Python, shell, analyse) en elke groep is een eigen levering. Elke lezer houdt de legacy-tak.
- **De projectie kan conflicteren.** Een rij die al in een andere terminale toestand staat, wordt niet rechtgetrokken; de telling meldt het conflict en een mens beslist.

**Wat het nog niet doet (groep 2 tot en met 4, later ADR op `audit-spoor-volledig`).**

- Het volledige identiteitsblok: `track_id`, `parent_dispatch`, de modelnaam uit het register met `model_raw` en `model_unresolved`, en `commit_sha` op elk poortresultaat. Punt 4 van het concept. De verwijzing "ADR-045 point 4" in de code wijst daarnaar.
- De eventstroom per dispatch met `events_sha256` en het aantal regels (punt 5).
- De hash-keten en de dagelijkse volledigheidstelling (punt 7).
- Correcties: `dispatch_correction`, de `supersedes`-keten en het herstelpad daarvan (OI-2352, OI-2353).
- De herbouwbaarheid van alle projecties uit het grootboek: een eigen, latere ADR (besluit van Vincent, 15-10).

**Hoe het gemeten wordt.** De vijf punten van het doel van de track, op het cohort vanaf de activeringscommit, plus de acceptatielevering K-meet: de steekproef van twintig dispatches, een harde kill van een lane, en de telling over het cohort, met de activeringscommit in `decision_ref`. K-meet meet ook de werkelijke vertraging tussen deadline en `abandoned`, omdat de runner-tik die niet kan garanderen.

## Hoe we meten dat het gehaald is

Letterlijk uit `vnx objective show audit-spoor-kern` (track `audit-spoor-kern`, project vnx-dev):

> Klaar wanneer, op het cohort vanaf de activeringscommit: (1) twintig dispatches via de deur hebben elk een dispatch_started voor de eerste worker-actie en precies een dispatch_finished met end_state uit completed/failed/timed_out/abandoned, met project_id, dispatch_id, role, writer_sha, schema_version 3, base_sha bij de start en head_sha (of no_push:<reden>) bij het einde; (2) een hard afgebroken lane geeft zonder handwerk abandoned bij de eerste sweep na deadline plus 30 minuten (sweep in de runner-tik, bij elke dispatch via de deur en bij elke build_t0_state), als open uitkomst; (3) het rapport wordt report_ingested, geen tweede eindregel; (4) check_active_drain, orphan_sweep, crash_recovery_sweep en open_outcomes beslissen op grootboek plus liveness; (5) de dagelijkse telling in ledger_health meldt nul starts zonder einde, nul eindes zonder start en nul dubbele eindes, met hartslag in de briefing. Plan: claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/audit-spoor-kern.md

De eigenaar van de acceptatie is de T0. K-meet legt per punt het commando en de getelde uitkomst vast in een rapport onder `docs/investigations/`.

## Alternatieven die zijn afgewezen

**(a) Alle zeven punten van het concept in één track houden (revisie 4 van `audit-spoor-volledig`).**
Afgewezen door de plan-gate van 27-10: zeven stoelrondes, tiebreaker STOP, zes open structurele gaten (OI-2352 tot en met OI-2357). Groep 1 is klein, dicht OI-1995, OI-1999, OI-1931 en OI-1932, en is de voorwaarde voor de rest. Een groter besluit dat niet convergeert levert geen enkele regel op.

**(b) Een nieuwe DB-toestand `abandoned` met een migratie.**
Afgewezen. Gemeten: `DISPATCH_STATES` kent hem niet (`scripts/lib/coordination_db.py:40-45`) en `DISPATCH_TRANSITIONS` heeft geen pad ernaartoe (`:54-71`). Een nieuwe toestand raakt de terminale verzameling (ADR-049), elke lezer en elke view, voor informatie die `timed_out` plus `metadata_json.end_state` al draagt. De projectietabel geeft hetzelfde antwoord zonder schemawijziging, en ADR-007 vraagt voor een nieuwe tabel of sleutel een eigen afweging die hier niets oplost.

**(c) Correcties in groep 1, zoals revisie 4 voorstelde.**
Afgewezen. De enige aanleiding was een late lane die alsnog eindigde na een `abandoned`. Dat geval wordt hier door een invariant uitgesloten (de lane houdt haar liveness tot na publicatie), niet gerepareerd. Correcties hebben bovendien geen herstelpad (OI-2352) en hun identiteit is tegen ADR-038 onderbepaald (OI-2353). Ze blijven bij `audit-spoor-volledig`.

**(d) Een aparte heartbeat- of liveness-daemon.**
Afgewezen. Er zijn al drie tikken (runner, deur, `build_t0_state`) en een vierde proces is machinestaat die niets toevoegt aan de vraag "leeft dit". De prijs is het benoemde venster tussen spawn en spawn-bestand; die is kleiner dan de prijs van een daemon die zelf kan sterven.

**(e) De sweep-marge vergroten zodat de 900 s-tik `abandoned` binnen 45 minuten garandeert.**
Afgewezen. Dat verandert het besluit van Vincent van 15-10 (30 minuten) om een intervalprobleem op te lossen (OI-2357). In plaats daarvan draait de sweep ook bij de deur en bij `build_t0_state`, en meet K-meet de werkelijke vertraging.

**(f) De sleutel op `dispatch_id` houden en `attempt` weglaten.**
Afgewezen. Een refire of retry op hetzelfde id zou dan geen eigen einde kunnen hebben, en de deur zou een refire niet kunnen weigeren zolang de vorige poging nog loopt. De sleutel is `(project_id, dispatch_id, attempt)`; dat is een eis uit de plan-gate van 27-10 ronde 2.

**(g) De converter `task_complete` laten schrijven en dat als het ene einde tellen.**
Afgewezen. Dan blijft de tweede schrijver bestaan en blijft het aantal dubbele eindregels groeien; 471 van 1.001 dispatches hadden er twee of drie. ADR-038 dedupliceert identieke uitkomsten, maar het verwijdert de tweede schrijver niet en het maakt van een rapport geen levenscyclusfeit.

**(h) Het receiptveld `track` nu hernoemen of schrappen.**
Afgewezen. Dat is punt 4 van het concept (groep 2) en vraagt eerst een meting van wie het leest. Een rename in groep 1 zou lezers breken voor een veld dat deze groep niet nodig heeft.
