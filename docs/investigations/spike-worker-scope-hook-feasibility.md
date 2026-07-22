# Spike: worker-scope PreToolUse-hook feasibility

**Dispatch:** `20260722-worker-scope-hook-spike`
**Branch:** `spike/worker-scope-hook-feasibility`
**Aard:** investigation-only. Geen enforcement-mechanisme gebouwd, geen wijziging aan `worker_permissions.py`, spawners, of settings.json.

**Pad-afwijking t.o.v. dispatch-spec:** de dispatch vroeg om dit rapport op
`claudedocs/spike-worker-scope-hook-feasibility.md`. `.gitignore:121` sluit `claudedocs/` echter
volledig uit — een bewuste OI-1373-beslissing (PR's #441-443) om strategie-/plandocs lokaal en
ongecommit te houden. Committen naar dat pad was dus niet mogelijk zonder die policy te
overschrijven. Dit rapport staat daarom op `docs/investigations/` (git-tracked) in plaats daarvan.

De worker-scope-enforce-hook-plan-gate ketste 2x af op één empirische onbekende:
vuurt een PreToolUse-hook in een gespawnde VNX-worker onder `--dangerously-skip-permissions`,
en wordt een worktree-lokale `.claude/settings.local.json` gehonoreerd? Deze spike draait
IN zo'n worker (tmux-spawn lane, dispatch `20260722-worker-scope-hook-spike`) en beantwoordt
de vraag empirisch, live, in de eigen sessie.

---

## E1 — vuurt een PreToolUse-hook onder skip-permissions?

**Bevestigd via `ps aux`** (eigen proces, PID 56523, gestart 08:02 in deze worktree):

```
claude --model claude-sonnet-5 --dangerously-skip-permissions
```

Dit is exact de vlag waarop de vraag draait — geen aanname, gemeten uit de lopende procestabel.

**Uitgevoerd:**
1. `.claude/settings.local.json` geschreven (MIDDEN in de al lopende sessie, dus ná session-start)
   met een PreToolUse-matcher op `Bash`:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "command": "echo \"HOOK_FIRED $(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> /tmp/worker-scope-spike-e1.log"
             }
           ]
         }
       ]
     }
   }
   ```
2. Direct daarna 4 losse `Bash`-tool-calls uitgevoerd (geen sessie-restart, geen nieuwe claude-launch).
3. `/tmp/worker-scope-spike-e1.log` gecontroleerd na elke ronde.

**Observatie (feiten):**
- Na de eerste `Bash`-call (die zelf o.a. `rm -f` op het logbestand deed) stond de marker al in het
  bestand vóórdat de rest van diezelfde bash-regel liep — het bestand werd ná de hook-write weer
  verwijderd door de `rm -f` in dezelfde command-string, en een volgende call herschreef de marker.
- Twee losse, opvolgende `Bash`-calls die het logbestand niet aanraakten, gaven **exact 4 regels**
  `HOOK_FIRED <timestamp>` voor **4 Bash-tool-calls** — 1:1, geen gemiste hits, geen dubbele hits
  buiten de verwachte (2 calls in hetzelfde bericht kregen dezelfde seconde-timestamp, wat klopt met
  gelijktijdige verwerking).
- Resultaat: **de hook vuurde op elke Bash-tool-call, 4/4, zonder uitzondering.**

**Vraag 2 — reload in lopende sessie vs. alleen bij sessiestart:**
De sessie waarin dit draait was al ~40+ minuten actief (PID gestart 08:02) vóórdat
`.claude/settings.local.json` werd geschreven. De hook vuurde al bij de **eerstvolgende**
tool-call na het schrijven van het bestand — geen sessie-restart, geen nieuwe `claude`-launch.
**Feit: hook-config uit `.claude/settings.local.json` wordt live herladen/opnieuw gelezen,
niet alleen bij sessiestart gecached.**

**Vraag 4 — onderdrukt `--dangerously-skip-permissions` PreToolUse-hooks?**
Nee. Deze sessie draait al met die vlag (bevestigd via `ps aux`, zie boven) en de hook vuurde
niettemin op elke call. `--dangerously-skip-permissions` onderdrukt de **permission-prompt**
(mens-in-de-loop-gate), niet de PreToolUse-hook-pijplijn zelf — dat zijn aantoonbaar
onafhankelijke mechanismen.

**Niet getest (eerlijk gerapporteerd):** een vergelijkende run van dezelfde hook in een sessie
gestart ZONDER `--dangerously-skip-permissions`. Dat vereist een nieuwe, apart gespawnde
`claude`-sessie met een andere launch-vlag — spawnen van een tweede claude-proces vanuit hier
zou zelf al door de bestaande `pretooluse_block_raw_claude_spawn.sh`-hook (zie E2) geblokkeerd
worden, en valt sowieso buiten de scope van deze spike (geen nieuwe dispatch). Gezien de vlag
zichtbaar geen effect had op de hook die al liep, en elke productie-worker via
`_default_launch_command()` altijd met deze vlag start (zie E2), voegt de "zonder-vlag"-vergelijking
weinig toe aan het praktische antwoord voor gespawnde workers.

---

## E2 — welk settings-pad wordt gehonoreerd?

**Bestaand bewijs in dezelfde worktree, vóór enige wijziging door deze spike:**
`.claude/settings.json` in deze worktree is **git-tracked** (`git ls-files` bevestigt het,
`git log` toont commits t/m #1174) en bevat al een actieve `PreToolUse`-hook op `Bash`
(`pretooluse_block_raw_claude_spawn.sh`, blokkeert rauwe `claude -p`/`--dangerously-skip-permissions`/
kimi/codex-spawns) en op `Task` (`pretooluse_block_subagent.sh`). Dit bestand is dus al vóór deze
spike het levende bewijs dat een repo-gecommit `.claude/settings.json`, uitgecheckt in een
per-dispatch worktree, wordt gediscovered door de gespawnde worker.

**Launch-regel gegrept** in `scripts/lib/tmux_interactive_dispatch.py`, functie
`_default_launch_command()` (regel 219-296, met `_launch_claude()` op regel 1040-1048 die de
string via `tmux send-keys -l` + losse `Enter`-keystroke verstuurt):

```python
return f"source ~/.zshrc 2>/dev/null; claude --model {model}{session_arg}{flags}"
```

waarbij `flags` in het **default (niet-scoped) pad** — dat is het pad dat deze worker daadwerkelijk
gebruikt, bevestigd via `ps aux` — gelijk is aan:

```python
flags = " --dangerously-skip-permissions"
```

**Geen `--settings`, geen `--permission-mode` in dit default pad.** Er wordt geen enkel settings-pad
expliciet meegegeven op de launch-regel. Dat geldt ook voor het **opt-in scoped pad**
(`VNX_ENFORCE_WORKER_PERMISSIONS=1` / `VNX_WORKER_SCOPED=1`, regel 281-288, gebouwd door
`worker_permissions.build_claude_scope_args()`): dat voegt `--permission-mode`,
`--strict-mcp-config --mcp-config {}` en `--allowedTools` toe, maar **ook daar geen `--settings`-vlag**.
Dezelfde afwezigheid van `--settings` is bevestigd in de subprocess-lane
(`scripts/lib/subprocess_adapter.py:55-122`, functie `_build_worker_scope_args`) — beide lanes
bouwen scope-args via dezelfde `worker_permissions`-module, geen van beide overschrijft het
settings-pad.

**Conclusie:** zonder `--settings`-vlag valt de gespawnde `claude` terug op zijn normale
project-settings-discovery vanuit de `cwd` waarin tmux de sessie start (de per-dispatch worktree,
zie `_spawn_session()`, regel 1004-1038: `["new-session", "-d", "-s", session, "-c", str(cwd)]`).
Dat is precies het pad dat E1 empirisch bevestigt: de worktree-lokale
`.claude/settings.json` (git-tracked) én `.claude/settings.local.json` (gitignored,
door deze spike zelf toegevoegd en gehonoreerd) worden allebei gelezen. Er is geen aparte
`--settings`-route nodig of aanwezig — het cwd-gebaseerde discovery-mechanisme volstaat en is al
actief bewezen door de bestaande spawn-blocker-hook in `settings.json`.

---

## E3 — kan de hook de worker-rol resolven?

```
$ printenv | grep -iE "VNX_|ROLE|TERMINAL"
AI_AGENT=claude-code_2-1-217_agent
CLAUDECODE=1
CLAUDE_CODE_CHILD_SESSION=1
CLAUDE_CODE_ENTRYPOINT=cli
CLAUDE_CODE_EXECPATH=/Users/vincentvandeth/.local/share/claude/versions/2.1.217
CLAUDE_CODE_SESSION_ID=04615f54-378b-4913-bf13-9c9f707f25ff
CLAUDE_EFFORT=xhigh
CLAUDE_PID=56523
VNX_CURRENT_DISPATCH_ID=20260722-worker-scope-hook-spike
VNX_DIGEST_EMAIL=vincentvd@gmail.com
VNX_DISPATCH_ID=20260722-worker-scope-hook-spike
VNX_SMTP_PASS=pktx cwax lwuh qogr
VNX_TMUX_MAX_CONCURRENT=10
VNX_TMUX_SIGNAL_DIR=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-tmux-sig-98skn4z5
```

**Bevestigd:** alleen `VNX_CURRENT_DISPATCH_ID` / `VNX_DISPATCH_ID` zijn beschikbaar. **Geen**
`VNX_WORKER_ROLE`, geen `ROLE`, geen `TERMINAL`-signaal in de pane-env. Dit dekt exact wat de
plan-gate al flagde.

**Root cause gevonden in de code** (niet aangenomen — gegrept):
`scripts/lib/worker_permissions.py:268` — `resolve_worker_profile(role, yaml_path=None)` — `role`
wordt als **in-process Python-argument** doorgegeven aan `_wp_build_claude_scope_args()` binnen
`_default_launch_command()` (aanroeper-kant, vóór de tmux-spawn). De rol is dus bekend bij de
**orchestrator/spawner**, op het moment dat de launch-string wordt samengesteld, en wordt
**gebakken in de `--allowedTools`/`--permission-mode`-vlaggen van diezelfde launch-string** —
maar nooit als env-var de tmux-pane in geëxporteerd. `_spawn_session()` exporteert alleen
`VNX_CURRENT_DISPATCH_ID` en optioneel `VNX_CLAUDE_SESSION_ID` (regel 1013-1025); een
`VNX_WORKER_ROLE`-export ontbreekt volledig.

**Conclusie:** een PreToolUse-hookscript dat binnen de worker draait, kan de rol NIET zelf
opzoeken via env — de plumbing bestaat simpelweg niet. Dit bevestigt D1 uit het plan: als een
settings.json-hook rol-afhankelijk moet gedragen (bv. andere scope per T1/T2/T3), moet
`_spawn_session()` een `VNX_WORKER_ROLE=<role>`-export krijgen naast de bestaande
`VNX_CURRENT_DISPATCH_ID`-export — dat is een kleine, geïsoleerde wijziging (één extra
`-e`-argument), maar staat buiten de scope van deze spike (geen wijziging aan spawners).

---

## E4 — subprocess-lane (best-effort, niet empirisch getest)

**Wat wél vastgesteld is (statische analyse, geen executie):**
`scripts/lib/subprocess_adapter.py:55-122` (`_build_worker_scope_args`) volgt exact hetzelfde
patroon als de tmux-lane: default `--dangerously-skip-permissions` (regel 67, 105-106), opt-in
scoped pad via dezelfde `worker_permissions`-functies, en **ook hier geen `--settings`-vlag** en
**geen rol-env-var-export** zichtbaar in de gegrepte sectie. Voor zover statisch zichtbaar zou
dezelfde cwd-based settings-discovery moeten gelden — de headless `claude -p`-launch bouwt zijn
argv op dezelfde manier, zonder een expliciet settings-pad te forceren.

**Expliciet NIET getest in deze spike:** of een PreToolUse-hook daadwerkelijk vuurt in een
`claude -p`-proces. Dat vereist een losse subprocess-lane-dispatch (deze spike draait zelf via de
tmux-interactive-lane, PID 56523, `--dangerously-skip-permissions` — niet via `claude -p`) en valt
buiten de scope van dit onderzoek. **Aanbeveling:** een aparte, kleine subprocess-dispatch die
dezelfde E1-marker-test herhaalt, vóórdat enforcement voor die lane wordt aangenomen te werken.

---

## Verdict

**Ja, een settings.json-PreToolUse-enforcement-hook is haalbaar in gespawnde tmux-lane workers.**

- **Settings-pad:** geen `--settings`-vlag nodig of aanwezig. Zowel een git-tracked
  `.claude/settings.json` als een gitignored `.claude/settings.local.json` in de per-dispatch
  worktree worden gehonoreerd via normale cwd-based discovery — bewezen door de reeds-bestaande
  spawn-blocker-hook (E2) én door de zelf-toegevoegde marker-hook (E1), beide in dezelfde worktree
  waarin de gespawnde worker daadwerkelijk draait.
- **Skip-permissions is geen blocker:** `--dangerously-skip-permissions` (het default launch-pad,
  bevestigd via `ps aux`) onderdrukt alleen de mens-in-de-loop permission-prompt, niet de
  PreToolUse-hook-pijplijn. De hook vuurde 4/4 in een sessie die al met die vlag draaide.
- **Config-wijzigingen zijn live:** een hook toegevoegd ná sessiestart vuurde al bij de eerstvolgende
  tool-call — geen restart nodig. Dat betekent ook: een hook die bij worktree-allocatie wordt
  weggeschreven (vóór de `claude`-launch-regel wordt verstuurd) is met zekerheid actief zodra de
  sessie draait.
- **Rol-plumbing is het enige echte gat (D1), en het is klein:** de rol is server-side bekend
  (`resolve_worker_profile(role)` in `worker_permissions.py`) maar wordt nergens als env-var de
  pane in geëxporteerd — alleen `VNX_CURRENT_DISPATCH_ID`/`VNX_CLAUDE_SESSION_ID` worden geëxporteerd
  in `_spawn_session()` (`tmux_interactive_dispatch.py:1013-1025`). Een rol-afhankelijke hook heeft
  dus **één concrete, geïsoleerde toevoeging** nodig: een `VNX_WORKER_ROLE`-export naast de
  bestaande dispatch-id-export, analoog aan hoe `VNX_CURRENT_DISPATCH_ID` nu al wordt doorgegeven.
  Zonder die export kan een hookscript wel *dat* er een Bash-call gebeurt, maar niet *welke rol*
  hem uitvoert — enforcement zou dan alleen rol-onafhankelijke regels kunnen toepassen (of zelf
  moeten afleiden uit `dispatch_id` → een lookup in central state, een indirecte en broze route).
- **Subprocess-lane blijft open (E4):** dezelfde argv-opbouw (geen `--settings`, geen rol-env)
  is statisch zichtbaar, maar niet empirisch bevestigd. Behandel dit als aanname-tot-bewezen,
  niet als vaststaand feit, tot een aparte subprocess-dispatch de marker-test herhaalt.

**Alternatieve laag nodig?** Nee, voor de tmux-lane niet — een settings.json-hook is het native,
bewezen mechanisme. Er is geen wrapper-rond-tool-invocatie nodig als alternatief; de bestaande
`pretooluse_block_raw_claude_spawn.sh` in dit repo is zelf al het bewijs dat dit patroon in
productie werkt. De enige bouwsteen die ontbreekt voor een *rol-bewuste* variant is de
`VNX_WORKER_ROLE`-env-export in de spawner — een gerichte, kleine wijziging voor een vervolg-PR,
niet een architectuur-heroverweging.
