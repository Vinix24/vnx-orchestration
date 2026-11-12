# Deltareview: meting van de schaduwronde (DR-5)

De deltaronde mag pas de beslissende ronde worden (DR-6) als een meting zegt dat zij niets mist. Dit document beschrijft die meting. Bindend is ADR-064, sectie "Drempel".

## Het commando

```
vnx review delta-compare --since <ISO-tijd met zone | Nd> [--until <idem>] [--planted <manifest.json>] [--json] [--data-dir <.vnx-data>]
```

Exit 0 is `pass`, 1 is `fail`, 3 is `insufficient`, 2 is een gebruiksfout (onleesbare tijd, manifest of rondegrootboek).

## Wat een paar is

Een regel in `state/review_gates/shadow/delta.ndjson` met een `delta_verdict`, waarvan de kop een codex-ronde in het rondegrootboek heeft met `recorded_at` binnen het venster. De tijd komt uit het rondegrootboek, want de schaduwregel heeft geen tijdstempel. Elk paar telt, er is geen selectie. Een keten van diepte 2 (de `parent_head` is zelf de kop van een deltapaar van dezelfde PR) telt mee in het totaal en staat apart in `pairs_by_depth`.

Regels met `full_review_reason` (de delta weigerde zichzelf) of `shadow_error` zijn geen paar. Ze worden per reden geteld en genoemd.

Een paar waarvan de blockers van de volle ronde niet te lezen zijn (rapport weg, geen verdictblok, bron van een bestand niet te lezen, of minder blockers gelezen dan het rondegrootboek telt) is `unmeasurable`. Dat paar telt niet mee en ook niet als 0 gemist (ADR-063). Een kop zonder codex-ronde in het rondegrootboek is eveneens `unmeasurable`.

## Een gemiste blocker

Een blocker die de volle ronde vond en de delta niet.

- Een blocker waarvan het id naar de delta ging (een sleutel van `statuses`) is gemist als de delta `fixed` antwoordde. Alleen `open` en `regressed` tellen als gevonden. Elk ander antwoord telt als gemist.
- Elke andere blocker is gemist, tenzij `new_findings` een blokkerende bevinding heeft op hetzelfde `file_path` binnen 10 regels. Heeft een van beide kanten geen regel, dan volstaat hetzelfde bestand. Het id van een nieuwe bevinding valt niet te vergelijken: het hasht de tekst, dus een andere formulering geeft een ander id.

Een paar met minstens een gemiste blocker is een gemist paar. Blockers die alleen de delta vond staan als `extra_delta_blockers` en laten de drempel ongemoeid.

## Tijd en tokens

`delta_duration_seconds` komt uit de schaduwregel. De duur van de volle ronde en de tokens staan in geen van beide bronnen en lezen als `unknown`. Dat is nooit 0.

## De drempel

`pass` vraagt tegelijk:

1. minstens 20 paren;
2. 0 gemiste blockers;
3. een manifest met minstens 10 geplante gevallen, alle gevonden.

`fail` volgt bij elke gemiste blocker of een geplant geval dat niet gevonden is, ook bij te weinig paren. Anders is het `insufficient`, met de reden erbij.

Het 95%-interval (tweezijdig exact, Clopper-Pearson, zonder extra afhankelijkheden) staat in de uitvoer voor het aandeel gemiste paren. Bij 0 in 20 is de bovengrens 16,8%. Bij 0 in 40 is dat 8,8%. Neem het interval over in het meetdocument bij het besluit voor DR-6.

## De geplante set

Tien synthetische vervolgkoppen, elk met precies een bekende blocker, door de schaduwronde gehaald zoals een echte vervolgkop:

1. Neem een PR met een volle ronde op een eerste kop. Voeg op een vervolgkop een fout toe in een bestand met een bekende regel. De fout is een echte blocker: een gebroken randgeval, geen stijlpunt.
2. Laat de volle ronde en de schaduwronde op die kop lopen. De schaduwregel komt in `shadow/delta.ndjson`.
3. Noteer per kop `pr_number`, `head`, `file_path` en `line` van de geplante fout.
4. Herhaal tot tien koppen, in tien verschillende fouttypen.

Het manifest:

```json
{"schema": "delta_planted/1", "cases": [
  {"pr_number": 1234, "head": "<40 tekens>", "file_path": "scripts/lib/x.py", "line": 42}
]}
```

Een geval is gevonden als de schaduwregel op die kop `delta_verdict` `fail` heeft en een blokkerende nieuwe bevinding op dat bestand binnen 10 regels van de geplante regel. Ontbreekt de schaduwregel, dan is het geval niet gevonden.

Deze PR voert de procedure niet uit. Zij levert het commando en de beschrijving.

## Uitvoer lezen

Per paar: PR, kop, diepte, gemiste blockers van het aantal blockers van de volle ronde, extra deltablockers, duur en tokens. Daarna de niet-paren per reden, de onmeetbare paren met reden, het totaal met interval, de geplante gevallen en de uitkomst. `--json` geeft dezelfde gegevens machineleesbaar.
