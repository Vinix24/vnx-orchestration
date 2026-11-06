# ADR-052 - Geen functie onder `scripts/` is langer dan 200 uitvoerbare regels

**Status:** Accepted (dispatch dlv-00ba568c901a, track functies-onder-200, kind van golf-2-hardenen, G2-15)
**Datum:** 2026-11-06
**Besloten door:** T0, op de planregel G2-15 van `golf-2-hardenen` en op de plan-gate-eisen 1 en 3 van de track `functies-onder-200`. Dit ADR legt de grens, de meting en de handhaving vast. Alle metingen staan op main `b078362f`; ADR-052 was daar vrij.
**Cross-refs:** plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/golf-2-hardenen.md` (rij G2-15); G2-0 (de meting, PR #2400); OI-2570 (een lege meting gaf exit 0); ADR-046 (de poortmotor); ADR-030 (de plan-first-gate).

## Context

De track `functies-onder-200` eist dat geen functie onder `scripts/` langer is dan 200 uitvoerbare regels, en dat dat zo blijft. Tot G2-0 was er alleen een ratchet per pull request: `scripts/ci/check_function_size_ratchet.py` vergelijkt basis en head en keurt een **nieuwe of gewijzigde** functie boven de 70 regels af. Die ratchet ziet een functie die al op main staat en nooit meer wordt aangeraakt niet. Een boom vol lange functies kan dus groen blijven zolang niemand ze aanraakt — precies de toestand die de track wil opruimen.

G2-0 (`scripts/ci/check_function_size_tree.py`, PR #2400) maakte de meting over de hele boom op een git-ref herhaalbaar. Wat ontbrak was de handhaving, en één faalvorm in de meting zelf: een `--prefix` die niets raakt (gemeten door de T0 op `24ef4c40`: `--prefix scrips/` gaf "0 functions in 0 files" en exit 0). Nul gemeten functies is geen bewijs dat de boom klein is; het is een typefout. OI-2570.

De grens van 200 is een **toestand** van de boom, niet een eigenschap van een diff. Daarom is de handhaving een eigen CI-stap die de hele boom op `HEAD` meet, naast — niet in plaats van — de ratchet van 70.

## Besluit

### 1. De grens

**Geen functie onder `scripts/` is langer dan 200 uitvoerbare regels.** De grens geldt voor elke functie in de zin van `function_size_gate.measure_source` (`scripts/lib/function_size_gate.py:258`): een `def`, `async def`, geneste functie inbegrepen.

- **De scope is de bestaande scope-regel.** Een pad telt mee als het op `.py` eindigt, onder `scripts/` ligt en geen paddeel `tests` of `spikes` heeft — `_in_scope` (`scripts/lib/function_size_gate.py:284`), publiek als `in_scope` (`scripts/lib/function_size_gate.py:362`); de tree-check gebruikt die regel ongewijzigd. Tests en spikes zijn dus uitgesloten: een test die een lange reeks gevallen uitschrijft is geen productiefunctie, en een spike is wegwerpwerk.
- **De grens gaat over uitvoerbare regels, niet over fysieke regels.** De telregel is `count_executable_lines` (`scripts/lib/function_size_gate.py:234`): lege regels, regels die alleen commentaar zijn en de eigen docstring van de functie tellen niet mee. Een functie met een lange docstring en een korte body is dus klein; dat is de bedoeling, want de grens meet onderhoudslast, niet documentatie.
- **200 is inclusief de `def`-regel.** Een functie met exact 200 uitvoerbare regels is schoon; 201 is een overtreding. De tests van G2-0 leggen dat vast (`tests/test_function_size_tree.py::test_exactly_the_max_is_clean`).

### 2. De meting

De meting is één commando, over de hele boom op een ref:

```
python3 scripts/ci/check_function_size_tree.py --ref HEAD --prefix scripts/ --max 200
```

De tekstuitvoer noemt eerst de samenvatting (`<A> above 200, <B> above 70, <F> functions in <N> files at <ref> (<sha8>)`), dan per niet-parseerbaar bestand `unparsed <pad>`, en dan per overtreding `<regels> <pad>::<qualname> (line <regel>)`, langste eerst. Met `--json` komt hetzelfde als één object; daarin is `unparsed` het **aantal** niet-parseerbare bestanden en `unparsed_paths` de gesorteerde lijst zelf, zodat de refactorinstructies van deze track `unparsed` met 0 kunnen vergelijken en de lijst toch leesbaar blijft.

De exitcodes zijn eindig:

| Exit | Betekenis |
|---|---|
| 0 | schoon: niets boven `--max`, niets ongeparseerd |
| 1 | minstens één functie boven `--max`, of minstens één bestand dat niet parseert |
| 2 | een usage- of git-fout, **of een meting van 0 bestanden** |

**Een lege meting faalt.** Als `--prefix` nul bestanden raakt, is er niets gemeten en dus niets bewezen: het commando schrijft één regel op stderr en eindigt met exit 2 (OI-2570). Dat sluit de faalvorm die de T0 op `24ef4c40` mat. Een bestand dat niet parseert is om dezelfde reden geen "klein": het telt als overtreding (exit 1) en wordt bij naam genoemd, want een ongemeten bestand is geen bewijs.

### 3. De handhaving

De meting is een **blokkerende stap** in de required job `lint-patterns` (`Lint Patterns (silent-except + atomic-write)`) van `.github/workflows/vnx-ci.yml:695`, direct na de stap `Function size ratchet` (`.github/workflows/vnx-ci.yml:678`). De stap draait op elke pull request-head en elke push naar main, met `set -euo pipefail`, zonder `continue-on-error`, zonder `|| true` en zonder `if:` die hem op een push overslaat. Hij draait vanuit de buitenste checkout, want hij heeft git nodig en de `VNX_HOME`-kopie heeft geen `.git`.

De stap mag niet stilzwijgend verdwijnen. `tests/test_function_size_ratchet.py::test_g2_15_workflow_step_sizes_the_whole_tree` laadt de workflow met `yaml.safe_load` en eist de stap, zijn plaats, zijn argumenten en het ontbreken van `continue-on-error` en `if`. Wie de stap weghaalt, maakt die test rood.

### 4. De verhouding tot de ratchet van 70

De twee checks bewaken verschillende dingen en blijven naast elkaar bestaan:

- **De ratchet van 70** (`FUNCTION_SIZE_LIMIT`, `scripts/lib/function_size_gate.py:15`; `scripts/ci/check_function_size_ratchet.py`) kijkt naar de **diff**: een nieuwe of gewijzigde functie boven de 70 regels is een overtreding. Hij voorkomt dat er nieuwe lange functies bijkomen.
- **De tree-check van 200** kijkt naar de **toestand**: elke functie onder `scripts/`, gewijzigd of niet, moet onder de 200 blijven. Hij dwingt de bestaande schuld af.

Een functie van 150 regels die niemand aanraakt is voor de tree-check schoon en voor de ratchet onzichtbaar; een nieuwe functie van 100 regels is voor de tree-check schoon en voor de ratchet een overtreding. De 200 is het plafond, de 70 is de richtprijs voor alles wat je aanraakt. Wie een bestaande functie van 190 regels met één regel uitbreidt, passeert de tree-check maar niet de ratchet; de ratchet is dan de strengere van de twee, en dat is bedoeld.

### 5. Een uitzondering vraagt een wijziging van dit ADR

Er is geen vlag, geen `--allow`, geen lijst met uitzonderingen en geen `# noqa`-equivalent. Een functie die niet onder 200 kan blijven, is een reden om dit ADR te amenderen: een nieuwe beslissing, met de functie, de gemeten lengte en de reden, en een status die dat besluit draagt. Tot die amendering is de overtreding een rode CI-stap. Een uitzondering die alleen in de workflow of in de code staat, bestaat niet als besluit en wordt door de test van punt 3 ook niet gedekt.

