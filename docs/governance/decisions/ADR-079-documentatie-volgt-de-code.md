# ADR-079 - Documentatie volgt de code: een gemeten scanset, vier regels en een eigenaar per bestand

**Status:** Voorgesteld 15-11-2026 (T0, track `documentatie-volgt-de-code`, levering A0)
**Datum:** 2026-11-15
**Besloten door:** de operator voor de track (mandaat van 15-11-2026 ~10:08Z) en de plan-gate op het plan `claudedocs/specs/documentatie-volgt-de-code/plan.md`: codex REVISE, tiebreaker START op versie 14 (`decision_ref: tiebreak:START`, rapport `plan-tiebreak-documentatie-volgt-de-code-24678f81`). Versie 15 draagt de verplichte wijziging (regel 7 en M5-b). Het nummer 079 is voor dit ADR gereserveerd; een parallelle levering schrijft ADR-080.
**Cross-refs:** ADR-063 (onbekend is nooit 0), ADR-078 (geheugen in de store), `scripts/ci/check_architecture_doc_paths.py`, `scripts/ci/check_docs_file_line_refs.py`, `scripts/check_subsystems_drift.py`, `scripts/lib/config_registry.py`.

Dit ADR legt vast waartegen de leveringen van de track bouwen: G1 (paden en links), G2 (versieclaims en symbolen), G3 (categorieën), M5 (register-documentatie) en de opruimrondes. Het bevat geen code. Waar het "moet" of "nooit" zegt, is dat een eis die een check of test kan controleren.

**Deterministisch of model.** Alles hier is deterministisch: een scanset uit `git ls-tree`, padmatching met `fnmatch`, een gesloten grammatica voor versieclaims, `ast` voor symbolen, een vergelijking met het register. Er is geen modelpad. Of een zin buiten de grammatica een claim is, beoordeelt een mens eenmalig (V1), geen model.

## Context

De documentatie onder `docs/` veroudert stil: claims over `VERSION` die niet meer kloppen, paden naar bestanden die niet meer bestaan, en subsystemen in het register zonder pagina. Er is geen gedeelde definitie van welk document bij welk soort wijziging hoort, en geen eigenaar per bestand. Een PR die gedrag verandert, laat het document dat dat gedrag beschrijft vaak liggen.

## Besluit

### 1. De scanset

De scanset is de uitvoer van `git ls-tree -r --name-only <sha> | grep -E '^(docs/.*\.md|README\.md|CONTRIBUTING\.md)$' | grep -Ev '^docs/(_archive|archive|investigations|compliance|governance/decisions)/' | sort -u`, op de sha die een check leest. Een check leest altijd de sha waarop hij draait, niet een vaste lijst.

### 2. De categorieën

Elk bestand van de scanset valt onder precies één van negen categorieën: Ingang, Verwijsstub, Verhaal, A, C, M-index, F, R en M-achtergrond. De voorrang is: eerst de Ingang-lijst, dan de vormregel van een verwijsstub, dan de eerste regel van `LAYER_RULES` die past. De paden per categorie staan in `scripts/ci/check_docs_layers.py::LAYER_RULES`; dit ADR kopieert ze niet.

- **Matchregel:** een pad past op een patroon als `fnmatch.fnmatchcase(pad, patroon)` waar is én pad en patroon evenveel `/` tellen. Zo dekt `*` precies één padsegment, en valt `docs/core/technical/X.md` niet onder `docs/core/*.md`.
- **Ingang:** een gesloten lijst van zeven bestanden (`README.md`, `CONTRIBUTING.md`, `docs/DOCS_INDEX.md`, `docs/README.md`, `docs/operations/README.md`, `docs/applications/README.md`, `docs/features/README.md`).
- **Verwijsstub:** hooguit 5 niet-lege regels, precies één kop (de titel met `#`) en minstens één relatieve link.

Een kopie op hetzelfde subpad onder `docs/_archive/` bewijst geen verplaatsing; G3 toetst verplaatsingen niet. Een bestaande link blijft geldig, ook bij een verplaatsing met een verwijsstub.

### 3. Het onderhoud per categorie

Elke categorie heeft één eigenaar en een of meer checks. De eigenaar is de partij die het bestand bijwerkt; er is op elk moment één eigenaar per bestand.

