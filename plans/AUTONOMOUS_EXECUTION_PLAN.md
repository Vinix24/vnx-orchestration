# Plan: Volledig Autonome VNX Executie

**Oorsprong**: Analyse 2026-02-28 (linkedin_engine als eerste target project)
**Laatste update**: 2026-03-04
**Status**: In uitvoering — 4/5 robustheid-issues opgelost

---

## Doel

VNX in staat stellen een compleet project (30+ PRs) autonoom uit te voeren via T0 met `--dangerously-skip-permissions`, zonder menselijke tussenkomst behalve wave-boundary approvals.

---

## Deel 1: Showstoppers

Blokkerende problemen die opgelost moeten zijn voordat autonome executie kan starten op een nieuw project.

| # | Showstopper | Status | Toelichting |
|---|-------------|--------|-------------|
| S1 | Geen bestaande codebase (bootstrap risico) | OPEN | PR-1 falen blokkeert alles. Vereist pre-flight validatie |
| S2 | Geen credentials (.env ontbreekt) | OPEN | API keys moeten vooraf geconfigureerd en gevalideerd zijn |
| S3 | Geen tooling validatie | OPEN | Pre-flight script nodig voor Node/Python/CLI checks |
| S4 | VNX Queue vereist menselijke acceptatie | OPEN | Popup-gate blokkeert elke dispatch zonder handmatige klik |
| S5 | Geen automated git/PR workflow | OPEN | Branch/commit/PR moet automatisch per dispatch |

### Benodigde componenten (showstoppers)

#### A. Pre-flight script (lost S1, S2, S3 op)
Script dat voor het eerste dispatch draait:
- Valideert CLI tools (node, python, supabase, claude, gh)
- Maakt `.env` aan van template met ingevulde credentials
- Verifieert API keys tegen live endpoints
- Zet test-database op (supabase local of apart project)
- Checkt disk space, RAM, en network connectivity

#### B. Queue auto-accept mode (lost S4 op)
Non-interactive mode voor VNX queue:
- CLI flag: `--auto-accept` of environment variable `VNX_AUTO_ACCEPT=true`
- Dispatches gaan direct van `queue/` naar `pending/` zonder popup
- Optioneel: wave-level approval (1 accept per wave i.p.v. per PR)
- Safety: dry-run mode die dispatches toont zonder uit te voeren

#### C. Git automation skill (lost S5 op)
Skill of script die per dispatch:
1. Branch aanmaakt (`git checkout -b feat/PR-{N}-{slug}`)
2. Changes staged en commit met conventionele message
3. `gh pr create` met titel uit FEATURE_PLAN
4. Tests runt en output opslaat als evidence
5. PR merget als tests slagen (of markeert voor review)

---

## Deel 2: Robustheid-Issues

Systeembetrouwbaarheid onder load — cruciaal voor onbeheerde operatie.

| # | Issue | Status | Oplossing | Commit |
|---|-------|--------|-----------|--------|
| R1 | Smart Tap verliest Manager Blocks stil | OPGELOST | Queue gate raised van >0 naar >=15, per-track cap van 5 | `5866dd2`, `f1ded1c` |
| R2 | receipt_write.lock niet atomair | OPGELOST | PID-file vervangen door `flock` (OS-level atomair) | `5866dd2` |
| R3 | Tmux 1.3s blocking per receipt | WON'T FIX | Bewuste keuze: systeem werkt stabiel, risico van timing-issues bij lagere sleep weegt niet op | — |
| R4 | receipt_flood.lock nooit auto-reset | OPGELOST | 5-minuten auto-expiry + cleanup on shutdown | `5866dd2` |
| R5 | Geen lease garbage collection | OPGELOST | GC op elke write in terminal_state_shadow.py | `5866dd2` |

### R3: Tmux sleep — bewust niet aangepast

