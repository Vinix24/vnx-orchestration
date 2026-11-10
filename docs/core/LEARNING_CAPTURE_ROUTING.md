# Learning capture routing

Working copy of ADR-067 (`docs/governance/decisions/ADR-067-learning-capture-routing.md`). The ADR decides; this page is what you open while capturing a lesson. When the two differ, the ADR wins.

## Welke bak waarvoor

| Recordtype | Home | Sleutel | Wanneer |
|---|---|---|---|
| Open item | `state/open_items.json` | `OI-<n>` | Een probleem dat de sessie overleeft en een status nodig heeft |
| Memory-notitie | `~/.claude/projects/<slug>/memory/<naam>.md` | bestandsnaam zonder `.md` | Een duurzame les die toekomstig gedrag verandert |
| Build-log-nugget | `build-log-bucket/YYYY-MM.md` | locator-hash van de kop | Een publiceerbare sessie-observatie |
| Leerlus | `state/quality_intelligence.db` | geen | Een uit bewijs afgeleid patroon: alleen voorstel, nooit een bron |

Bij een gelijke vondst in twee bakken wint de volgorde open item, dan notitie, dan nugget. Eén record heeft precies één home. De andere bakken verwijzen alleen.

## Verwijzen

- Naar een open item: `OI-<n>`, gelezen met `OI-[1-9][0-9]*`.
- Naar een notitie: `[[naam]]`, zonder alias-deel.
- Een nugget is nooit een doel. Een nugget mag wel verwijzen.

Een verwijzing die niet oplost leest als `onbekend (<reden>)`, nooit als `open`.

## Wanneer een paar telt

De audit vergelijkt per paar twee records van verschillende bakken: `oi_mem`, `oi_nug` of `mem_nug`. Een paar telt voor een venster `(project_id, since, until)` wanneer minstens één van de twee records nieuw is in dat venster. De partner mag elke leeftijd hebben. Het venster komt uit `explicit` (de Done-meting), `chain` (de nachtelijke run) of `first24h` (de eerste run). Een meting zonder nieuwe records (`anchors_total == 0`) is `null` en geen nul.

## De drie standen van een paar

| Stand | Betekenis |
|---|---|
| `linked` | Het niet-home record draagt de sleutel van het home record. De audit ziet dat zelf. |
| `dismissed` | De operator zegt dat het geen duplicaat is, met een reden in `capture_dispositions.ndjson`. |
| `open` | Geen van beide. Alleen deze stand telt in `open_pairs`. |

## Adoptievolgorde

1. De nachtelijke run schrijft `state/capture_audit.json` en zet `duplicate_capture_open` en `capture_audit_bins` in de beacon.
2. Draai `python3 scripts/capture_audit.py report` (met `--show` om namen lokaal op te lossen in de terminal).
3. Per open paar: voeg `OI-<n>` of `[[naam]]` toe aan het niet-home record, of draai `python3 scripts/capture_audit.py dismiss --pair <id> --reason <tekst>`. Een lege reden wordt geweigerd.
4. De volgende run toont het paar als `linked` of `dismissed`.

De commando's `report` en `dismiss` landen in PR-4 van het spoor. Tot dan is het leesmodel zelf de enige plek om de open paren te zien.

## Bronnen en binding

De audit leest alleen via `state/capture_sources.json`, geschreven door `python3 scripts/capture_audit.py bind`. Het bevat het `project_id`, de geheugenmap (onder `~/.claude/projects/`), de bucketmap en de bucketslug. Zonder dat bestand, of bij een afwijkend `project_id`, is de bak `unbound` en is `open_pairs` `null`. Entries van een andere slug worden geteld en niet gelezen.

## Waarom

- **Open item**: alleen daar staat een status die sluit, en een les zonder status leest als actueel terwijl het probleem dicht kan zijn.
- **Memory-notitie**: alleen een notitie wordt bij een volgende dispatch teruggezet, dus een les die gedrag moet veranderen hoort hier.
- **Build-log-nugget**: een observatie voor publicatie heeft geen status en geen gedrag nodig, en wint daarom de laagste voorrang bij een dubbele vondst.
- **Leerlus**: een afgeleid patroon is geen bron, en goedkeuren zou via `ingest_approved_rules` een preventieregel in dispatches zetten.
