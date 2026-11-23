# ADR-090 - De accountmappen en hun slots komen uit CLAUDE_CONFIG_DIR en een mapping

**Status:** Voorgesteld 18-11-2026 (track `agnostisch-standaard`, levering ADR-A, dispatch `dlv-b96737603d5c`). Volgt ADR-075 op voor de accountbron en ADR-086 besluit 2 voor de paden van het toepassingsbewijs, via cross-ref; hun bestanden blijven ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-18
**Besloten door:** T0, op de plan-gate van 18-11 (ronde 5, tiebreaker STOP volgens de stop-rule, gaten opgelost in versie 6 van het plan). Het nummer 090 is door de T0 op 18-11 voor deze levering gereserveerd.
**Cross-refs:** ADR-065 (een T0 wijzigt nooit zijn eigen permissies), ADR-075 (de alinea over `account` na de lijst van wat de bestanden nooit verlaat; slots `vnx`, `sm`, `unknown`), ADR-079 (§ 83, bestaande ADR's zijn bevroren), ADR-086 (besluit 2, de tabel met settings-paden), ADR-087 (de globale laag en `scope: global`); plan `claudedocs/specs/agnostisch-standaard/plan.md` (gitignored, niet in de repo), § V2.6, V3.3, V4.5 en V6.5; open item OI-3000; track `agnostisch-standaard`, levering AG-3 (`dlv-1c991e361f55`).

**Deterministisch of model.** Alles in deze ADR is deterministisch: een vaste standaardmap, een omgevingsvariabele, een lijst met een gesloten set slotnamen, een padregel per map en een weigering bij het laden. Er komt geen model aan te pas. Een fout slot geeft een verkeerde permissieregel of een verkeerd bewijspad, en dat moet met een toets te vinden zijn.

## Context

Twee lezers kennen vandaag de accountmappen van de operator als letterlijke paden (gemeten op `e2d1fc8b`):

- `scripts/lib/permission_events_sources.py:22-23`: `DEFAULT_ACCOUNT_CONFIGS` is `~/.claude.json` en `~/.claude-salesminds/.claude.json`, en `ACCOUNT_SLOTS = ("vnx", "sm")`. Regel 90 koppelt die twee met `zip`: het eerste bestand is `vnx`, het tweede `sm`.
- `scripts/lib/permission_applied_check.py:25`: `USER_FILES` is `vnx` → `.claude/settings.json` en `sm` → `.claude-salesminds/settings.json`, onder de thuismap.

ADR-075 legt vast dat `account` uitsluitend `vnx`, `sm` of `unknown` is, en dat het slot volgt uit het `oauthAccount`-blok van die twee `.claude.json`-bestanden. ADR-086 besluit 2 noemt voor het bewijs van `applied` met bereik `user` dezelfde twee mappen.

Voor een buitenstaander is `~/.claude-salesminds` een privémap van de operator. Op zijn machine bestaat hij niet, en een tweede account staat daar in de map die hij zelf in `CLAUDE_CONFIG_DIR` zet. AG-3 haalt de vaste map uit de code. Deze ADR legt eerst het contract vast waartegen AG-3 bouwt.

## Besluit

**1. De accountmappen.** De lezers kennen drie bronnen, in deze volgorde:

- `~/.claude`, altijd;
- de map in `CLAUDE_CONFIG_DIR`, als die gezet is in de omgeving van het lezende proces;
- de mappen in de lijst van besluit 4, als die gezet is.

Geen enkele andere map wordt in code genoemd. Reden: `~/.claude` is de standaardmap van Claude Code op elke machine, en `CLAUDE_CONFIG_DIR` is de manier waarop Claude Code zelf een andere map kiest. Alles daarbuiten noemt de operator zelf.

**2. Een mapping naar de gesloten slots; het gebeurteniscontract verandert niet.** De lijst is `[{map, slot}]`, met `slot` uit `vnx` of `sm`.

- `~/.claude` is standaard `vnx`. Een regel in de lijst voor `~/.claude` vervangt die standaard.
- `CLAUDE_CONFIG_DIR` krijgt het slot van zijn regel in de lijst. Staat hij er niet in, dan is zijn slot `unknown`. Wijst hij naar `~/.claude`, dan is het dezelfde map.
- Mappen worden vergeleken na `expanduser` en het oplossen van het pad. Twee regels voor dezelfde map, of twee mappen met hetzelfde slot (de standaard van `~/.claude` telt mee), weigeren bij het laden: één slot wijst naar precies één map, anders is het bewijspad van besluit 6 niet eenduidig.
- Elke andere slotnaam, een regel zonder `map` of `slot`, of een waarde die geen lijst van zulke objecten is, weigert bij het laden met een melding die de sleutel en de waarde noemt. De lezer schrijft dan geen gebeurtenis met een geraden slot.
- `permission_event/1`, zijn schrijver, de validatie en de lezers blijven ongewijzigd. Bestaande gebeurtenissen blijven geldig.

Reden: zo blijft de gesloten set van ADR-075 het contract, en verandert alleen waar een slot vandaan komt. Met de standaardmapping en de regel uit besluit 7 levert de machine van de operator dezelfde slots als vandaag.

**3. Welk bestand per map.** Het accountbestand van een map is `<map>/.claude.json`, behalve voor `~/.claude`: dat is `~/.claude.json`, in de thuismap ernaast. Dat is de regel die Claude Code zelf volgt, en het is wat de code vandaag leest: `Path.home() / ".claude.json"` voor de standaardmap en `Path.home() / ".claude-salesminds" / ".claude.json"` voor de tweede (`permission_events_sources.py:22`). De lezer bouwt het pad uit de map met deze regel; er staat geen accountbestand meer als letterlijk pad in de code.

**4. Waar de lijst leeft: de registersleutel `VNX_ACCOUNT_DIRS`, scope `global`.** Type `string`, met als waarde de lijst als JSON, standaard `[]`. De sleutel valt onder config-een-oppervlak, dat elke registersleutel bezit en de globale laag `~/.vnx/config.json` levert (ADR-087 besluit 1 en 2). Een projectrij wordt geweigerd. AG-3 wacht op de registratie van deze sleutel in `scripts/lib/config_registry.py` en leest hem via `config_registry.get`, dat de globale laag ook zonder gekoppelde store leest.

Reden: welke accountmappen er zijn, is een eigenschap van de machine en haar operator, niet van één project. Twee projecten op dezelfde Mac zien dezelfde accounts, en de scan draait ook in een job zonder project. Een registersleutel voegt geen tweede configuratie-oppervlak toe: hij heeft de audit, de voorrang en de lezer van ADR-087. De sleutel krijgt `requires_approval`, omdat een verkeerde mapping een permissie aan het verkeerde account koppelt.

**5. Privacy.** Een gebeurtenis draagt alleen het slot, nooit het pad van een map, de `accountUuid` of een e-mailadres. Het pad staat alleen in de lokale configuratie (`~/.vnx/config.json` en zijn auditlog, beide buiten de repo). Een weigering bij het laden noemt de sleutel en de waarde in een melding op de console of in het log van het proces, nooit in een gebeurtenis, rapport of PR-tekst. De vergelijking van de uuid gebeurt in geheugen, zoals ADR-075 al besloot.

**6. Het bewijspad voor `applied` volgt dezelfde mapping.** Voor bereik `user` is het toegestane pad: slot → map → `<map>/settings.json`. Met de standaardmapping en de regel uit besluit 7 zijn dat de twee paden van ADR-086 besluit 2: `vnx` → `~/.claude/settings.json`, `sm` → `~/.claude-salesminds/settings.json`. Een slot zonder map in de mapping heeft geen toegestaan pad, en de controle weigert het `applied`-record. Bereik `project` verandert niet. ADR-086 blijft bevroren; deze ADR wijzigt de tabel van besluit 2 voor bereik `user` via deze cross-ref.

Reden: de map die het slot bepaalt, is dezelfde map waar de operator de regel toepast. Twee bronnen voor één koppeling zouden uit elkaar lopen.

**7. De overgang op de machine van de operator.** Vóór AG-3 de vaste `~/.claude-salesminds` uit de code haalt, staat de regel `{map: ~/.claude-salesminds, slot: sm}` in `VNX_ACCOUNT_DIRS` op vnx-dev. De T0 zet die regel en meet vóór de merge van AG-3 dat de lezer hem ziet. AG-3 heeft een toets die bewijst dat een gebeurtenis uit die map met de regel het slot `sm` houdt, en dat de bewijscontrole met de regel `~/.claude-salesminds/settings.json` toestaat.

Reden: zonder die regel valt elke sessie van het tweede account na de merge op `unknown`, en meet de effectmeting van ADR-086 een daling die er niet is.

**8. Geen vrije slotnamen.**

*Grens, aanvaard:* een buitenstaander kan alleen `vnx` en `sm` toekennen, ook als die namen voor hem niets betekenen. Een vrije slotnaam vraagt `permission_event/2` met een nieuwe schrijver, validatie, drie lezers en een lezer voor oude gebeurtenissen. Dat zijn minstens drie PR's op een permissiepad, voor een buitenstaander die in fase 1 één account heeft. Neutrale namen in plaats van `vnx` en `sm` horen bij een eigen besluit na 2.0; dat is een open vervolg van deze ADR.

## Gevolgen

**Config-een-oppervlak registreert:**

- `VNX_ACCOUNT_DIRS`, type `string`, standaard `[]`, scope `global`, `requires_approval`, met de beschrijving uit besluit 2 en 4.

**AG-3 bouwt en test, na die registratie:**

- `permission_events_sources.py`: de mappen uit besluit 1, de slots uit besluit 2, het bestand per map uit besluit 3; `DEFAULT_ACCOUNT_CONFIGS` als letterlijke tweede map verdwijnt;
- `permission_applied_check.py`: `USER_FILES` komt uit dezelfde mapping (besluit 6);
- toetsen: zonder lijst en zonder `CLAUDE_CONFIG_DIR` alleen `vnx`; met alleen `CLAUDE_CONFIG_DIR=<tmp>` is zijn slot `unknown` en wordt `~/.claude-salesminds` niet gelezen; de overgangsregel van besluit 7 geeft `sm`; met de standaardmapping zijn de paden gelijk aan ADR-086; met een andere map voor `sm` leest de controle die map; een onbekende slotnaam, een dubbele map, een dubbel slot en een waarde die geen geldige lijst is weigeren elk, met de sleutel in de melding en zonder gebeurtenis.

**Wat deze ADR niet toestaat:**

- een accountmap of accountbestand als letterlijk pad in code, behalve `~/.claude` en `~/.claude.json`;
- een slotnaam buiten `vnx`, `sm` en `unknown` in een gebeurtenis;
- een pad, uuid of e-mailadres in een gebeurtenis;
- een projectrij voor `VNX_ACCOUNT_DIRS`.

**Amendementen.** ADR-075 (de bron van `account`) en ADR-086 besluit 2 (de paden voor bereik `user`) gelden voortaan zo. Hun bestanden veranderen niet.

**Open vervolg.** Neutrale slotnamen, en daarmee `permission_event/2`, na 2.0 in een eigen ADR.

## Alternatieven

- **Een vrije slotnaam per map.** Nu afgewezen: zie besluit 8. Het kost een nieuwe gebeurtenisversie en minstens drie PR's op een permissiepad, voor een behoefte die in fase 1 niet bestaat.
- **Automatisch elke `~/.claude*`-map ontdekken.** Afgewezen: dat raadt. Een map die de operator niet genoemd heeft, zou gelezen worden, en het slot zou afhangen van de volgorde waarin het bestandssysteem de mappen teruggeeft.
- **De twee letterlijke paden houden.** Afgewezen: op de machine van een buitenstaander bestaat `~/.claude-salesminds` niet, en zijn tweede account blijft dan altijd `unknown`.
- **De lijst als eigen bestand of omgevingsvariabele.** Afgewezen: dat is een tweede configuratie-oppervlak naast ADR-087, zonder audit en zonder vaste voorrang.
