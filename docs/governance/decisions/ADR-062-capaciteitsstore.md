# ADR-062 - De capaciteitsstore: slots, veilige toelating, eigendom van de werklast en beleid van de operator

**Status:** Aangenomen voor track `rekenslot-per-machine`, RS-0; activering volgt apart.
**Datum:** 2026-11-09
**Besloten door:** de operator (Vincent van Deth), besluit 09-11-2026; uitgewerkt volgens planrev 3 en beide plan-gate-rondes van 09-11: rev 1 en rev 2 codex REVISE, telkens tiebreaker deepseek-v4-pro START met verplichte wijzigingen.
**Cross-refs:** [ADR-007](ADR-007-multitenant-project-id-stamping.md), [ADR-026](ADR-026-per-project-store-with-governance-federation.md); plan `claudedocs/2026-11-07-naar-1-9/plannen/rekenslot-per-machine.md` (rev 3, 09-11-2026 ~11:30Z).

Dit is het contract voor RS-2 tot en met RS-8, geen activering of implementatie. Besluiten 8 tot en met 14 gaan bij conflict vóór 1 tot en met 7; hieronder zijn de oudere regels daarmee verenigd. De leveringsnummers volgen de rev-3-tabel: de runnerspike is RS-10 en de hooks zijn RS-11.
Alles is deterministisch: JSON-validatie, rechtencontrole, procesidentiteit, slotboekhouding, bankierstoets, tijdvensters en modusovergangen. De schaduwrij berekent een schatting; er is geen modelaanroep nodig. Kalibratie en activering vragen meetbewijs en een operatorbesluit, geen modeloordeel.

## Context en metingen

De Mac mini (Mac16,10, 10 cores: 4P+6E, 24 GB) deelt lokaal werk van SEOcrawler, mission-control, sales-copilot, VNX-bouwers en interactieve sessies. VNX-CI op GitHub-runners belast deze host niet.
Gemeten op 08-11-2026 om 16:45: load 27,8, 0% idle, compressor 9 GB, swap 3,8 van 5,1 GB, 87 MB vrij. Niemand verdeelt de capaciteit.
De drie scripts `poort-na-load.sh`, `slot_queue.sh` en `claude_queue.sh` wachten op load, die een minuut achterloopt. Op 08-11 wachtte de SEO-poort veertien minuten tot de load twee minuten onder 12 bleef.
De in het plan aangehaalde SEO-analyse telt 117 van 216 runs met n=2 of 3 door drukte (M30); n=6 was 15% sneller dan plafond 4 (M24). Over 88 poorten was de mediaan 23,0 minuten naast ander werk en 17,5 minuten rustig. Het plan geeft deze analyses geen afzonderlijke meetdatum.
Het startbudget van 10 slots, waarvan 2 voor T0's en interactief werk, is een uitgangspunt dat fase 0 bijstelt. Een volle poort krijgt pas na meting zijn slotgetal; 1 slot per runnerjob is eveneens een uitgangspunt.

## Besluit

### 1. Geneste aanvragen delen de oudercapaciteit met een plafond

Een profiel bepaalt `slots`, `max_slots` inclusief kinderen, en toegestane `children`. Het voorbeeld runnerjob met 1 slot, plafond 8 en kinderen `seo-gate` en `builder-tests` is geen gekalibreerd beleid.
Een kind van N slots krijgt een deellease wanneer actieve deelleases samen met N binnen de G toegekende ouderslots passen; dat logt `nested` zonder tweede hosttoelating. Anders vraagt het kind het tekort bij terwijl de ouder G houdt: een kind van 8 onder een ouder van 1 vraagt 7 bij.
De ouder groeit nooit boven `max_slots` (`over_profile_ceiling`, 78); een niet toegestaan kindprofiel geeft `profile_not_allowed_under_parent`, 78. Ook tussenliggende ouders begrenzen hun kinderen; de host telt alleen wortelleases, zodat een deellease geen dubbele capaciteit boekt.
Elke nieuwe toelating en bijvraag moet na toekenning veilig blijven: er bestaat een volgorde waarin alle houders hun resterende behoefte uit vrije en eerder teruggegeven slots kunnen krijgen. Anders wacht de aanvraag met `unsafe_state`. Besluit 8 bepaalt de volgorde.

### 2. Een lease leeft zolang de werklast leeft

