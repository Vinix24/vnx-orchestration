# ADR-095 - De ochtendronde, contract `ochtendronde/1`

**Status:** Voorgesteld 19-11-2026 (T0, track `ochtendronde`, levering D0). Amendeert ADR-066 via cross-ref (besluit 12); het bestand van ADR-066 blijft ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-19
**Besloten door:** de operator, 19-11-2026 (elke ochtend om 07:00 Europe/Amsterdam gaan vuren en mergen dicht in elk project; learning- en dreamvoorstellen worden met hem besproken); plan v2 `claudedocs/specs/ochtendronde/plan.md` (gitignored, niet in de repo), na de plan-gate van v1 met de verplichte wijziging van de tiebreaker (besluit 2).
**Cross-refs:** ADR-007 (composite sleutel over `project_id`), ADR-063 (onbekend is nooit 0), ADR-066 (dream-liveness), ADR-072 (steering-store, `applied` en `applied_ref`), ADR-076 (de inbox als enige wachtrij van punten), ADR-079 (§ 83, bestaande ADR's zijn bevroren), ADR-086 (effectmeting vanaf het moment van toepassen), ADR-087 (een configuratie-oppervlak); onderzoek `dlv-herwerk-bronnen-onderzoek` (19-11).

## Context

Learning en dreaming leveren elke nacht voorstellen en niemand beslist erover. Gemeten op 19-11: dream archiveert zonder dat de bronrij verandert (179 archiefrijen over 86 patronen in 3 cycli), suggested edits richten zich op een bestand dat niet bestaat en kennen geen handler voor `prevention_rules`, en alleen `vnx-dev` draait learning en dream. Een ochtendpauze bestaat niet: `vnx pause` zet de receipt-processor stil, en de deur en `pr_merge.py` lezen de marker niet.

Vincent besloot op 19-11 dat om 07:00 Europe/Amsterdam vuren en mergen dichtgaan, dat lopende bouwers en poorten doordraaien, en dat de pauze eraf gaat zodra alle ochtendpunten van het project beslist zijn, uiterlijk om 09:00. Daarbij drie eisen: een voorstel gaat alleen naar iets wat een agent leest, een approve past toe, en na 14 dagen wordt het effect gemeten.

Deze ADR is het contract waartegen de elf bouwleveringen (D1a tot en met D4) bouwen. Hij bevat geen code.

## Besluit

**1. De pauze is een functie, geen opgeslagen vlag.** `is_paused(project, now, beslisstatus)` rekent uit de config-sleutels, de klok en de beslisstatus, en geeft een reden terug. De kern leest geen klok: `now` wordt geïnjecteerd. Na de deadline geeft de functie altijd `False`, ook als geen job draait, de Mac sliep of de T0 dood is. De naden zijn `stop_conditions.py` in de deur en `admit_main` in `pr_merge.py`. Eigenaar: D1a (kern), D1b (naden).

**2. Veilige activering.** `VNX_MORNING_PAUSE_ENABLED` heeft default uit. De naden van D1b doen niets zolang de sleutel uit staat; toetsen zetten hem aan via injectie. Alleen D4 zet de default aan, en D4 hangt af van D1b en D2c (en dus D2a en D2b). De pauze kan zo niet actief worden zonder herstartbestendige agenda met beslisstatus en zonder start/stop-route. Eigenaar: D1b, D4.

**3. Dagsnapshot.** Om de wektijd schrijft `tick` per project `state/ochtendronde/<YYYY-MM-DD>.json`: de lijst `point_id`'s die op dat moment open stonden, met bron, ref, advies-slot en classificatie. Het bestand wordt één keer per (project, dag) geschreven en daarna nooit herschreven. Een herstart leest het terug. Start, einde (reden `decided` of `deadline`), elke weigering en elke override zijn een regel in `state/ochtendronde.ndjson`, sleutel `(project_id, dag, seq)`. Eigenaar: D2a.

**4. De beslisstatus is afgeleid.** Een punt is beslist als zijn inboxpunt `closed` is (ADR-076 besluit 5: een punt sluit op de staat van zijn bron). De status wordt nergens apart bijgehouden; snapshot plus inbox-ledger geven na een kill dezelfde stand. Een onleesbare ledger of onbekende status telt als niet beslist (ADR-063). Eigenaar: D2a.

**5. Bronidentiteit over projecten.** Een punt is overkoepelend als zijn doelpad in de fabric ligt (`skills/`, `agents/`, `scripts/lib/prompts/`, de deur, de poorten) of als dezelfde vingerafdruk in twee of meer projecten voorkomt. Zo'n punt wordt geopend in de ledger van `vnx-dev` met `ref = project:<bronproject>/<oorspronkelijke ref>`. `point_id` volgt ADR-076 besluit 3 (`sha256(project_id | bron | ref | revisie)[:16]`), dus dezelfde bron geeft overal hetzelfde punt. Het punt sluit op de bronstaat in het bronproject. Uitvoeren gebeurt alleen via de CLI van dat project met `--project-id`, nooit met een directe schrijfactie in een andere store. Eigenaar: D2b.

**6. Pauzetelling.** Een punt telt alleen voor de pauze van het project in wiens ledger het staat. Een overkoepelend punt uit project X houdt dus `vnx-dev` vast, niet X. Eigenaar: D2b.

**7. Opslag en sleutels.** Snapshot, grootboek en ledger zijn lokaal per project; er komt geen centrale tabel. De ledgersleutel is `(project_id, dag, seq)`. Slaat Mission Control besluiten op in Supabase (`operator_decisions`), dan geldt ADR-007: `project_id TEXT NOT NULL` en een composite UNIQUE over `(project_id, point_id, decided_at)`. Eigenaar: D0 (dit besluit), D2a (ledger).

**8. Wektijd uit de projectconfig.** `VNX_MORNING_PAUSE_START`, `_DEADLINE`, `_TZ` en `_ENABLED` zijn registersleutels met scope project (ADR-087). De launchd-job heeft geen vaste tijd: hij draait elke 15 minuten `vnx ochtendronde tick`, en `tick` beslist per project of de wektijd verstreken is en de snapshot van vandaag ontbreekt. Wekken gaat via het tmux-venster van de T0 uit de laatste rotatiestate (`state/t0_rotation/<run>/delivery.json`, veld `new_window`), met Enter als aparte toets, en alleen nadat `pane_current_command` een Claude-proces toont; daarnaast een push via `push-due` (dat alleen reserveert). Zonder levende T0 opent `tick` een inboxpunt bij `vnx-dev`. Eigenaar: D4.

**9. Een voorstel gaat alleen naar iets wat een agent leest.** De leeslijst is gesloten: `CLAUDE.md`, `agents/<rol>/CLAUDE.md`, `skills/<x>/SKILL.md`, `scripts/lib/prompts/base_worker.md`. `generate` weigert elk ander doel, en elk doel dat niet bestaat, en telt de weigering in de beacon. Eigenaar: D3b.

**10. Een approve past toe.** `accept` staget een doc-dispatch met de exacte diff. Het voorstel is pas `applied` op de `pr_merged`-receipt van die dispatch, met de PR als `applied_ref` (ADR-072). Faalt de dispatch, dan blijft het voorstel `approved` en toont de agenda van de volgende dag het als open, met de dispatch erbij. Eigenaar: D3c.

**11. Een dream-akkoord maakt de bronrij ongeldig.** `approve_cycle` zet `valid_until` en `invalidation_reason` op de bronrij in dezelfde transactie als het archief. `_fetch_patterns` leest alleen rijen met `valid_until IS NULL`. Eigenaar: D3a.

**12. Dream-skip (amendeert ADR-066).** Dream slaat een cyclus over als de invoer-hash gelijk is aan die van de laatst beoordeelde cyclus of van een cyclus die nog op review wacht. De reden is `unchanged_input`; die komt in de toegestane redenen van `dream_liveness/1` (`_ALLOWED_REASONS` in `scripts/lib/dream_liveness.py`), zodat de liveness-watch de skip leest als een poging met reden en niet als een gemiste nacht. ADR-066 zelf blijft ongewijzigd; deze ADR amendeert hem via cross-ref (ADR-079 § 83). Eigenaar: D3a.

**13. Effectmeting na 14 dagen.** Bij `applied` legt `edit_history.json` de basislijn vast en `remeasure_at = applied_at + 14d` (ADR-086: meten vanaf het moment van toepassen, niet vanaf het besluit). Een nachtelijke controle schrijft de uitkomst `behouden`, `terugdraaien voorgesteld` of `niet meetbaar`. Onbekend is nooit 0 (ADR-063): ontbrekende, misvormde of onbekende meetdata geeft `niet meetbaar`, nooit `behouden`. De uitkomst wordt een ochtendpunt. Herwerk wordt ook direct op `review_gates/rounds.ndjson` gemeten, niet alleen via `rework_report.py`. Eigenaar: D3d.

## Gevolgen

- Elke levering bouwt tegen deze nummering; een afwijking is een amendement op deze ADR, geen stille keuze in code.
- Elke tak die een pauze opheft, een punt sluit of een voorstel `applied` noemt, beslist op positief bewijs. Een ontbrekend of onbekend veld houdt de pauze vast tot de deadline en laat een punt open.
- Elke bouwlevering levert per afwijzende tak een test met het veld ontbrekend, `None`, misvormd en een onbekende waarde.
- Tot D4 gemerged is, doet de VNX-T0 de ochtendronde met de hand voor `vnx-dev`: om 07:00 lokaal niets vuren of mergen, de punten met advies voorleggen, verder na besluit of om 09:00.
- Een rode main tijdens de pauze blijft herstelbaar via de override `--override-morning-pause` met `revert_or_fix_forward` of een operatorreden (D1b).
- De jobs voor D3e en D4 gaan via operating; elke merge die launchd raakt valt onder het besluit van 17-11.

## Grenzen

- Deze ADR levert geen Mission Control-code. D2c levert het schema `ochtendagenda/1`; track `agent-overzicht` M2 leest dat en zet de knoppen.
- De twaalf herwerkremedies van het onderzoek van 19-11 (inclusief P1 en P2) horen bij track `herwerk-per-model-en-reviewer`, niet bij deze track.
- Deze ADR wijzigt geen ander ADR-bestand.
