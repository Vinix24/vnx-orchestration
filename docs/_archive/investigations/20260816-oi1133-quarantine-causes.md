# OI-1133 — vier quarantaines, twee oorzaken, één al opgelost

Dispatch-ID: 20260816-p14-quarantine-causes

## Summary

De vier OI-1133-quarantaines uit `scripts/ci/test_exclusions.txt` (regels 156, 185, 212, 213)
zijn geen vier losse defecten. Het zijn twee oorzaken en één inmiddels opgelost symptoom.

`test_init_migrate_bootstrap.py` faalt door collection-order-vervuiling: een buurbestand zet
`VNX_PROJECT_ID=vnx-dev` op moduleniveau en die waarde lekt de hele sweep in, waarna de
fail-closed init-migratie weigert. `test_t0_decision_log.py` en `test_t0_escalations_log.py`
faalt door eenzelfde mechaniek: de cursor-reset vertrouwt op inode-verschil, wat op ext4
(CI-image) niet werkt omdat een herschreven bestand daar dezelfde inode terugkrijgt.
`test_quality_advisory_pipeline.py` is de uitzondering: de "NoneType .status"-faalvorm is op
HEAD niet meer reproduceerbaar, want de guard die hem afvangt is al gemerged (#1390).

Elke faalvorm is hieronder gereproduceerd met een gerichte buur-combinatie (niet de volledige
sweep) en afgezet tegen de solo-run van hetzelfde bestand. Geen van de vier bestanden is in
deze dispatch gerepareerd en geen van de vier is uit de excludelist gehaald. Dit is een
meetopdracht.

## Changes

Geen codewijziging. Dit is een meetopdracht (zie dispatch-instructie). Dit document is de
enige output in de PR.

## Verification

### 1. `test_init_migrate_bootstrap.py` — collection-order-vervuiling (OI-946-klasse)

Excludelist-regel: "18/32 failed in PR#1458 Profile A sweep (vnx init/migrate exit non-zero
on the CI image; OI-946 class)".

Solo, schone omgeving:

```
$ python3 -m pytest -q tests/test_init_migrate_bootstrap.py
................................                                         [100%]
32 passed in 2.30s
```

De kleinste reproducerende vorm is niet het buurbestand maar de gelekte variabele zelf. Eén
env-var is genoeg om exact het sweep-getal te reproduceren:

```
$ VNX_PROJECT_ID=vnx-dev python3 -m pytest -q tests/test_init_migrate_bootstrap.py
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_stop_hook_wired[minimal]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_pretooluse_guard_wired[default]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_pretooluse_guard_wired[minimal]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_hook_script_paths_are_absolute[default]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_hook_script_paths_are_absolute[minimal]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_hook_script_paths_exist_on_disk[default]
FAILED tests/test_init_migrate_bootstrap.py::TestInitHooksWiring::test_hook_script_paths_exist_on_disk[minimal]
18 failed, 14 passed in 0.76s
```

18 failed, 14 passed. Exact het getal uit de excludelist. De gerichte buur-combinatie die dit
in een echte sweep veroorzaakt (het vervuilende bestand plus het doelwit):

```
$ python3 -m pytest -q tests/test_link_sessions_dispatches_cleanup.py tests/test_init_migrate_bootstrap.py
18 failed, 25 passed in 0.98s
```

Oorzaak. `tests/test_link_sessions_dispatches_cleanup.py:20` staat op moduleniveau:

```python
os.environ.setdefault("VNX_PROJECT_ID", "vnx-dev")
```

`setdefault` op moduleniveau in een testbestand is een proces-globale mutatie. In een
directory-sweep (`pytest tests/`) importeert pytest alle testmodules tijdens collection,
alfabetisch op bestandsnaam. `test_link_sessions_dispatches_cleanup.py` importeert dus vóór
`test_init_migrate_bootstrap.py` en de env-var blijft voor de rest van het proces staan.
`monkeypatch.setenv` ruimt op bij teardown; een kale `os.environ.setdefault` op moduleniveau
doet dat niet.

Zodra `test_init_migrate_bootstrap` daarna draait, roepen 18 van de 32 tests `vnx_init` of
`vnx_migrate` aan en eisen rc==0. Die commando's lopen door de bootstrap-keten naar
`resolve_init_project_id` (`scripts/lib/project_id_migration.py:126-144`), de fail-closed
ADR-007-controle. Die verzamelt drie bronnen en eist dat ze het eens zijn:

