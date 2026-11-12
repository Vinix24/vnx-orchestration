# ADR-068 - Learning-signaal-attributie: eigendom uit drie signalen, overdracht van hele rijen, goedkeuring gebonden aan de wijziging

**Status:** Voorgesteld 10-11-2026 (T0, track `learning-signal-reattribution`, levering L1); aangenomen zodra de T0 L1 merget.
**Datum:** 2026-11-10
**Besloten door:** de operator voor besluit 2 en 3 van de 1.9-sprint (07-11-2026, letterlijk hieronder); het plan `claudedocs/2026-11-07-naar-1-9/plannen/learning-signal-reattribution.md` (revisie 2, 08-11-2026, plan-gate van 08-11 en de tiebreaker-voorwaarden) voor de besluiten 1 tot en met 20. ADR-068 was vrij op `origin/main` `46d6305e7` (10-11-2026): de hoogste is ADR-069 en 068 is door de T0 aan deze track toegewezen (064 deltareview, 065 loop, 066 dream, 067 capture).
**Cross-refs:** ADR-005 (NDJSON-grootboek en de afgeleide SQLite), ADR-007 (samengestelde sleutel over `project_id`), ADR-056 (zelfstandigheid als meetbaar begrip), ADR-063 (onbekend is nooit 0), ADR-065 (leren is voorstellen), ADR-067 (learning-capture-routing).

De operator heeft op 07-11-2026 besloten, onder meer:

> **2.** zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; leren en dreaming blijven voorstellen.
>
> **3.** Learning-tracks ontparkeren zodra ledger-is-falsifiable gemerged is; corpus-quarantaine omkeerbaar (verplaatsen, niet wissen).

Dit ADR legt vast waartegen de leveringen L2 tot en met L6 bouwen: hoe eigendom van een leerpatroon wordt vastgesteld (een stemming over drie signalen), welke zes categorieën een patroonrij kan krijgen, hoe een rij van het ene projectbestand naar het andere gaat (hele rijen, nooit een herbouw), hoe een crash of drift wordt opgevangen, wat een goedkeuringsrecord voor persistentie moet zijn en wat "klaar" bewijst. Het bevat geen code. Alles erin is deterministisch: NDJSON parsen, SQL, een gesloten categorieënset en een state machine per rij. Er zit geen modelcall in. Attributie is een functie van feiten (welke receipt noemt welk project, waar staat hij, waar schreef hij zijn rapport) en niet van oordeel. Een model voegt hier alleen ruis toe en maakt de uitslag onherhaalbaar. De enige open natuurlijke taal is de reden die de operator bij een goedkeuring typt.

## Context

Patronen in de intelligence-stores dragen het verkeerde project na gekopieerde receipts en gedefaulte bodies. Zes feiten, gemeten op `origin/main` `a1376e5c8` (08-11-2026) tenzij een datum erbij staat:

- **De bronbug is weg, de formalisering niet.** De deur resolvet `project_id` fail-closed (`_resolve_project_id` in `scripts/lib/dispatch_cli.py:122`). Restdefaults staan nog in `scripts/lib/project_scope.py:36` (`DEFAULT_PROJECT = "vnx-dev"`). Niets dwingt een read-only intelligence-laag af.
- **De bestaande repair-tool levert bij niemand af.** `apply_decisions` (`scripts/lib/pattern_reattribution.py:451`) doet `UPDATE {table} SET project_id = ? WHERE id = ?` (`:472`) binnen het bestand dat hij scant. Een consumer opent het bestand van zijn eigen project (`_get_central_qi_conn`, `scripts/lib/intelligence_selector.py:267`) met een verse verbinding per aanroep en filtert op `project_id = ?` (`scripts/lib/intelligence_sources/proven_pattern.py:267`). Een rij die in het seocrawler-bestand naar `vnx-dev` wordt herstempeld, blijft in het seocrawler-bestand staan: seocrawler ziet hem niet meer en vnx-dev heeft hem nooit.
- **De grondwaarheid van de tool is store-lidmaatschap** (`build_dispatch_project_index`, `pattern_reattribution.py:140`; `decide_row`, `:175`). Een receipt in de ontvangende store "hoort" daar. Droge run op 07-11: 1148 rijen gescand, 0 verplaatst.
- **De prefix in `source_dispatch_ids` breekt de join.** De import schrijft `<project_id>:<id>` (`_prefix_value`, `scripts/lib/migrate_import.py:253`), de lezer (`_parse_source_dispatch_ids`, `pattern_reattribution.py:103`) stript niet. Sales-copilot: 50 geprefixte elementen, 0 kale, 41 van 41 rijen `no_match`.
- **De leerlus-poort faalt open.** `LearningLoop(persist=True)` opent de live-DB voor write zonder goedkeuring en omzeilt `persist_enabled()` (`scripts/learning_loop.py:178`). De bestaande operator-poort `_validate_disposition_inputs` (`:1781`) eist alleen een niet-lege `approval_id`.
- **Het bewijs in de body is zelf besmet.** Van 495 dispatch-ids met precies één fysieke kopie waarvan de body een ander project noemt dan de store, zijn er 356 waarvan rapportpad en store gelijk zijn en alleen de body afwijkt: de body is gedefault naar `vnx-dev`. Een aantoonbaar misrouted single-copy (rapportpad en body gelijk, store wijkt af) komt vandaag 0 keer voor. De regel moet dat geval wel herkennen en de 356 laten staan.

Twee meetfeiten bepalen de vorm van de oplossing. De live-DB is geen stilstaand bestand: alle vijf de databases staan in WAL-modus, minstens 30 modules schrijven erin en `antipatterns.id` loopt tot 472032 bij 1136 rijen, dus id's zijn niet stabiel. En 1155 van 1237 vnx-dev-rijen hebben geen bruikbare `source_dispatch_ids`: ADR-005 noemt de SQLite een herbouwbare projectie, maar voor deze rijen is er niets om uit te herbouwen.

## Besluit

De 20 besluiten volgen het plan en dragen dezelfde nummers. Besluit 5 en 7 uit revisie 1 zijn vervallen en staan hier met hun vervanger, zodat niemand ze terugleest als geldend.

### A. Route en reikwijdte

**Besluit 1. Route B, niet route A.** ADR-005 maakt de afgeleide DB bijwerkbaar vanuit het grootboek. Route A (de bestaande tool harden) repareert alleen de subset waar de receipt al in de juiste store staat: 0 van 1148 op vnx-dev. Route B is: de attributie komt uit het grootboek, de rij verhuist naar het DB-bestand van het juiste project en intelligence wordt alleen via een goedgekeurd voorstel geschreven.

**Besluit 4. Rijen die niet te attribueren zijn blijven staan, nooit weggehaald of geschrapt.** Reden: operatorbesluit 3 ("verplaatsen, niet wissen") en dat 1155 van 1237 vnx-dev-rijen geen ids dragen.

**Besluit 6. Eén attributiewaarheid, in het bestaande `scripts/lib/pattern_reattribution.py`.** De overdracht (L3a, L3b) en de CLI (L6) importeren de publieke helpers. Een tweede engine zou dezelfde regel dupliceren.

**Besluit 8. Geen `learning_projection`-sleutel in `run_reconcile`.** Dat vergt een gepaarde edit van de golden-test van de reconcile-samenvatting en `objective_reconcile.py` is doelwit van een andere track. Het meetoppervlak is de CLI (L6).

**Besluit 10. Deze track poort alleen de learning-loop-schrijver.** `scripts/lib/pattern_upsert.py` en de andere QI-schrijvers (`intelligence_bridge`, `memory_consolidator`, `intelligence_import`) blijven buiten scope. Ze horen bij `learning-capture-fragmentation` en `intelligence-corpus-quarantine`.

**Besluit 11. De prefix-strip is voorwaardelijk.** Alleen een prefix die een bekend project-id is wordt gestript. `headless-review:` (6 elementen in seocrawler-v2) is een dispatch-namespace en blijft staan.

### B. Attributie (contract)

