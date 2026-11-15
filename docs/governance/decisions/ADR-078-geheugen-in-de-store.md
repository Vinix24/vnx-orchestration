# ADR-078 - Geheugen in de store: een waargenomen grootboek en geldigheid zonder wissen

**Status:** Aangenomen 2026-11-14 (PR #2882, 87987b1be). Voorgesteld 14-11-2026 (T0, track `geheugen-in-de-store`, levering M1).
**Datum:** 2026-11-14
**Besloten door:** de operator voor de track (14-11-2026 ~15:58Z: "geheugenlaag: ja"; ~19:14Z: deel van 2.0). De T0 voor de uitwerking, op het plan `claudedocs/specs/geheugen-in-de-store/plan.md` (versie 2) en de plan-gate van 14-11-2026 (codex REVISE, tiebreaker START, deepseek-v4-pro, ronde 2). De verplichte wijziging van de tiebreaker is dat dit ADR de binding- en geldigheidscontracten normatief vastlegt. De meting M0 (`dlv-a65f469a2ee3`, 14-11 19:03Z tot 19:06Z) gaf de cijfers. Het nummer 078 is het eerste vrije op `origin/main` (ADR-077 is het hoogste).
**Cross-refs:** ADR-005 (append-only NDJSON als waarheid), ADR-026 (store per project), ADR-063 (onbekend is nooit 0), ADR-065 (leren is voorstellen), ADR-067 (vangst en routering van lessen), ADR-072 (geen tekst bewaren, gebonden bron), ADR-077 (afgescheurde staart).

Dit ADR legt vast waartegen M2 (binding en scan), M3 (geldigheid in de indexer), M4 (SessionStart), M5a en M5b (doctor) bouwen. Het bevat geen code. Waar dit ADR "moet" of "nooit" zegt, is dat een eis die een test kan controleren.

**Deterministisch of model.** Alles in dit ADR is deterministisch: de binding (een bestand dat de operator schrijft), de scan (bytes, sha256, een vergelijking met de laatste stand), het lezen van de frontmatter (YAML met een vaste veldvolgorde), de geldigheid (datumvergelijking met een expliciete `nu` plus een keten van hooguit 10 schakels), de OI-stand (een opzoeking in `open_items.json`) en de doctor (vaste regels per soort voorstel). Er is geen modelpad. Een notitie classificeren of samenvatten zou wel om een model vragen, en dat doet dit ADR juist niet: de waarneming moet herhaalbaar en zonder tekst zijn (besluit 2, alternatief D).

## Context

Alles in de fabric staat eerst in een grootboek (ADR-005), behalve de gecureerde geheugennotities in `~/.claude/projects/<slug>/memory/`. Claude Code schrijft ze, de operator bewerkt ze, en `scripts/lib/operator_memory_indexer.py` zet ze in een bouwopdracht. Een gewijzigde of verwijderde notitie laat geen spoor na, en een achterhaalde les weegt even zwaar als een verse.

M0 telde op 14-11 (zonder `MEMORY.md`) 533 notities voor vnx-dev, 287 voor seocrawler-v2 en 177 voor mission-control. Daarvan verwijzen er 113, 17 en 24 alleen naar gesloten OI's (`done`, `deferred` of `wontfix`). M0 vond acht plekken waar het plan niet klopt. Die staan hieronder als besluit: twee slug-coderingen (besluit 4), een notitiemap die een ander grootboek citeert (besluit 8), drie mappen die alleen via een padprefix bij een project horen (besluit 4), varianten in de frontmatter (besluit 5), dekking in plaats van budget als echte indexgrens (besluit 9), twee bestaande bindingen en een bestaande schrijver (besluit 4 en 9), en mappen met alleen een index (besluit 4). Het negende punt is het getal 109 in ADR-067 (besluit 10).

## Besluit

### 1. Schema `memory_ledger/1`

Het grootboek is `state/memory_ledger.ndjson` in de project-store van het gebonden project (ADR-026). Het is append-only: één JSON-object per regel, UTF-8, afgesloten met `\n`. Elke regel is één waargenomen gebeurtenis van één notitie, of één gebeurtenis van de scan zelf.

**Velden:**

| Veld | Inhoud |
|---|---|
| `schema` | exact `memory_ledger/1` |
| `observed_at` | tijd van de waarneming, `JJJJ-MM-DDTHH:MM:SSZ`, UTC |
| `project_id` | het project van de binding (besluit 4), nooit afgeleid uit een pad |
| `source` | de eerste 16 hex van sha256 over het opgeloste absolute pad van de gebonden map: het pad zelf staat niet in de regel |
| `note` | bestandsnaam zonder pad, met `.md`; `MEMORY.md` telt mee als notitie (besluit 9) |
| `event` | een van `added`, `changed`, `removed`, `validity_changed`, `unreadable`, `partial` |
| `sha256` | sha256 over precies de gelezen bytes van het hele bestand (besluit 7) |
| `prev_sha256` | `sha256` van de laatste stand van dezelfde `source` plus `note`; `none` op `added` |
| `frontmatter` | `ok`, `absent` (geen blok) of `unparseable` (blok dat niet als YAML-mapping te lezen is) |
| `type` | volgens besluit 5; `index` voor `MEMORY.md` |
| `node_type` | volgens besluit 5 |
| `origin_session` | `originSessionId` volgens besluit 5 |
| `valid_from`, `invalid_at`, `superseded_by` | de waarde uit de frontmatter als tekst, letterlijk zoals gelezen; `null` als het veld er niet staat |
| `reason` | alleen op `unreadable` en `partial` (zie onder) |
| `processed`, `total` | alleen op `partial`: het aantal verwerkte en het aantal te verwerken notities |

**Velden per gebeurtenis:**

| `event` | Wanneer | Velden naast `schema`, `observed_at`, `project_id`, `event` |
|---|---|---|
| `added` | een notitie zonder laatste stand, of een notitie waarvan de laatste stand `removed` is | `source`, `note`, `sha256`, `prev_sha256`, `frontmatter`, `type`, `node_type`, `origin_session`, de drie geldigheidsvelden |
| `changed` | `sha256` wijkt af van de laatste stand en de drie geldigheidsvelden zijn gelijk | dezelfde velden als `added` |
| `validity_changed` | `sha256` wijkt af en ten minste één geldigheidsveld wijkt af | dezelfde velden; één regel, geen extra `changed` |
| `removed` | de laatste stand bestaat en is niet `removed`, en het bestand staat niet meer in de map | `source`, `note`, `prev_sha256` |
| `unreadable` | besluit 7 | `source`, `note`, `reason` |
| `partial` | besluit 7 | `source`, `reason` (`budget`), `processed`, `total` |

De **laatste stand** van een notitie is de laatste regel met dezelfde `source` en `note` en een `event` uit `added`, `changed`, `validity_changed` of `removed`. `unreadable` en `partial` veranderen die stand niet. Een scan schrijft per notitie hooguit één regel. Een scan zonder wijziging schrijft 0 regels.

**Onbekend.** Een bron die er niet is (geen `originSessionId`, geen `type`, geen `node_type`, of een frontmatter die `absent` of `unparseable` is), krijgt de tekst `unknown`. Nooit een lege tekst, nooit `0`, nooit een weggelaten veld. Een geldigheidsveld dat niet in een leesbare frontmatter staat, is `null`: dat is een bekende waarde ("geen grens"), geen onbekende bron. Bij een `unparseable` of `absent` frontmatter zijn de drie geldigheidsvelden `unknown`.

Een onbekend veld, een ontbrekend verplicht veld of een `event` buiten de lijst maakt een regel ongeldig. De schrijver weigert hem en de lezer telt hem als `ongeldig` met het regelnummer, zoals ADR-077 besluit 4.

### 2. Privacy: nooit tekst uit een notitie

Het grootboek bevat nooit tekst uit een notitie: geen body, geen `description`, geen `name`, geen citaat. Dat is de regel van ADR-072. Wat erin staat zijn hashes, gesloten labels (`type`, `node_type`), een sessie-id en datums. `superseded_by` is een bestandsnaam en geldt als label. Een regel met tekst uit een notitie is een E3-fout: de PR gaat terug, ook als de tests groen zijn. M2 bewijst het met een test die de body en de `description` van elke testnotitie in het grootboek zoekt en niets vindt.

De bestandsnaam van een notitie is het label van de operator en blijft lokaal. Het grootboek verlaat de machine nooit: het staat alleen in de lokale store, gaat niet naar Supabase, niet in een receipt die gedeeld wordt en niet in een PR.

### 3. Waarnemen, niet goedkeuren

Een regel zegt "zo stond de notitie er op `observed_at`". Hij zegt nooit wie hem schreef, wie hem goedkeurde of of hij klopt. Er is geen veld `approved_by`, `author` of `trusted`. `origin_session` is een gegeven uit de frontmatter, geen bewijs van herkomst: de waarde kan met de hand gezet of gekopieerd zijn. `observed_at` is ook geen ontstaansdatum. M0 vond dat de mtimes van de vnx-dev-notities herschreven zijn, dus de scan gebruikt de mtime alleen voor de leesconsistentie (besluit 7) en zet hem niet in het grootboek.

### 4. De binding: `state/memory_sources.json`, schema `memory_sources/1`

**Eigenaar.** Alleen de operator schrijft de binding, met `vnx memory bind --dir <pad>`. Elke aanroep voegt één map toe. Geen code van de scan, de doctor, de indexer of een SessionStart-hook schrijft of wijzigt het bestand. M2 is eigenaar van de code, de operator van de binding per project.

**Vorm.** Dezelfde vorm als `capture_sources.json` en `steering_sources.json`:

| Veld | Inhoud |
|---|---|
| `schema` | exact `memory_sources/1` |
| `project_id` | het project van de store waarin het bestand staat |
| `memory_dirs` | lijst van opgeloste absolute paden, zonder dubbele |
| `bound_at` | tijd van de laatste `bind`, UTC |

Elke `bind` legt de oude en de nieuwe lijst ook vast als `state_mutation`-receipt in het eigen grootboek (ADR-005).

**Geen afgeleide slug.** De scan en de doctor leiden nergens een slug af uit een projectpad, een cwd of het register. M0 vond twee coderingen. Op schijf worden `/`, `.` en `_` een `-`. De indexer vervangt alleen `/` (`scripts/lib/operator_memory_indexer.py:168`). Voor SEOcrawler_v2 is een afgeleide slug dus fout. De resolver `_project_memory_dir` blijft voor de injectie zoals hij is. Dat die injectie voor SEOcrawler_v2 waarschijnlijk leeg is, volgt uit de code en is niet gedraaid (M0); dit ADR wijzigt de indexer daarvoor niet. De scan en de doctor lezen alleen de binding.

**Zonder binding** meldt de scan `unbound` en schrijft hij niets: geen grootboekregel en geen leeg grootboek. De doctor meldt dan alleen `unbound`.

**Wat `bind` weigert**, met de reden en exit niet 0:

1. een pad dat geen bestaande map is onder `~/.claude/projects/`, of dat na het oplossen buiten die boom valt;
2. een map met geen enkele notitie naast `MEMORY.md`. Een map met alleen een index (de negen terminal-mappen uit M0) is geen bindingsdoel;
3. een map onder een klant- of persoonlijke wortel uit `~/.vnx/content_boundary.json` (onder meer `~/Business/clients/`) voor een project dat niet zelf in die wortel staat. Een klantmap hoort nooit bij een dev-project. `bind` kent alleen de opgegeven map, dus om dit te toetsen codeert hij elke klantwortel op beide manieren (alleen `/`, en `/`, `.` en `_`) en weigert hij bij een treffer op een van beide. Die codering dient alleen om te weigeren, nooit om een map te kiezen;
4. een map die al in de binding van een ander project staat (geen gedeelde map, alternatief B).

**Mappen die M0 alleen via een padprefix kon koppelen**: de vendored map `Development-SEOcrawler-v2--claude-vnx-system`, `Development-vnx-dev-githost` en `Development-pa-engine`. Die binden alleen door een eigen `bind --dir` van de operator op precies die map. Geen code stelt ze voor, voegt ze toe of neemt ze mee als submap. Voor `Development-pa-engine` geldt ook regel 3 als het doelproject in `client_project_ids` staat.

**Relatie met de twee bestaande bindingen.** `capture_sources.json` (ADR-067) en `steering_sources.json` (ADR-072) hebben elk een `memory_dir`. Alle drie moeten dezelfde map noemen: elke `memory_dir` van die twee moet in `memory_dirs` staan. Wijkt een ervan af, of bestaat een binding waar `memory_sources.json` ontbreekt, dan meldt de doctor een voorstel `binding_divergence` met de drie waarden. Hij lost het nooit op en schrijft in geen van de drie bestanden. Dit ADR voegt de bindingen niet samen. Elk blijft eigendom van zijn track.

### 5. De frontmatter lezen

De frontmatter is het blok tussen een eerste regel `---` en de volgende regel `---` aan het begin van het bestand. Het blok wordt gedecodeerd uit precies de gehashte bytes (besluit 7) en gelezen met een veilige YAML-lezer. Het moet een mapping opleveren, anders is het `unparseable`.

Elk veld (`type`, `node_type`, `originSessionId`, `valid_from`, `invalid_at`, `superseded_by`) wordt eerst gelezen uit `metadata.<veld>`, dan uit het `<veld>` op het hoogste niveau. Is `metadata` geen mapping, dan alleen van het hoogste niveau. Bijvoorbeeld de 7 notities met `originSessionId` op het hoogste niveau (M0) krijgen hun sessie-id en niet `unknown`.

Een frontmatter die niet parset (M0: 29, 3 en 2 notities, meestal een `:` zonder aanhalingstekens in `description`) of die ontbreekt (3 notities in seocrawler-v2), is geen fout van de scan. De notitie krijgt een gewone regel met `frontmatter: unparseable` of `absent`, en `type`, `node_type`, `origin_session` en de geldigheidsvelden zijn `unknown`.

`metadata.node_type` staat in 395, 240 en 147 notities. Het grootboek legt hem vast in het veld `node_type`, als label zoals `type`. Dit ADR maakt van de waarden van `type` en `node_type` geen gesloten lijst. Het grootboek neemt een waarde over zolang die een tekst van hooguit 64 tekens is uit `[A-Za-z0-9_-]`. Een andere waarde wordt `unknown`, zodat er via dat veld geen vrije tekst in komt.

### 6. Geldigheid: `valid_from`, `invalid_at`, `superseded_by`

De velden staan in de frontmatter, bij voorkeur onder `metadata` (besluit 5). Alleen een mens zet ze, of een voorstel van de doctor dat de operator aanneemt en zelf in de notitie zet. Geen code van VNX schrijft ze (besluit 9). M0 vond ze in 0 notities.

**Datums.** Dit zijn de leesbare vormen: `JJJJ-MM-DD` (betekent 00:00:00Z van die dag), `JJJJ-MM-DDTHH:MM:SSZ` en `JJJJ-MM-DDTHH:MM:SS+00:00`. Een datum die de YAML-lezer als datum teruggeeft, geldt als 00:00Z. Een tijd zonder zone, een andere zone, of een andere vorm is onleesbaar.

**Regel.** Op een expliciet meegegeven `nu` (UTC) is een notitie **tijdelijk buiten werking** als (a) een leesbare `invalid_at` ≤ `nu`, of (b) een leesbare `valid_from` > `nu`. Een notitie gaat niet in een bouwopdracht als (a) of (b) geldt, of als (c) de keten van `superseded_by` een geldige opvolger bereikt:

- De waarde van `superseded_by` is de bestandsnaam van de opvolger, met of zonder `.md`, in dezelfde gebonden map. Een pad of een andere map telt als ontbrekend.
- De keten volgt `superseded_by` van notitie naar notitie, hooguit 10 schakels. Hij stopt bij de eerste opvolger die bestaat en niet tijdelijk buiten werking is. Dan geldt (c).
- Geldt (c) niet, omdat een opvolger ontbreekt, de keten een cyclus heeft, 10 schakels geen geldige opvolger geven, of de laatste opvolger zelf buiten werking is zonder eigen opvolger, dan blijft de notitie geldig. Er komt dan een waarschuwing met de soort (`successor_missing`, `cycle`, `chain_too_long`, `successor_not_valid`).
- De opvolger wordt op eigen merites gescoord en nooit automatisch aan een bouwopdracht toegevoegd.

**Onleesbaar of ontbrekend.** Een onleesbare datum geldt als niet gezet, met een waarschuwing `unreadable_date` en het veld. Een notitie met een frontmatter die `unparseable` of `absent` is, valt niet onder deze regel en blijft dus geldig. Een waarschuwing laat een notitie nooit stil wegvallen: liever een oude les te veel in beeld dan een les die stil verdwijnt. Elke waarschuwing wordt in M5b ook een voorstel van de doctor (`validity_warning`).

**Buiten werking is niet gewist.** Een notitie buiten werking blijft in de map en in het grootboek. Ze verdwijnt alleen uit `fetch_relevant_memories` (M3).

### 7. Scanconsistentie, herstel en tijdsgrens

**Wat de scan leest.** Elk `*.md`-bestand één niveau diep in elke gebonden map, `MEMORY.md` inbegrepen, in bytevolgorde van de bestandsnaam. Een symlink alleen als hij binnen dezelfde map uitkomt, anders `unreadable` met reden `outside_dir`. De map wordt één keer per scan opgesomd. `removed` volgt uit die ene opsomming. De scan opent elk bestand alleen om te lezen en schrijft nooit in de map.

**Eén keer lezen, die bytes hashen.** Per bestand doet de scan `stat`, leest hij de bytes één keer, en doet hij opnieuw `stat`. `sha256`, de frontmatter en de geldigheidsvelden komen alle drie uit precies die bytes. Wijken grootte of mtime af tussen de twee `stat`s, dan probeert hij het één keer opnieuw. Mislukt dat weer, dan schrijft hij `unreadable` met reden `changed_during_read`. Een leesfout geeft `unreadable` met reden `os_error:<errno-naam>`. `unreadable` verandert de laatste stand niet. De volgende scan probeert het gewoon opnieuw.

**Schrijven.** Onder een exclusieve `fcntl.flock` op `state/memory_ledger.ndjson.lock`. Elke regel plus `\n` gaat in één `write` met `O_APPEND`, gevolgd door `fsync`. De vergelijking met de laatste stand gebeurt onder hetzelfde slot, zodat twee gelijktijdige scans geen dubbele regel schrijven.

**Afgescheurde staart.** De regel van ADR-077 besluit 4 geldt letterlijk: de `\n` is de commitmarkering. Een laatste stuk zonder `\n` telt nooit als gebeurtenis. Vóór de eerste nieuwe regel zet de schrijver het onder het slot apart in `state/memory_ledger.ndjson.afgescheurd` (`offset`, `sha256`, bytes in base64), kort hij het grootboek in tot na de laatste `\n` en doet hij `fsync`. De lezer telt `afgescheurd` en `ongeldig` en crasht niet.

**Tijdsgrens bij SessionStart.** Een scan vanuit SessionStart (M4) heeft een budget van 2 seconden monotone tijd, gerekend vanaf de start van de scan. Is het budget op, dan maakt hij de lopende notitie af, schrijft hij `partial` met `processed` en `total`, en stopt hij. De volgende scan begint vooraan en slaat notities zonder wijziging over (die schrijven 0 regels), en gaat zo verder waar de vorige stopte. `vnx memory scan` vanaf de commandoregel heeft geen budget.

**Een fout breekt SessionStart nooit.** Elke uitzondering in de scan wordt gevangen. `t0_state.json` krijgt dan `memory_ledger.status = error` met `memory_ledger.reason` (soort fout en melding, zonder tekst uit een notitie). De andere standen zijn `ok`, `partial` en `unbound`. De verdere vorm van het blok legt M4 vast.

### 8. Bij welk project een OI hoort

Een notitie hoort bij het project van haar binding, nooit bij het project van het proces dat haar leest. Een `OI-<n>` in een notitie wordt opgezocht in `open_items.json` van dat project, via de lees-API van `open_items_manager.py`. M0 vond dat `_attach_open_item_status` de store van het lopende proces gebruikt, en dat seocrawler-v2 6 en mission-control 8 OI-ids citeren die hun eigen grootboek niet kent.

Een id dat het grootboek van het project niet kent, telt als `unknown`, nooit als gesloten. Een notitie is alleen kandidaat voor `invalid_at` (M5b) als ze ten minste één OI citeert, als elk geciteerd id bekend is, en als elk ervan de stand `done` of `wontfix` heeft. `deferred` telt niet als gesloten, want het probleem bestaat nog. Volgens M0 zijn dat dan 107, 16 en 22 kandidaten (in plaats van 113, 17 en 24). Eén onbekend id maakt een notitie dus nooit tot kandidaat. Een PR-verwijzing wordt alleen opgezocht in de `pr_merged`-receipts van het eigen grootboek, zonder `gh`. Een PR die daar niet staat, is `unknown`.

### 9. Wat de doctor wel en niet doet

`vnx memory doctor` leest de gebonden mappen, het grootboek, `open_items.json` en de eigen receipts. Hij schrijft alleen `state/memory_doctor.json`, per run atomisch vervangen (tijdelijk bestand en `rename`). Hij schrijft nooit in een geheugenmap, nooit in `memory_sources.json`, `capture_sources.json` of `steering_sources.json`, en nooit in het grootboek. Een test bewijst dat de map na een run byte voor byte gelijk is.

Elk voorstel heeft een stabiel `id`: sha256 over de canonieke JSON-lijst `[soort, source, note, bewijs]`. Een voorstel dat in een volgende run ontbreekt, telt als opgelost. Dit is de gesloten lijst van soorten: `invalid_at_candidate` (besluit 8), `dead_wikilink`, `index_missing_file`, `file_not_in_index` (de dekking volgt `[[naam]]` door de `*-index`-notities, zodat op vnx-dev 40 overblijven en niet 447), `index_over_budget` (200 regels of 25 KB), `validity_warning` (besluit 6), `frontmatter_unparseable`, `binding_divergence` (besluit 4) en `unbound`. Zodra de wachtrij van de track `operator-vragen-tot-antwoord` er is, gaan de voorstellen daarheen. Tot dan staan ze alleen in het bestand.

**De ene bestaande schrijver.** `scripts/apply_suggested_edits.py` (`_apply_memory_edit`) voegt een regel toe aan een `MEMORY.md`. Hij valt buiten de regel van de scan en de doctor, want hij is geen van beide. Hij schrijft alleen na een goedkeuring van de operator (`accept` en dan `apply`) volgens ADR-065, en dit ADR verandert hem niet. Zijn wijzigingen komen in het grootboek als een gewone waargenomen `changed` of `added` van `MEMORY.md`, zonder een apart herkomstveld (besluit 3).

### 10. Relatie met andere ADR's

**ADR-005.** Dit ADR dicht het laatste gat in "eerst een grootboek". Het grootboek legt waarnemingen vast, geen afgeleide stand. De stand van een notitie wordt afgeleid uit de laatste regel. Een `bind` is een besluit en krijgt een `state_mutation`-receipt.

**ADR-026.** Grootboek, binding en doctorbestand staan in de store van het gebonden project. Er is geen gedeeld grootboek en geen centrale tabel. Komt die er ooit, dan geldt ADR-007: een samengestelde sleutel over `project_id`, `source` en `note`.

**ADR-065.** De doctor stelt voor en beslist niet. Een geldigheidsveld komt alleen in een notitie door een mens of een aangenomen voorstel. `apply_suggested_edits.py` blijft de route van ADR-065 voor `MEMORY.md`.

**ADR-067.** De tabel van ADR-067 noemt het geheugen als bron van lessen. Dit ADR geeft die bron een grootboek en een geldigheid. Correctie op `ADR-067-learning-capture-routing.md:27`: de 109 daar telt verschillende OI-verwijzingen (228 refs uit 141 notities, gemeten 08-11), geen notities. M0 telde op 14-11 de notities die alleen gesloten OI's citeren: 113 in vnx-dev, 17 in seocrawler-v2 en 24 in mission-control. Met de regel van besluit 8 (`deferred` is niet gesloten) zijn dat 107, 16 en 22.

**ADR-072.** Dezelfde privacyregel: geen tekst uit de bron bewaren, alleen hashes, labels en tellingen. Hetzelfde bindingsprincipe: een bron is pas een bron als de operator hem bindt. De `memory_dir` in `steering_sources.json` moet dezelfde map noemen (besluit 4).

**ADR-077.** De regel voor een afgescheurde staart, het apart zetten en de lezer die `afgescheurd` en `ongeldig` telt, gelden ongewijzigd voor `memory_ledger.ndjson`. Een notitie die een besluit beschrijft, blijft een werkregel. Het besluit zelf hoort in het register van ADR-077.

### 11. Afgewezen alternatieven

**A. Een vectorstore als bron van waarheid.** Afgewezen: een embedding is niet herhaalbaar te hashen, niet append-only en niet terug te lezen tot de bytes die er stonden. De notitie op schijf blijft de bron, en het grootboek neemt waar.

**B. Een gedeelde opslag of graaf over projecten.** Afgewezen: dan loopt een klantmap of een ander project in een dev-project, en een OI-id zou over projecten heen betekenis krijgen die het niet heeft (besluit 8).

**C. Agents die hun eigen geheugen bewerken zonder poort.** Afgewezen: dan wordt een les stil buiten werking gezet of herschreven. Geldigheid komt alleen van een mens of een aangenomen voorstel (ADR-065).

**D. Een LLM in het tel- of classificatiepad.** Afgewezen: dan is de telling niet herhaalbaar en moet notitietekst naar een model, en de scan en de doctor moeten op dezelfde bytes hetzelfde antwoord geven.

**E. Ruwe prompttekst of notitietekst bewaren.** Afgewezen: het grootboek zou dan privétekst van de operator en van klanten dragen. Hashes en labels zijn genoeg om een wijziging te zien (ADR-072).

## Gevolgen

- Elke wijziging van een gebonden notitie, ook die van `apply_suggested_edits.py`, laat een regel achter met de oude en de nieuwe hash. Een verwijderde notitie heeft een `removed`.
- Een notitie gaat buiten werking zonder dat iets gewist wordt. Bij twijfel blijft hij in beeld en meldt de doctor waarom.
- Een project zonder binding krijgt geen grootboek en meldt `unbound`. Dat is zichtbaar, niet stil.
- SEOcrawler_v2 en elk project waarvan de map niet uit `/` naar `-` volgt, zijn bindbaar zonder slug-regel.

| Levering | Testplicht uit dit ADR |
|---|---|
| M2 | `unbound` schrijft niets; elke weigering van `bind` (1 tot en met 4); `added` voor elke notitie; tweede scan 0 regels; één wijziging geeft één `changed` met de oude hash in `prev_sha256`; `validity_changed` in plaats van `changed`; `removed`; `unreadable` na een wijziging tijdens het lezen, ook na de herhaalpoging; afgescheurde staart apart gezet; `partial` bij een budget op; frontmatter `absent` en `unparseable` geven `unknown`; `metadata.<veld>` vóór het hoogste niveau; body en `description` van elke testnotitie niet in het grootboek; geen veld leeg of `0` |
| M3 | (a), (b) en (c) apart; datum zonder tijd is 00:00Z; elke onleesbare vorm; cyclus, ontbrekende opvolger, keten van 11, opvolger zelf buiten werking: telkens geldig met een waarschuwing; de opvolger wordt niet toegevoegd |
| M4 | een gegooide fout in de scan geeft `status: error` met reden en een geslaagde SessionStart |
| M5a, M5b | de map is na een run byte voor byte gelijk; een stabiel id over twee runs; een onbekend OI-id en een `deferred` OI geven nooit `invalid_at_candidate`; `binding_divergence` wijzigt geen bindingsbestand |
