# Protocol: de effectmeting van intelligence-injectie

**Track:** `intelligence-effect-measurable` (golf-zelfstandig-leren), rij PR-12.
**Status:** binding vóór het venster opent. Dit document staat naast de code en de
besluiten: de normatieve tekst is `docs/governance/decisions/ADR-0*-intelligence-effectmeting.md`
en het plan `claudedocs/2026-11-07-naar-1-9/plannen/intelligence-effect-measurable.md`
(revisie 2, 08-11-2026). Dit protocol herhaalt geen besluit en voegt er geen toe; het
legt vast hoe een operator de meting uitvoert en leest, met de exacte commando's uit
`## Done measurement` van dat plan.

**De meetweg is deterministisch.** De arm is een SHA-256 over vier strings, de
configuratieparser is een gesloten veldenlijst, de uitkomstklassen zijn een vaste
tabel over `DISPATCH_STATES`, de rijping is een vergelijking met `injected_at + 72 uur`,
de schatter is een Mantel-Haenszel-gewogen risicoverschil met een gesloten
variantieformule, en de verdictklassen zijn vier intervallen op één getal. Er is geen
model in de meetweg: geen open natuurlijke taal, geen oordeel onder ambiguïteit, geen
synthese over ongestructureerde bronnen. De enige vrije tekst is de reden die de
operator bij het openen en sluiten typt; die wordt opgeslagen en getoond, nooit
geïnterpreteerd. Daarom zijn alle onderstaande uitkomsten reproduceerbaar en op
fixtures testbaar.

## De ene harde regel

Deze tekst staat woordelijk gelijk in de code (`MIN_PER_ARM`), in het ADR en in
`## Done measurement`, en geldt overal waar hieronder een aantal staat:

> **HARDE GRENS: een verdict bestaat alleen bij n ≥ 200 opgeloste dispatches per arm.
> De grens is de constante `MIN_PER_ARM = 200` in de code en is door geen vlag,
> omgevingsvariabele of configuratierij te verlagen. De deadline 01-12-2026 verlengt
> het venster en houdt de track open. Onder de grens is de uitkomst altijd
> `geen verdict` (exit 3, reden `below_floor`), nooit een verdict bij lagere n.**

Het commando kent daarom geen `--min-per-arm`: `effect --min-per-arm 10` is een
onbekend argument en `argparse` weigert het met **exit 2**. `MIN_PER_ARM` mag alleen
omhoog door een codewijziging met een ADR-wijziging.

## Paden en constanten

- `QI` = `$HOME/.vnx-data/vnx-dev/state/quality_intelligence.db`
- `RC` = `$HOME/.vnx-data/vnx-dev/state/runtime_coordination.db`
- `P` = het project-id uit `project_root.resolve_project_id()` (voor dit project
  `vnx-dev`); nooit een hardgecodeerde tenant.

SQLite wordt alleen gelezen: `python3 -I` met een absolute
`file:<pad>?mode=ro&immutable=1`-URI, nooit een relatief pad. Een commando dat een
store niet kan openen is **exit 1** met een fout op stderr; dat is een blokkade, geen
leeg antwoord.

## (0) De basis

```bash
vnx --version   # of: git -C "$HOME/.vnx-system/versions/current" rev-parse --short HEAD
python3 -I scripts/lib/migration_effectiveness_probe.py
```

De versie is de 1.9-kop en niet `v1.8.0`. De probe meldt **nul schendingen** en
`claimed_version` **38**. Ontbreekt de probe of meldt hij een schending, dan is de
store niet in de vorm die het cohort vereist en stopt de meting hier.

## (1) De legacy gather-lane ligt buiten het dispatchpad

```bash
python3 -m pytest tests/test_legacy_intel_lane_ratchet.py -q
git grep -n 'gather_intelligence.py" gather' scripts/dispatcher_minimal.sh
```

De ratchet-test geeft **exit 0** en bewijst vijf argumenten in de legacy-aanroep en dat
de deur bij `VNX_SINGLE_ENTRY_DISPATCH=1` niet langs `dispatcher_minimal.sh` gaat. Een
niet-groene ratchet is een blokkade: zolang de legacy-lane in het pad staat, is de
herkomst van een injectierij niet eenduidig.

## (2) Het cohort: aantallen en controles, nooit een verdict

