# ADR-089 - Linux is een volwaardig platform, Windows via WSL2, native Windows buiten scope

**Status:** Voorgesteld 18-11-2026 (track `agnostisch-standaard`, levering ADR-P, dispatch `dlv-09267f67f0fc`).
**Datum:** 2026-11-18
**Besloten door:** de operator, besluit B-20261118-ee6c4aae (18-11). Geschreven door T0 uit het plan.
**Cross-refs:** plan `claudedocs/specs/agnostisch-standaard/plan.md` (gitignored, niet in de repo), § "De platform-ADR" en V2.7; onderzoek `claudedocs/specs/framework-instelbaar/onderzoek-2026-11-18.md` (gitignored), § A.4 en § A.7; levering INV-A (`dlv-5e9c0072f381`, onderdeel 4: de macOS-only constructies op het Linux-pad); track `daemons-expliciet` (afgesplitst van `optionele-features-expliciet` op 18-11, besluit B-20261118-3ced8444); vervolgtrack `linux-gebruikerspad`.

## Context

VNX is gebouwd en gedraaid op macOS. Niets in `docs/governance/decisions/` zegt op welke platforms het framework moet werken. Het onderzoek van 18-11 (§ A.7) laat zien wat een buitenstaander tegenkomt: `vnx init` installeert launchd-agents en meldt een mislukking alleen als waarschuwing, en de README noemt macOS nergens. Een gebruiker op Linux of Windows weet dus niet wat er te verwachten valt.

De track `agnostisch-standaard` haalt de operatorstandaarden uit de code. Voordat daar code voor landt, moet vastliggen voor welk platform die code bedoeld is. De operator besliste op 18-11: Linux volwaardig, Windows via WSL2, native Windows buiten scope.

Wat de code vandaag aan POSIX veronderstelt, gemeten op `4e34cd5f1` (origin/main op 18-11). Bereik is de fabric uit het onderzoek (§ A.4), zonder tests, in zsh:

```
FAB=(scripts bin skills .github vnx_cli hooks lib packages configs templates)
git grep -n fcntl 4e34cd5f1 -- $FAB ':!tests' ':!**/tests/**' | wc -l    # 353 regels
git grep -l fcntl 4e34cd5f1 -- $FAB ':!tests' ':!**/tests/**' | wc -l    # 97 bestanden
git grep -n tmux  4e34cd5f1 -- $FAB ':!tests' ':!**/tests/**' | wc -l    # 1.096 regels
git grep -l tmux  4e34cd5f1 -- $FAB ':!tests' ':!**/tests/**' | wc -l    # 164 bestanden
git ls-tree -r --name-only 4e34cd5f1 -- $FAB | grep -v -e '^tests/' -e '/tests/' | grep -c '\.sh$'   # 179 shellscripts
git ls-tree -r --name-only 4e34cd5f1 | grep -c '\.sh$'                   # 259 shellscripts, hele repo
```

De doeltekst van het plan noemde op `5488d041` 295 `fcntl`-regels in 86 bestanden, 1.009 `tmux`-regels en 166 shellscripts, zonder het commando. Met het fabric-bereik hierboven geeft `5488d041` dezelfde getallen als `4e34cd5f1` (353, 97, 1.096, 179); het verschil met de doeltekst zit in het bereik, niet in de code. De getallen in deze ADR zijn de herhaalbare.

Alle 16 jobs in `.github/workflows/vnx-ci.yml` draaien op `ubuntu-24.04`. Geen workflow in `.github/workflows/` draait op macOS. De CI meet dus al alleen Linux.

## Besluit

**1. Linux is een volwaardig platform.** Elk pad dat een gebruiker raakt (`vnx init`, `vnx dispatch`, de mergedeur, de reviewpoorten, de receipt-processor) draait op Linux. Een macOS-only constructie op dat pad is een fout, geen beperking. Reden: de track bestaat voor een buitenstaander, en die draait meestal geen Mac; de CI draait bovendien al op Linux.

