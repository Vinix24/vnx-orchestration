# ADR-082 - Harde stops op de ruwe vorm: amendement op ADR-075 besluit 4 en 9

**Status:** Voorgesteld 16-11-2026 (track `permissieblokkades-leren`, levering L5a, dispatch `dlv-a3ada95bb4e6-a4`)
**Besloten door:** de plan-gate op `claudedocs/specs/permissieblokkades-leren/plan.md`: de verplichte wijzigingen op versie 2 (ronde 4) en versie 3 (`ab4410ee7e54`, ronde 6), en versie 4 (`5ee12e546c02`, tiebreaker START, ronde 8, 16-11) met `bindingen-v4.md`.
**Cross-refs:** ADR-075 (besluit 3 maskering, 4 normalisatie en `event_id`, 9 harde stops), ADR-056 (promoveren), ADR-065 (leren is voorstellen), ADR-079.

Dit ADR amendeert ADR-075 besluit 4 (de identiteit van een gebeurtenis) en besluit 9 (de classificatie); het bestand van ADR-075 verandert niet. **Deterministisch:** een vaste tabel in `scripts/lib/permission_hard_stops.py`, zonder model en zonder I/O.

## Context

De ledger bewaart alleen de gemaskeerde, genormaliseerde `vorm`, waarin veiligheidsinformatie weg is (een sleutel is `<secret>`, `cd <pad> &&` is eraf, van een kopieerblok staat alleen de eerste regel erin). De eerste L5a (#2971) herkende namen alleen op commandopositie; codex en de T0 vonden vijftien omzeilingen van één soort (een wrapper, quote of escape verborg het commando), en voorvoegsels één voor één toevoegen convergeert niet.

## Besluit

1. **De ruwe vorm, vóór het schrijven.** `permission_events.build_event` classificeert de ruwe, ongemaskeerde tekst uit de brongebeurtenis vóór maskering en normalisatie en zet alleen de uitkomst in het gesloten veld `hard_stop`. Bij B4a is de ruwe vorm het hele kopieerblok; van een ander gereedschap dan Bash is het de naam met `_` als spatie (`mcp__supabase__apply_migration` leest `supabase`), de opgeslagen `vorm` blijft de naam. De ruwe tekst komt nooit in de ledger.
2. **Fail-closed zonder bewijs.** Geen ruwe tekst (een Bash-aanroep zonder niet-lege string-`command`, `raw=None`, een maskering die faalt) is `evidence_unavailable`; een regel zonder `hard_stop` of met een onbekende waarde leest `melding` ook zo. Zo'n gebeurtenis wordt nooit een allow-voorstel.
3. **Woorden waar ook, nooit commandopositie.** Secrets draaien op de onaangeraakte ruwe tekst (de maskering verandert de tekst, of een hoofdletter-marker als `TOKEN`). De rest draait op een projectie waarin elke regelvoortzetting (`\` plus newline) eruit is, zodat de helften één woord worden, en zonder `'`, `"` en `\`: een woord waar ook, in elke schrijfwijze, met als grens geen letter, cijfer, `_` of `-` (voor `ssh`, `scp`, `sftp`, `mosh` ook geen `.`). `op` telt alleen met een eigen subcommando, ook na globale vlaggen. Recursieve `rm` is het woord `rm` plus een recursieve vlag waar ook, zonder uitzondering voor de scratchpad. `gh` of `github` plus `delete` in elke schrijfwijze, ook binnen een woord (`-XDELETE`). `git push` met `-d`, `--delete` of een refspec `:ref`/`+:ref` is `verwijderen`.
4. **Indirectie als geheel, één regel.** Na de secrets en vóór de tabel is een vorm `indirection` als hij zijn commando kan verbergen: een shell-metateken in de tekst zonder `'` en `"` maar mét backslashes (`` | ; & < > ( ) $ ` { } * ? [ ] ! ``, een newline binnen de vorm, of een backslash, dus ook pijpen, redirects, substitutie, `$'…'`, braces, globs, extglob en escapes als `\163`), een woord dat tekst uitvoert die de classifier niet leest (`sh`, `bash`, `zsh`, `dash`, `ksh`, `fish`, `csh`, `tcsh`, `source`, `eval`, `python`/`python2`/`python3`/`python3.<n>`, `perl`, `ruby`, `node`, `deno`, `bun`, `php`, `lua`, `tclsh`, `osascript`, `awk`, `gawk`, `xargs`), of `.` als commando (`. /tmp/f`). Er zijn geen opgesomde constructies of glob-naar-naam-vertaling meer: te breed is veilig, want `indirection` wordt alleen een melding, nooit een allow-voorstel.
5. **Een harde stop houdt zijn eigen `event_id`.** Is `hard_stop` niet leeg, dan gaat het mee in de invoer: `sha256(project_id|bron|sessie|ts|vorm|hard_stop)[:16]`. Is het leeg, dan is de id byte voor byte die van ADR-075 besluit 4. De validatie rekent de id op dezelfde manier na.

Te ruim herkennen geeft alleen een melding, nooit een voorstel. De promote-melding noemt de route van ADR-056: `metadata.autonomous_promotion_eligible` op de track plus een plan-gate-grant die `VERIFIED` leest.

**Test (L5a):** `tests/test_permission_hard_stops.py`: per categorie het hele pad met behouden categorie en `evidence_unavailable` zonder ruwe bron; elke vorm uit plan § Versie 4 en de bindingen; twee B4a-blokken met één eerste regel geven twee regels; een herscan schrijft niets; een schone gebeurtenis houdt de id van main.

## Gevolgen

- L5b en L7 lezen `hard_stop` via `permission_hard_stops.melding` en classificeren de opgeslagen `vorm` niet opnieuw.
- Een regel van vóór L5a heeft geen `hard_stop` en is voor L5b `evidence_unavailable`. Is hij nu een harde stop, dan schrijft de volgende scan één nieuwe regel mét de stop; een schone regel blijft één regel. L5b leest per vorm en elke melding in een groep wint.
- Wat de tekst niet draagt (de inhoud van een script, een naam die een interpreter samenstelt) valt door; daarvoor weigert L5b een regel met een wrapper of interpreter vooraan.

## Alternatieven

Afgewezen: de ruwe vorm bewaren (in strijd met ADR-075 besluit 2 en 3), en commandopositie met een lijst voorvoegsels (vijftien omzeilingen, de lijst convergeert niet).