```bash
python3 scripts/intel_injection_join.py cohort --qi "$QI" --rc "$RC" --project-id "$P"
```

Per arm toont het commando `resolved`, `unresolved`, `excluded`, `unknown`, `immature`
en de controles. De uitkomst is één van drie:

- **exit 0** — cohort aanwezig, alle controles groen en n ≥ 200 opgeloste dispatches per
  arm.
- **exit 3** — een weigering met een reden (zie de fail-closed-tabel). Dit is een
  geblokkeerde meting, geen leeg cohort.
- **exit 1** — een store is onbruikbaar.

`cohort` rekent geen schatter en geen verdict; het is de tussenstand van 17-11.

## (3) Het venster is begrensd en het cohort is reconstrueerbaar

Openen (operatorstap, in deze volgorde, elk met een audit-regel): eerst
`VNX_INTEL_AB_WINDOW` één keer met een nieuw `window_id` en `salt` via
`config_store_db.set_config`, daarna `VNX_INJECTION_WHY_ENABLED=1` en
`VNX_INTEL_AB_TEST=1`. Een andere fractie of salt is een nieuw `window_id` en dus een
nieuw venster; de bestaande `VNX_INTEL_AB_WINDOW`-rij wordt nooit overschreven.

```bash
python3 -I -c "import sqlite3; c=sqlite3.connect('file:$RC?mode=ro&immutable=1',uri=True); print(c.execute(\"select count(*) from pragma_table_info('intelligence_injections') where name in ('ab_arm','ab_window')\").fetchone())"
python3 -c "import sys; sys.path.insert(0,'scripts'); from intelligence_ab_report import weekly_ab_lift; print(weekly_ab_lift('$QI','$RC','$P'))"
```

```bash
python3 -I -c "import sqlite3; c=sqlite3.connect('file:$RC?mode=ro&immutable=1',uri=True); print(c.execute(\"select config_key, config_value from project_config where project_id='$P' and config_key in ('VNX_INTEL_AB_WINDOW','VNX_INTEL_AB_TEST','VNX_INJECTION_WHY_ENABLED')\").fetchall())"
python3 -I -c "import sqlite3; c=sqlite3.connect('file:$RC?mode=ro&immutable=1',uri=True); print(c.execute(\"select count(*) from project_config_audit where project_id='$P' and config_key='VNX_INTEL_AB_WINDOW'\").fetchone())"
python3 -I -c "import sqlite3; c=sqlite3.connect('file:$QI?mode=ro&immutable=1',uri=True); print(c.execute('select count(*), max(created_at) from pattern_injection_outcome').fetchone())"
```

De `pragma_table_info`-telling is **(2,)**. `weekly_ab_lift` geeft **exit 0**; een
`RuntimeError: ... no such table` of een andere fout is een blokkade. Het cohort is
begrensd op `ab_window like '<window_id>:%'` en meldt `unstamped_after_open 0`,
`assignment_not_reproducible 0`, `config_diverged 0` en `arm_invalid 0`. In
`project_config` staan de drie sleutels met een geldig venster-JSON en `1`, `1`; in
`project_config_audit` staat per `window_id` precies één `VNX_INTEL_AB_WINDOW`-regel en
over de drie sleutels minstens drie regels. `pattern_injection_outcome` telt **> 544**
(de stand van 07-11) met een `max(created_at)` binnen het venster, en vult met minstens
één nieuwe rij per week. Een lege of ontbrekende telling is een blokkade, geen nulmeting.

## (4) De effectvergelijking: één verdictklasse, of geen verdict

```bash
python3 scripts/intel_injection_join.py effect --qi "$QI" --rc "$RC" --project-id "$P"
```

`effect` roept `cohort` aan en rekent daarna de gestratificeerde schatter, het 95
procent-interval en de verdictklasse. Per arm toont het `n`, `success_rate`,
`unresolved`, `unknown`, de dekking (dispatches zonder injectierij, zonder
`dispatches`-rij, dubbele injectierijen, vervallen strata), het gestratificeerde `d`, het
ruwe controlegetal en de verdictklasse.

- **exit 0** — een verdictklasse bij n ≥ 200 per arm en alle controles groen. Elke
  klasse telt als done: `treatment_better`, `control_better`,
  `equivalent_within_5pp`, `inconclusive`. `inconclusive` betekent "geen aantoonbaar
  effect van 5 procentpunt of meer", niet "mislukt".