**2. Windows via WSL2.** De documentatie noemt WSL2 als de ingang voor Windows-gebruikers. Er komt geen native Windows-code. Reden: WSL2 levert een echte Linux-kernel, dus besluit 1 dekt Windows zonder tweede codepad.

**3. Native Windows buiten scope.** Reden is de meting hierboven: 353 `fcntl`-regels in 97 bestanden, 1.096 `tmux`-regels in 164 bestanden en 179 shellscripts in de fabric. Elke lock, de tmux-laag en de shellscripts zijn POSIX. Een native poort betekent elk van die plekken herschrijven, voor een gebruiker die met WSL2 al geholpen is.

*Grens, aanvaard:* een Windows-gebruiker zonder WSL2 kan VNX niet draaien.

**4. De CI meet Linux.** De Linux-runner van de CI is de plek waar het platform gemeten wordt. Een platform-skip op darwin telt niet als meting, en een toets die lokaal op de Mac groen is maar op de runner niet draait, is niet gemeten. Reden: een lokale run op macOS zegt niets over het pad dat besluit 1 belooft.

**5. Doel en stand zijn gescheiden.** Het doel is besluit 1. De stand is de telling die INV-A (`dlv-5e9c0072f381`, onderdeel 4) levert: de macOS-only constructies op het Linux-pad (`date -v`, `stat -f`, `sed -i ''`, `open`, `pbcopy`, `security`, `osascript`, launchd buiten `VNX_DAEMONS`), met pad en regel. De resterende constructies gaan naar een vervolgtrack `linux-gebruikerspad`. Reden: zonder telling is "Linux volwaardig" een bewering, geen meting.

*Grens, aanvaard:* er is geen claim "Linux volwaardig" voor 2.0 zolang `linux-gebruikerspad` niet is afgerond. Tot dan geldt besluit 1 als doel en is de telling van INV-A de stand.

**6. Wat elders al gedekt is.** De daemons en `vnx init` op Linux horen bij track `daemons-expliciet` (op 18-11 afgesplitst van `optionele-features-expliciet`, besluit B-20261118-3ced8444). Deze ADR beslist daar niets over. Reden: één eigenaar per onderwerp; launchd tegenover systemd of geen daemon is daar een eigen ontwerpkeuze.

## Gevolgen

- Een PR die een macOS-only constructie toevoegt op een gebruikerspad, is een fout onder besluit 1. Een reviewer mag hem daarop afwijzen.
- Een toets met een darwin-skip telt niet als bewijs voor Linux (besluit 4). Het bewijs is een groene run op de Linux-runner.
- De documentatie (README, QUICKSTART) noemt de platforms: Linux volwaardig als doel, Windows via WSL2. Die tekst volgt in de track die de stand afmaakt, niet in deze ADR.
- INV-A levert de telling; SLOT-1 van `agnostisch-standaard` noemt het aantal en de vervolgtrack `linux-gebruikerspad`.

**Wat deze ADR niet beslist:**

- hoe de daemons op Linux draaien (track `daemons-expliciet`);
- welke macOS-only constructies eerst gerepareerd worden (vervolgtrack `linux-gebruikerspad`, op de lijst van INV-A);
- een CI-runner op macOS.

## Alternatieven

**Native Windows-ondersteuning.** Afgewezen. Elke lock (353 `fcntl`-regels in 97 bestanden), de tmux-laag (1.096 regels in 164 bestanden) en de 179 shellscripts zijn POSIX. Een native poort vraagt een tweede implementatie van al die plekken, en een Windows-runner in de CI om haar te meten. WSL2 geeft hetzelfde resultaat zonder tweede codepad.

**Alleen macOS, met Linux als best effort.** Afgewezen. Een buitenstaander op Linux is precies de gebruiker voor wie de track `agnostisch-standaard` bestaat. Bovendien draait de CI al uitsluitend op Linux: een framework dat Linux alleen als best effort behandelt, meet zijn eigen platform niet.