**Besluit 2. De grondwaarheid is een stemming over drie signalen per receipt-kopie: de body-`project_id`, de fysieke store en de wortel van het rapportpad.** Een kopie in store S met body B en rapportpad onder `/.vnx-data/R/` geeft stemmen voor S, B en R. Een ontbrekend of niet-bekend project is geen stem: de sentinels `unknown`, leeg, `None` en `null` tellen niet. Per dispatch-id telt de stemming over alle kopieën samen. Een project P wint een id als P minstens twee stemmen heeft én meer dan alle andere samen. Reden: de body is besmet door de oude default (356 van 495), de store is besmet door een misroute en het rapportpad is een derde schrijver die zijn eigen dataroot kent. Alleen overeenstemming van twee onafhankelijke signalen is bewijs. De body alleen of de store alleen is dat nooit.

**Besluit 3. Fail-closed categorieënset, en alleen `moves` grijpt in.** Zes categorieën per patroonrij: `positive_own`, `store_only`, `moves`, `ambiguous`, `absent`, `unattributed`. De regels:

- (a) een id zonder receipt is `absent` en telt niet mee in de rijbeslissing;
- (b) alle gewonnen ids wijzen naar één P gelijk aan de DB-eigenaar: `positive_own`;
- (c) naar één P ongelijk aan de DB-eigenaar: `moves`;
- (d) naar meer dan één P: `ambiguous`;
- (e) alleen ids die een receipt hebben maar geen winnaar (één stem of een gelijkspel): `store_only`. Dit verifieert nooit en verhuist nooit;
- (f) alleen ids zonder receipt: `absent`;
- (g) geen ids, alleen sentinels, een niet-lijst (zoals de 570 rapportpaden in seocrawler-v2) of niet te parsen: `unattributed`.

Ontbrekend, `None`, malformed of een onbekende waarde is nooit een reden om te verplaatsen en nooit een reden om vrij te stellen. Een winnaar zonder eigen DB-bestand onder `~/.vnx-data/<project>/state/` kan geen bestemming zijn en geeft `ambiguous`. Een `ambiguous`-rij gaat nooit automatisch: de operator beslist per rij.

### C. Overdracht (contract)

**Besluit 12. Receipts worden nergens verplaatst, herschreven of verwijderd.** De consumer opent per aanroep het DB-bestand van zijn eigen project en filtert op `project_id`. Er is geen cache en geen vernieuwstap. Het enige dat een consumer nodig heeft om het juiste signaal te krijgen, is dat de rij in het bestand van het juiste project staat met de juiste `project_id`. Receipts verplaatsen levert daar niets aan, het schrijft in een append-only grootboek en breekt een eventuele hash-chain. Het bewijs wordt op zijn plaats gelezen. De helft van het trackdoel die zegt dat receipts verplaatsen volstaat, is hiermee afgewezen met deze meting.

**Besluit 13. De bewerking is een overdracht van hele rijen, geen herbouw.** Een rij wordt vastgelegd als volledige voorafbeelding (alle kolommen) plus de bijbehorende junction-rijen (`pattern_usage`, `dispatch_pattern_offered`, `pattern_injection_outcome`, gesleuteld op `pattern_id`) en wordt zonder herberekening in het bestand van de bestemming gezet. Reden: de rijen zijn geen projectie van het grootboek (1155 van 1237 vnx-dev-rijen hebben geen bron-ids). Een herbouw zou ze moeten verzinnen of weglaten.

**Besluit 14. Identiteit: `content_key` en een nieuw `id`.** Een rij krijgt in de bestemming een nieuw `id`, want id's zijn niet stabiel en twee bestanden kunnen dezelfde id dragen. De identiteit is `content_key`: de sha256 over de canonieke JSON van alle kolommen behalve `id` en `project_id`. De insert is idempotent: `NOT EXISTS` op `content_key` binnen het bestemmingsproject. Bestaat dezelfde inhoud al, dan telt dat als `already_present`, wordt er niets bijgeschreven en legt het journal vast dat deze move die bestemmingsrij niet heeft aangemaakt (`created: false`, besluit 17). Junction-rijen krijgen de nieuwe `pattern_id` (het prefixschema `intel_sp_` en `intel_ap_` staat in `_PATTERN_ID_PREFIX`, `pattern_reattribution.py:85`).

