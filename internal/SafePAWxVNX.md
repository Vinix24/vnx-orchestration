# SafePaw x VNX: Collaboration Framework

**Datum**: 2026-03-08
**Van**: Vincent van Deth (VNX Digital)
**Doel**: Verkennen van samenwerking tussen SafePaw en VNX orchestratie
**Status**: Voorstel / discussiedocument

---

## Context: Wie zijn wij

### VNX Digital (Vincent)

Ik bouw **VNX**: een multi-agent orchestratiesysteem voor AI-gedreven softwareontwikkeling. Het systeem coördineert Claude Code, Codex CLI en Gemini CLI over parallelle terminals met:

- **Dispatch queue** met human-in-the-loop goedkeuring
- **Intelligence layer** die leert van historische patronen (welk model werkt voor welke taak, welke tag-combinaties voorspellen failures)
- **Append-only receipt ledger** (NDJSON) als audit trail met Composite Quality Scores
- **LLM benchmark framework** dat 8+ lokale modellen (Ollama) en Claude API systematisch vergelijkt op identieke taken
- **Nachtelijke intelligence extraction** met conversation analysis en model routing hints

**Stack**: Python, Bash, SQLite, tmux, Ollama
**Hardware**: Mac Mini M4, 24GB RAM
**Schaal**: 1466 dispatches verwerkt, 6 maanden dagelijks gebruik, 4 gelijktijdige terminals

**Volgende stap**: Overgang naar autonome nachtelijke runs (`--dangerously-skip-permissions`) waar T0 als orchestrator zonder menselijke tussenkomst doorontwikkelt.

### SafePaw (beautifulplanet)

Security gateway voor self-hosted AI assistants (OpenClaw). Go-based reverse proxy met:

- **10-layer middleware chain** (auth, rate limiting, brute-force, CORS)
- **Prompt injection scanner** (14 heuristische patronen)
- **Output scanner** (XSS, secret leakage, system prompt detection)
- **STRIDE threat model** (27 gedocumenteerde bedreigingen)
- **258+ tests** waarvan 7 fuzz targets
- **React setup wizard** voor configuratie

**Stack**: Go, React, Docker Compose, Redis, PostgreSQL

---

## Waarom samenwerken

Onze projecten overlappen niet op code-niveau (Go vs Python, web gateway vs lokaal orchestratiesysteem), maar **complementeren op kennisgebied**:

| Jij (SafePaw) hebt | Ik (VNX) heb |
|---------------------|-------------|
| Security engineering methodiek (STRIDE, threat modeling) | Multi-agent orchestratie ervaring (1466 dispatches) |
| Prompt injection detectie patronen (14 regex + fuzz targets) | LLM benchmark infra (8 modellen, 30+ runs, 60 uur compute) |
| Output scanning (XSS, secrets, system prompt leaks) | Intelligence/learning loop (patronen die falen voorspellen) |
| Defense-in-depth architectuur | Lokale LLM expertise (Ollama, model vergelijkingen) |
| Incident response playbooks (6 runbooks) | Publicatiekanaal (LinkedIn, blog, technisch publiek) |

**Samenwerking = sparringpartner, niet co-developer.** Kennisdeling, threat modeling, en eventueel gezamenlijke publicaties.

---

## Aanbevelingen voor Vincent (VNX)

### Prioriteit 1: Voordat je naar skip-permissions gaat

#### 1.1 MCP Server Scanning
Draai `mcp-scan` (Snyk/Invariant) op alle actieve MCP servers:
- Notion, GitHub, Supabase, Playwright, sequential-thinking, context7
- Bekende CVE's: Anthropic's eigen Git MCP server had 3 CVE's in januari 2026
- Tool: `npx @anthropic-ai/mcp-scan` of `pip install mcp-scan`

#### 1.2 Claude Code Hooks als vangnet
VNX heeft al een hook-systeem. Voeg security hooks toe:

**PreToolUse hook** -- blokkeer destructieve commands:
```bash
# Blokkeer: rm -rf /, git push --force main, curl|sh, wget|sh
# Blokkeer: bewerkingen buiten projectdirectory
# Blokkeer: .env, credentials, SSH key lezen
```

**PostToolUse hook** -- scan output op secrets:
```bash
# Detecteer: sk- (OpenAI), AKIA (AWS), private keys, tokens
# Detecteer: base64-encoded secrets
# Waarschuw maar blokkeer niet (log naar receipt ledger)
```

#### 1.3 Sandbox voor nachtelijke runs
- Gebruik `--sandbox` mode of draai in devcontainer
- Als een agent `rm -rf /` uitvoert, is het de container, niet de Mac
- Docker + `--network none` voor runs die geen internet nodig hebben

