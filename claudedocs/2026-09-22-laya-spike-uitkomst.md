# Laya finding-triage spike: uitkomst

Drie rondes classificatie van poortbevindingen met een lokaal Laya Core ML-model.
Doel: testen of een klein on-device model poortbevindingen kan sorteren op stijl,
correctheid, beveiliging en beleid. Het corpus is 657 bevindingen uit de
review-gate-results store van het `vnx-dev` project.

De uitkomst staat hier. Het corpus zelf (`scripts/spikes/laya_finding_triage/data/`)
is niet gecommit; het is regenereerbaar uit de store van wie het script draait.

## De drie rondes

### Ronde 1: baseline met taalconfound

Vier runs, 2 checkpoints x 2 talen, maar de label-taal was vast gekoppeld aan het
checkpoint: de `typed-decisions`-variant draaide altijd Engels, de `multilingual`-
variant altijd Nederlands. De instructietekst was Engels. Dat verweerde taal met
modelvariant, dus een verschil tussen de runs kon niet aan de taal worden
toegeschreven. Ronde 1 rapporteerde bovendien `laya_td_distribution` als nul over
alle vier de klassen terwijl het bestand 657 geldige voorspellingen bevatte.

### Ronde 2: taalconfound weg, telbug gerepareerd

Twee veranderingen.

1. `03_laya_classify.py` kiest de label-taal via een optioneel `[nl|en]` argument
   (default `nl`). De `typed-decisions`-variant draaide opnieuw met Engelse labels
   (`style`, `correctness`, `security`, `policy`). De `multilingual`-variant ook.
   Vier runs naast elkaar: 2 checkpoints x 2 talen, instructie-tekst identiek.

2. De telbug had twee defecten. Een walrus `td_dist = dist(td_dist :=
   dist(td_labels))` overschreef de echte distributie met `dist()` op een dict van
   counts, wat nul opleverde. En `findings = {r["id"]: r for r in ...}` stopte 657
   rijen in een dict met 618 unieke sleutels; 39 duplicate IDs werden stil
   weggegooid, `total` werd 618 in plaats van 657. Beide gerepareerd.

Resultaat: de `typed-decisions`-variant geeft in beide talen lage confidence.
Dat laag-blijven is geen taaleffect maar een eigenschap van het td-model op deze
taak. Het taalconfound is geisoleerd en weggenomen.

### Ronde 3: definities in de instructie

Ronde 3 verandert precies een variabele: de definities van de vier klassen staan
nu in de instructietekst die aan `agent.predict` wordt doorgegeven, in plaats van
alleen in de docstring (het ronde-2 confound). Nieuwe run op laya-ane, Engelse
labels, met definities. De definitie-tekst vrat 21 tokens van het 96-budget, dus
verwacht waren meer capaciteitsfouten. De falsificatie hielp drempel >69%.

## De vier runs naast elkaar

Getallen uit `data/comparison.json` (ronde 2), gegenereerd door
`04_compare_and_sample.py`.

| run | checkpoint | labels | geclassificeerd | cap-fouten | verdeling (s/c/b/p) | mediaan kans | boven 0,50 | P50 | P95 |
|---|---|---|---|---|---|---|---|---|---|
| ane_nl | multilingual ANE 96 | nl | 328/657 | 329 (50,1%) | 47/158/109/14 | 0,3189 | 25,6% | 0,39 ms | 68 ms |
| td_nl | typed-decisions 1024 | nl | 657/657 | 0 | 26/213/178/240 | 0,0137 | 0% | 319 ms | 511 ms |
| ane_en | multilingual ANE 96 | en | 354/657 | 303 (46,1%) | 6/273/73/2 | 0,6698 | 73,2% | 5,4 ms | 6,4 ms |
| td_en | typed-decisions 1024 | en | 657/657 | 0 | 110/375/143/29 | 0,0252 | 0% | 324 ms | 522 ms |

De `ane_en`-run heeft de hoogste mediaan confidence (0,6698) en het grootste
aandeel boven 0,50 (73,2%), maar classificeert 303 van de 657 bevindingen niet
(capaciteitsfouten). De `td`-varianten classificeren alles maar blijven laag in
confidence.

De ronde-3 nieuwe run (ane96 met definities, `laya_predictions_ane96_en_defs.jsonl`)
valt buiten deze tabel omdat het een aparte toets kreeg; de cijfers staan in de
grondwaarheidstoets hieronder.

## Grondwaarheidstoets

