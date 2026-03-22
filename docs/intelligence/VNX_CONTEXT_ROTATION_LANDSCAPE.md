# Context Rotation Landscape — Marktonderzoek

> Onderzoek: 2026-02-23 | Auteur: T-MANAGER | Scope: GitHub repos, Anthropic issues, tech forums

## Conclusie

**Niemand heeft een volledige closed-loop context rotation gebouwd.** Er zijn 5 projecten die delen van het probleem oplossen, maar allemaal missen ze minimaal 1 van de 3 kerncomponenten: automatische detectie, tmux automation, of handover injectie. Het VNX Context Rotation System is het eerste dat alle drie combineert in een volledig geautomatiseerde pipeline.

---

## Bestaande Projecten

### 1. claude-code-handoff (Sonovore)

- **URL**: https://github.com/Sonovore/claude-code-handoff
- **Datum**: 2026-02-21 (zeer recent)
- **Aanpak**: Handmatig `/handoff` slash command → genereert `.claude/context.md` → SessionStart hook injecteert bij volgende sessie
- **Hooks**: SessionStart (inject) + PreCompact (re-inject voor autocompaction)
- **Automatische detectie**: Nee — gebruiker moet handmatig `/handoff` triggeren
- **Stop hook block**: Nee
- **tmux /clear**: Nee
- **Handover inject**: Ja, via SessionStart + PreCompact hooks
- **Modi**: Context mode (single file), Task mode (meerdere files), Recovery mode (transcript parsing)
- **Vergelijking**: Alleen het recovery-deel van de VNX pipeline. Geen detectie, geen automatische /clear.

### 2. claude-session-restore (ZENG3LD)

- **URL**: https://github.com/ZENG3LD/claude-session-restore
- **Aanpak**: Rust CLI die JSONL session files parst via multi-vector analyse (tasks, user messages, tool operations, git history)
- **Automatische detectie**: Nee — handmatig `restore session` of `session-summary list`
- **Stop hook block**: Nee
- **tmux /clear**: Nee
- **Handover inject**: Ja, maar achteraf — parst vorige sessie transcripts
- **Technisch**: Tail-based reverse parsing voor grote bestanden (2GB+), Rust workspace
- **Vergelijking**: Achteraf reconstructie tool. Geen real-time monitoring, geen automatische interventie.

### 3. claude_code_agent_farm (Dicklesworthstone)

- **URL**: https://github.com/Dicklesworthstone/claude_code_agent_farm
- **Aanpak**: Orchestratie framework voor 20+ parallelle Claude Code agents met automatische context monitoring
- **Automatische detectie**: Semi — `--context-threshold 20` parameter laat agents zelf clearen bij lage context
- **Stop hook block**: Nee
- **tmux /clear**: Ja — Ctrl+R broadcast van `/clear` naar alle agents
- **Handover inject**: Nee — rely op file-based task queues en problem registries
- **Vergelijking**: Meest vergelijkbaar met VNX qua multi-agent tmux orchestratie. Maar geen handover documenten — na /clear begint de agent opnieuw aan zijn task queue zonder context van de vorige sessie.

### 4. /wipe gist (GGPrompts)

- **URL**: https://gist.github.com/GGPrompts/62bbf077596dc47d9f424276575007a1
- **Aanpak**: Bash script als slash command: handoff genereren → clipboard backup → tmux send-keys `/clear` → inject handoff na 8s delay
- **Automatische detectie**: Nee — handmatig `/wipe` commando
- **Stop hook block**: Nee
- **tmux /clear**: Ja — `tmux send-keys` + `load-buffer` + `paste-buffer`
- **Handover inject**: Ja — inject in terminal na 8 seconden delay
- **Technisch detail**: Gebruikt literal mode (`-l`) voor tmux send-keys, clipboard als fallback
- **Vergelijking**: Technisch het dichtst bij de VNX rotate flow (tmux load-buffer + paste-buffer patroon is identiek). Maar handmatig getriggerd, geen context pressure detectie, geen locking, geen multi-terminal awareness.

### 5. claude-code-context-sync (Claudate)

- **URL**: https://github.com/Claudate/claude-code-context-sync
- **Aanpak**: Save/resume context across multiple windows
- **Automatische detectie**: Nee — handmatig save/resume
- **Stop hook block**: Nee
- **tmux /clear**: Nee
- **Handover inject**: Ja, cross-window sync
- **Vergelijking**: Multi-window focus maar geen automatische detectie of clearing.

---

## Feature Requests bij Anthropic

