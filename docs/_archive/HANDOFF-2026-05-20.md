# HANDOFF — VNX per 2026-05-20

> **Voor nieuwe Claude Code sessie na /clear**: lees dit eerst. Bevat de huidige stand na de 2026-05-17 sprint + beslissingen van 2026-05-20.

## Operator

Vincent van Deth (solopreneur NL). 1e persoon enkelvoud altijd, NL tenzij anders, geen em-dashes. Profile: `~/.claude/profile.md`.

## TL;DR

Wave 5/6/7 zijn gemerged en stabiel. Wave 8 (Smart Router) is in flight op branch `refactor/intelligence-selector-split` via PR #602. Probleem: PR #602 bevat migration-werk dat er niet in thuishoort, waardoor Codex blockers blijft vinden op niet-Wave-8 code. Volgende stap: PR splitsen.

Volledige roadmap: [`claudedocs/roadmap-2026-05-20-master.md`](claudedocs/roadmap-2026-05-20-master.md).

---

## Wat er gedaan is (2026-05-17 sprint)

27 PRs gemerged + 19 productie-blockers gefixed. Wave 5/6/7/8 fast-path compleet:

- Wave 5: Control Centre + state-aggregator (PR-5.x cluster)
- Wave 6: Elastic Worker Pool (PR-6.0 t/m 6.8)
- Wave 7: DeepSeek V4 + Kimi K2.6 + GLM-5.1 (PR-7.1 t/m 7.7 + p0a/p0b)
- Wave 8 partial: intelligence_selector split (2511 LOC > 321 LOC + 9 modules, PR #554)

Codebase-staat: `v1.0.0-rc1` uitgebracht. Smart router operationeel (route_decisions.ndjson produceert geldige output).

---

## Huidige staat: Wave 8 in flight

**Branch**: `refactor/intelligence-selector-split`
**PR**: #602 (gepland voor split vanwege scope-vermenging)

Wave 8 omvat de intelligence_selector.py refactor en de Smart Router. PR #602 bevat echter ook 5 migration-commits en bench auto-commits die er niet in thuishoren. Codex blijft daardoor migration-bugs vinden in een PR die dat niet zou moeten bevatten.

Gefixte blockers die al in de branch zitten:
- 3 HIGH-severity bevindingen uit code review gefixed (commit `c71801c`)
- Codex ronde 1: tenant_id rollback blocker gefixed (commit `90ad5fc`)

Codex ronde 2 heeft 2 nieuwe blocking findings in de migration scripts. Die zitten niet in Wave 8 scope, maar in de PR omdat de commits er zijn ingeslopen.

---

## Open decisions

| ID | Onderwerp | Status |
|---|---|---|
| D1 | Operationele tool vs Platform | BESLOTEN: Hybrid C, review 2026-08-01 |
| D2 | Centralisatie pad | BESLOTEN: route C (incremental MC > sales-copilot > SEOcrawler over 5 dagen) |
| D3 | LOC reductie scope | Open, start na D2 voltooid |
| D4 | Conversation analyzer re-activate | Open |
| D5 | Critical gaps follow-up | Open |
| D6 | Library swap (DSPy/smolagents) | Open, alleen relevant als D1=A |
| D8 | Kimi/GLM/Moonshot in benchmark + blog | Open, relevant voor LinkedIn-strategie |
| D9 | Wave 8 packaging als publieke release | Wacht op Wave 8 close |
| D10 | Blog publicatie strategie | Wacht op claude-launcher v0.0 launch |

---

## Volgende stappen in volgorde

**Stap 1: Wave 8 PR splitsen (15-30 min)**

1. Maak `fix/wave8-blockers-clean` branch vanaf main
2. Cherry-pick alleen Wave 8 commits: `7c9ae90` (intelligence split) + `c71801c` (3 HIGH blockers)
3. Open nieuwe PR, sluit #602 of houd het open voor migration-cleanup
4. Codex gate + CI green + merge

**Stap 2: Pre-cutover hardening (1-2 dagen)**

- Worktree-cleanup: 20+ oude vnx-* worktrees archiveren naar `~/Archive/vnx-worktrees-2026-05-20/`
- Project_id scoping audit op centrale DB-queries
- Schema-versioning + migration rollback per migration
- Receipt-processor project_id-filter audit

**Stap 3: Wave 2a centralisatie (5 dagen, D2 route C)**

MC > sales-copilot > SEOcrawler met 24-48u burn-in per project.
Feature-flags rc3: VNX_RUNTIME_PRIMARY=1, VNX_CANONICAL_LEASE_ACTIVE=1, VNX_USE_CENTRAL_DB=1.

**Stap 4: Wave 3 DB retirement (na 7d burn-in)**

Symlink per-project DBs naar centrale, archiveer per-project DBs.

---

## Parallelle workstreams

Drie streams die los lopen van VNX-centralisatie:

- **claude-launcher** (open source product, niet VNX): PRD + launch-strategie klaar in `~/Desktop/BUSINESS/development/claude-launcher/`. Build loopt in aparte sessie.
- **claude-conversation-manager v0.2**: eigen repo Vinix24/claude-conversation-manager. PR #2-#5 deels in flight.
- **Renewance proposal**: €50-100K, deze week opsturen.

---

## Bench-data (definitief, 2026-05-17)

| Model | Quality | Correct% | Dur | Cost/dispatch |
|---|---|---|---|---|
| Kimi K2.6 (OpenRouter) | 8.71 | 86% | 233s | $0.015 |
| GLM-5.1 | 7.86 | 71% | 107s | $0.012 |
| DeepSeek V4-Flash | 7.29 | 86% | 32s | $0.0008 |
| DeepSeek V4-Pro | 7.29 | 71% | 189s | $0.006 |
| Claude Sonnet 4.6 | 6.71 | 71% | 192s | $0.045 |
| Kimi-for-coding (CLI) | 5.43 (artefact) | 57% | - | $0 sub |
| Claude Opus 4.6 | 4.43 | 43% | 94s | $0.30 |
| Claude Opus 4.7 | 4.29 | 43% | 98s | $0.30 |
| Claude Haiku 4.5 | 3.71 | 29% | 71s | $0.015 |

Kimi-for-coding 5.43 = artefact (3 lege runs door delivery-bug). Op 4 succesvolle taken: 8.75, identiek aan K2.6 OR.

---

## Operationele context

**Werkdir:** `/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt`
**Huidige branch (begin sessie):** `refactor/intelligence-selector-split`
**Open PRs:** #602 (Wave 8, split gepland), #553 (OI-1489), #476 (codex-onboarding)

**Persistent worktrees (hergebruikbaar):**
vnx-wt-65b, sr1, sr2, sr3, sr4, 65c, 65d, 65e, vnx-wt-wiring, wiregate, receipts, vnx-wt-kimifix

---

## Start nieuwe sessie

Eerste prompt na `/clear`:

> Lees HANDOFF.md. Staat per 2026-05-20: Wave 5/6/7 gemerged, Wave 8 in flight via PR #602 (split nodig). Volgende stap: cherry-pick Wave 8 commits naar clean branch, codex gate, CI, merge. Daarna pre-cutover hardening.

---

## Cross-referenties

- Volledige roadmap: `claudedocs/roadmap-2026-05-20-master.md`
- Open decisions detail: `claudedocs/open-decisions-roadmap-2026-05-17.md`
- Wave 8 code review: `claudedocs/review-wave8-smart-router-2026-05-17.md`
- Dispatch primer: zie vorige HANDOFF of `.vnx/docs/`