### 6. Terugdraaien

De handhaving terugdraaien is de stap `Function size tree` uit `.github/workflows/vnx-ci.yml` verwijderen, in één PR, met dit ADR als de reden en een nieuwe status (**Superseded** of **Deprecated**) in plaats van stilte. De meting zelf blijft daarna gewoon bestaan: `scripts/ci/check_function_size_tree.py` is een los commando en verandert niet mee. De ratchet van 70 raakt het terugdraaien niet — die staat op zichzelf en blijft staan. Wie alleen de drempel wil verleggen, amendeert punt 1; wie de handhaving wil stoppen, draait de stap terug en legt dat hier vast.

## Gevolgen

- **De boom op de dag van merge moet schoon zijn.** De refactors G2-1c tot en met G2-1e, G2-4 en G2-5 lopen nog; op de basis van deze PR staan er nog overtredingen en de nieuwe stap is dus rood. Dat is de stap die werkt, geen defect. De T0 merge't deze PR als laatste, wanneer `--ref origin/main` 0 boven 200 meldt.
- **Elke PR daarna betaalt dezelfde prijs.** Een pull request die een functie over de 200 duwt, is rood op een stap die de diff niet leest. Dat is de bedoeling: de grens is een toestand.
- **De meting is goedkoop en deterministisch.** `git ls-tree` plus `ast.parse` per bestand, geen modelcall, geen netwerk. Dezelfde ref geeft dezelfde uitkomst.
- **Een typefout in `--prefix` is nu luid.** De lege meting van OI-2570 eindigt op exit 2 in plaats van een groene 0-files-regel.

## Alternatieven die zijn afgewezen

**(a) Alleen de ratchet van 70, geen tree-check.** Afgewezen. De ratchet ziet een bestaande functie van 250 regels niet zolang niemand haar aanraakt; de track eist een plafond voor de hele boom, niet alleen voor de diff.

**(b) De tree-check in de bestaande lint-stap hangen.** Afgewezen. Die stap eindigt met exit 0 zodra er geen toegevoegde regel in `scripts/` of `dashboard/` is, en zou de tree-check dus stil overslaan — dezelfde faalvorm die de ratchet al tot een eigen stap maakte.

**(c) Een lege meting als waarschuwing laten passeren.** Afgewezen (OI-2570). Nul gemeten bestanden betekent dat de meting niet deed wat de opdracht zei; een waarschuwing in een CI-log leest niemand, en de stap zou groen blijven terwijl hij niets mat.

**(d) Een uitzonderingenlijst in de workflow of in een configbestand.** Afgewezen. Een lijst naast dit ADR wordt een tweede, stilzwijgende grens. Een uitzondering hoort een besluit te zijn, en een besluit hoort hier te staan.