### Prioriteit 2: Na stabiele skip-permissions

#### 2.1 LlamaFirewall CodeShield
- **Python-native** (past direct in VNX stack)
- Scant AI-gegenereerde code op security issues voordat het gecommit wordt
- Integreer in receipt processor: elke code-output door CodeShield halen
- Ondersteunt Semgrep rules, 8 talen

#### 2.2 Lasso Security Hooks
- Specifiek gebouwd voor Claude Code
- 50+ detectiepatronen
- Integreert via Claude Code's hook systeem
- Waarschuwt in plaats van blokkeert (minder verstorend voor autonome runs)

#### 2.3 Dispatch-level risk scoring
Voeg aan de intelligence layer toe:
- Risk assessment per dispatch (bevat de instructie external content?)
- Automatic escalation: als een dispatch content van een MCP server bevat, verhoog risk level
- Bij hoog risico: forceer menselijke goedkeuring (ook in skip-permissions mode)

### Niet doen

- SafePaw integreren als code (Go vs Python mismatch, gebouwd voor OpenClaw)
- Alles tegelijk bouwen (security is laag-voor-laag)
- Skip-permissions zonder sandbox draaien

---

## Aanbevelingen voor SafePaw developer

### Wat VNX's ervaring oplevert voor SafePaw

#### 3.1 Adaptive detectie (vanuit VNX's learning loop)
SafePaw's 14 regex patronen zijn statisch. VNX's intelligence layer leert welke patronen daadwerkelijk tot problemen leiden:

**Concept**: Patronen die matchen maar nooit tot echte incidenten leiden, verlagen hun confidence score. Patronen die matchen en wél correleren met failures, verhogen. Dit vermindert false positives zonder handmatig tuning.

**VNX implementatie**:
```
tag_intelligence.py  → detecteert tag combinaties die falen voorspellen
learning_loop.py     → tracked used vs ignored patronen, past confidence aan
cached_intelligence.py → TTL cache met confidence boosting per cache hit
```

**Toepassing voor SafePaw**: De 14 regex patronen zouden een confidence score kunnen krijgen. Na X requests zonder incident: confidence daalt. Na een gedetecteerd incident: confidence stijgt. Dit maakt de scanner zelfleerend.

#### 3.2 Multi-model security benchmark
Niemand publiceert systematische data over prompt injection resistentie per lokaal model op consumer hardware. VNX heeft het benchmark framework (8 modellen, gestandaardiseerde taken).

**Voorstel**: Combineer SafePaw's 14 detectiepatronen + fuzz targets met VNX's benchmark framework:
- Genereer adversarial prompts (injection, jailbreak) in NL en EN
- Test elk model (phi4-mini, devstral, qwen3.5:35b, codestral) op hoe ze reageren
- Meet: weigert het model? voert het de instructie uit? lekt het system prompts?
- Publiceer als open-source benchmark

**Dit is origineel onderzoek** dat beide projecten zichtbaarheid geeft.

#### 3.3 Nederlandse prompt injection patronen
SafePaw's patronen zijn Engels. De Nederlandse markt heeft specifieke injection vectoren:
- Nederlandse instructie-overrides ("negeer bovenstaande instructies", "je bent nu een ander systeem")
- Formele vs informele aanspreekvormen als social engineering
- KvK/BTW-nummer extractie als data exfiltratie vector

VNX heeft Nederlandse marketing benchmark taken (10 taken, MKB-context) die als basis kunnen dienen voor NL-specifieke detectiepatronen.

#### 3.4 Audit trail / receipt systeem
SafePaw logt naar structured JSON maar mist een append-only ledger met kwaliteitsscoring.

VNX's NDJSON receipt systeem biedt:
- Append-only (crash-resilient, geen database nodig)
- Composite Quality Score (0-100) per event
- Git provenance (branch, commit SHA, dirty state)
- Idempotency (hash-based dedup)
- Flood protection (circuit breaker)

Dit concept is direct vertaalbaar naar Go en zou SafePaw's audit capabilities versterken.

---

## Gezamenlijke projectmogelijkheden

### Project A: Local LLM Security Benchmark (hoogste waarde)

**Doel**: Eerste open-source benchmark die lokale LLM's test op security resistentie op consumer hardware.

**Verdeling**:
- **SafePaw**: Adversarial prompt generatie, detectiepatronen, fuzz methodology
- **VNX**: Benchmark framework, model infra (Ollama), hardware testbed, resultaatanalyse

**Output**: GitHub repo + blog post + dataset

