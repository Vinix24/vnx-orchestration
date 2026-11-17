# ADR-086 - Effectmeting vanaf het moment van toepassen, en wat een handmatige /permissions-wijziging bewijst

**Status:** Voorgesteld 17-11-2026 (track `permissieblokkades-leren`, levering L7a0, dispatch `dlv-70eab6f401a8-a0`). Amendeert ADR-072 en ADR-075 via cross-ref; hun bestanden blijven ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-17
**Besloten door:** T0, op binding 12 van de plan-gate van 16-11 (plan v4, `bindingen-v4.md`).
**Cross-refs:** ADR-065 (een T0 wijzigt nooit zijn eigen permissies), ADR-072 (besluit 9, `applied` en `applied_ref`), ADR-075 (besluit 6, bewaring; de lezer over de grens), ADR-079 (§ 83, bestaande ADR's zijn bevroren), ADR-084 (de drie permissiesoorten); plan `claudedocs/specs/permissieblokkades-leren/plan.md` (gitignored, niet in de repo), § Effectmeting; track `permissieblokkades-leren`, levering L7a.

## Context

Het plan meet na `applied` dezelfde vorm in de 7 dagen voor en na `decided_at`. Dat klopt niet. Tussen het besluit en het toepassen kunnen dagen liggen: Vincent keurt goed, en past de regel later toe met `/permissions`. Het ná-venster zou dan dagen tellen waarin de regel nog niet werkte. Codex zag dit in de plan-gate (punt 6).

Daarnaast bestaat er voor `applied` bij de soort `permissie` geen bewijs. ADR-072 vraagt een PR-nummer of een commit. Een `/permissions`-wijziging heeft geen van beide: zij staat in een settings-bestand buiten de repo. Zonder bewijs kan een T0 `applied` schrijven voor een regel die nooit is toegepast, en dan meet de lezer een effect van niets (codex punt 7).

ADR-075 besluit 6 legt de bewaring vast en zegt dat L7 live en archief leest. Het bewaarcommando `prune` heeft nog geen schema.

## Besluit

**1. Het anker is het moment van toepassen.** Het meetvenster is de 7 dagen vóór en de 7 dagen ná `applied_at`. `applied_at` is het moment waarop het `applied`-record is geschreven nadat de bewijscontrole (besluit 2) slaagde. Het is niet de `decided_at` van het besluit. Dit vervangt "voor en na `decided_at`" in het plan en de redenering in ADR-075 besluit 6 die daarop rust.

*Grens, aanvaard:* is de regel eerder toegepast dan vastgelegd, dan bevat het voor-venster dagen waarop de regel al werkte. Dat vertekent naar een kleinere daling, nooit naar een grotere.

**2. Bewijs van een handmatige wijziging (soort `permissie`).** `applied_ref` heeft de vorm `settings:<pad>#<eerste 16 hex van de sha256 van de exacte regeltekst>`. Het commando `applied` controleert op het moment van vastleggen, alleen lezend:

- het bestand is geldige JSON;
- `permissions.allow` is een lijst;
- die lijst bevat de exacte regeltekst van het voorstel.

Het record bewaart het pad, de regel, de sha256 van het hele bestand en de mtime. Het pad moet bij bereik en account van het voorstel passen:

| Bereik | Toegestane paden |
|---|---|
| `project` | `<repo>/.claude/settings.json`, `<repo>/.claude/settings.local.json` |
| `user`, account `vnx` | `~/.claude/settings.json` |
| `user`, account `sm` | `~/.claude-salesminds/settings.json` |

Elk ander pad, een ontbrekende regel of een onleesbaar bestand weigert het `applied`-record. Positief bewijs of geen record. De controle schrijft nooit een settings-bestand. Een T0 legt `applied` alleen vast via deze controle (ADR-065). Voor `bouwgewoonte` en `bouwer_allowlist` blijft de bestaande regel: een PR-nummer of een commit.

**3. De uitkomstwaarden.**

- `pending`: er zijn minder dan 7 dagen verstreken sinds `applied_at`.
- `unknown` met een reden: `retention_expired` (ADR-075 besluit 6), `coverage_not_ok` (een bron van de vorm was in een van beide vensters niet `ok`), of `no_anchor` (er is geen geldig `applied`-record).
- Anders beide getallen: sessies vóór en sessies ná. Een daling wordt alleen genoemd met beide getallen, nooit als percentage zonder.

**4. Lezen over de bewaargrens.** De meting leest het live grootboek en de archiefbestanden in `state/archive/permission_events/`, geschreven door `prune` (`scripts/lib/permission_events_prune.py`). `prune` heeft nog geen schema. Dat schema is een later besluit.

**5. De keten van L6, voor de goede orde (#2991).** De keten draait in de dream-stap na de steering-stap, in vaste volgorde: scan, koppelen, berekenen, wegschrijven. Koppelen verbindt een `asked`-gebeurtenis (B4a) met de eerste `executed` vnx-CLI-aanroep van `actor_class operator` met dezelfde `pair_key`, binnen 30 minuten. De `pair_key` is de vnx-CLI plus het bekende subcommandopad, zonder placeholders, positionele argumenten of vlaggen. Een aanroep koppelt hooguit één vraag. Passen twee vragen op één aanroep, dan koppelt geen van beide en telt de aanroep als `ambiguous`. Een ontbrekende aanroep is `unpaired`, nooit "Vincent deed het niet".

## Gevolgen

**L7a bouwt en test:**

- de bewijscontrole in `scripts/operator_steering.py applied`: pad per bereik en account, JSON, lijst, exacte regel, record met pad, regel, sha256 en mtime. Per tak een test met het veld ontbrekend, `None`, misvormd en een onbekende waarde; elk blijft een weigering;
- de effectmeting met `applied_at` als anker en de uitkomsten van besluit 3, met een test per `unknown`-reden;
- de lezer over live plus archief.

**Wat deze ADR niet toestaat:**

- een `applied` voor `permissie` zonder de controle;
- een settings-pad buiten het bereik en account van het voorstel;
- elke schrijfactie naar een settings-bestand, ook door de controle zelf.

**Amendementen.** ADR-072 (`applied_ref` voor `permissie`) en ADR-075 (besluit 6: anker `applied_at`) gelden voortaan zo. Hun bestanden veranderen niet.