**Besluit 15. Schema blijft behouden (`schema_gap`).** De overdracht bouwt geen schema op. De insert gebruikt de kolommen die in `PRAGMA table_info` van bron én bestemming voorkomen. Heeft de bron een kolom met een waarde ongelijk aan `NULL` die de bestemming mist, dan wordt de move geweigerd met de categorie `schema_gap` en gerapporteerd. Het plan legt `PRAGMA user_version` van beide bestanden vast (nu 28, 29 en 34). Alleen `success_patterns`, `antipatterns` en de drie junction-tabellen worden aangeraakt. Elke andere tabel (`pattern_activation`, de quarantainetabellen) blijft ongemoeid.

**Besluit 16. Geen bestandsswap en geen snapshot van het hele bestand.** De databases staan in WAL en minstens 30 modules schrijven erin: een kopie die later terugkomt, verliest alles wat er tussen kopie en swap werd geschreven. Elke wijziging is één `BEGIN IMMEDIATE`-transactie per bestand, waarin de tool eerst de bronrij opnieuw leest, haar `content_key` herberekent en vergelijkt met de voorafbeelding in het plan. Is het verschil niet nul, dan slaat hij de rij over met `drift` en wijzigt niets. SQLite serialiseert de transactie met de andere schrijvers. Andere rijen en tabellen blijven onaangeroerd. Besluit 5 uit revisie 1 (herbouw naar een nieuw bestand en een swap door de operator) is hiermee **vervallen**.

**Besluit 17. Gedeeltelijke uitvoering: journal, `resume`, `undo`, en een plan dat aan zijn bewijs is gebonden.** Elke move heeft een state machine in een append-only journal (`<state-dir van het bronproject>/reattribution/<plan-id>/journal.ndjson`, `fsync` per regel): `planned`, `copied(dest_id, created)`, `verified`, `source_removed`, `done`. `created` is `true` als deze move de bestemmingsrij heeft ingevoegd en `false` als de rij `already_present` was. Hetzelfde geldt per junction-rij: het journal legt vast welke junction-rijen deze move heeft ingevoegd. Volgorde per move: insert in de bestemming (eigen transactie), opnieuw lezen en vergelijken van `content_key` en junction-aantallen, pas dan verwijderen uit de bron (eigen transactie). Een crash laat een rij in beide bestanden staan (zichtbaar dubbel, nooit verloren). `resume` maakt het plan af vanaf het journal. `undo` zet voor elke `done`-move de voorafbeelding terug in de bron (onder de oude id als vrij, anders onder een nieuwe) en verwijdert een bestemmingsrij alleen als het journal voor die move `created: true` zegt én haar `content_key` nog gelijk is aan de voorafbeelding. Een rij met `created: false` raakt `undo` nooit aan: dan herstelt hij alleen de bronrij (als `source_removed` is bereikt). Hetzelfde geldt voor junction-rijen: alleen de rijen die deze move heeft ingevoegd worden verwijderd. Een journalregel zonder `created`, of met een waarde die geen boolean is, laat `undo` die move weigeren met een expliciete fout (fail closed): hij raadt nooit. Er is geen claim van atomiciteit over meerdere bestanden: de claim is dat geen rij verloren gaat en elke stap te hervatten of terug te draaien is. Een plan is gebonden aan de paden van de betrokken bestanden, hun `user_version` en de receipt-offsets waarop het besluit rustte. De beslissing per rij wordt bij `apply` opnieuw berekend. Is het resultaat niet meer `moves` naar dezelfde P, dan wordt de rij overgeslagen met `evidence_changed`.

### D. Goedkeuring (contract)

**Besluit 18. Leren blijft een voorstel: persist vereist een goedkeuringsrecord dat aan de exacte wijziging is gebonden.** Een niet-lege proposal-id is geen goedkeuring, want `_validate_disposition_inputs` (`scripts/learning_loop.py:1781`) eist ook alleen een niet-lege `approval_id`. Besluit 7 uit revisie 1 (een token) is hiermee **vervallen**. Het contract:

- Het record staat in `<state-dir>/learning_approvals/<proposal-id>.json` met `proposal_id`, `changes_sha256`, `approved_by`, `approved_at`, `reason` en `consumed_at` (leeg bij aanmaak).
- `changes_sha256` is de sha256 over de canonieke JSON van precies de wijzigingen die `persist_to_intelligence_db` (`scripts/learning_loop.py:1022`) zou schrijven. In shadow-modus staat dat voorbeeld al in `self.shadow["persist_preview"]` (`:1102`). Het persistpad herberekent de hash in het proces en vergelijkt.
- `VNX_LEARNING_LOOP_APPROVED=<proposal-id>` selecteert alleen het bestand en verleent niets. Een ontbrekende, lege, niet-bestaande, malformed, verlopen (`consumed_at` gezet) of niet-overeenkomende waarde weigert met een expliciete fout vóór de eerste write. `approved_by` moet een waarde uit `_DISPOSITION_VALID_APPROVERS` zijn (`:1677`).
- Eenmalig: na een geslaagde persist wordt `consumed_at` gezet. Hetzelfde record bruikbaar voor een tweede persist is een weigering.
- Een record wordt alleen aangemaakt door `python3 scripts/lib/qi_learning_approval.py approve --proposal-id <id> --reason <tekst>`, dat een interactieve terminal eist (`stdin` en `stdout` zijn een tty) en anders weigert.
- Het persistpad schrijft de record-hash in zijn receipt.
- **Het voorbehoud:** een proces met schrijfrecht op de state-dir kan een record vervalsen. De bescherming is tamper-evident en niet tamper-proof: de hash staat in het receipt, dus een vervalsing is achteraf te vinden. Een sterkere gate (een handtekening met een sleutel buiten de repo) is een aparte levering in `zelfstandigheid-meten`.
- `VNX_LEARN_PERSIST` (default `"1"`, `scripts/learning_loop.py:1554`) blijft zoals hij is. Hij is alleen bereikbaar achter de poort hierboven.

### E. Procesbesluiten

**Besluit 9. Het ADR is een eigen levering (L1).** De contracten staan in dit ADR voordat code ze gebruikt. Wijkt een bouwer af van een contract, dan wijzigt hij eerst dit ADR in een eigen PR.

**Besluit 19. ADR-nummer, afhankelijkheden en reviewklasse volgen één regel.**

- (a) Het bestand heet `ADR-<vrij nummer>-learning-signaal-attributie.md`. Het nummer is het eerste vrije op de kop waarop L1 vuurt en de T0 wijst het toe, want dertien plannen claimen hetzelfde nummer. De `adr`-preflight bewaakt de uniciteit (`tests/test_adr_numbers_unique.py`). Voor dit ADR is dat **068**.
- (b) L1 is een harde afhankelijkheid van L2, L3a en L4. Revisie 1 gaf die rijen "deps: geen".
- (c) Het aantal reviewstoelen volgt de risicoklasse in het plan. Een rij met twee stoelen en een opus-vloer is `high` en staat zo in de tabel, een rij met één stoel is `medium`. L2, L3b, L4 en L6 zijn vrijwillig `high`, naar het precedent van de corpus-quarantaine.

### F. Bewijs van klaar (contract)

**Besluit 20. Het bewijs van "klaar" meet behoud, aanwezigheid en aflevering.** `moves == 0` is ook waar na een DELETE of een lege DB. De Done-meting eist daarom vier dingen:

- **behoud**: elke geplande rij komt precies één keer in de bestemming voor en nul keer in de bron onder het bronproject;
- **aanwezigheid**: de bestemmingsrij voldoet aan de selector-predicate van de consumer (`project_id = ?` en `valid_until IS NULL OR valid_until > datetime('now')`) in het bestand dat `_get_central_qi_conn` voor het bestemmingsproject opent;
- **aflevering**: op de fixture levert `_query_central` zelf de rij aan het bestemmingsproject en niet aan het bronproject;
- **behoud bij undo**: een test waarin de bestemming dezelfde inhoud al bevat vóór de move (`already_present`, `created: false`), waarna `undo` die rij op haar plaats laat.