| Categorie | Eigenaar | Check |
|---|---|---|
| Ingang | de eigenaar van de levering die het bestand raakt | G1 |
| Verwijsstub | niemand, hij verandert niet | G3 deelt in, G1 toetst de links |
| Verhaal | de PR die het verhaal verandert | G1, G2 |
| A | `scripts/generate_architecture_doc.py` voor de `BEGIN GENERATED`-blokken, een mens voor de rest | G1, G2 |
| C | de PR die het gedrag verandert | G1 |
| M-index | M5 en de PR die een subsysteem toevoegt | `check_subsystems_drift.py`, G2 |
| F | de PR die een track sluit | G1, G2 |
| R | de PR die de handeling verandert | G1, G2 |
| M-achtergrond | de PR die het mechanisme verandert | G1, G2, `check_docs_file_line_refs.py` |

**Gegenereerde inhoud** bewerkt niemand met de hand. Gegenereerd zijn precies drie plekken: de kolommen subsystem, what, flag en status van de tabel in `docs/core/SUBSYSTEMS.md` (en vanaf M5-b ook de nieuwe linkkolom die het `doc`-veld van het register toont); elk blok tussen een `BEGIN`- en `END`-markering met het woord `generated` (hoofdletterongevoelig) in `docs/core/00_VNX_ARCHITECTURE.md`; en heel `docs/core/DAEMON_LIVENESS.md`. G1 en G2 geven elke bevinding op die plekken het label `generated`. Zo'n bevinding wordt opgelost in de bron (het register, `scripts/generate_architecture_doc.py` of `scripts/generate_daemon_liveness_md.py`, met hun invoer) en in dezelfde PR opnieuw gegenereerd. Een opruimronde slaat zulke bevindingen over. De proza van `SUBSYSTEMS.md` en zijn healthkolom zijn handwerk. M5-b laat `check_subsystems_drift.py` de waarde van de linkkolom vergelijken met het `doc`-veld van het register, en classificeert een afwijking als `generated`.

### 4. De versieclaimgrammatica

De grammatica van G2 (`--rule versions`) is gesloten en hoofdlettergevoelig voor `VERSION`. `VERSION` staat kaal, in backticks, of als link `` [`VERSION`](VERSION) ``. `<x>` is `\d+\.\d+\.\d+`, met of zonder backticks.

- **Een claim is:** `VERSION is <x>`, `VERSION says <x>`, `VERSION zegt <x>`, `VERSION staat op <x>`, `VERSION <x>`, `last release <x>`, `latest release (is) <x>` en `laatste release (is) <x>`.
- **Voorrang:** staat direct vóór de vorm `since`, `sinds`, `vanaf`, `tot`, `until` of `in`, dan is het geen claim (bijvoorbeeld `since VERSION 1.8.0`).
- **Geen claim:** een kleine `version` of een kop `**Version**:` (een formaatversie), `release <x> migrates`, een bereik, en alles in `CHANGELOG.md`, in een verwijsstub of in een fenced codeblok.
- **Bevinding:** een claim waarvan `<x>` afwijkt van de inhoud van `VERSION`.

Een zin buiten de grammatica ziet G2 niet; V1 behandelt die eenmalig met de hand. Een PR die `VERSION` ophoogt staat rood tot hij de claims in dezelfde PR bijwerkt: de release en zijn documentatie landen samen.

### 5. De symboolregel

Geldt alleen in `docs/modules/*.md` en `docs/features/*.md` (G2, `--rule symbols`). Een verwijzing naar code heeft de vorm `` `pad.py::naam` `` of `` `pad.py::Klasse.methode` ``. Het bestand is getrackt en de naam is in dat bestand gedefinieerd als `def`, `class` of toekenning op moduleniveau, of als methode van die klasse (via `ast`). `pad.py:<n>` in die twee mappen is een bevinding: een regelnummer veroudert bij elke merge.

### 6. De padregel

G1 (`--rule paths` en `--rule links`).