`vnx slot run <profiel> -- <commando>` maakt met `setsid` een werklastsessie en blijft als toezichthouder staan. Het record bewaart sessie-id en pid plus starttijd; besluit 9 preciseert de eigenaar per soort.
Vrijgeven en opruimen wachten tot de werklastsessie leeg is én alle kindleases vrij zijn. Een gestorven toezichthouder met levend werk houdt capaciteit vast (`owner_gone_tree_alive`); een verlopen heartbeat trekt geen capaciteit in.
`release` eist het juiste `lease-id:token` én een aanroeper uit de eigenaarsessie of dezelfde gebruiker als de eigenaar. Levende werklast zet de lease op `releasing`, zonder slots vrij te geven. Een verlopen token geeft `stale_token`.
Fencing geldt bij release, heartbeat en bijvraag; pid plus starttijd voorkomt verwarring door pid-hergebruik. Fencing stopt geen weesrekenwerk: de levenscheck houdt de bezette capaciteit vast. Losse `acquire` heeft altijd `--owner-pid` nodig.

### 3. Het beleid is van de operator; de toestand is gedeeld

Operating heeft op 09-11-2026 `/Users/Shared/vnx-slots` ingericht: eigenaar `vincentvandeth`, groep `vnxslot` (gid 601, leden `vincentvandeth` en `ci-runner`), modus `2770` en een overerfbaar groeps-ACL (`group:vnxslot allow list,add_file,search,add_subdirectory,delete_child,... file_inherit,directory_inherit`).
Gemeten: umask 022 levert een bestand van 0644 op dat door het geërfde ACL voor beide accounts schrijfbaar is; `ci-runner` kan de thuismap nog steeds niet lezen. De store staat buiten Werkruimte: de slotlog hoort in Time Machine.
De store bevat `state.json`, `ledger.lock` en `slotlog/`. `/Users/Shared/vnx-slots-policy/host.json` staat apart: dezelfde eigenaar-uid als de store, map 0755, bestand 0644, geen ACL en geen groepschrijfrecht. Een submap van de store zou het groeps-ACL erven en voldoet niet.
Beleid is JSON voor de stdlib-client. Elke aanvraag en `doctor` controleren storegroep, geen rechten voor anderen, overerfbaar groeps-ACL en beleidsrechten/eigenaar/ACL via `ls -le`. Ontbrekende of ongeldige gegevens weigeren met 78; ze zijn geen toestemming.
De modus staat alleen in het beleid (`off`, `observe`, `enforce`), te wijzigen door de beleidseigenaar met `vnx slot mode set`. Er is geen `VNX_SLOT_MODE` of omgevingsbudget; de aanvraag noemt profiel en project-id. `state.mode` hieronder is alleen de afgeleide uitvoeringsstatus van die beleidskeuze, inclusief de overgang.

Vertrouwensmodel, letterlijk: schrijvers van de toestand zijn samenwerkende clients. Elk lid van `vnxslot` kan `state.json` beschadigen. Dat is aanvaard, want beide leden zijn accounts van de operator. De garantie is "de host bepaalt het budget tegenover samenwerkende clients", niet bescherming tegen een kwaadwillende. Een corrupte toestand is een weigering (78), nooit `off`.

### 4. Geheugen begrenst elke zware aanvraag

`heavy: true` is een profieleigenschap; `seo-gate`, `mc-ci` en `builder-tests` zijn uitgangspunten. Metingen komen uit `sysctl vm.swapusage` en de compressorpagina's van `vm_stat`.
Een zware aanvraag wacht wanneer swap binnen 5 minuten minstens `swap_growth_mb` groeit én de compressor boven `compressor_gb` staat. Startwaarden 256 MB en 8 GB zijn een aanname uit 08-11, bij te stellen met fase 0 en `observe`.
Monsters ouder dan 120 seconden zijn niet vers; de aanvrager meet zelf waar nodig, volgens de vakkenregel van besluit 11. Herstel vereist twee opeenvolgende schone monsters uit verschillende vakken.
Bij ontbrekende meting wacht zwaar werk met `memory_metric_unavailable` tot `max_wait_s`, daarna 75. Niet-zwaar werk ondervindt deze geheugenregel niet. In `observe` worden de redenen alleen gelogd.

### 5. Eerst observeren; vier voorwaarden voor afdwingen

`observe` registreert echte leases, sessies, slots en plafonds, en laat meteen toe. De schaduwrij rekent met de `enforce`-regels en echte houdtijden: per aanvraag `would_wait`, redenen en `estimated_wait_s` met `estimate: true`.
De wachttijd is een schatting, nooit exact: afdwingen verandert zowel duurtijden als geheugendruk. Ontbrekende historie geeft geen verzonnen nul; de schatting is dan onbekend.
`vnx slot mode set enforce` weigert zolang niet alle vier activeringsvoorwaarden aantoonbaar zijn in `enforce_activation`:

| Voorwaarde | Bewijs en eigenaar |
|---|---|
| RS-1 is gedaan; het gemeten slotgetal en zijn meetdocument staan in het beleid | vnx-T0 meet drie volle SEO-poorten; SEO-T0 plant ze, op verzoek van vnx-T0 via tmux. |
| doctor geeft exit 0 als vincentvandeth én als ci-runner | vnx-T0 verzorgt de eigen run; operating draait als `ci-runner` met sudo en logt de uitslag; de activering citeert die logregel. |
| minstens 7 dagen observe met zware aanvragen uit minstens twee projecten | vnx-dev via RS-9; SEOcrawler via een eigen PR van SEO-T0. vnx-T0 levert wrapper/runbook en vraagt de omzetting. |
| een besluit van de operator met datum en verwijzing | De operator beslist na het meetbewijs. |

`off` naar een andere modus gaat eerst naar `observe`: onder `off` is lopend werk niet geadministreerd. Naar `off` teruggaan laat bestaande leases staan tot de werklast weg is. Besluit 10 bepaalt de afvoer naar `enforce`.

### 6. De runner krijgt een leesbare, zelfstandige client

`ci-runner` leest niets onder `~vincentvandeth` (0700). `vnx slot install-client` installeert een stdlib-only Python-pakket in `/Users/Shared/vnx-slot-client/`, leesbaar voor dat account, met de eigendomsregels van het beleid en zonder ACL. Het draait met `/usr/bin/python3` en importeert niets uit de VNX-repo.
VNX levert client, installatie en hooks. Operating zet `ACTIONS_RUNNER_HOOK_JOB_STARTED` en `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` in de runner-`.env` via `scripts/runner-registreren.sh`; mission-control regelt `runs-on` in PR B.
Nesting herkent de ouder aan de sessie van de aanroeper; `VNX_SLOT_LEASE` is hooguit een hint. Geen leasedoorgifte via hookomgevingsvariabelen is vereist.
RS-10 meet onder launchd als `ci-runner`, met echte runner en testrepo: normaal einde, annulering, runnerherstart en tweede jobpoging. Het legt account/omgeving van beide hooks, jobsessie, de drie GitHub-labels in beide hooks en eventuele doorgifte via `GITHUB_ENV` vast.
Negatief bewijs over hookaccount/omgeving, jobsessie of joblabels betekent planrevisie vóór RS-11. De adapter wacht begrensd per profiel (uitgangspunt 30 minuten), met herkenbare weigering in de joblog; een defect budget is nooit succes.

### 7. Componenten hebben een eigen grens

| Component | Interface en verantwoordelijkheid |
|---|---|
| RS-2 `slot_store.py` | Opent store/beleid, toetst rechten en ACL, leest JSON; `transaction()` leest en schrijft atomair onder de controlelock; levert `doctor`. |
| RS-3 `slot_admission.py` | Pure functies over toestand en beleid: veilige reeks, aanvragen, deelleases, bijvragen, fencing en opruimbesluit met levenscheck als invoer; geen IO. |
| RS-4 `slot_liveness.py` | Geeft positief vastgesteld leven/einde of onbekend voor sessies, pid/starttijd, gevonden afstammelingen en recursieve kindleases. |
| RS-5 `slot_memory.py` | Levert monsters, tijdvenster, groei/niveau en herstelbesluit. |

> Aanvraag → storetransactie → toelating met levenscheck en geheugenmeting → lease plus slotlog; supervisor bewaakt de werklast.

Eén `ledger.lock` gebruikt `flock(LOCK_EX)` alleen voor de korte kritieke sectie. `state.json` gaat via tijdelijk bestand en `os.replace` binnen die lock. Clients pollen elke 2 seconden; rijlengte en wachttijd zijn begrensd.
Alleen het flock-patroon uit `dispatch_serialization.py` wordt hergebruikt. Diens enkele vrije slot, `Path.home()` met 0700, omgevings-N en `force_release` voldoen niet. Een onbekende levenscheck is nooit bewijs om capaciteit opnieuw uit te geven.

### 8. Bijvragen volgen de veilige reeks

Elke ronde onder de controlelock sorteert de huidige houders op resterende behoefte (`max_slots` min toegekend). Een houder past in de veilige reeks als zijn behoefte past in vrije slots plus de slots die eerdere houders teruggeven.
Eerst worden bijvragen in die reeks bekeken; toekenning mag alleen als de toestand daarna veilig blijft. Voor pool 8, A op 1 met plafond 8, B op 1 met plafond 7 en 6 vrij, krijgt B eerst 6. Na B's einde komen 7 vrij en krijgt A zijn 7.
Daarna volgen nieuwe aanvragen in aankomstvolgorde: alleen de voorste mag door als de toestand veilig blijft; een latere nieuwe aanvraag passeert nooit een eerdere. Een al onveilige toestand laat geen nieuwe toelating toe.
`bypassed_n` telt en logt hoe vaak een wachtende aanvraag gepasseerd is. Boven `max_bypass` mag geen bijvraag meer voorgaan die zijn eigen toelating onveilig zou maken. Eindigheid van houders is de aanname achter de eerlijkheidsredenering, geen gemeten looptijdgarantie.

