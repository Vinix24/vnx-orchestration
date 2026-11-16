# ADR-082 - Harde stops op de ruwe vorm: amendement op ADR-075 besluit 9

**Status:** Voorgesteld 16-11-2026 (track `permissieblokkades-leren`, levering L5a, dispatch `dlv-a3ada95bb4e6-a`)
**Datum:** 2026-11-16
**Besloten door:** de plan-gate op het plan `claudedocs/specs/permissieblokkades-leren/plan.md`: de verplichte wijzigingen op versie 2 (tiebreaker START, deepseek-v4-pro, ronde 4, 14-11) en op versie 3 (`ab4410ee7e54`, tiebreaker START, deepseek-v4-pro, ronde 6, 14-11 16:13Z).
**Cross-refs:** ADR-075 (besluit 3 maskering, 4 normalisatie, 8 dekking, 9 harde stops), ADR-056 (promoveren), ADR-065 (leren is voorstellen), ADR-079 (een bestaand ADR-bestand verandert niet).

Dit ADR amendeert ADR-075 besluit 9. Het wijzigt het bestand van ADR-075 niet (ADR-079); wie besluit 9 leest, leest dit amendement erbij.

**Deterministisch of model.** Deterministisch: een vaste tabel per categorie in `scripts/lib/permission_hard_stops.py`, zonder model en zonder I/O.

## Context

ADR-075 besluit 9 zegt welke vormen harde stops zijn, maar niet op welke tekst de classificatie draait. De ledger bewaart alleen de gemaskeerde en genormaliseerde `vorm` (besluiten 3 en 4). Daarin is veiligheidsinformatie weg: een sleutel is `<secret>`, een pad is `~/...` of `<scratchpad>`, een voorvoegsel `cd <pad> &&` of `NAAM=waarde` is eraf, en van een kopieerblok staat alleen de eerste regel erin. Een classificatie op de opgeslagen vorm kan daardoor een harde stop missen, en een gemiste harde stop kan een allow-voorstel worden. L1b houdt de ruwe vorm bewust niet vast.

## Besluit

1. **Classificatie op de ruwe vorm.** Harde stops worden geclassificeerd op de ruwe, ongemaskeerde, ongenormaliseerde vorm, gelezen uit de brongebeurtenis vóór normalisatie en maskering. Maskering en normalisatie gelden alleen voor wat naar de ledger en de dekking gaat, nooit voor de invoer van de classificatie. Een projectie die de veiligheidsinformatie houdt (de werkwoorden en markers `rm -rf`, `git push --delete`, secrets en `TOKEN`/`PASSWORD`, Supabase-migraties, `gcloud`/`hcloud`/`ssh`, `launchctl`/`systemctl`, deploys en herstarts) mag als invoer dienen. Bij B4a is de ruwe vorm het hele kopieerblok, niet alleen de eerste regel.
2. **Fail-closed zonder bewijs.** Is het ruwe bewijs voor een kandidaat niet beschikbaar of niet te reconstrueren (geen tekst, geen str, een maskering die faalt), dan wordt de gebeurtenis `alleen_melding` met `reden: evidence_unavailable`. Zo'n gebeurtenis wordt nooit een allow-voorstel. Een ledgerregel zonder het veld `hard_stop`, of met een onbekende waarde, leest L5b als `evidence_unavailable`.
3. **Classificatie vóór het schrijven, de uitkomst op de gebeurtenis.** De classificatie draait bij het lezen in de scan: de lezers B1, B2, B3 en B4a in `scripts/lib/permission_events_sources.py` geven de ruwe tekst aan `permission_events.build_event`, die classificeert vóór de maskering. Het resultaat staat op de gebeurtenis in het gesloten veld `hard_stop`: een categorie (`verwijderen`, `secrets`, `supabase`, `server`, `units`, `deploy_herstart`, `promote`), `evidence_unavailable`, of leeg voor een vorm die de tabel niet kent. De ruwe tekst komt nooit in de ledger. `event_id` blijft gebouwd op `vorm`, niet op `hard_stop`, zodat een herscan geen dubbele regels schrijft.

De categorieën en de promote-melding (`metadata.autonomous_promotion_eligible` op de track plus een plan-gate-grant die `VERIFIED` leest) blijven die van ADR-075 besluit 9. Een `rm` met een recursieve vlag telt als verwijderen, behalve als elk doel een absoluut pad onder een Claude-scratchpad is (`/private/tmp/claude-<uid>/.../scratchpad/...` of `/tmp/claude-<uid>/...`) zonder `..`; een relatief pad, een `~`-pad, een variabele en elke andere map met de naam `scratchpad` liggen erbuiten, en na `--` is elk woord een doel; een doel dat niet te ontleden is, telt als verwijderen. Een herkende vorm wordt nooit `permissie`, `bouwer_allowlist` of `bouwgewoonte`, door geen later besluit, telling of heropening; een niet-herkende vorm valt door naar de doelgroepregel van L5b.

**Test (L5a):** `tests/test_permission_hard_stops.py`: per categorie één ruwe vorm door het hele pad (lezer, scan, schrijven, teruglezen) met de categorie behouden terwijl `vorm` gemaskeerd en genormaliseerd is, en een grep op de verzonnen sleutel geeft nul treffers in de ledger; per categorie één gebeurtenis zonder ruwe bron die `evidence_unavailable` wordt; promote noemt `autonomous_promotion_eligible` en `VERIFIED`; een vorm buiten elke categorie valt door; een herscan schrijft niets.

## Gevolgen

- L5b en L7 lezen `hard_stop` van de gebeurtenis (via `permission_hard_stops.melding`) en classificeren de opgeslagen `vorm` niet opnieuw.
- Regels die vóór L5a geschreven zijn, hebben geen `hard_stop` en zijn voor L5b `evidence_unavailable`, dus nooit een allow-voorstel. Een herscan vervangt ze niet: hun `event_id` bestaat al.
- Een tabel die een vorm mist, is met een test te vinden en met een PR te repareren; te ruim herkennen geeft alleen een melding, nooit een voorstel.

## Alternatieven

- **De ruwe vorm bewaren voor L5.** Afgewezen: in strijd met ADR-075 besluiten 2 en 3, en een lek bij elke ledgerkopie.
- **Classificeren op de opgeslagen `vorm`.** Afgewezen: de maskering en normalisatie halen precies de informatie weg die de classificatie nodig heeft.
