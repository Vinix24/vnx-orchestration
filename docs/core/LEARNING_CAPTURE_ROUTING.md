# Learning capture routing — snelle referentie

Besluit, motivering en auditcontract: [ADR-067](../governance/decisions/ADR-067-learning-capture-routing.md). Gebruik hieronder alleen de route en handelingen voor een capture.

## Kies één home

| Recordtype | Home | Wanneer |
|---|---|---|
| Open item | `state/open_items.json` | Probleem dat de sessie overleeft en een status nodig heeft. |
| Memory-notitie | `~/.claude/projects/<slug>/memory/<naam>.md` | Duurzame les die toekomstig gedrag verandert. |
| Build-log-nugget | `build-log-bucket/YYYY-MM.md` | Publiceerbare sessie-observatie. |
| Leerlus | `state/quality_intelligence.db` | Afgeleid voorstel; geen bestemming voor een rechtstreekse capture. |

Bij overlap wint open item, dan notitie, dan nugget. Bewaar het record in die home en verwijs vanuit de andere bakken met `OI-<n>` (`OI-[1-9][0-9]*`) of `[[naam]]` (zonder alias). Een nugget is nooit een doel. Een onopgeloste verwijzing blijft `onbekend (<reden>)`.

## Bronnen binden

Gebruik steeds de state-map van het bedoelde project. Eenmalig binden, met de geheugenmap onder `~/.claude/projects/` en de eigen bucketslug:

```bash
python3 scripts/capture_audit.py bind --state-dir <project-state> --memory-dir <geheugenmap> --bucket-dir <bucketmap> --bucket-slug <slug>
```

Dit schrijft `state/capture_sources.json`. Zonder geldige projectbinding is de bak `unbound`; `open_pairs: null` bewijst geen nul duplicaten.

## Adoptievolgorde

1. De nachtelijke run schrijft `state/capture_audit.json`.
2. Lees de open paren met `python3 scripts/capture_audit.py report --state-dir <project-state> --show`. Namen verschijnen alleen in de terminal; `--show` kan niet samen met `--json`. Ontbreekt het leesmodel of is het onleesbaar, dan stopt `report` met exit 2.
3. Per `open` paar: voeg de home-verwijzing toe aan het niet-home record, of leg de operatorbeslissing vast met `python3 scripts/capture_audit.py dismiss --state-dir <project-state> --pair <id> --reason <tekst>`. Neem de id uit het rapport en geef een niet-lege reden; de beslissing komt in `state/capture_dispositions.ndjson`.
4. De volgende run toont het paar als `linked` of `dismissed`. Een onleesbare dispositieledger telt niets als afgehandeld en wordt door `report` gemeld.