### 9. De eigenaar is de werklast, ook over kind-sessies

Een `run`-lease hoort bij de nieuwe werklastsessie. Een geneste `run` maakt een eigen sessie en bewaart `parent_lease`. Een ouder blijft bestaan zolang zijn eigen sessie of enige kindlease leeft, recursief; ook als zowel oudersessie als geneste toezichthouder verdwenen zijn.
Voor losse `acquire` is `--owner-pid` verplicht en wijst hij het werkproces aan, niet een blijvende shell; geen standaard op de aanroepersouder. De lease blijft zolang eigenaar (pid plus starttijd) of een eerder via de `ppid`-keten gevonden afstammeling leeft. Iedere controle zoekt en onthoudt zulke afstammelingen.
Een geldig releaseverzoek wacht op het verdwijnen van eigenaar en gevonden afstammelingen. Voor een capaciteitsgarantie bij werk met kinderen is `run` de aangewezen weg. Een runnerlease hoort bij de jobinstantie van besluit 12; RS-10 moet bewijzen dat jobs buiten de blijvende `Runner.Listener`-sessie lopen en dat hun sessie na afloop leeg is.

### 10. Enforce begint met leeglopen

`vnx slot mode set enforce` zet na de activeringspoort eerst `enforce_pending`. Bestaande observe-leases worden als `grandfathered` gemarkeerd en lopen uit met hun reservering; hun bijvragen worden meteen toegekend en als `grandfathered` gelogd. Nieuwe aanvragen wachten.
De uitvoering wordt `enforce` zodra er geen grandfathered lease meer is óf de toestand met de echte plafonds veilig is; resterende leases verliezen dan de grandfathered-markering, met behoud van een lopend releaseverzoek. De log bevat de overgangsduur. Na `max_transition` valt de uitvoering terug naar `observe`, met een logregel, nooit naar `off`.
Het overgangsrecord bewaart die afloop: een poll of herstart herhaalt een verlopen overgang niet vanzelf. Een nieuw expliciet moduscommando kan opnieuw activering en overgang starten. Het record is geen tweede bron van operatorbeleid.

### 11. Tijdretentie en zwaar per aanvraag

Er is hostbreed maximaal één monster per 30-secondevak; een schrijver schrijft alleen als dat vak leeg is. Retentie is de laatste 10 minuten, niet een vast aantal monsters. Ook zonder supervisor meet een aanvraag zelf wanneer het huidige vak leeg is.
Voor de groeiregel zijn binnen het venster van 5 minuten twee bruikbare monsters nodig die minstens 4 minuten uiteen liggen; gebruik het vroegste en laatste bruikbare monster. Bij te weinig historie geldt de niveauregel: `swap_used >= swap_level_mb` én `compressor >= compressor_gb`.
`swap_level_mb` is een beleidsdrempel zonder startgetal in het plan; dit ADR verzint dat niet. Herstel vraagt twee opeenvolgende schone monsters uit verschillende vakken.
Zwaar hoort bij de aanvraag: nieuw, geneste deellease en bijvraag worden elk getoetst. Ook een zware `seo-gate` binnen al toegekende slots van een lichte runnerouder krijgt de geheugencontrole. Tijdens `observe` en voor grandfathered bijvragen blijft dit een logbesluit, overeenkomstig besluiten 5 en 10.

### 12. De jobinstantie heeft een eigen identiteit

De sleutel is `RUNNER_NAME` plus pid en starttijd van de `Runner.Worker` van die job. Eén geregistreerde runner draait één job tegelijk. `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT` en `GITHUB_JOB` zijn labels, geen sleutel.
De starthook schrijft `lease-id:token` naar `$RUNNER_TEMP/vnx-slot-lease`; de completed-hook leest dit bestand en geeft precies die lease vrij met het token en de levensregels.
RS-10 draait twee matrix-instanties tegelijk op twee runners en bewijst verschillende sleutels en juiste releases. Is maar één runner aanwezig, dan toont de spike dat de instanties na elkaar lopen.

### 13. Supervisor, schaduwrij en CLI zijn afzonderlijke leveringen

| Component | Interface en verantwoordelijkheid |
|---|---|
| RS-6 `slot_supervisor.py` | Maakt sessies en kindleases, verzorgt heartbeat en monsters; gebruikt RS-4/RS-5. |
| RS-7 `slot_shadow.py` | Simuleert toelating via RS-3; geeft `would_wait`, redenen en gelabelde wachttijdschatting. |
| RS-8 CLI | Verbindt componenten voor `acquire`, `run`, `status`, `release`, `mode`, slotlog, overgang en activeringspoort. |