| Issue | Titel | Status | Relevantie |
|-------|-------|--------|------------|
| [#9118](https://github.com/anthropics/claude-code/issues/9118) | Ability to /clear from hook scripts | **Gesloten — NOT_PLANNED** | Direct relevant: vraagt precies wat VNX omzeilt via tmux |
| [#3314](https://github.com/anthropics/claude-code/issues/3314) | Context Window Reset Without Session Restart | Open | Beschrijft het kernprobleem dat VNX oplost |
| [#11455](https://github.com/anthropics/claude-code/issues/11455) | Session Handoff / Continuity Support | Open | Vraagt om `/handoff` als native feature |
| [#21388](https://github.com/anthropics/claude-code/issues/21388) | Context Management Options (`--compress-record`, `--context-mode`) | Open | Wil CLI flags voor context management gedrag |
| [#18878](https://github.com/anthropics/claude-code/issues/18878) | Allow configuring or disabling 'clear context' default in plan mode | Open | Gerelateerd aan /clear gedrag |
| [#13989](https://github.com/anthropics/claude-code/issues/13989) | Add /restart command | Open | Wil sessie restart zonder terminal sluiten |
| [#3656](https://github.com/anthropics/claude-code/issues/3656) | Restore Blocking Stop Command Hooks | Open | Bug: `decision: "block"` werd genegeerd |
| [#3046](https://github.com/anthropics/claude-code/issues/3046) | /clear causes transcript issues breaking Stop hooks | Open | Technische complicatie die VNX moet omzeilen |

**Opvallend**: Issue #9118 (ability to /clear from hooks) is door Anthropic gesloten als NOT_PLANNED. Dit bevestigt dat de VNX aanpak via tmux send-keys de enige haalbare route is — Anthropic biedt geen native hook-based clearing.

---

## Vergelijkingsmatrix

| Feature | VNX Rotation | handoff | session-restore | agent_farm | /wipe gist | context-sync |
|---------|-------------|---------|----------------|------------|-----------|-------------|
| Automatische context detectie | ✅ Stop hook + remaining_pct | - | - | Semi (threshold flag) | - | - |
| Stop hook `decision: "block"` | ✅ | - | - | - | - | - |
| Handover document generatie | ✅ Claude schrijft md | ✅ /handoff command | ✅ Transcript parsing | - | ✅ Handmatig | ✅ Save command |
| tmux /clear automation | ✅ nohup async | - | - | ✅ Ctrl+R broadcast | ✅ send-keys | - |
| SessionStart recovery inject | ✅ additionalContext | ✅ SessionStart hook | ✅ Skill-based | - | ✅ paste-buffer | ✅ Sync inject |
| Multi-terminal awareness | ✅ Per-terminal locking | - | - | ✅ Multi-agent | - | ✅ Multi-window |
| Atomic locking | ✅ mkdir + timestamp TTL | - | - | - | - | - |
| Fallback chain (T2/T3) | ✅ --fallback worker bootstrap | - | - | - | - | - |
| Volledig geautomatiseerd (zero human) | ✅ | - | - | - | - | - |

---

## Unieke VNX Differentiators

### 1. Automatische detectie via Stop hook

Geen enkel project gebruikt Stop hooks met `decision: "block"` om context pressure te detecteren en handover te forceren. De agent_farm heeft een `--context-threshold` maar dat is self-monitoring door de agent, niet een hook-based enforcement.

### 2. Closed-loop zero-touch automation

De volledige keten zonder menselijke interventie:
```
Statusline schrijft remaining_pct
  → Stop hook detecteert ≥80% used → decision: "block"
    → Claude schrijft handover document
      → PostToolUse detecteert ROTATION-HANDOVER
        → nohup vnx_rotate.sh (tmux /clear + inject)
          → SessionStart recovery injecteert handover
            → Terminal hervat automatisch
```

Alle andere projecten vereisen minimaal 1 handmatige stap.

### 3. Multi-terminal concurrency met atomic locking

mkdir-based atomic locks met timestamp stale detection (TTL=300s). Voorkomt race conditions bij gelijktijdige rotation in T1/T2/T3. Geen ander project heeft dit — de agent_farm doet broadcast clearing zonder per-agent coördinatie.

### 4. Fallback chain architectuur

SessionStart recovery met `--fallback` parameter zodat T2/T3 worker bootstrap niet verstoord wordt door rotation recovery. Graceful degradatie als handover niet gevonden wordt.

---

## Risico's en Kanttekeningen

| Risico | Bron | Mitigatie in VNX |
|--------|------|-----------------|
| Stop hook `decision: "block"` wordt genegeerd | [#3656](https://github.com/anthropics/claude-code/issues/3656) | Monitoring + fallback detectie |
| /clear breekt transcript voor Stop hooks | [#3046](https://github.com/anthropics/claude-code/issues/3046) | Rotation recovery is SessionStart-based, niet Stop-based |
| Anthropic biedt geen native /clear from hooks | [#9118](https://github.com/anthropics/claude-code/issues/9118) (NOT_PLANNED) | tmux send-keys omzeiling |
| Statusline remaining_pct reset na /clear | [#13765](https://github.com/anthropics/claude-code/issues/13765), [#16189](https://github.com/anthropics/claude-code/issues/16189) | VNX reset context_window.json in recovery |

---

## Token Tracking Landschap (Bonus)

Gerelateerd maar apart domein — token *kosten* tracking vs context *lifecycle* management:

| Tool | Wat het doet | Relatie tot VNX |
|------|-------------|-----------------|
| [toktrack](https://github.com/mag123c/toktrack) | SIMD-fast cost/usage dashboard over Claude/Codex/Gemini/OpenCode sessions | Complementair — kan post-rotation sessiekosten tracken |
| Claude Code native | `usage.input_tokens` / `output_tokens` / `cache_*` in session JSONL | Data is er al in `~/.claude/projects/` |
| VNX receipts | `context_rotation` event met `context_used_pct` | Track wanneer rotations plaatsvinden |

VNX heeft al token data van alle 3 CLIs (Claude: 1020 sessies/854MB, Codex: 153 sessies/431MB, Gemini: 2947 bestanden) maar geen aggregatie dashboard. toktrack zou daar bovenop kunnen als viewer.

---

## Live Validatie Evidence (VNX T1, 2026-02-23)

Er is inmiddels een **live T1-validatie** uitgevoerd (niet alleen isolated tests). Daarbij is een test `ROTATION-HANDOVER` bestand bewust geschreven vanuit T1 om de PostToolUse detector te triggeren.

### Wat live bewezen is

- `PostToolUse` detector detecteert handover write in T1
- lock acquire + async rotator launch werkt
- `vnx_rotate.sh` resolved correct T1 pane (`%1`)
- tmux `/clear` wordt automatisch verstuurd naar T1
- continuation prompt wordt automatisch gepaste na `/clear`
- `context_rotation` receipt wordt geschreven (informatief, geen T0 actie)

### Live evidence bundle

Bundelpad:

- `.claude/vnx-system/docs/intelligence/evidence/context-rotation-live-20260223-163501/`

Inhoud:

- `README.md` — scenario + resultaat
- `hook_events.snippet.log` — detector logs (detectie / lock / rotator start)
- `vnx_rotate_T1.snippet.log` — pane target + `/clear` + continuation prompt + cleanup
- `t0_receipt_context_rotation.snippet.ndjson` — receipt snapshot
- `handover_test_T1.md` — test handover document
- `evidence_summary.md` — samenvatting + resterende timing-tuning item

### Opmerking (timing/UX)

De core flow werkt live, maar er is een **klein timing-detail** gezien rond de Enter/paste timing direct na `/clear`. Dit lijkt een tmux/UI readiness race en is oplosbaar met extra settle delay / readiness check. Dit is geen architectuurblokker.

---

## Marktpotentieel (Praktische Inschatting)

**Ja — hier kun je waarschijnlijk punten mee scoren**, mits je het goed positioneert.

Waarom dit tractiepotentieel heeft:

- Het probleem is echt en zichtbaar: lange Claude/Codex sessies compacten of degraderen
- Er zijn meerdere community-oplossingen, maar vooral handmatig / half-automatisch
- Anthropic biedt geen native hook-driven `/clear` route (NOT_PLANNED), dus workaround-engineering is waardevol
- Teams met multi-agent/tmux setups voelen dit probleem het hardst en zoeken pragmatische oplossingen

Waar je op kunt scoren:

1. **Thought leadership**
- Laat zien dat je niet alleen een idee hebt, maar een werkende closed-loop pipeline met evidence.

2. **Operational credibility**
- Je hebt hooks, locking, receipts, fallback chains, testplan en live bewijs.
- Dat onderscheidt je van “scripts and vibes”.

3. **Open-source niche leadership**
- Zelfs als de doelgroep klein is, is die doelgroep invloedrijk (power users / infra-minded AI devs).

### Hoe positioneren (sterkste angle)

Niet claimen:
- “We solved AI memory”

Wel claimen:
- “We built a production-minded context rotation control plane for long-running Claude Code workflows”
- “Closed-loop context rotation with audit trail, fallback recovery, and tmux automation”
- “Opt-in, evidence-backed, multi-terminal aware”

### Wat je geloofwaardigheid verder verhoogt

- Public demo gif/video van live T1 rotation (handover write -> `/clear` -> resume)
- Korte postmortem/engineering write-up over de timing race en fix
- Benchmarks / usage stats later (hoe vaak rotations, success rate, median resume time)
