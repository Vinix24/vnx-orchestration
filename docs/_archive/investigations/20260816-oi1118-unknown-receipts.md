# OI-1118 — 3.792 unknown-receipts, niet 19

Dispatch-ID: 20260816-p12b-unknown-receipts-triage

## Summary

OI-1118 citeert "19 unknown-dispatches" als representatief voor het probleem. Dat getal is
zelf correct gemeten (200 dispatches, venster sinds 2026-08-03, plan-gate-zetels en
benchmark-ruis uitgesloten — zie herkomst hieronder), maar het is een steekproef over 200
dispatches, niet de populatie. Volledige telling over `t0_receipts.ndjson` (27.739 regels,
geen steekproef, geen `head`) geeft **3.792** receipt-regels met `status=unknown`, verspreid
over 2.760 unieke dispatch-ids. De 3.792 splitsen in twee bewezen oorzaken en een lege derde:
**2.585 rapportage-/parse-gat** (het rapport bestaat nog op schijf, actief tot en met
2026-08-10) en **1.207 leveringsgat** (het rapportpad bestaat niet meer, uitsluitend
2026-05-13 t/m 2026-06-23, samenvallend met de central-store-migraties van andere projecten).
Er is geen legitieme-tussentoestand-categorie: alle 3.792 zijn `event_type=task_complete`,
een terminale event-vorm in dit systeem. De rootcause voor categorie (a) is gepind:
`scripts/report_parser.py:530-541` defaultet `status` (met `terminal`, `task_id`,
`dispatch_id`, `gate`) blanket naar de letterlijke string `'unknown'` zodra een rapportveld
ontbreekt — en de mandatory report body contract (`## Summary`/`## Changes`/`## Verification`/
`## Open Items`) vereist nergens een `Status:`-veld, dus een volledig contract-conform rapport
kan alsnog op `unknown` sluiten.

## Changes

Geen codewijziging. Dit is een meetopdracht (zie dispatch-instructie). Dit document is de
enige output.

## Verification

### 1. Herkomst van "19"

OI-1118 (`~/.vnx-data/vnx-dev/state/open_items.json`, `id=OI-1118`,
`origin_dispatch_id=20260810-t0-hermeting-oi1035`, `created_at=2026-08-10T16:20:48`) is de
opvolger van OI-1035 met, letterlijk uit de item-tekst:

> "Gemeten door T0 op 2026-08-10, zelfde methode als OI-1035 (plan-gate-zetels en
> benchmark-ruis uitgesloten), venster sinds 2026-08-03, 200 dispatches. STAND: 173 met
> afsluitend receipt = 86%, 27 zonder = 14%. [...] UITSPLITSING VAN DE 27: 13 met alleen
> status 'unknown', 6 met leeg plus 'unknown', 5 met 'contract_invalid' [...] 1 blocked+
> requested, 1 guard_error+timeout, 1 leeg. DE KERN IS DE UNKNOWN-GROEP VAN 19."

13 + 6 = 19. Onafhankelijk bevestigd via de audit trail:

```
$ grep -n "OI-1118" ~/.vnx-data/vnx-dev/state/open_items_audit.jsonl
2193:{"timestamp": "2026-08-10T16:20:48.353927", "actor": "T0", "action": "add",
"item_id": "OI-1118", "severity": "warn", "dispatch_id": "20260810-t0-hermeting-oi1035",
"pr_id": null}
```

Het getal "19" is dus geen fout — het is de eenheid (dispatches, niet receipt-regels), het
venster (200 dispatches sinds 2026-08-03, niet de volledige 27.739-regel ledger) en de
uitsluitingen (plan-gate-zetels, benchmark-ruis) die het klein houden. Het clusterplan en het
open item citeren het getal zonder die context, waardoor het leest als "de hele populatie is
19". Dat is de bevinding: niet een meetfout, maar een schaal-verwarring tussen steekproef en
populatie. `claudedocs/open-items-dispositie-20260815.md:32` herhaalt dezelfde 19 zonder de
context, wat de verwarring een dag later opnieuw bevestigt in plaats van corrigeert.

### 2. Volledige telling

```
$ wc -l ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson
   27739 t0_receipts.ndjson
```

Status-vocabulaire over de volledige 27.739 regels (Python, `json.loads` per regel, geen
sampling, `__missing_field__`/`__empty_string__`/`__null__` apart geteld van echte waarden):

```
   10238  success
    4239  failed
    3792  unknown
    3322  contract_invalid
    2301  done
    2168  __missing_field__     (status-veld afwezig — 2081 daarvan event_type=state_mutation,
                                  een ander recordtype, zie hieronder)
     960  failure
     260  timeout
     204  requested
     132  not_executable
      49  __empty_string__      (status-veld aanwezig maar leeg — NIET hetzelfde als afwezig
                                  of "unknown")
      38  not_configured
      24  blocked
       3  complete
       3  no_ready_pr
       1  done — awaiting CI + T0 gate
       1  COMPLETE
       1  COMPLETE — push + PR created
       1  completed
       1  in_progress
       1  guard_error
sum: 27739  (sluitend)
```