RS-2/3/4/5 kunnen na RS-0 apart bouwen; RS-8 volgt RS-2/3/6/7. Levenscheck, supervisor, toelating, store en CLI krijgen reviewpoort `high`; geheugen en schaduwrij `medium`. De levenscheck beslist immers of bezette capaciteit opnieuw wordt uitgegeven.

### 14. Inrichting en activering hebben eigenaars

Elk bestand dat de module in de gedeelde store maakt, inclusief tijdelijke bestanden, gaat via `os.open` plus `os.fchmod(fd, 0o660)`. Dit vervangt de oudere opmerking dat 0660 vervalt. Het geldt niet voor het afzonderlijke beleid en de client, die de rechten van besluit 3/6 houden.
Operating toonde het geërfde ACL bij 0644 aan in `~/.local/state/vnxslot/aanmaken.log` (`TOETS GESLAAGD`). Setgid is op macOS niet nodig; 2770 blijft als signaal. Terugweg: `~/Business/operating/scripts/vnxslot-groep.sh --terug`, uitsluitend bij een lege map.
RS-9 maakt in fase 2 met `vnx slot bootstrap` de beleids- en clientmap als `vincentvandeth`, zonder sudo (`/Users/Shared` is 1777), en installeert met `install-client` de stdlib-client plus `doctor`. RS-11 levert daarna de hooks.
De activeringseigenaars staan in besluit 5. Runnerprofielen gaan pas naar enforce na RS-11 én een positieve RS-10. Tot dan houdt `runner_reserve` ruimte vrij voor runnerjobs die het slot nog niet kennen.
De MC-runner draait tijdelijk weer als `vincentvandeth`; omzetting wacht op MC PR B (`runs-on` via `CI_RUNNER`), afgestemd met MC-T0. SEO en MC beheren hun eigen PR's; VNX levert wrapper, client en runbook.

## Gedeeld contract voor RS-2 tot en met RS-8

Dit deel concretiseert veldnamen en typen waar het plan alleen gedrag geeft. JSON-getallen zijn eindig; slotgetallen zijn gehele aantallen, geen booleans. Tijden zijn UTC epochseconden, duurvelden seconden; processtarttijd is de stabiele OS-startidentiteit, geen heartbeat. Onbekende enums, ontbrekende verplichte velden en corrupte JSON zijn 78, geen lege of vrije toestand. `null` is uitsluitend toegestaan waar hieronder genoemd.

### Toestand: state.json

| Veld | Type en betekenis |
|---|---|
| `holders` | Object van `lease_id` naar leaserecord, inclusief kinderen; alleen records zonder `parent_lease` belasten de hostpool. |
| `queue` | Geordende lijst aanvraagrecords; volgorde bewaart aankomst, de selectie volgt besluit 8. |
| `samples` | Op tijd gesorteerde lijst monsters: `bucket_start`, `measured_at`, `swap_used_mb`, `compressor_gb`, `status` (`ok` of `unavailable`); waarden alleen bij `unavailable` null. Vakstart is epoch afgerond op 30 seconden; retentie 600 seconden. |
| `mode` | Afgeleide uitvoering: `off`, `observe`, `enforce_pending`, `enforce`; nooit een beleidsinvoer of omgevingsschakelaar. |
| `fencing_counter` | Monotoon oplopend geheel getal; elke nieuwe lease krijgt het volgende token, nooit hergebruiken bij release/reap. |
| `transition` | Null buiten een overgangshistorie; anders `started_at`, `finished_at` (null zolang pending), `outcome` (`pending`, `activated`, `timed_out`). Een timeout houdt uitvoering observe tot een nieuw expliciet moduscommando. |
| `shadow` | Schaduwtoestand met `holders` en `queue` in dezelfde vormen, afzonderlijk van echte reserveringen; schaduwvelden geven nooit capaciteit vrij. |
| `memory_recovery` | `blocked` (boolean) en `clean_buckets` (vakstarts van maximaal de laatste twee opeenvolgende schone monsters); hetzelfde vak telt maar eenmaal. |

Lege beginlijsten worden alleen bij bewuste initialisatie gemaakt; een verdwenen/beschadigde bestaande toestand is geen nieuwe lege store. Vrij is `total - reserved - runner_reserve` minus de toegekende wortelslots; reserveringen en dubbele kindtelling mogen nooit als vrij bewijs gelden. Binnen een ouder tellen de toegekende directe kindslots tegen diens budget; recursieve bijvragen respecteren alle ouderplafonds. Gelijke resterende behoefte wordt stabiel gebroken op `admitted_at`, daarna `lease_id`.

