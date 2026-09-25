# Subagents en receipts: zichtbaar is nog niet geborgd

Datum: 2026-09-24. Type: read-only onderzoek, operatorvraag 24-09. Dispatch-ID: `20260924-subagent-observability-onderzoek`.
Gemeten op: `claude --version` = 2.1.281 (macOS). Docs en changelog opgehaald op 2026-09-24 van code.claude.com.
Markeringen in dit rapport: **Afgeleid** is mijn conclusie uit meetdata. **Oordeel** is een keuze die de operator kan overrulen. **Onbepaald** is niet vastgesteld, met de meting die het zou beslissen.

## Antwoord

1. **De zwarte doos bestaat niet meer.** Sinds 15-11-2025 (2.0.42) geeft `SubagentStop` een agent-id en een transcriptpad. Sinds 05-03-2026 (2.1.69) draagt elke hook binnen een subagent `agent_id` en `agent_type`. Op deze machine heeft elk van de 70 gemeten subagents een eigen transcript met model, tokens per API-call, alle tool-calls en de eindtekst.
2. **Zichtbaar is niet geborgd.** Van dezelfde 70 subagents pushten er 67 code en gebruikten er 55 `gh pr`. Geen van de 70 heeft een dispatch-id, een rapport in `unified_reports`, een receipt of een dispatch-register-boeking. Er hing geen `SubagentStart`- of `SubagentStop`-hook. Wat ontbreekt is identiteit, isolatie en een afdwingpunt. Zichtbaarheid ontbreekt niet.
3. **Oordeel: optie (b), in smalle vorm en na een meetspike.** Optie (a) blijft van kracht voor alles wat commit, pusht of een PR opent. Optie (c) wordt nu afgeraden.
4. **De bestaande guards zijn het zwakste punt.** Twee guards die subagents moesten tegenhouden hebben in de meting niets tegengehouden (35 van 35 en 70 van 70 aanroepen kwamen door). Elke variant van (a) of (b) begint met een guard waarvan de werking is aangetoond.

## 1. Wat Claude Code biedt