Het aantal moves is dat van het goedgekeurde plan, geen vast getal. Op de live-DB is het bewijs op predicate-niveau, want `_query_central` geeft maximaal 20 rijen op `confidence_score DESC`. Het bewijs op selector-niveau levert alleen de fixture. Ontbrekend bewijs is een blokkade en nooit een pass: een lege of ontbrekende uitvoer, een database die niet te openen is, een ontbrekende sleutel in de JSON of een `verify` zonder plan telt niet als klaar.

## Consequenties

- **Positief:** één regel voor eigendom die de gedefaulte body (356 van 495) laat staan en een echt misrouted single-copy wel herkent. Het scenario is vandaag 0 keer aanwezig en wordt met een fixture getest.
- **Positief:** de rij komt terecht in het bestand dat de consumer opent. Dat is de enige route die bij de consumer aflevert.
- **Positief:** een crash, drift of gewijzigd bewijs verliest nooit een rij. De ergste uitkomst is een zichtbaar dubbele rij die `resume` of `undo` herstelt.
- **Positief:** persist is een handeling die een agent structureel niet kan uitvoeren, en een vervalsing laat een hash in het receipt achter.
- **Negatief, en aanvaard:** de goedkeuring is tamper-evident en niet tamper-proof (besluit 18). Wie onder dezelfde OS-gebruiker schrijfrecht op de state-dir heeft, kan een record maken.
- **Negatief, en aanvaard:** de 570 seocrawler-antipatterns met een rapportpad in `source_dispatch_ids` blijven `unattributed`. Ze dateren van vóór de receipt-stores en een parser die pad naar id vertaalt zou een derde attributiebron zijn zonder tegenbewijs.
- **Negatief, en aanvaard:** de overdracht is een operatorhandeling met rijgerichte transacties en geen atomaire bestandswissel. Meerdere bestanden zijn niet atomair (besluit 17).
- **Volgorde:** de overdracht draait vóór `quarantine` en `purge` van `intelligence-corpus-quarantine`, nooit tussen die twee in. Op `scripts/learning_loop.py` bouwt één bouwer per keer.

## Wat dit ADR niet doet

Het bevat geen code en wijzigt geen database. Het bouwt de overdracht, de CLI en de goedkeuringspoort niet; L2 tot en met L6 doen dat, elk tegen het contract hierboven. Het poort de andere QI-schrijvers niet (besluit 10), repareert de bronbug van de gedefaulte `project_id` niet verder dan de deur al doet en maakt geen receipt, grootboekregel of hash-chain ongeldig (besluit 12).

## Amendement 2026-11-12 (L3a-c): `collision` en `no_project_column`

De repetitie op kopieën van de live databases (12-11, code `c41411fe`) liet twee fouten zien.

- **`collision`.** 14 geplande moves naar vnx-dev, waarvan `apply` er 3 weigerde op `UNIQUE (project_id, pattern_type, title)` (seocrawler-v2 `success_patterns` 2, 187 en 201). `verify` gaf `planned 14, present 11` en `report` bleef die 3 onder `moves` tellen, zodat `moves == 0` nooit kon halen. De planner leest nu de UNIQUE-indexen van de bestemmingstabel. Een bestemming die al een rij met dezelfde indexwaarden onder het bestemmingsproject heeft, geeft een `collision` (lijst `collision_rows`, telling `collision`) en nooit een move. `report` telt die rij onder `collision` en niet onder `moves`. Een index op `id` is rij-identiteit en telt niet mee; een partiële of expressie-index die niet te toetsen is, geeft `unplanned`.
- **`no_project_column`.** Een tabel zonder `project_id` (pacompany-engine, 0 rijen, ouder schema) liet planner en `report --all` crashen. Die tabel wordt nu geteld per database onder `no_project_column` en nooit een move. Een database die niet te openen is blijft een fout.
- Een `collision` is voor de operator per rij, zoals `ambiguous`: niets voegt haar samen of verwijdert haar automatisch (besluit 2: het leren stelt voor, de mens beslist). `collision_rows` en `no_project_column` zitten in het `plan_id`; `apply` hasht ze mee wanneer het plan ze draagt.