| Aanvraagveld in `queue` | Type en betekenis |
|---|---|
| `request_id`, `requested_at` | Hostbreed unieke string en ontvangsttijd; lijstpositie breekt gelijke aankomsttijden. |
| `kind`, `lease_id`, `parent_lease` | `new`, `nested` of `top_up`; betrokken lease/ouder-id, null als nog niet van toepassing. Een wachtende deellease kan geheugen missen zonder extra hostslots te vragen. |
| `profile`, `project_id`, `uid` | Niet-lege profiel/projectstrings en gebruiker-uid van de aanvraag. |
| `slots`, `max_slots`, `owner` | Gevraagde profielslots (bij top-up: tekort), toepasselijk plafond en eigenaarvelden uit het leaserecord; budgetwaarden komen uitsluitend uit beleid. Alleen bij een wachtende nieuwe run mogen de nog te maken werklast-pid/starttijd/sessie null zijn; bij een toegekende lease zijn die bekend. |
| `bypassed_n`, `reasons` | Niet-negatieve teller en lijst redencodes; iedere passage wordt zichtbaar in de log. |
| `deadline_at` | `requested_at + profiles[profile].max_wait_s`; een poll verlengt de wachttijd niet. |

### Eén leaserecord

| Veld | Type en betekenis |
|---|---|
| `lease_id` | Hostbreed unieke, niet-lege string; sleutel in `holders`. |
| `token` | Geheel fencingtoken uit de teller; vereist bij heartbeat, bijvraag en release; geen logveld. |
| `profile`, `project_id` | Niet-lege strings; project-id wordt expliciet aangeleverd, nooit stil `vnx-dev`. |
| `slots_granted`, `max_slots` | Toegekende slots en beleidsplafond inclusief kinderen; childslots zitten binnen oudertoekenning. Observe kan de hostpool overschrijden, niet profielplafonds omzeilen. |
| `owner_kind` | `run`, `acquire` of `runner`. |
| `session_id` | Werklastsessie voor run/runner; bij acquire alleen context, geen vervanging voor de procesboomcheck. |
| `owner_pid`, `owner_start_time`, `owner_uid` | Identiteit van werklastleider bij run, expliciete werkproces bij acquire, `Runner.Worker` bij runner; uid voor releaseautorisatie. |
| `supervisor_pid`, `supervisor_start_time` | Toezichthouderidentiteit bij run; null bij soorten zonder supervisor. |
| `parent_lease` | Lease-id van directe ouder, anders null; relaties zijn acyclisch en verwijzen naar bestaande leases. |
| `state` | `active`, `releasing` of `grandfathered`; elk blijft capaciteit tellen tot de levenscheck vrijgeven toestaat. |
| `grandfathered` | Boolean voor observe-erfenis tijdens overgang; blijft waar als de zichtbare state `releasing` wordt, zodat release het drainbesluit niet vervalst. |
| `admitted_at`, `heartbeat_at` | Tijd van toelating en laatste geldige heartbeat; geen TTL die levend werk wegneemt. |
| `descendants` | Bij acquire onthouden lijst van `pid` plus `start_time`, gevonden via ppid; bij andere soorten leeg. |
| `runner` | Bij runner: `name`, `worker_pid`, `worker_start_time` en labels `github_run_id`, `github_run_attempt`, `github_job`; anders null. Workeridentiteit komt overeen met eigenaarvelden. |

### Beleid: host.json

| Veld | Type en betekenis |
|---|---|
| `total`, `reserved` | Positief totaal en niet-negatieve interactieve reserve; 10 en 2 zijn uitgangspunten, geen meting. |
| `profiles` | Object op profielnaam; onbekende namen zijn een beleidsfout, geen automatisch profiel. |
| `profiles.*.slots`, `profiles.*.max_slots` | Positieve gehele aantallen, eerste vraag hooguit plafond; het SEO-slotgetal volgt RS-1. |
| `profiles.*.children`, `profiles.*.heavy` | Lijst toegestane profielnamen en boolean voor geheugencontrole per aanvraag. |
| `profiles.*.max_wait_s` | Positieve maximale wachttijd; runneruitgangspunt 1800 seconden. |
| `memory.swap_growth_mb`, `memory.swap_level_mb`, `memory.compressor_gb` | Niet-negatieve geheugendrempels in de genoemde eenheden; 256 MB groei en 8 GB compressor zijn aannames; niveaudrempel moet de operator bepalen. |
| `max_bypass` | Niet-negatief geheel aantal passages voor de eerlijkheidsgrens van besluit 8; plan geeft geen startwaarde. |
| `max_transition` | Positieve maximale overgangsduur in seconden; geen startwaarde in het plan. |
| `max_queue` | Positieve maximale rijlengte; naam concretiseert de begrensde rij uit Approach, zonder een grens te verzinnen. |
| `runner_reserve` | Niet-negatieve gereserveerde slots voor nog niet aangesloten runners; samen met `reserved` kleiner dan `total`. Pas na RS-11 en positieve RS-10 aanpassen zonder dubbel reserveren. |
| `mode` | Uitsluitend `off`, `observe`, `enforce`, geschreven door beleidseigenaar; pending is een uitvoeringsstatus. |
| `enforce_activation` | Object met de vier bewijsrecords hieronder; leeg/onbekend is geen geslaagde activering. |
| `enforce_activation.calibration` | `measured_at`, `measurement_ref`, `seo_slots`; bewijs van RS-1 op drie volle poorten, slotgetal gelijk aan het SEO-profiel. |
| `enforce_activation.doctor` | Per account `vincentvandeth` en `ci-runner`: `checked_at`, `exit_code` exact integer 0 en `evidence_ref`; operating-logregel voor ci-runner. |
| `enforce_activation.observe` | `started_at`, `ended_at`, `project_ids`, `evidence_ref`; minstens 7 dagen en zware aanvragen van minstens twee verschillende projecten in de slotlog. |
| `enforce_activation.operator` | `decided_at`, `decision_ref`; verwijst naar het expliciete besluit tot activering. |