Bron per regel: docs-pagina en changelog. Versies en datums komen uit de changelog (https://code.claude.com/docs/en/changelog).

| Mechanisme | Sinds | Wat erin staat | Bron |
|---|---|---|---|
| `SubagentStop` hook | 1.0.41 (03-07-2025) | `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message`, `stop_hook_active`, `background_tasks`, `session_crons`. Kan blokkeren: exit 2 of `decision: "block"` houdt de subagent draaiende. Geen `model`- of tokenveld in de payload | hooks#subagentstop; changelog 2.0.42 (`agent_id`, `agent_transcript_path`), 2.1.47 (`last_assistant_message`), 2.1.145 (`background_tasks`) |
| `SubagentStart` hook | 2.0.43 (18-11-2025) | `agent_id`, `agent_type`. Kan de spawn niet blokkeren. Kan wel context in de subagent injecteren | hooks#subagentstart |
| Agent-identiteit op elk hook-event | 2.1.69 (05-03-2026) | `agent_id` (alleen binnen een subagent), `agent_type` | hooks#common-input-fields; changelog 2.1.69 |
| Tool-hooks binnen subagents | docs, geen versie genoemd | Hooks uit settings, managed policy en plugins draaien ook binnen subagents. `PreToolUse` en `PostToolUse` dragen daar `agent_id` | hooks (hook locations) |
| Eigen transcript per subagent | docs, geen versie genoemd | `~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl`. Wordt na `cleanupPeriodDays` (standaard 30 dagen) verwijderd | sub-agents#resume-subagents |
| `PostToolUse` op de Agent-tool | 2.1.212 voor `modelsUsed` | `tool_response`: `status`, `agentId`, `content`, `resolvedModel`, `modelsUsed`, `totalTokens`, `totalDurationMs`, `totalToolUseCount`, `usage`. `totalTokens` en `usage` gelden alleen voor het laatste API-verzoek | hooks#agent |
| Achtergrond als standaard | 2.1.198 (docs) | Een achtergrondsubagent geeft alleen `status: "async_launched"`, `agentId`, `outputFile`, `resolvedModel` terug. Geen usage. Changelog 2.1.232 (13-08-2026) noemt hetzelfde voor interactieve sessies | hooks#agent; changelog 2.1.232 |
| `SubagentHandback` | 2.1.271 (docs), alleen in auto mode | De subagent levert zijn rapport via deze tool. De tekst staat in `tool_input.message`, niet in `last_assistant_message` | hooks#subagentstop |
| `isolation: worktree` | 2.1.49 (19-02-2026), frontmatter 2.1.50 | Tijdelijke worktree, standaard vanaf de default branch. Wordt automatisch opgeruimd als de subagent niets wijzigt. Git-commando's naar de hoofdcheckout worden geweigerd sinds 2.1.203 en 2.1.210. `worktree.baseRef` sinds 2.1.133 | sub-agents (frontmatter reference); changelog 2.1.49, 2.1.50, 2.1.133, 2.1.210 |
| OpenTelemetry-spans | 2.1.139 (11-05-2026) en 2.1.145 | `claude_code.llm_request` en `claude_code.tool` dragen `agent_id` en `parent_agent_id`. Subagent-spans nesten onder de Agent-tool-span. `tool_use_id` koppelt aan hook-payloads. Tracing is beta (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`) | monitoring-usage#traces-beta; changelog 2.1.139, 2.1.145 |
| OpenTelemetry-metrics en -events | n.v.t. | Tellers en `api_request`-events dragen `query_source` (`main`, `subagent`, `auxiliary`), `agent.name`, tokens en `cost_usd`. Zelfgekozen agentnamen worden `custom`. In de docs staat `agent_id` alleen bij spans | monitoring-usage#cost-counter, #api-request-event |
| Headless stream | n.v.t. | `claude -p --output-format stream-json`: subagentberichten dragen `parent_tool_use_id`. Subagenttekst alleen met `--forward-subagent-text`. Hook-events met `--include-hook-events`. `--bare` slaat hooks en subagents over | headless (follow subagent messages); cli-reference |

Twee dingen uit de docs die de receipt-vraag raken:
- Het model zit niet in de `SubagentStop`-payload. Het staat in het transcript en in `resolvedModel` van de `PostToolUse` op de Agent-tool.
- `SubagentStop` vuurt ook voor interne agents van Claude Code (promptsuggesties, `/btw`). `agent_type` is dan leeg of de sessie-agent (hooks#subagentstop).

## 2. Wat er op deze machine werkelijk gebeurt

### 2.1 Opzet

Sessie `64f47ab0-841c-4683-b670-a5ff47fc3ca3`, SEOcrawler_v2 T0. Transcript: `~/.claude/projects/-Users-vincentvandeth-Development-SEOcrawler-v2--claude-terminals-T0/64f47ab0-841c-4683-b670-a5ff47fc3ca3.jsonl`, 14.218 regels, eerste record 2026-09-14T16:45:42Z, laatste 2026-09-23T15:54:02Z. Het is dus één sessie over negen dagen, niet over 23-09 alleen.

Ik tel 70 tool-calls met naam `Agent` (66 `developer`, 4 `technical-writer`), naast 82 `SendMessage` en 4 `TaskStop`. De opdracht noemt 73. Waar die 73 vandaan komt is **Onbepaald**. De subagentmap bevat 70 transcripts en 70 `meta.json` (140 bestanden), dus 70 is intern consistent.

Alles is gelezen, niets is uitgevoerd.

### 2.2 Waar staat wat

| Veld | Terug te vinden? | Waar | Bewijs |
|---|---|---|---|
| Model | Ja | Startrecord `resolvedModel` (70 van 70 `claude-sonnet-5`). Per API-bericht `message.model` in het subagenttranscript | hoofdtranscript regel 249; `subagents/agent-a06bc9999717ea6d7.jsonl` regel 9 |
| Tokens | Ja, per API-bericht | `message.usage` (input, output, cache_creation, cache_read) in het subagenttranscript. Notificatie `<subagent_tokens>` alleen voor het laatste verzoek | zie 2.3 |
| Tool-calls | Ja | `tool_use`-blokken in het subagenttranscript. 8.750 in totaal: Bash 5.871, Read 1.538, Edit 1.236, SubagentHandback 74, Write 30, MCP 1 | `subagents/agent-a15f1b521de45517b.jsonl` (124 calls, notificatie zegt 124) |
| Uitkomst | Ja, als lopende tekst | `<result>` in een `queue-operation`-record van het hoofdtranscript. Ook `SubagentHandback.message` in het subagenttranscript (61 van 70 agents) | hoofdtranscript regel 256; `agent-a041e79e9667629d5.jsonl` regel 923 |
| Status | Ja | `<status>` in de notificatie: 125 completed, 12 failed, 1 killed, 1 stopped (139 notificaties) | hoofdtranscript regel 256 e.v. |
| Werkmap en branch | **Nee** | Alle 45.930 records van de 70 subagents dragen `cwd` = `.../SEOcrawler_v2/.claude/terminals/T0` en `gitBranch` = `main` | `agent-a06bc9999717ea6d7.jsonl` regel 1 |
| Isolatie | Alleen als instructie | `isolation` stond in 0 van 70 aanroepen. 69 van 70 agents noemen een `-wt-`-worktreepad in hun Bash-commando's, omdat de prompt dat eiste | hoofdtranscript regel 187 (`input` zonder `isolation`) |
| Dispatch-id, rapportbestand, receipt | **Nee** | Er is geen veld en geen hook die het schrijft | zie 2.5 |
| Werkelijk gedraaide hooks | Ja | 5.864 `hook_success`-attachments voor `PreToolUse:Bash` (`activate_venv.sh`) binnen subagent-transcripts. Bewijs dat hooks binnen subagents vuren | `agent-a041e79e9667629d5.jsonl` regel 12 |
| Account | **Nee** | Niet in transcripts. Zie 2.6 | n.v.t. |

`meta.json` is klein: `agentType`, `description`, `toolUseId`, `spawnDepth`, `requestShape`, `requestNonInteractive`. Voorbeeld: `subagents/agent-a0f31dfaf4b6dba6c.meta.json`. Geen model, geen tokens. Wel `toolUseId`: dat is de sleutel naar de Agent-aanroep in het hoofdtranscript en naar OTel-spans en hook-payloads.

Een detail voor een receipt-bouwer: 12 assistantberichten dragen model `<synthetic>`. Een receipt dat het model uit het laatste bericht neemt kan daarop stranden. Neem het model uit het laatste niet-synthetische bericht.

### 2.3 De notificatie is geen kostenmaat

De notificatie meldt `subagent_tokens` en `tool_uses` en `duration_ms`, alleen bij 125 van 139 (de completed). De docs zeggen dat `totalTokens` het laatste verzoek betreft (hooks#agent). Gemeten voor agent `a15f1b521de45517b`:

| | Notificatie | Transcript |
|---|---|---|
| Tokens | 280.818 | laatste verzoek: 279.718 (verschil 0,4 procent) |
| Hele run | niet gemeld | 221 API-berichten, cache-read 39.376.434, cache-creation 586.314, output 87.097, input 442 |

Over alle 70 agents: notificatiesom 38.698.015 tokens. Transcriptsom: 15.852 API-berichten, input 31.704, output 6.031.415, cache-creation 96.162.012, cache-read 4.712.833.015. De notificatie ziet ongeveer 1 procent van wat het transcript telt. Wie kosten of verbruik in een receipt wil, sommeert `message.usage` uit het transcript. **Afgeleid:** de notificatie of de `tool_response` is daarvoor onbruikbaar.

### 2.4 Wat de subagents deden

67 van 70 voerden `git push` uit, 63 `git commit`, 55 `gh pr`. Het waren dus bouw- en merge-opdrachten (`developer`), geen onderzoek. **Afgeleid:** de vraag "mag een subagent een receipt leveren" is in deze sessie een vraag over governed bouwwerk geweest, niet over read-only onderzoek.

De uitkomst is lopende tekst. In de laatste `SubagentHandback` van de 61 agents noemen er 41 een volledige commit-sha en 57 een PR-nummer. Een schema is er niet. Een receipt moet zijn feiten dus uit het transcript halen (Bash-invoer en -uitvoer), niet uit het rapport.

Eén agent kreeg 18 completion-notificaties. Een `SendMessage` hervat een afgeronde agent onder hetzelfde `agentId` (sub-agents#resume-subagents), dus één agent is niet één run. Verdeling: 58 agents 1 notificatie, 4 agents 2, 3 agents 4, en de rest 6 tot 18.

### 2.5 Wat er niet was

In de settings die deze sessie laadde (`~/Development/SEOcrawler_v2/.claude/settings.json`, `settings.local.json`, `terminals/T0/.claude/settings.json`) staat geen `SubagentStart` of `SubagentStop`. Er is dus niets dat een receipt, register-event of rapport kon schrijven. Dat geldt ook voor `~/.claude/settings.json`, `~/.claude-salesminds/settings.json` en de VNX-repo (`rg 'SubagentStop|SubagentStart'` over het repo, zonder `claudedocs/` en `.vnx-data/`: 0 treffers).

Er staat in dat transcript een `PreToolUse`-hook `activate_venv.sh` (matcher `Bash`) met 5.864 records tegenover 5.871 Bash-calls. De hook `t0-readonly-enforcer.sh` (matcher `*`) heeft geen eigen `hook_success`-record. Waarom niet is **Onbepaald** (mogelijk schrijft Claude Code geen record bij lege stdout). Meting: een wegwerp-hook die altijd iets print en de payload logt.

### 2.6 Bijzaak: het account is niet vast te stellen

De `.output`-bestanden onder `/private/tmp/claude-501/.../tasks/` zijn symlinks naar het subagenttranscript. 7 van de 71 links voor deze 70 agents lopen via `~/.claude-salesminds/projects/...` en 64 via `~/.claude/projects/...`. Beide paden komen op dezelfde map uit, want `~/.claude-salesminds/projects` is een symlink naar `~/.claude/projects`. Dat zegt dat een deel van de agents onder `CLAUDE_CONFIG_DIR=~/.claude-salesminds` is gestart. Welk account dat was is **Onbepaald**: de regel `claude-accounts.md` zegt dat alleen `/status` uit die sessie het weet. Voor het kostenlabel: een subagent-record draagt geen accountidentiteit.

### 2.7 De guards vingen niets

| Guard | Waar | Uitkomst in de meting |
|---|---|---|
| `t0-readonly-enforcer.sh`, PreToolUse matcher `*` | SEOcrawler_v2 `.claude/settings.json` | 70 van 70 Agent-aanroepen kwamen door (`status: async_launched`). Volgens de docstring van `scripts/hooks/pretooluse_t0_no_subagents.py` gebruikt die hook exit 1, en dat blokkeert niet. De docs bevestigen dat: alleen exit 2 of `permissionDecision: "deny"` blokkeert (hooks#exit-code-output) |
| `pretooluse_block_subagent.sh`, matcher `Task` | vnx-orchestration `.claude/settings.json` | In de VNX-T0-transcripts (26-08 t/m 06-09): 35 Agent-aanroepen, 34 `async_launched`, 1 API-fout, **0 geblokkeerd**. De hook let op `tool_name == "Task"`. De tool heet in de docs `Agent`. **Afgeleid:** de matcher raakte de tool nooit. **Onbepaald** of die sessies dit settings-bestand laadden. Meting: payload-log in een scratch-T0 |

Dit sluit aan bij de docstring van #1907 (448 `Agent`-calls en 0 `Task` in 143 T0-transcripts) en bij de merge van vandaag (`c7ca8d81`, PreToolUse-hook die `Agent` en `Task` weigert in T0 met `permissionDecision`). Die nieuwe guard is nog niet in een echte T0 gemeten. Ik heb dat niet kunnen doen zonder een sessie te starten.

## 3. Vergelijking met de headless lane

Kolom "Headless" verwijst naar code en `docs/core/DISPATCH_RULES.md` in deze worktree (main `6503ef6d`).

| Eigenschap | Headless lane | Subagent nu | Mechanisme | Wat ontbreekt |
|---|---|---|---|---|
| Eigen worktree | Elke dispatch krijgt een worktree (`dispatch_envelope.py:938`, `create_dispatch_worktree`), basis `origin/main` | Kan, met `isolation: worktree` (2.1.49). Tijdelijk, basis de default branch, opgeruimd zonder wijzigingen. Lokaal 0 van 70 gebruikten het | Frontmatter of aanroepparameter. Hooks `WorktreeCreate`/`WorktreeRemove` (2.1.50) | Een benoemde dispatch-branch. Het worktreepad in de hook-payload of het record: **Onbepaald**. Changelog noemt `worktreePath` en `worktreeBranch` in achtergrondnotificaties, lokaal niet gemeten want geen isolatie gebruikt. Meting: één wegwerp-run met `isolation` |
| Rapport volgens contract | Worker schrijft `unified_reports/<dispatch-id>.md` met vier koppen. Claude levert die zelf, 262 van 262 (DISPATCH_RULES §8) | Geen bestand. Wel `last_assistant_message` en `SubagentHandback.message` | `SubagentStop`-hook: valideer met `report_body_contract.validate_body()`, blokkeer met `decision: "block"` tot het rapport klopt | De hook moet het bestand schrijven en het dispatch-id kennen. Een lus-bewaking op `stop_hook_active` is nodig |
| Receipt met model en provider | Converter weigert zonder echt model (`validation.py:365`, `_validate_model_present`) | Model beschikbaar via `resolvedModel` en het transcript. Provider is altijd claude. Niet in de `SubagentStop`-payload | `PostToolUse` op Agent (`resolvedModel`, `modelsUsed`) plus transcript via `agent_transcript_path` | Model en tokens moeten uit het transcript worden gelezen. Idempotentiesleutel per `(agent_id, run)` want hervatten geeft meer stops |
| Review-gate op een PR | Gate-requests, resultaten en rapport op de PR (DISPATCH_RULES §2, regel 36). `_enforce_push_pr` dwingt push en PR af (`dispatch_envelope.py:128`) | Een subagent kan zelf pushen en een PR openen (55 van 70). De gate draait daarna gewoon | Bestaande gates op de PR | De sleutel. Zonder dispatch-id kan de gate geen request aan het werk koppelen. De push+PR-afdwinging na afloop bestaat niet. `SubagentStop` blokkeren kan in-band, maar een subagent die niet meewerkt of stopt door een limiet laat niets achter |
| Dispatch-register-boeking | De deur boekt (`dispatch_register.ndjson`, best-effort, DISPATCH_RULES §2.1 regel 62) | Niets | Een hook kan een event schrijven | Het dispatch-id moet uit de aanroep komen. Zie de opzet in (b) |
| Kostenlabel (subscription) | `cost=$0.0000` bevestigt de subscription-lane (DISPATCH_RULES §6 regel 128, §7 regel 139) | Erft de auth van de ouder. OTel geeft `cost_usd` als schatting per `api_request` met `query_source=subagent`. Tokens uit het transcript | OTel of transcriptsom | Een label per receipt. Het account is niet in het record te vinden (2.6) |
| Sessielimiet en serialisatie | `serialize_lane`, N-slot lock voor claude (DISPATCH_RULES §6) | Geen serialisatie. Subagents delen de sessielimiet van de ouder. 12 van 139 notificaties zijn `failed` met "You've hit your session limit". Bij 7 daarvan staat de laatste modeltekst als `<result>` (een halve zin midden in het werk), bij 5 ontbreekt `<result>` | Geen | Een subagent-spawn omzeilt de lock. Bij een limiet eindigt de run zonder rapport volgens het contract |
| Rechten | Scoped permissions per rol (DISPATCH_RULES regel 101) | Erft de modus van de ouder. Onder auto, acceptEdits en bypass wint die modus van `permissionMode` in de agentdefinitie. MCP-tools worden geërfd (sub-agents#available-tools) | `tools` en `disallowedTools` in de agentdefinitie | Een allowlist die MCP-tools uitsluit. De ouder gebruikte `mcp__supabase__execute_sql` 39 keer |
| Bewaartermijn | Receipts en rapporten in `.vnx-data` | Transcripts worden na 30 dagen gewist | `cleanupPeriodDays` verhogen, of het transcript bij `SubagentStop` kopiëren en hashen | Een receipt dat alleen naar het transcriptpad wijst verweest na 30 dagen |

## 4. Oordeel in drie opties

**Regels die hier gelden.** `no-anthropic-sdk`: subagents zijn een CLI-mechanisme. De opties hieronder hebben de SDK niet nodig en mogen hem niet gebruiken. De SDK-docs vragen bovendien een API-key en sluiten claude.ai-login voor derden uit (https://code.claude.com/docs/en/agent-sdk/overview). "LLM stelt voor, policy beslist": de ouder stelt een spawn voor, een deterministische hook beslist. Het receipt komt uit transcriptfeiten, niet uit wat het model over zichzelf schrijft.

### (a) Subagents blijven verboden voor governed werk

- **Bouwen:** niets nieuws voor de regel zelf. Wel de guard aantonen (zie aanbeveling 1).
- **Risico:** de regel bestaat op papier en niet in de praktijk. Twee guards faalden stil (2.7), en 105 aanroepen liepen erdoor. De schade is gemeten: 67 pushes zonder dispatch-id in één sessie.
- **Oordeel:** juist en nodig voor alles wat commit, pusht of een PR opent. Niet houdbaar zonder meting dat de deny werkt.

### (b) Subagents mogen voor read-only onderzoek, receipt via een hook

Twee vormen. **Afgeleid:** vorm b1 is veel veiliger dan b2.
- **b1: subagents binnen een governed dispatch.** De ouder is een headless worker met een dispatch-id. De subagent-runs worden een onderdeel van dat receipt (aantal, agent-ids, tokens, transcript-hash). De hook erft het dispatch-id uit de omgeving van de ouder. **Onbepaald:** of `VNX_DISPATCH_ID` of een gelijkwaardige variabele in de hook-omgeving staat. Meting: `env` loggen in een scratch-hook.
- **b2: subagents in een ongovernede sessie (T0, interactief).** Geen ouder-dispatch. Elke run heeft een eigen dispatch-id nodig en de T0-guard (#1907) moet een uitzondering krijgen.

Wat te bouwen, in PR's van 150 tot 300 regels:
1. **PR 1, meetspike.** Wegwerp-hooks die de payloads van `PreToolUse(Agent)`, `SubagentStart`, `SubagentStop` en `PostToolUse(Agent)` loggen. Beslist de **Onbepaald**-punten 1, 2, 3, 5 en 6 in sectie 5. Geen productiegedrag.
2. **PR 2, `SubagentStop` naar receipt.** Leest `agent_transcript_path`. Haalt model, som van `usage`, aantal tool-calls en tijden uit het transcript. Kopieert en hasht het transcript. Valideert het rapport met `validate_body()` en blokkeert met `decision: "block"` (maximaal twee keer, bewaakt met `stop_hook_active`). Schrijft `unified_reports/<dispatch-id>.md` met Model en Provider en laat de bestaande converter het receipt maken.
3. **PR 3, `PreToolUse(Agent)` als policy.** Staat alleen agenttypes toe uit een allowlist, met `tools` zonder Bash, Edit, Write en zonder MCP-schrijftools. Weigert als de prompt geen geldig dispatch-id draagt. Gebruikt de deny-vorm `permissionDecision: "deny"`, niet exit 1.

**Risico's:**
- Dekking: een hook werkt alleen in sessies die de settings laden. `claude -p --bare` slaat hooks over. De envelope- en subprocess-dispatch gebruiken `--bare` niet (`rg -e '--bare'` over `dispatch_envelope.py` en `subprocess_dispatch.py`: 0 treffers).
- Falen zonder stop: 12 runs eindigden door de sessielimiet. Of `SubagentStop` dan vuurt is **Onbepaald** (punt 2). Meting: een subagent forceren te stoppen met `TaskStop`, en een met een afgekapte sessie.
- Meer stops per agent: 18 notificaties bij één agent. Een receipt per stop dupliceert, dus de sleutel is `(agent_id, run)`.
- Interne agents: `SubagentStop` vuurt ook voor promptsuggesties en `/btw` met lege `agent_type`. Filter erop.
- Read-only afdwingen: MCP-tools worden geërfd en een background-subagent behoudt alle MCP-tools. De allowlist moet ze expliciet weren.
- Rapporttekst is model-proza. Gebruik hem als claim en toets de feiten (sha, PR) tegen Bash-uitvoer.
- Bewaartermijn: het transcript moet buiten de 30-dagen-sweep worden gezet.

**Oordeel:** dit is de goede richting voor onderzoek. Begin met b1. Bouw PR 1 eerst en beslis PR 2 en PR 3 op de uitkomst.

### (c) Subagents vervangen de headless lane

- **Bouwen:** alles uit (b), plus een benoemde dispatch-branch per subagent (via `isolation: worktree` en `WorktreeCreate`), een push+PR-afdwinging na afloop, serialisatie tegen de sessielimiet, scoped permissions per rol, een kostenlabel per receipt, een register-boeking, een herstelpad voor dode runs en een bewaarbeleid voor transcripts.
- **Risico:** de ouder is één punt van uitval. Een sessielimiet beëindigde 12 van 139 runs zonder rapport volgens het contract. De ouder-LLM kiest ook zelf of en wat hij spawnt, dus "policy beslist" hangt volledig aan één PreToolUse-guard, en die faalde in 2.7. De headless lane dwingt push en PR af *na* de worker en buiten diens medewerking om (`dispatch_envelope.py:128`). Voor een subagent bestaat dat afdwingpunt niet.
- **Oordeel:** nu afgeraden. Terugkomen als sectie 5 punten 1 tot 4 positief zijn gemeten.

## 5. Onbepaald, met de meting die het beslist

| # | Vraag | Waarom het telt | Meting |
|---|---|---|---|
| 1 | Vangt matcher `Task` de tool `Agent`? Lokaal: 0 van 35 | Bepaalt of de oude guard ooit werkte | Scratch-T0 met een hook die `tool_name` logt, één Agent-aanroep |
| 2 | Vuurt `SubagentStop` bij `failed` (sessielimiet), `killed` en `stopped`? | Een receipt op falen bestaat alleen als het event vuurt | Scratch-project met een log-hook, `TaskStop` op een draaiende subagent |
| 3 | Draagt een `isolation: worktree`-run een `worktreePath` en branch naar hook of notificatie? Lokaal 0 van 70 gebruikten het | Isolatie zonder pad in het record is niet te auditen | Eén wegwerp-run met `isolation: worktree` en het record lezen |
| 4 | Dragen OTel-logevents `agent_id`, of alleen spans? Docs noemen het bij spans | Bepaalt of kosten per subagent uit OTel te halen zijn zonder transcript | OTLP naar een lokale console-exporter, één subagent |
| 5 | Staat `VNX_DISPATCH_ID` of een gelijkwaardige variabele in de hook-omgeving van een subagent? | Sleutel van b1 | `env` loggen in een scratch-hook binnen een headless dispatch |
| 6 | Waarom geeft `t0-readonly-enforcer.sh` (matcher `*`) geen `hook_success`-record? | Onduidelijk of de hook wel liep | Wegwerp-hook met altijd stdout |
| 7 | Welk account draaide de agents met een `.claude-salesminds`-link? | Kostenlabel en accountregels | `/status` uit die sessie. Uit de transcripts is het niet af te leiden |
| 8 | Waar komen de 73 Agent-aanroepen uit de opdracht vandaan? Ik tel 70 | Vergelijkbaarheid van de meting | Vraag aan de operator welke teller dat was |
| 9 | Wanneer werd `SubagentHandback` in de changelog geïntroduceerd? De hooks-docs noemen 2.1.271, de changelog-pagina noemt de tool niet | Datumnauwkeurigheid van sectie 1 | Changelog op GitHub doorzoeken (niet gedaan) |

## 6. Aanbeveling

1. **Bewijs eerst de guard.** Draai PR 1 uit (b) als meetspike, punten 1, 2, 3, 5 en 6. Zet daarna een wekelijkse canary in een scratch-T0: één `Agent`-aanroep die geweigerd moet worden. Een guard die niet wordt getoetst faalt stil, zoals twee keer is gebeurd.
2. **Houd (a) voor alles wat commit, pusht of een PR opent.** De sessie liet 67 pushes zonder dispatch-id zien.
3. **Bouw (b1) na de spike**, in de volgorde PR 2 dan PR 3. Begin binnen een governed dispatch. Een ongovernede sessie (b2) pas als b1 in gebruik is.
4. **Bouw (c) niet.** De headless lane levert twee dingen die een subagent niet heeft: een worktree en push+PR-afdwinging die buiten de worker om lopen, en een lock tegen de sessielimiet.
5. **Zet `cleanupPeriodDays` op een bewuste waarde** of kopieer transcripts bij `SubagentStop`, zodat een receipt na 30 dagen nog naar iets wijst.

Gebruik de docs-versie van 24-09 als peildatum. Hooks en OTel veranderen snel: 2.1.268 en 2.1.271 (september 2026) voegden nog velden en een tool toe.

## 7. Bronnen

Docs en changelog, opgehaald 2026-09-24:
- https://code.claude.com/docs/en/hooks (secties: SubagentStart, SubagentStop, common input fields, Agent, exit-code output)
- https://code.claude.com/docs/en/sub-agents (isolation, resume-subagents, available tools, permission modes)
- https://code.claude.com/docs/en/monitoring-usage (traces-beta, cost counter, api request event)
- https://code.claude.com/docs/en/changelog (1.0.41, 2.0.42, 2.0.43, 2.1.47, 2.1.49, 2.1.50, 2.1.69, 2.1.133, 2.1.139, 2.1.145, 2.1.210, 2.1.232, 2.1.275)
- https://code.claude.com/docs/en/headless en https://code.claude.com/docs/en/cli-reference
- https://code.claude.com/docs/en/agent-sdk/overview en /agent-sdk/subagents

Lokaal, alleen gelezen:
- `~/.claude/projects/-Users-vincentvandeth-Development-SEOcrawler-v2--claude-terminals-T0/64f47ab0-841c-4683-b670-a5ff47fc3ca3.jsonl` en de map `64f47ab0-.../subagents/` (70 transcripts, 70 meta-bestanden)
- `/private/tmp/claude-501/-Users-vincentvandeth-Development-SEOcrawler-v2--claude-terminals-T0/*/tasks/*.output` (symlinks)
- Settings: `~/.claude/settings.json`, `~/.claude-salesminds/settings.json`, `~/Development/SEOcrawler_v2/.claude/settings.json`, `settings.local.json`, `terminals/T0/.claude/settings.json`, `.claude/settings.json` in vnx-orchestration
- VNX-code in deze worktree: `scripts/hooks/pretooluse_block_subagent.sh`, `scripts/hooks/pretooluse_subagent_guard.py`, `scripts/hooks/pretooluse_t0_no_subagents.py`, `scripts/lib/dispatch_envelope.py`, `scripts/lib/append_receipt_internals/validation.py`, `docs/core/DISPATCH_RULES.md`
- `git show c7ca8d81` (#1907)

Methode: één Python-telling per claim over de JSONL-bestanden, geen uitvoering van sessiecode. Ik heb per ongeluk één `git log` en één `sed` op de hoofdcheckout van de VNX-repo gedaan (read-only). Daarna heb ik uitsluitend in de worktree gelezen.

## VNX Report
- research_question: Zijn Claude Code-subagents observeerbaar en auditeerbaar genoeg voor een receipt, of blijft de headless lane nodig?
- scope: Claude Code 2.1.281 docs en changelog van 2026-09-24, lokale transcripts van SEOcrawler_v2 T0 sessie 64f47ab0, VNX-code in main 6503ef6d. Read-only.
- depth: deep
- source_count: 14
- uncertainty_flags: 9
- quality_self_assessment: De docs, de changelog en 70 lokale subagent-transcripts zijn samen gemeten en de tellingen zijn herhaalbaar. Negen punten staan als Onbepaald (waaronder SubagentStop bij falen, het worktreepad, het dispatch-id in de hook-omgeving en de werking van de nieuwe guard) omdat ik geen sessie of hook mocht draaien. Vertrouwen in het oordeel is hoog voor "niet vervangen" en middelhoog voor de vorm van (b).
- open_items: [meetspike PR 1, guard-canary, operatorvraag over de 73 aanroepen]