**Situatie**: Elke receipt delivery naar T0 kost 1.3s (sleep 1 + sleep 0.3). Bij 3 simultane terminal completions is T0 ~4s geblokkeerd.

**Besluit (2026-03-04)**: Niet aanpassen. Het systeem werkt stabiel met de huidige timing. Het risico van tmux paste-buffer timing-issues bij lagere sleep weegt niet op tegen de marginale verbetering. De andere 4 fixes hebben de kritieke bottlenecks weggenomen.

---

## Deel 3: Autonome Safeguards

Extra beveiliging nodig voor onbeheerde operatie.

| # | Safeguard | Status | Beschrijving |
|---|-----------|--------|-------------|
| G1 | Cost guardrails | OPEN | Budget tracker voor Claude CLI, auto-pause bij 80% maandbudget |
| G2 | Quality gate enforcement | OPEN | Automatische test+coverage check na elke PR (tsc, vitest, pytest, eslint) |
| G3 | Wave-based execution | OPEN | T0 dispatcht waves i.p.v. individuele PRs, met auto-gates tussen waves |
| G4 | External API mocking | OPEN | Alle externe API calls (PhantomBuster, Mollie, etc.) op mocks in autonomous mode |
| G5 | Rollback capability | OPEN | Automatische `git revert` als quality gate faalt na merge |

### Wave-based execution schema

| Wave | PRs | Automatische gate |
|------|-----|-------------------|
| 1 | Bootstrap (project setup) | npm/pytest werkt, .env geladen |
| 2 | Database + core integrations | Schema compleet, migrations OK |
| 3 | Backend processors | Tests passing, coverage >=80% |
| 4 | Frontend shell + components | tsc + vitest OK |
| 5 | Integration + polish | E2E tests passing |
| 6 | Deploy + monitoring | Health checks OK |

---

## Deel 4: Overload Scenario's

Gedocumenteerde scenario's en hun huidige status.

### Scenario A: 3 terminals klaar tegelijk
- **Voor**: Block 2 en 3 silently lost (queue gate blokkeerde)
- **Na fix**: Queue gate op >=15, per-track cap 5. Alle 3 blocks komen door. OPGELOST

### Scenario B: Startup recovery na crash
- **Voor**: 20 reports × 3s = 60s T0 blocking
- **Na fix**: Lock is atomair, flood auto-resets na 5min. Blocking nog steeds ~26s (20 × 1.3s tmux). VERBETERD, niet volledig opgelost (R3)

### Scenario C: Flood protection
- **Voor**: receipt_flood.lock permanent, handmatig rm vereist
- **Na fix**: Auto-expiry na 5 minuten. OPGELOST

---

## Aanbeveling: Uitvoeringspad

### Fase 1: Autonome infrastructuur (middellange termijn)
- [ ] B: Queue auto-accept mode
- [ ] C: Git automation skill
- [ ] G2: Quality gate enforcement script

### Fase 2: Project onboarding (per nieuw project)
- [ ] A: Pre-flight script (tooling + credentials)
- [ ] G1: Cost guardrails configuratie
- [ ] G3: Wave definitie op basis van FEATURE_PLAN
- [ ] G4: Mock configuratie voor externe APIs

### Fase 3: Eerste autonome run
- [ ] Semi-autonoom: wave approvals (6-8 clicks per project)
- [ ] Evaluatie en iteratie
- [ ] Volledig autonoom (zero-touch) als semi-autonoom stabiel is

---

## Referenties

- Originele analyse: `/Development/linkedin_engine/AUTONOMOUS_EXECUTION_ANALYSIS.md`
- Lock hardening commit: `5866dd2` (vnx-orchestration, 2026-02-28)
- Smart tap queue fix: `f1ded1c` (vnx-orchestration, 2026-02-27)
- VNX Architecture: `.claude/vnx-system/docs/core/00_VNX_ARCHITECTURE.md`
- Robustheid lock overzicht: zie Deel 2 hierboven