- **exit 3** — `below_floor` of een andere weigering; er is geen verdict en de track
  sluit niet.
- **exit 2** — een onbekend argument (zoals `--min-per-arm`): de vlag bestaat niet.
- **exit 1** — een store is onbruikbaar.

De armverdeling is aanvullend (telt de niet-gerijpte mee):

```bash
python3 -I -c "import sqlite3; c=sqlite3.connect('file:$RC?mode=ro&immutable=1',uri=True); print(c.execute(\"select coalesce(ab_arm,'NULL'), count(distinct dispatch_id) from intelligence_injections where project_id='$P' and ab_window like '<window_id>:%' group by 1\").fetchall())"
```

Een `control`-regel en een `treatment`-regel horen er elk te staan. Een `NULL`-regel na
het openen is `unstamped_after_open` en blokkeert.

## (5) De secundaire maten: rapporteren, nooit het verdict bewegen

```bash
python3 scripts/intel_injection_join.py secondary --qi "$QI" --rc "$RC" --project-id "$P"
```

`secondary` rekent F1, F2, F3, CQS en de open-items-regels, elk met een status
`fired`, `clear` of `not_evaluable` en de dekking. Het commando wijzigt het verdict en
de exitcode van `effect` nooit. CQS en open items zijn voor de deursleutel nu
`not_evaluable` (0 `dlv-*` van 5764 op 08-11); dat is een gemeten dekking en geen falen,
en geen reden om done te weigeren.

## Fail-closed: een ontbrekende, `None`, onleesbare of onbekende waarde is nooit een pass

| Situatie | Uitkomst | Waarom geen pass |
|---|---|---|
| `intelligence_injections` mist `ab_arm` of `ab_window` | probe-schending, `exit 3` | de kolom is niet bewezen; een lege lezing is geen nulmeting |
| een rij na `opened_utc` zonder `ab_window` | `unstamped_after_open`, `exit 3` | een ongestempelde rij kan in geen enkele arm worden geteld |
| meer dan één `ab_window` binnen het cohort | `config_diverged`, `exit 3` | het venster is niet één bevroren configuratie |
| een arm buiten `control`/`treatment` (`NULL`, `placebo`, leeg) | `arm_invalid`, `exit 3` | een onbekende arm is geen van beide armen |
| een arm die niet uit de configuratie herrekent | `assignment_not_reproducible`, `exit 3` | de toewijzing is niet reconstrueerbaar |
| een staat buiten `DISPATCH_STATES`, `NULL`, of geen `dispatches`-rij | `unknown_states`, `exit 3` | een onbekende staat telt nooit als succes of mislukking |
| `unresolved` + `unknown` verschilt meer dan 5 pp tussen de armen | `differential_attrition`, `exit 3` | de opgeloste groep is scheef |
| n < 200 opgeloste dispatches per arm | `below_floor`, `exit 3` | de harde grens; nooit een verdict bij lagere n |
| meer dan 10 procent van de opgeloste dispatches in vervallen strata | `strata_coverage`, `exit 3` | de schatter zou op een restje rusten |
| een stratum met een lege arm | valt uit de schatting, staat in de uitvoer | de schatting blijft eerlijk; de dekking wordt gemeld |
| een store die niet opent | `exit 1` | geen lezing is geen goede lezing |
| `--min-per-arm` | `argparse exit 2` | de vlag bestaat niet; de grens is een constante |

## De sluitregel

Openen: **10-11-2026**, na de corpus-quarantaine (verplaatsen, nooit wissen) en na de
release. Tussenstand: **17-11-2026**, alleen `cohort`. Eerste toetsmoment:
**24-11-2026** met `effect`; haalt een arm de grens dan niet, dan loopt het venster door
en is de uitkomst `geen verdict`. Vanaf dag 14 toetst de T0 wekelijks. Deadline:
**01-12-2026** — die verlengt het venster en houdt de track open, ze sluit niets en geeft
geen verdict.

De track sluit op het verdict, met `window_id`, de vensterdatums, het commit-bereik en de
uitvoer van `cohort`, `effect` en `secondary` erbij, en niet op het feit dat de code
gemerged is. Zodra `effect` een verdict geeft, gaan `VNX_INTEL_AB_TEST` en
`VNX_INJECTION_WHY_ENABLED` op `0`, met een audit-regel erbij. Een datum sluit het
venster niet.