3.792 matcht exact het cijfer uit de dispatch-instructie. `__missing_field__` (2.168) en
`__empty_string__` (49) zijn twee verschillende vormen die geen van beide "unknown" zijn — een
afwezig veld, een leeg veld en de letterlijke string "unknown" zijn drie aantoonbaar
verschillende dingen en worden hier niet door elkaar gehaald.

De 3.792 zelf, op vorm:

```
$ python3 -c "... event_type, schema_version, source, timestamp, field-presence over de 3792 ..."
  3792 task_complete           (event_type — 100%, geen andere event_type in deze groep)
  3792 __missing__             (source-veld — 100% afwezig)
  3470 __missing__ / 322 "2"   (schema_version — legacy vs modern schema)
min datum 2026-05-13, max datum 2026-08-10
```

Elke `unknown`-receipt is dus een voltooid `task_complete`-event — geen enkele zit in een
event_type dat op "nog bezig" wijst (er bestaat geen `task_started`/`task_pending` variant in
deze groep). Uniciteit:

```
$ python3 -c "... unique dispatch_id in de 3792 unknown-records ..."
unieke dispatch_id: 2760   (van de 3.792 regels — 1.032 regels zijn een tweede of derde
                             receipt-regel voor een dispatch_id die al eerder unknown was)
```

### 3. Uitsplitsing naar oorzaak

De opdracht vraagt (a) rapportage-gat, (b) leveringsgat, (c) legitieme tussentoestand. Het
onderscheidende, mechanische signaal tussen (a) en (b) is of `report_path` — een veld dat op
alle 3.792 regels aanwezig is — nog een bestaand bestand op schijf aanwijst:

```
$ python3 -c "
import json, os
exists = missing = 0
with open('t0_receipts.ndjson') as f:
    for line in f:
        rec = json.loads(line)
        if rec.get('status') == 'unknown':
            rp = rec.get('report_path')
            if rp and os.path.isfile(rp):
                exists += 1
            else:
                missing += 1
print(exists, missing)
"
2585 1207
```

2585 + 1207 = 3792. Sluit exact, geen derde categorie nodig op vorm — hieronder de inhoudelijke
verificatie van wat elke kant betekent.

**Categorie (a) — rapportage-/parse-gat: 2.585 receipt-regels, 2.435 unieke rapportbestanden,
2.431 unieke dispatch-ids.** Elk van deze `report_path`-waarden wijst naar een bestand dat
vandaag nog bestaat, allemaal onder de huidige centrale store
(`~/.vnx-data/vnx-dev/unified_reports/`). Datumbereik 2026-06-03 t/m **2026-08-10** — dus tot
en met de laatste dag in de hele unknown-populatie. Contract-conformiteit van de 2.435 unieke
bestanden, gemeten door de vier verplichte koppen te zoeken en een `status:`/`Status:`-veld te
zoeken (case-insensitive, geen sampling):

```
has alle 4 verplichte koppen (## Summary/## Changes/## Verification/## Open Items): 1882
mist >=1 kop:                                                                        553
heeft ergens een expliciet status:-veld met waarde:                                   20
heeft NERGENS een status:-veld:                                                     2415
```

1.882 van de 2.435 (77%) zijn **volledig contract-conforme rapporten** — vier koppen aanwezig,
substantiële inhoud (voorbeeld hieronder) — die alsnog op `unknown` sluiten, puur omdat de
verplichte report body contract nergens een `Status:`-veld eist. Stichprobe
`20260724-worker-pin-flip-kimi-to-sonnet.md` (frontmatter `exit_code: 0`,
`duration_seconds: 1233`, volledige Summary/Changes/Verification/Open Items-body): geen
`status:` of `Status:` waar dan ook in het bestand behalve `contract_status: authored` (een
ander veld). De rootcause zit in `scripts/report_parser.py:529-541`:

```python
# Ensure required fields have defaults
metadata.setdefault('terminal', 'unknown')
metadata.setdefault('gate', 'unknown')
metadata.setdefault('status', 'unknown')
metadata.setdefault('confidence', 0.50)
metadata.setdefault('task_id', 'unknown')
metadata.setdefault('dispatch_id', 'unknown')
...
metadata['status'] = _clean_scalar(metadata.get('status', 'unknown'), default='unknown')
```

Dat is een blanket-default op vijf velden tegelijk, niet een status-specifieke beslissing.
`scripts/lib/report_to_receipt_converter.py:951-960` (het schema_version:2-pad) neemt die
waarde vervolgens ongewijzigd over als `status_raw` en schrijft hem letterlijk in de receipt
zodra het rapport geen expliciete terminal-success- of terminal-failure-status claimt en
contractueel geldig is:

```python
receipt: Dict[str, Any] = {
    **base,
    "event_type": "task_complete",
    "status": status_raw,
}
```

De 553 (23%) die wél minstens één kop missen zijn een aparte, herkenbare subklasse: een
onvolledig rapport dat om een andere — begrijpelijke — reden op `unknown` sluit. De 20
bestanden (0.8%) die wél een expliciet status-veld dragen maar toch als `unknown` in de ledger
staan zijn een derde, klein residu: een parse-mismatch tussen het gebruikte veldformaat en wat
de extractor herkent (niet verder gepind binnen deze meetopdracht — zie Open Items). Alle drie
zijn "rapportage-gat": er is bewijs dat het werk is gebeurd en gerapporteerd; de receipt draagt
alleen geen bruikbare status.

**Categorie (b) — leveringsgat: 1.207 receipt-regels, 331 unieke dispatch-ids.** Het
`report_path`-veld wijst naar een bestand dat niet meer bestaat. Uitsplitsing van die 1.207 op
padprefix:

```
605  /Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt   (gereapte/verwijderde worktree)
360  (leeg pad — report_path was nooit gevuld)
175  .../BUSINESS/development/mission-control                     (pre-migratie MC-lokale store)
 57  .../BUSINESS/development/sales-copilot                       (pre-migratie sales-copilot store)
  7  overige/malformed paden
  3  .../Development/SEOcrawler_v2                                (pre-migratie SEO-lokale store)
```

Datumbereik van deze 1.207: **2026-05-13 t/m 2026-06-23**, geen dag erna. Dat is geen
toevallige knip: Mission Control's cutover naar de centrale store is gemeten op 2026-06-23
(sessie-memory `mission-control-central-cutover-done`), en SEOcrawler volgde op 2026-07-15.
Vóór die cutover schreven die projecten hun eigen `.vnx-data/unified_reports/` pad relatief aan
hun eigen projectroot; na de migratie verdween dat lokale pad, en de oude receipt-regels in de
gedeelde ledger bleven naar een pad wijzen dat niet meer bestaat. `vnx-roadmap-autopilot-wt`
(605 stuks, de grootste subgroep) is een worktree die inmiddels is gereapt — dezelfde
mechaniek als de OI-1118-eigen tekst zelf beschrijft voor het verse geval van 2026-08-10
("de worktree wordt daarna gereapt en dan is het weg"), hier toegepast op een oudere batch. Dit
zijn dispatches waarvan het bewijs niet meer op te halen is: geen rapport, geen manier om
retroactief vast te stellen of het werk slaagde. Vandaar leveringsgat, niet rapportage-gat: het
verschil met (a) is niet de oorzaak van de defaulting (dezelfde blanket-default-mechaniek raakt
waarschijnlijk ook deze receipts — een deel van de sample-records draagt zelfs `dispatch_id:
"unknown"` naast `status: "unknown"`, wat wijst op een nog primitiever of ouder rapportformaat
in die externe projecten), maar dat het bewijsmateriaal zelf weg is.

**Categorie (c) — legitieme tussentoestand: 0.** Alle 3.792 zijn `event_type=task_complete`.
Er is geen apart record-type in deze groep dat een "nog bezig"-status zou representeren (geen
`task_started`, geen `task_pending`). Een `task_complete`-event is in dit systeem een terminale
gebeurtenis; er is geen legitieme reden waarom een terminaal event op een niet-geëvalueerde
status zou mogen blijven staan. Deze categorie is leeg, niet bij benadering maar aantoonbaar.

| Categorie | Aantal | Som-check |
|---|---:|---|
| (a) rapportage-/parse-gat | 2.585 | report_path bestaat op schijf |
| (b) leveringsgat | 1.207 | report_path bestaat niet (meer) |
| (c) legitieme tussentoestand | 0 | alle 3.792 zijn event_type=task_complete |
| **Totaal** | **3.792** | 2.585 + 1.207 + 0 = 3.792, sluit exact |

### 4. Is `unknown` nog steeds actief?

Categorie (b) is aantoonbaar dood: laatste voorkomen 2026-06-23, gebonden aan
central-store-migraties die inmiddels zijn afgerond. Categorie (a) — de meerderheid, 2.585 van
3.792 — is dat niet: laatste voorkomen 2026-08-10, en de code die de blanket-default zet
(`report_parser.py:530-541`) is vandaag ongewijzigd. Volledige dagtelling, geen steekproef,
elke dag 2026-07-18 t/m 2026-08-16 (vandaag), totaal aantal receipts die dag naast het aantal
`unknown` erin:

```
datum         totaal  unknown   pct
2026-07-18       135       37  27.4%
2026-07-19        60       14  23.3%
2026-07-20        25        6  24.0%
2026-07-21       145       43  29.7%
2026-07-22       661      139  21.0%
2026-07-23       260       86  33.1%
2026-07-24       102        0   0.0%
2026-07-25        45        0   0.0%
2026-07-26        15        0   0.0%
2026-07-27        31        0   0.0%
2026-07-28       515        0   0.0%
2026-07-29       174        0   0.0%
2026-07-30       135        0   0.0%
2026-07-31       129        0   0.0%
2026-08-01       719        0   0.0%
2026-08-02       466        0   0.0%
2026-08-03       510       24   4.7%
2026-08-04       740       42   5.7%
2026-08-05        59        0   0.0%
2026-08-06        28        0   0.0%
2026-08-07        17        0   0.0%
2026-08-08       127       40  31.5%
2026-08-09        78       28  35.9%
2026-08-10       164       16   9.8%
2026-08-11        19        0   0.0%
2026-08-12        67        0   0.0%
2026-08-13         0        0    n/a  (geen enkele receipt die dag)
2026-08-14        29        0   0.0%
2026-08-15        93        0   0.0%
2026-08-16        11        0   0.0%
```

Zes opeenvolgende dagen (2026-08-11 t/m 2026-08-16, met dagelijkse receipt-activiteit behalve
08-13) zonder een nieuwe `unknown`. Dat is geen bewijs van een oplossing: het patroon is al
langer intermitterend — een negen-dagen-stilte (2026-07-24 t/m 2026-08-02) werd gevolgd door
een nieuwe burst op 2026-08-03. Een zes-dagen-stilte past binnen datzelfde historische
patroon. Conclusie met bewijs, niet met giswerk: het mechanisme dat categorie (a) veroorzaakt
staat nog in de code, is niet aangepakt door de vandaag gemergede PR #1559 (commit `30733388`,
OI-1148 — geverifieerd met `git show 30733388 --stat`, geen enkele hit op
`report_parser.py`/`report_to_receipt_converter.py`), en het laatste voorkomen (2026-08-10)
ligt zes dagen terug in een patroon dat eerder al negen dagen stil is geweest zonder dat de
oorzaak weg was. Dit is dus **niet aantoonbaar opgelost, momenteel niet producerend**.

### 5. Wat dit niet is

Geen enkele historische receipt is aangepast. `t0_receipts.ndjson` is alleen lezend benaderd
(elke opdracht hierboven is een `wc`/`grep`/`python3 json.loads`-leesactie; geen enkel commando
schrijft naar `.vnx-data/`). Er is geen codewijziging in deze dispatch: de vraag of de
report body contract een `Status:`-veld moet gaan eisen, of de blanket-default in
`report_parser.py` moet worden vervangen door een fail-loud gedrag, en of de 3.792 historische
receipts gecorrigeerd worden, zijn drie aparte beslissingen die de operator nog moet nemen.

## Open Items

- **Rootcause categorie (a) is gepind, niet gerepareerd.** `report_parser.py:530-541` blanket-
  defaultet vijf velden (incl. `status`) naar `'unknown'`; de report body contract vereist geen
  `Status:`-veld. Een fix is een ontwerpkeuze (contract uitbreiden met een verplicht
  status-veld, óf de converter laten afleiden uit `exit_code`/`contract_valid` in plaats van
  blind te defaulten) — niet in scope van deze meetopdracht.
- **20 rapporten met een expliciet status-veld sluiten alsnog op `unknown`.** Klein residu
  (0.8% van categorie a), wijst op een los parse-format-mismatch. Niet individueel
  onderzocht binnen deze opdracht.
- **schema_version:2-groep bevat twee receiptvormen per dispatch** (een volle
  `report_parser`-vormige regel plus, voor 101 van de 322, een aanvullende compacte
  `receipt_verdict.py`-annotatie met `decision`/`reason`/`evidence_complete`). Die tweede vorm
  classificeert een reeds-`unknown` receipt als "investigate" — het is een downstream
  observatielaag (ADR-035), geen aparte bronoorzaak van de `unknown`-waarde zelf. Niet verder
  uitgesplitst binnen deze opdracht.
- **Correctie van de 3.792 historische receipts is een aparte operator-beslissing**, expliciet
  buiten scope van deze meetopdracht (zie dispatch-instructie punt 4).
- **OI-1118's eigen tekst blijft technisch correct** voor de smalle meting die het beschrijft
  (19 van 200 dispatches sinds 2026-08-03). Aanbeveling, niet uitgevoerd: het item herformuleren
  zodat "19" niet los van zijn steekproefcontext wordt gelezen, of een nieuw item openen voor de
  volledige 3.792-populatie met verwijzing naar dit document.
