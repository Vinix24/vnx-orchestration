# VNX Launch — To Do

**Datum:** 21 februari 2026
**Naar aanleiding van:** Marketing team review + brainstorm

---

## Content aanpassingen (done)

- [x] Provocatieve stelling toegevoegd aan Blog 1: "Every multi-agent system without an audit trail is a liability, not an asset."
- [x] Provocatieve stelling toegevoegd aan Blog 2: "The real risk of AI agents isn't hallucination — it's the inability to reconstruct what happened."
- [x] Provocatieve stelling toegevoegd aan Blog 3: "Databases are the wrong abstraction for governance."
- [x] Provocatieve stelling toegevoegd aan Blog 4: "If your monitoring depends on the provider's API, you don't have monitoring."
- [x] Blog 5 geverifieerd — titel + opening zijn al sterk genoeg
- [x] "Where this is going" visie-sectie toegevoegd aan Blog 1 (3 trends + kernclaim)
- [x] Blog 3 "Implement this yourself" verplaatst naar appendix

## GitHub repo

- [ ] GitHub Discussions aanzetten
- [ ] Categorie aanmaken: "Architecture Decisions"
- [ ] Categorie aanmaken: "Show Your Setup"

## LinkedIn engagement

- [ ] 3x per week reageren op hun content met inhoudelijke VNX-inzichten
- [ ] Authority peers lijst (hieronder) volgen op LinkedIn + X

### Authority peers — Multi-agent & orchestratie
1. **João Moura** — Creator CrewAI, multi-agent orchestratie pioneer (X + LinkedIn)
2. **Harrison Chase** — CEO LangChain, agent frameworks + LangSmith observability (X + LinkedIn)
3. **Yohei Nakajima** — Creator BabyAGI, autonomous agent task planning (X)
4. **Ankush Gola** — Co-founder LangChain, agent evaluation en governance (LinkedIn)
5. **Erik Schluntz** — Anthropic staff, tool use + agentic capabilities (X)

### Authority peers — AI developer tooling
6. **Simon Willison** — Creator LLM CLI, praktische LLM dev tools (X + blog)
7. **Swyx (Shawn Wang)** — Latent Space podcast, AI engineer infrastructure (X)
8. **Andrej Karpathy** — Software 3.0 filosofie, LLM educatie (X)
9. **Linus Ekenstam** — AI tools educator, breed bereik (X + YouTube)

### Authority peers — AI infrastructure & observability
10. **Hamel Husain** — ML engineer, AI evals + observability expert (X + blog)
11. **Rohan Paul** — AI engineer, dagelijkse insights over AI development (X + Substack)
12. **Austin Vernon** — AI infrastructure economics, cost analysis (X + blog)

### Authority peers — Open source & community
13. **Anthropic MCP team** — Model Context Protocol creators, agent interoperability (X + GitHub)
14. **Andrew Ng** — DeepLearning.AI, breed bereik in AI engineering (X + LinkedIn)

### Engagement strategie per categorie
- **Multi-agent peers (1-5):** "In my experience running 2,472 dispatches, governance is the gap..." — directe relevantie
- **Tooling peers (6-9):** Reageer op posts over Claude Code/Cursor met concrete VNX-inzichten
- **Infra peers (10-12):** Deel data over token governance, receipt-based observability
- **Community (13-14):** Participeer in MCP/agent standaardisatie discussies

## Author bio

- [ ] Bio op blog updaten met architect-positionering + proof of work (2,472 dispatches, 3 productiesystemen)

## Signature visual

- [ ] Mermaid blauwdruk staat klaar: `docs/internal/GLASS_BOX_GOVERNANCE_DIAGRAM.mermaid`
- [ ] Laat oppoetsen in Napkin Pro / ChatGPT naar gelikte visual

### Brand colors
- **Primary:** `#0A2463` (deep navy blue)
- **Secondary:** `#F97316` (bright orange)
- **Accent:** `#E5F6FF` (light blue)

### Design prompt voor Napkin Pro / ChatGPT
> Create a clean, modern architecture diagram for "Glass Box Governance" — a multi-agent AI governance system. The diagram should show a circular flow with these elements:
>
> **Top:** "AI Agents" block containing provider logos/labels (Claude, Codex, Gemini, ...) — these feed into the governance layer.
>
> **Center — the Glass Box (4 pillars in a cycle):**
> 1. OBSERVE (External Watcher) — filesystem observation, provider-neutral
> 2. RECORD (Receipt Ledger) — append-only NDJSON, chain of custody
> 3. VERIFY (Quality Gates) — async evaluation, evidence-based closure
> 4. CONTROL (Staging Gates) — human approval, scope verification
>
> **Bottom center:** "Orchestrator (T0)" — receives verdicts, sends dispatches
>
> **Side element (dashed/glowing):** "1,100+ Patterns" feedback loop from Receipt Ledger back to Orchestrator — representing the self-learning aspect
>
> **Flow arrows:** Agents → OBSERVE → RECORD → VERIFY → Orchestrator → CONTROL → back to Agents. Plus dashed arrow: RECORD → Patterns → Orchestrator.
>
> **Style:** Dark background (#0A2463 deep navy), accent lines in #F97316 (orange), text in white/light. Clean, minimal, no 3D effects. Think: architectural blueprint meets modern SaaS dashboard. The diagram should look like something a senior architect would put in a conference talk — professional, not decorative.
>
> **Key message the visual must communicate:** "Every agent action is observed, recorded, verified, and controlled — regardless of provider. The governance data feeds back into smarter orchestration."
>
> Export as SVG and PNG (transparent background). Target width: 1200px.

## HN launch

- [ ] HN-opening herschrijven met contrarian lead: "After running 2,472 multi-agent dispatches over 6 months, I'm convinced that the biggest risk of AI agents isn't hallucination — it's the inability to reconstruct what happened after the fact."

## Podcast outreach (na blogserie)

- [ ] Shortlist maken van 5 AI engineering podcasts (Latent Space, Practical AI, etc.)
- [ ] Pitch voorbereiden na publicatie blogserie

## Tagline

- [ ] Overal consistent gebruiken: "VNX — Glass Box Governance for AI Agents"
- [ ] Kernbelofte kiezen: architectuurprincipe, niet productomschrijving (bijv. "You can't govern what you can't observe")