- **Kandidaat:** een inline backtick-token dat `scripts/ci/check_architecture_doc_paths.py::_BACKTICK_PATH_RE` en `_looks_like_repo_path` accepteren; G1 importeert die twee en kopieert ze niet. Fenced codeblokken tellen niet mee. Bestanden onder `docs/examples/` vallen buiten de padregel, hun links tellen wel.
- **Normalisatie, in deze volgorde:** knip `#anker` af, dan `::symbool`, dan een regelachtervoegsel (`:12`, `:12-20`, `:12–20` met en-dash, lijsten met komma's), dan een achtervoegsel `:naam` met een identifier.
- **Geen pad:** een token waarvan het laatste segment alleen cijfers en punten is (een schema-id).
- **Oplossing, in deze volgorde:** begint het token met `./` of `../`, dan relatief aan de map van het document. Anders een exact getrackt bestand of een getrackte map uit `git ls-files`. Anders precies één getrackt bestand of map dat eindigt op `/<token>`. Nul of twee of meer treffers is onopgelost.
- **Uitzondering:** alleen voor een onopgelost token, en de lijst staat in `scripts/ci/check_docs_stale_refs.py::PATH_EXCEPTIONS` (runtime-store- en lokale mappen, en het installatiepad `.claude/vnx-system/` in een consumentenrepo). Omdat de uitzondering pas na de oplossing komt, maskeert ze nooit een pad dat in de repo bestaat.
- **Bevinding:** elk ander onopgelost token.
- **Links:** elke markdownlink `[tekst](doel)` buiten een codeblok, behalve `http:`, `https:`, `mailto:` en een kaal `#anker`. Het doel zonder anker en titel moet relatief aan de map van het document een getrackt bestand of een getrackte map zijn. Een link naar een bevroren map mag, als het doel bestaat. Ankers toetst G1 niet.

### 7. De koppeling register-documentatie

Elke rij in het register (`scripts/lib/config_registry.py`) heeft een `doc`-veld met een pad onder `docs/modules/` of een reden uit de gesloten lijst `geschrapt`, `geen-module` en `niet-gebouwd`. Een subsysteem zonder vlag is een rij zonder vlag. Een dubbel subsysteem is een alias van de rij waar het bij hoort. `scripts/check_subsystems_drift.py` dwingt dat af, en een registerrij zonder geregenereerd `docs/core/SUBSYSTEMS.md` geeft exit 1.

### 8. Het uitzonderingsbeleid

Een regel in `PATH_EXCEPTIONS` of `LAYER_RULES`, een nieuwe bevroren map en de uitzondering voor `docs/examples/` veranderen alleen in een coderonde met een reviewpoort. De reden staat als commentaar naast de regel en in de PR-beschrijving. Een opruimronde voegt nooit een uitzondering toe. Een bestaand bestand onder `docs/_archive/`, `docs/archive/`, `docs/investigations/`, `docs/compliance/` of `docs/governance/decisions/` verandert nooit; alleen een nieuw ADR en een verplaatsing met `git mv` naar een nieuw pad onder `docs/_archive/` met een verwijsstub op het oude pad zijn toegestaan.

### 9. Documentatie in dezelfde PR

Een PR die gedrag verandert dat een document onder `docs/` beschrijft, werkt dat document in dezelfde PR bij. G1, G2 en G3 dwingen het mechanische deel af in CI. Voor de rest draagt `scripts/lib/prompts/base_worker.md` één regel die naar ADR-079 verwijst, zodat elke bouwer hem ziet.

## Gevolgen

- De checks G1, G2 en G3 en de koppeling van M5 hebben één normatieve bron; een wijziging van een regel is een wijziging van dit ADR of van de code die het ADR benoemt, nooit van een los document.
- Een PR met alleen docs kan rood staan op een claim, pad of link die hij niet zelf schreef; de eigenaar uit de tabel van besluit 3 lost dat op.
- Een release-PR moet de versieclaims in dezelfde PR bijwerken.
- `scripts/ci/check_docs_layers.py` en `scripts/ci/check_docs_stale_refs.py` bestaan op de datum van dit ADR nog niet; de namen `LAYER_RULES` en `PATH_EXCEPTIONS` worden door G3-a en G1-a geleverd.
- Dit ADR bevat de getallen van de basismeting niet; die horen bij de checks en hun fixtures.
