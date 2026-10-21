# ADR-044 - De provider-schakelaar: één bestand van de operator, fail-closed, buiten de projectstore

**Status:** Accepted (dispatch dlv-6b7a5d330cdd, track provider-schakelaar, deliverable F; de schakelaar zelf kwam met A tot en met E in #2160, #2162 en de PR's daarvoor)
**Date:** 2026-10-21
**Decided by:** operator, besluiten van 12-10 (1a: één schakelaar per provider voor alle projecten met een override per project; 2b: claude en codex buiten de schakelaar in deze eerste versie; 3a: eigen track voor release 1.7.3), uitgewerkt in het plan met twee plan-gate-rondes.
**Cross-refs:** plan `claudedocs/specs/provider-schakelaar/plan.md`; `docs/core/PROVIDER_SWITCH.md` (de uitwerking voor de operator); ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-007 (`project_id`); ADR-037 (uitval van een stoel is afwezigheid, geen tegenstem); ADR-041 (tiebreaker bij de review-cap).

## Context

De operator wil providers als DeepSeek en GLM zelf aan en uit zetten, op één plek. Dat kon niet. Elke bestaande knop schakelde één poort, één consument of één route, en de consumenten die niet gedekt waren bleven bellen. Gemeten op 12-10: de review-takeover-keten, de plan-gate-stoelen, het deliberation-panel, de router, de tagger, de scout, de nachtelijke analyse en dream kozen elk hun eigen provider en keken naar een eigen sleutel. Wie deepseek uit de takeover-keten haalde, had de tagger, de scout, de tiebreaker en elke spec met een deepseek-lane nog steeds aan het bellen.

Twee dingen maken een centrale knop riskant. Een knop die per ongeluk terugvalt op "aan" brengt precies de aanroepen terug die de operator uitzette. En een knop die een worker zelf kan omzetten, is geen besluit van de operator meer.

## Besluit

### 1. Eén bestand van de operator, buiten de projectstore

`~/.vnx/providers.json` naast `~/.vnx/projects.json` en `~/.vnx/content_boundary.json`. Het bestand hoort bij de machine en bij de operator, niet bij een project. Een tabel in een projectstore zou per project moeten worden gevuld en kan een ander project niet raken. ADR-007 wordt niet geraakt: er komt geen tabel of kolom in een centrale store, project-id's staan alleen als sleutel in dit bestand.

De schakelaar benoemt de modelmaker (familie), niet de route. `deepseek` dekt deepseek-harness, `deepseek_gate`, de classifier-provider en DeepSeek via OpenRouter. Een globale sectie geldt voor alle projecten. Een projectsectie overschrijft die voor één project. De versieregel is voorwaarts compatibel: onbekende families en velden worden genegeerd en bewaard, alleen een hogere major, ongeldige JSON of een verkeerd type op een bekende familie maakt het bestand onleesbaar.

### 2. Fail-closed

Een ontbrekend bestand betekent alles aan. Een onleesbaar bestand weigert elke schakelbare familie. Een identifier of host die de familiemap niet kent wordt geweigerd. Reden: een operator die het bestand schreef, bedoelde iets. Een stille herstart op "aan" na een kapot bestand brengt de uitgeschakelde aanroepen terug zonder dat iemand het ziet. De prijs is dat een onbekende `ANTHROPIC_BASE_URL`-host of een litellm-model met een onbekende maker geweigerd blijft tot `provider_switch.py` ze kent. Dat is bewust zonder override, en `PROVIDER_SWITCH.md` zegt hoe je er een toevoegt.

`vnx providers repair` is de weg terug: het bewaart het onleesbare bestand, bouwt de toestand opnieuw op uit het append-only auditlog en schrijft hem atomair. Zonder bruikbaar auditlog is er alleen `--empty`.

### 3. De omgevingsoverride kan alleen uitzetten

`VNX_OVERRIDE_PROVIDER_<FAMILIE>` zet een familie uit voor één proces. Aanzetten kan hij niet: een on-waarde wordt genegeerd met een waarschuwing die de CLI noemt. Een andere waarde weigert de familie. Reden: de override is de noodrem. Een omgevingsvariabele is te makkelijk te erven en te vergeten, en een worker kan er een zetten. Opnieuw aanzetten gebeurt alleen via het bestand, met de CLI, met een auditregel van actor, werkmap en ouderproces. De schrijfverbs weigeren (exit 5) in een linked worktree en met een dispatch-markering in de omgeving, zodat een worker de schakelaar niet kan terugdraaien.

### 4. Claude en codex staan er in versie 1 buiten

Besluit 2b. Claude en codex dragen elke worker-lane en elke reviewstoel. Uitzetten bespaart geen kosten maar stopt de fabric. De klasse `tooling` (de `gh`-poorten) roept geen model aan, en `auto` wordt door de router naar een lane vertaald die dan wordt gecontroleerd. Alle vier zijn niet schakelbaar en altijd toegestaan. Een latere versie kan ze erbij nemen als een apart besluit.

### 5. Twee lagen, en een guard die de census bewaakt

Laag 1 is de garantie: elke functie die een niet-claude provider-proces of -verzoek start, roept `provider_switch.require` aan vóór de eerste launch. Laag 2 is het gedrag: de deur, `provider_dispatch`, de router, de takeover-keten, de panels en de intelligence-consumenten zetten een weigering om in het juiste fabric-gedrag voordat een launch-functie bereikt wordt. De coverage-guard (`tests/test_provider_switch_coverage.py`) detecteert launch-functies met een AST-scan en eist dat elke in de census staat, `require` aanroept vóór de eerste launch, of met reden is vrijgesteld. Zonder die guard omzeilt de volgende nieuwe consument de schakelaar weer.

Een weigering is geen bereikbaarheidsfeit. Ze wordt nooit geboekt als auth-, credit- of quotafout en niet geschreven naar de known-rejected-controle van de deur, de lane-cooldown of het gate-bereikbaarheidsrecord. Waar ze wordt vastgelegd, heet klasse en reden `provider_switched_off`. Opnieuw aanzetten werkt daardoor bij de volgende aanroep.

### 6. De schakelaar en de contentgrens zijn twee aparte controles

De contentgrens beslist of bepaalde inhoud naar een provider mag. De schakelaar beslist of die provider mag worden aangeroepen. Allebei moeten toestaan. Geen van beide verruimt de ander.

## Gevolgen

- **Minder router-kandidaten.** Met goedkope lanes uit kiest de router volgens zijn bestaande tier-regels uit wat overblijft. Dat kan meer claude-abonnementsgebruik betekenen. `lane_available` noemt per lane de reden van overslaan.
- **Een panel krimpt, met een vloer.** Een uitgezette stoel valt weg en wordt niet vervangen. De vloer van de plan-gate wordt berekend op het aantal stoelen waar de run recht op had (`min(2, N)`), niet op het gekrompen panel. Een panel met recht op twee of meer stoelen certificeert dus nooit op één stem. Onder de vloer weigert de run voordat er iets is verstuurd. Het deliberation-panel houdt zijn bestaande `synthesis_min_seats`. De tiebreaker weigert als zijn provider uit staat, en het spoor blijft geblokkeerd tot de provider weer aan staat of de operator attesteert.
- **Een takeover-keten kan eindigen in `chain_exhausted`.** Met kimi, deepseek en glm uit blijft alleen codex over en een PR krijgt geen tweede reviewstoel. De CLI waarschuwt bij het schrijven, en elke hop in het record noemt `provider_switched_off`, zodat dit anders leest dan een opgebruikt quotum. De merge-deur verandert niet.
- **Een onbekende identifier blokkeert totdat de map hem kent.** Een nieuwe providerwaarde moet in `provider_switch.py` en in de registry-inventaristest, anders faalt CI. Dat is de bedoeling: de fout valt in CI en niet in productie.
- **Projectcode valt buiten de schakelaar.** De schakelaar dekt de aanroepen van de VNX-engine. Een project dat zelf een provider aanroept (mission-control heeft een eigen DeepSeek-lane) blijft bellen. `vnx providers check` is de haak voor zulke code, en OI-2048 volgt mission-control.
- **Gepinde projecten kennen de schakelaar pas na de uitrol.** Een project op een engine van vóór de schakelaar negeert hem. `vnx providers list` toont per project of de engine hem kent.
- **De operator houdt één audittrail.** Elke wijziging staat met actor, tijd, bereik, oude en nieuwe waarde en reden in `providers_audit.ndjson`, append-only naast het bestand.