Bewijsverwijzingen mogen in het beleid staan, niet als pad in de slotlog. Veldpresentie alleen bewijst de vier voorwaarden niet: de validator verifieert inhoud, duur, identiteit en positieve uitkomsten. Ontbrekend, null, malformed of onbekend bewijs blokkeert activering; een policy-fout maakt nooit vanzelf `off`.

### Slotlog: gebeurtenissen en velden

De host schrijft NDJSON naar `slotlog/YYYY-MM.ndjson`, zonder paden, commando's, tokens of secrets. Elke regel draagt project_id. Ook modusbeheer gebruikt het expliciete project-id van de aanroeper; onderhoud aan een lease neemt dat van de lease over.

| Velden op iedere regel | Betekenis |
|---|---|
| `ts`, `event`, `project_id`, `uid`, `mode` | Tijd, gebeurtenis, project, gebruiker en effectieve uitvoeringsstatus. |
| `request_id`, `lease_id`, `parent_lease`, `profile`, `slots` | Correlatie en capaciteit; null uitsluitend als het gebeurtenistype geen aanvraag/lease/profiel heeft. |
| `wait_s`, `hold_s`, `swap_used_mb`, `compressor_gb` | Gemeten duur en geheugen; null wanneer nog niet meetbaar of niet van toepassing, nooit een verzonnen 0. |
| `reasons`, `bypassed_n` | Redencodelijst (leeg bij onvoorwaardelijke toekenning) en passageteller, null zonder aanvraag. |

| Event | Moment en aanvullende velden |
|---|---|
| `requested` | Ontvangst van een aanvraag, gevraagd aantal slots. |
| `admitted` | Echte nieuwe toelating, `slots_granted`, `max_slots`. |
| `would_wait` | Schaduwuitkomst per observe-aanvraag: `would_wait` boolean, `estimated_wait_s` (null bij onbekend), altijd `estimate: true`. |
| `waited` | Werkelijk wachten/herbeoordelen, verstreken `wait_s` en actuele redenen/passages. |
| `refused` | Weigering, `exit_code` exact 75 of 78 en concrete reden. |
| `topped_up` | Bijvraag toegekend: `slots` is toename, `slots_granted` het nieuwe totaal; `grandfathered` boolean. |
| `nested` | Deellease binnen ouderbudget; `parent_lease` verplicht. |
| `released`, `reaped` | Respectievelijk releaseverzoek afgerond en opruiming na levenscheck; werkelijke houdtijd. |
| `lease_held` | Nog levende werklast na verdwijnen toezichthouder, reden `owner_gone_tree_alive`. |
| `mode_changed` | Beleidsuitvoering verandert: `from_mode`, `to_mode`, `transition_s` (null buiten overgang); timeout vermeldt `transition_timeout`. |

### Redencodes en exitcodes

| Redencode | Betekenis / gevolg |
|---|---|
| `budget_full`, `queue_ahead`, `unsafe_state` | Capaciteit ontbreekt, eerdere nieuwe aanvraag wacht of bankierstoets faalt: wachten/schaduwreden. |
| `memory_guard`, `memory_metric_unavailable` | Zwaar werk wacht op herstel of bruikbare meting; in observe alleen loggen. |
| `over_profile_ceiling`, `profile_not_allowed_under_parent` | Plafond overschreden of kindprofiel verboden: 78. |
| `stale_token`, `owner_mismatch`, `owner_pid_required` | Fencing/autorisatie ongeldig of verplichte acquire-eigenaar ontbreekt: 78. |
| `owner_gone_tree_alive` | Werklast of kindlease leeft nog; capaciteit blijft bezet. |
| `store_invalid`, `policy_invalid`, `state_corrupt` | Rechten, beleid/budget of toestand niet geldig: 78, herstel door operator. |
| `activation_incomplete` | Niet alle vier voorwaarden positief bewezen: 78. |
| `enforce_pending`, `grandfathered`, `transition_timeout` | Wachten op drain, observe-erfenis of terugval naar observe met overgangsduur. |
| `queue_full`, `wait_timeout` | Rijgrens of maximale wachttijd bereikt: 75; onderliggende wachtredenen blijven in de log. |