De toets (`05_ground_truth_check.py`) selecteert de bevindingen die letterlijk
gaan over een functie die over de 70-regel-drempel gaat. Per constructie is dat
stijl. Wie dat fout classificeert faalt op een geval waarvan de waarheid vaststaat.

Selectie-regex uit de dispatch: `70[- ]line|70 executable|function size|oversized`.
Deze matcht 124 bevindingen. De dispatch noemde 110; de subgroep die T0 mat op
76/110 = 69% laat de kale "function size"-term weg. Beide groepen worden
gerapporteerd.

Per methode over de 124-groep (uit `data/ground_truth_check.json`):

| methode | geclassificeerd | cap-fouten | correct (stijl) | percentage juist |
|---|---|---|---|---|
| regex-baseline | 124 | 0 | 77 | 62,1% |
| ronde-2 ANE-EN | 124 | 28 | 4 | 3,23% (4,17% van non-cap) |
| ronde-3 nieuwe run | 124 | 112 | 2 | 1,61% (16,67% van non-cap) |

Per methode over de 110-groep (strijkt):

| methode | geclassificeerd | cap-fouten | correct (stijl) | percentage juist |
|---|---|---|---|---|
| regex-baseline | 110 | 0 | 67 | 60,91% |
| ronde-2 ANE-EN | 110 | 25 | 4 | 3,64% (4,71% van non-cap) |
| ronde-3 nieuwe run | 110 | 98 | 2 | 1,82% (16,67% van non-cap) |

De ronde-3 nieuwe run gaf 577/657 (87,8%) capaciteitsfouten, tegen 303 (46,1%)
in ronde 2. Slechts 80 voorspellingen over; 12 op de 110 zekere gevallen.

## Falsificatie-oordeel

Vooraf vastgelegd: de nieuwe run haalt het als hij BOVEN 69% uitkomt op de zekere
gevallen. Daaronder of gelijk is falen.

- 124-groep: 1,61% juist. FAIL.
- 110-groep: 1,82% juist. FAIL.

T0's regex-claim van 76/110 = 69% is weerlegd. De regex-baseline scoort
67/110 = 60,91% (77/124 = 62,1%). De drempel die de nieuwe run moest overtoppen
wordt door de regex zelf al niet gehaald. De definitie-tekst in de instructie
hielp niet; de capaciteitsfouten namen juist toe.

Het oordeel: het Laya-model classificeert poortbevindingen niet betrouwbaar op
deze taak, in geen van de drie rondes.

## Reproductiestappen

Het corpus wordt gegenereerd, niet gecommit. Volgorde:

1. `python3 01_extract_findings.py`
   Leest `review_gates/results/*.json` uit de centrale store (via
   `resolve_central_data_dir(resolve_project_id())`) en schrijft
   `data/findings.jsonl`. Print token-lengte statistieken. Dit script is
   read-only over de store.

2. `python3 02_regex_baseline.py`
   Leest `data/findings.jsonl`, classificeert met trefwoord-regex, schrijft
   `data/regex_predictions.jsonl`.

3. `python3 03_laya_classify.py <model_dir> <out_name> [nl|en]`
   Classificeert met een Laya Core ML checkpoint. Vereist `laya_coreml` en een
   lokaal model (niet gecommit, 1,4 GB). Schrijft
   `data/laya_predictions_<out_name>.jsonl`. Vier runs: ane96/td1024 x nl/en.

4. `python3 03b_laya_classify_with_definitions.py <model_dir> <out_name> [nl|en]`
   Ronden-3 variant: definities in de instructietekst. Schrijft
   `data/laya_predictions_<out_name>.jsonl`.

5. `python3 04_compare_and_sample.py`
   Vergelijkt alle runs, schrijft `data/comparison.json` en een gestratificeerde
   steekproef (zie `claudedocs/2026-09-22-laya-spike-steekproef.md`).

6. `python3 05_ground_truth_check.py --new-run data/laya_predictions_ane96_en_defs.jsonl --ronde2-run data/laya_predictions_ane96_en.jsonl --regex data/regex_predictions.jsonl --findings data/findings.jsonl`
   De grondwaarheidstoets. Schrijft `data/ground_truth_check.json` en print het
   falsificatie-oordeel.

7. `python3 test_count_bug.py`
   Regressietest op de telbug uit ronde 1. Geen pytest nodig, alleen stdlib.

Het corpus (`data/`) staat in `.gitignore` van de spike-map. Modellen (`models/`)
en venv (`.venv/`) ook. Wie de spike reproduceren wil draait eerst stap 1; wie de
laya-runs wil draait heeft daarnaast een lokaal model nodig.
