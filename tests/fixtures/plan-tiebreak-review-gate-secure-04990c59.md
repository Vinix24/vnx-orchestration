# Plan-gate tiebreak — review-gate-secure

**Dispatch-ID**: plan-tiebreak-review-gate-secure-04990c59
**Model**: deepseek-v4-pro
**Provider**: deepseek-harness
**Role**: plan-gate tiebreaker (na 2 panelrondes zonder PASS)

## Summary

Beslissing: **START**, geen verplichte wijziging.

Ik heb niet opnieuw als zetel gereviewd. Ik heb getoetst of dit plan convergeert
of rondjes draait, aan twee vragen: kloppen de dragende regelverwijzingen op de
boom, en is de scope na twee rondes gekrompen in plaats van gegroeid. Beide zijn
waar.

Alle zeven probleemgaten (OI-1442, OI-1618, OI-1617, OI-1434/OI-1280, OI-1644,
OI-1645) hebben elk een concrete deliverable met een rode test die op main faalt,
en de volgorde-eis (A5/A6 eerst, dan A1+A3 parallel, A2 na A1, A4 apart) is
vrij van bestandsoverlap. De ronde-1- en ronde-2-bevindingen zijn stuk voor stuk
verwerkt in de tabel, met bewijs waar de auteur een eigen claim moest corrigeren
(bijv. A3 "stale door #1749" is toegespitst op het restgat dat #1749 expliciet
niet dichtte).

De opus-zetel gaf tweemaal geen oordeel. Dat is gemeten als een tmux-
leveringsdefect (alleen de laatste promptregel komt aan, blocker-OI gefiled),
geen planoordeel. Er zit geen stille REVISE achter die time-outs.

## Changes

Geen code gewijzigd. Review-only rol; niets anders geschreven dan dit rapport.

## Verification

Elke dragende verwijzing tegen de huidige boom nagelopen. Allemaal exact.

| claim | bewijs |
|---|---|
| glm diff gaat ongemarkeerd de prompt in | `scripts/glm_gate.py:181-191` `_build_prompt` plakt `diff_text` kaal achter instructie + `_VERDICT_CONTRACT`; `grep -ciE "sanitiz|escape|neutral|untrusted"` = 0/0 |
| diepte hangt alleen op de codex-lane | `grep -rln gate_depth scripts/` → alleen `scripts/lib/gate_artifacts.py`; glm/kimi kennen geen diepte |
| deur slikt poortverplichting stil in | `dispatch_cli.py:1329 _register_gate_obligation`, `:1128 _record_bookkeeping_failure` met contract "bookkeeping must never block the door"; ~29 `except Exception`-plekken |
| leeg en onleesbaar zijn niet-scorend | `plan_gate_panel.py:114 VERDICT_FENCE`, parse `:641` (empty) en `:646` (no verdict block found) beide `parse_error` |
| tiebreaker telt alleen ronden | `plan_gate_tiebreaker.py:472 should_run_tiebreaker`, `:362 read_round_count`, drempelcheck `:490` |
| derde hook stuurt de dode vorm | `pretooluse_worker_scope_enforce.py:281` emitteert `{"decision": "block", "reason": ...}` |
| derde aanroeper van de recorder | `gate_reanchor_cli.py:322` roept `record_terminal_result` aan |
| enum-drift-gat is reëel | `test_oi1645_peer_is_review_gate.py` bestaat; `test_closure_verifier_gate_enum_drift.py` heeft 0 treffers op `_REVIEW_PEER_GATES`/`_NON_REVIEW_SIGNER_REASONS` |

ADR-007: geen deliverable voegt een central-DB-tabel toe; niet van toepassing.

## Open Items

De twee open vragen zijn correct gescoped. Vraag 1 (`claude_github_optional`
als review-peer) is een operator-besluit en A6 pint de huidige keuze tegen
drift. Vraag 2 (blocking vs advisory voor "instructie in de diff") hoeft de
build niet te blokkeren: de canary-test in A1 pint het gedrag ongeacht de
severity; de severity zelf is een label dat later gewijzigd kan worden.

```vnx-plan-tiebreak
{
  "outcome": "START",
  "required_change": "",
  "rationale": "Het plan is na twee rondes geconvergeerd: elke dragende file:line-verwijzing klopt op main, beide REVISE-rondes zijn met bewijs verwerkt, en de volgorde-eis is vrij van bestandsoverlap. De opus-afwezigheid is een gemeten tmux-leveringsdefect, geen planoordeel; er is geen gat dat een derde ronde zou dichten."
}
```