| Exitcode | Betekenis |
|---|---|
| 0 | Slotbewerking geslaagd; observe laat toe met schaduwuitkomst. Geen bewijs van geslaagde werklast. |
| 75 | Tijdelijke capaciteitsweigering na maximale wacht of volle rij; de gevraagde toekenning blijft uit, al lopend ouderwerk behoudt zijn lease. |
| 78 | Ongeldige configuratie, rechten, toestand, aanvraag, eigenaar/token of activering; geen automatische omzeiling. |

Deze codes betreffen de slotbewerking; `run` geeft na daadwerkelijke start de exitstatus van de werklast door. Een expliciet door de operator gekozen `off` omzeilt toelating; defecte toestand of defect beleid selecteert nooit die modus. Bestaande leases worden niet gewist bij rollback.

## Uitzondering op ADR-026 en toepassing van ADR-007

ADR-026 houdt de per-project governance-store canoniek. Alleen de fysieke hostcapaciteit krijgt een hostbrede store buiten de projectstores, zodat ook werk zonder VNX-dispatch meedoet. Er zijn geen slotreceipts, geen centrale tabel en geen gedeelde database; de maandelijkse slotlog is de lezer. De bestaande governance-rapportage van dispatches verandert niet.
ADR-007 blijft gelden: project_id in elke logregel, expliciet en zonder stil standaardproject. Er is geen tabel, dus geen composite key of schema-migratie; dit is geen uitzondering voor toekomstige centrale tabellen.

## Rollback per fase en grens van het besluit

| Fase | Leveringen en terugweg |
|---|---|
| 0 | RS-0/RS-1 besluiten en meten; geen toelating actief. Stop meetwerk of draai documentwijziging terug; bestaande poorten blijven draaien. |
| 1 | RS-2 t/m RS-8 componenten en CLI, eerst observe. Beleidseigenaar zet `vnx slot mode set off`; lopende leases blijven tot de werklast weg is. Geen geforceerde release of verwijderen van toestand. |
| 2 | RS-9 bootstrap, client, wrapper en pytest-sessiehaak alleen bij VNX-dispatch. Zet off en draai wrapper/haak terug naar de oude scripts; die blijven tot deze fase stabiel is. |
| 3 | RS-10 spike, daarna RS-11 hooks. Haal hookinstellingen uit runner-`.env` en zet beleid off; herstel oude runnerroute. Operating kan de groep uitsluitend bij een lege map terugdraaien. |

Dit ADR bouwt geen tweede machine en geen multi-machine scheduler; geen daemon, receipts of centrale capaciteitstabel. Geen enforcement voor runnerprofielen vóór RS-11 en positieve RS-10. Het vervangt geen projectpoortregels en voert SEO/MC-omzettingen niet zelf uit.
Fases 0–3 zijn pas klaar als de rev-3-acceptatiepunten per levering gemeten zijn (de tabel telt RS-0 t/m RS-11). De hele track vraagt daarnaast alles door één slot na SEO/MC-omzetting en een nul-/nameting na een week enforce; de tweede machine blijft `later` of wordt expliciet naar een volgende track verplaatst.

## Restrisico's en open metingen

- Een proces dat zelf een nieuwe sessie opent ontsnapt aan de telling.
- Een afstammeling van een losse acquire die naar launchd verhuist voordat hij gezien is, ontsnapt.
- Een lid van vnxslot kan de toestand beschadigen.

Slots kunnen de werkelijke last verkeerd schatten; daarom kalibratie en observe. Geheugendrempels kunnen te vaak blokkeren; daarom meetwaarden, herstel en bijstelling in beleid. Een runnerhook kan jobs stilzetten; daarom begrensde wacht, zichtbare weigering en rollback. Maandlogs groeien bewust mee in Time Machine.
Open meetvragen blijven het echte SEO-slotgetal inclusief subprocessen buiten xdist, de geheugendrempels en de jobsessie/hookomgeving. SEO/MC bepalen hun omzettingsmoment; VNX verzorgt de bootstrap en `doctor` toetst. Dit ADR claimt geen gemeten activering.