```python
path_pid = _pid_from_db_path(Path(db_path))
marker_pid = _read_marker_from_path(Path(db_path))
env_pid = (os.environ.get("VNX_PROJECT_ID") or "").strip() or None
...
distinct = set(sources.values())
if len(distinct) > 1:
    raise RuntimeError("ADR-007 fail-closed: project_id conflict for init backfill ...")
```

In de test wijst `db_path` naar een `tmp_path`-data-root. De path-anchor en de marker lossen
dan op naar de slug van die tijdelijke map (bijvoorbeeld `test-hook-script-paths-exist-o1`).
De gelekte `env:VNX_PROJECT_ID=vnx-dev` wijkt daarvan af. Twee bronnen, twee waarden, dus
`len(distinct) > 1` en `RuntimeError`. De 18 init/migrate-tests zien rc != 0 en falen. De
andere 14 tests raken die fail-closed-keten niet en blijven groen.

Terug naar CI: ja, onder voorwaarde. De te repareren code zit niet in dit bestand maar in de
vervuiler. Zolang `test_link_sessions_dispatches_cleanup.py:20` de module-level `setdefault`
behoudt, faalt `test_init_migrate_bootstrap.py` in elke sweep waar de vervuiler alfabetisch
eerder draait. De reparatie is: de env-mutatie verhuizen naar een fixture met
`monkeypatch.setenv` (die rolt terug), of de init-tests expliciet isoleren tegen een ambient
`VNX_PROJECT_ID`.

### 2. `test_quality_advisory_pipeline.py` — "NoneType .status" is stale (OI-948-klasse)

Excludelist-regel: "2/43 failed in PR#1458 Profile A sweep (NoneType .status; OI-948 class)".

Solo:

```
$ python3 -m pytest -q tests/test_quality_advisory_pipeline.py
...........................................                              [100%]
43 passed in 0.53s
```

Met hetzelfde vervuilende buurbestand als hierboven (de slechtste buur):

```
$ python3 -m pytest -q tests/test_link_sessions_dispatches_cleanup.py tests/test_quality_advisory_pipeline.py
......................................................                   [100%]
54 passed in 0.53s
```

Geen faalvorm te reproduceren, solo noch met buur. De exacte PR#1458-failure kan ik op macOS
niet herhalen, maar code-inspectie verklaart waarom hij op HEAD onbereikbaar is geworden. De
symptoomtekst "'NoneType' object has no attribute 'status'" kwam van `payload.py:517`
(`result.status == "appended"`) wanneer `_write_receipt_under_lock` een `None` teruggaf. Dat
pad is sinds #1390 (commit `522aef92`, 2026-08-04) dicht:

```python
# payload.py:502-517
# OI-948: _write_receipt_under_lock is annotated -> AppendResult and
# every code path returns an AppendResult instance.  A None here would ...
if result is None:
    raise AppendReceiptError("internal_null_result", ...)

if result.status == "appended":
```

Drie onafhankelijke feiten maken `None` onbereikbaar: (a) de guard hierboven fail-closed op
`None`, (b) `_write_receipt_under_lock` (`idempotency.py:191-281`) retourneert op elk pad een
`AppendResult` of raiset, en (c) `_enrich_completion_receipt` (`enrichment.py:299`) is volledig
best-effort en retourneert altijd een dict. Er is geen code meer die `None` kan doorgeven aan
`result.status`.

Terug naar CI: ja, direct. De faalvorm is op HEAD niet reproduceerbaar en de guard die hem
afvangt is al gemerged. Voorwaarde is alleen een schone Profile A-sweep die de 43 groen
bevestigt. Dit bestand hoeft niet te wachten op een reparatie.

### 3. `test_t0_decision_log.py` — cursor-reset vertrouwt op inode-verschil (2/60)

Excludelist-regel: "2/60 failed in PR#1458 Profile A sweep (same-length source-replacement
cursor detection)".

Solo (macOS, APFS):

```
$ python3 -m pytest -q tests/test_t0_decision_log.py
............................................................             [100%]
60 passed in 0.18s
```

De faalvorm is CI-image-specifiek en manifesteert zich lokaal niet als pytest-rood. De oorzaak
is het bestandssysteem, niet een buurbestand. De reset-logica in
`scripts/lib/t0_decision_log.py:388` kijkt alleen naar inode-verschil:

```python
# Inode mismatch: source file was replaced (same or greater line count) → reset
if saved_inode is not None and saved_inode != 0 and current_inode != 0 and current_inode != saved_inode:
    cursor = 0
```