**Waarom waardevol**: Niemand doet dit. Enterprise security labs testen GPT-4/Claude maar niet lokale modellen. Indie developers draaien lokale modellen zonder enig inzicht in hun security eigenschappen.

### Project B: STRIDE Threat Model voor Multi-Agent Orchestratie

**Doel**: SafePaw's STRIDE aanpak (27 bedreigingen voor AI gateway) toepassen op multi-agent development workflows.

**Uniek**: OWASP heeft een Top 10 voor Agentic AI (dec 2025), maar geen specifieke threat model voor lokale multi-agent orchestratie met skip-permissions.

**Verdeling**:
- **SafePaw**: Threat modeling methodiek, security categorisatie
- **VNX**: Real-world use cases (1466 dispatches, 4 terminals, nachtelijke runs)

**Output**: Publiek threat model document, eventueel OWASP-contributie

### Project C: Kennisuitwisseling (laagste drempel)

Regelmatig (maandelijks?) sparren over:
- Nieuwe CVE's in AI coding tools
- Detectiepatronen die werken/falen
- Architectuurbeslissingen
- Elkaars code reviewen vanuit security-perspectief

---

## Technische referenties

### VNX bronbestanden (voor SafePaw developer als context)

| Component | Pad | Beschrijving |
|-----------|-----|-------------|
| Intelligence engine | `.claude/vnx-system/scripts/gather_intelligence.py` | 1540 regels, pattern matching + model routing |
| Learning loop | `.claude/vnx-system/scripts/learning_loop.py` | Confidence tracking per patroon |
| Tag intelligence | `.claude/vnx-system/scripts/tag_intelligence.py` | Tag combinatie analyse |
| Benchmark framework | `.claude/vnx-system/scripts/llm_benchmark.py` | 40KB, multi-model vergelijking |
| Benchmark coding v2 | `.claude/vnx-system/scripts/llm_benchmark_coding_v2.py` | Hardere coding taken |
| Receipt processor | `.claude/vnx-system/scripts/receipt_processor_v4.sh` | 1105 regels, flood protection |
| Dispatcher | `.claude/vnx-system/scripts/dispatcher_v8_minimal.sh` | 1324 regels, skill activation |
| Benchmark resultaten | `.claude/vnx-system/reports/benchmarks/` | 30+ runs, 8 modellen, .md + .json |

### SafePaw bronbestanden (voor Vincent als context)

| Component | Pad | Beschrijving |
|-----------|-----|-------------|
| Prompt injection scanner | `services/gateway/scanner/body_scanner.go` | 14 heuristische patronen |
| Output scanner | `services/gateway/scanner/output_scanner.go` | XSS, secrets, system prompt |
| Middleware chain | `services/gateway/middleware/` | 10 layers |
| Threat model | `docs/STRIDE.md` | 27 bedreigingen |
| Fuzz targets | `services/gateway/scanner/*_fuzz_test.go` | 7 fuzz targets |
| Incident playbooks | `docs/RUNBOOK.md` | 6 response procedures |

### Relevante externe frameworks (Python-native, voor VNX integratie)

| Framework | Repo | Toepassing |
|-----------|------|-----------|
| LlamaFirewall | Meta open source | CodeShield voor AI-gegenereerde code scanning |
| mcp-scan | `github.com/snyk/agent-scan` | MCP server vulnerability scanning |
| LLM Guard | Protect AI | 15 input + 20 output scanners |
| Lasso hooks | lasso.security | Claude Code-specifieke hook-based detectie |
| Veto SDK | veto.so | Authorization layer voor agent-acties |

### Relevante research

| Paper/Bron | Kernconclusie |
|------------|--------------|
| AIShellJack (2025) | 83.4% attack success rate op coding agents in auto-mode |
| OWASP Agentic AI Top 10 (dec 2025) | Agent Goal Hijacking = #1 risico |
| Lasso: Claude Code backdoor | "Claude verwerkt untrusted content met trusted privileges" |
| ToxicSkills (Snyk) | 36.8% van agent skills bevat security issues |
| IDEsaster (2025) | 30+ kwetsbaarheden in AI coding tools |

---

## Volgende stappen

1. **Dit document delen** met SafePaw developer
2. **Korte call** plannen om wederzijdse interesse te peilen
3. **Project A of C kiezen** als eerste concrete samenwerking
4. **MCP-scan draaien** op VNX's MCP servers (onafhankelijk van samenwerking)
5. **Security hooks bouwen** voor skip-permissions overgang

---

*Opgesteld door VNX Manager. Alle technische details zijn geverifieerd tegen de broncode van beide projecten.*