Twee tests bouwen hierop en doen `unlink()` plus herschrijven van dezelfde lengte:
`test_cursor_resets_when_source_replaced_same_length` (regel 504, eist `written == 3`) en
`test_legacy_cursor_upgrade_enables_same_length_replacement_detection` (regel 550, eist
`written2 == 1`). Op ext4 (het CI-image `ubuntu-latest`) krijgt een herschreven bestand op
hetzelfde pad vaak dezelfde inode terug. Dan geldt `current_inode == saved_inode`, de reset
vuurt niet, de nieuwe regels worden overgeslagen en `written` wordt 0 in plaats van 3.

Ik kan geen ext4 mounten op deze macOS-machine, dus reproduceer ik de faalvorm door het echte
`process_events_file`-pad te draaien met een `os.stat` die dezelfde inode teruggeeft (ext4) en
daarna met de echte inode (APFS). Dit is de productiecode, niet een namaak:

```
$ python3 - <<'PY'
... importeert process_events_file; draait 3 events (cursor=3, inode opgeslagen) ...
... herschrijft het bestand met 3 andere events; patcht os.stat zodat de inode GELIJK blijft ...
pass 1 (initial 3 events)          written = 3 | cursor = 3 | inode = 975331755
pass 2 (ext4: inode reused)        written = 0    <- test asserts written == 3, FAILS
pass 3 (APFS: inode changed)       written = 3
PY
```

Pass 2 is exact de ext4-situatie: zelfde lengte, zelfde inode, geen reset, `written == 0`, de
assert faalt. Pass 3 is APFS: nieuwe inode, reset vuurt, `written == 3`. De APFS-probe
bevestigt waarom dit lokaal altijd groen is: na 2000 unlink-plus-recreate cycli is de inode
0 keer hergebruikt (monotone allocatie). Op ext4 is hergebruik de norm, niet de uitzondering.

Terug naar CI: nee, nog niet. Eerst moet de cursor-identiteit bestandssysteem-onafhankelijk
worden. Inode alleen is onvoldoende. De reparatie is: naast (of in plaats van) de inode een
content-digest van de laatst verwerkte regel, of bestandsgrootte plus mtime, in de cursor
opslaan, zodat een same-length-vervanging ook bij inode-hergebruik wordt opgemerkt.

### 4. `test_t0_escalations_log.py` — zelfde inode-oorzaak (1/60)

Excludelist-regel: "1/60 failed in PR#1458 Profile A sweep (same-length source-replacement
cursor detection)".

Solo (macOS, APFS):

```
$ python3 -m pytest -q tests/test_t0_escalations_log.py
............................................................             [100%]
60 passed in 0.19s
```

Zelfde oorzaak als quarantaine 3. Dit bestand heeft één test met de unlink-plus-recreate
vorm: `test_cursor_resets_when_source_replaced_same_length` (regel 494). De mechaniek is
identiek en deelt dezelfde code (`t0_escalations_log.py` gebruikt dezelfde reset-op-inode-
verschil-logica). Daarom is het 1/60 in plaats van 2/60: één test raakt het mechanisme, niet
twee.

Terug naar CI: nee, nog niet, en de voorwaarde is dezelfde als bij quarantaine 3. De twee
t0-log-bestanden repareren samen zodra de cursor-identiteit niet meer alleen op inode leunt.

## Open Items

- **Quarantaine 1 (init/migrate-bootstrap): de vervuiler is niet gerepareerd.**
  `tests/test_link_sessions_dispatches_cleanup.py:20` zet `VNX_PROJECT_ID` op moduleniveau via
  `os.environ.setdefault`. De reparatie is een fixture met `monkeypatch.setenv`, of isolatie van
  `VNX_PROJECT_ID` in de init-tests. Niet uitgevoerd in deze meetopdracht.
- **Quarantaine 2 (quality-advisory-pipeline): geen reparatie nodig, wel een bevestiging.**
  De NoneType-guard (#1390, `522aef92`) is gemerged. Het bestand kan terug zodra een schone
  Profile A-sweep de 43 groen bevestigt. Aanbeveling: de excludelist-regel weghalen in de
  eerstvolgende sweep-PR, niet in een losse PR.
- **Quarantaine 3 en 4 (t0-logs): cursor-identiteit moet bestandssysteem-onafhankelijk worden.**
  Inode-verschil alleen is onvoldoende op ext4. Reparatie: content-digest van de laatst verwerkte
  regel, of grootte-plus-mtime, meenemen in de cursor. Beide bestanden komen samen terug zodra
  deze reparatie op main staat.
- **De volledige suite is in deze dispatch niet gedraaid** (dispatch-instructie). De solo-runs en
  gerichte buur-combinaties hierboven zijn de enige uitgevoerde testruns.
