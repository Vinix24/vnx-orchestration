# OI-Triage 20260801-T2 — bestaansrecht OI-105 t/m OI-128

Dispatch-ID: 20260801-t2-oi-triage

## Summary

Twaalf open items (2026-06-03, OI-105 t/m OI-128) getoetst tegen de huidige code op een verse worktree vanaf origin/main. Uitkomst: 11 ACHTERHAALD (waarvan 3 met betekeniswijziging: OI-108, OI-112, OI-114), 1 ONBEOORDEELBAAR (OI-105). Geen BESTAAT-KLEIN of BESTAAT-GROOT. Geen codewijzigingen nodig: geen van de items reproduceert nog als een open defect. De drie items die nog een letterlijke kern hebben (OI-108 audit-shape, OI-112 status-afleiding, OI-114 provider_dispatch staging-flags) zijn veranderd van betekenis doordat de architectuur is verschoven (audit naar open_items_audit.jsonl; converter mapt terminal+status; staging naar de single-entry door). De gemini-render-incident (OI-105) is een runtime-waarneming uit een specifieke run die niet opnieuw te reproduceren is; de runner heeft sindsdien stall-detection, vertex-routing en cwd-pinning gekregen. Aanvullend: 2 pre-bestaande test-failures in tests/test_open_items_gate_certification.py (niet gerelateerd aan deze items).

## Changes

Geen codewijzigingen. Dit rapport is de enige output. Alle twaalf items blijven open in de ledger; T0 sluit ze op basis van dit bewijs.

## Verification

OI-105 | ONBEOORDEELBAAR | Runtime-incident (runner pids 75513/82267, PR #811, 2026-06-03) is niet reproduceerbaar op afroep en de root-cause is nooit vastgesteld; context (pids, PR) is weg. De exit_nonzero-classificatie bestaat nog bij design (scripts/gate_runner.py:513-515), maar gemini_review heeft sindsdien stall-detection (GATE-6/7/8; 180s drempel in scripts/lib/headless_adapter.py:89), vertex-routing (VNX_GEMINI_ROUTING=vertex, gate_runner.py:91) en cwd-pinning (OI-708). Een herhaling van de stille-275s-exit-1 zou nu als 'stall' op ~180s worden geclassificeerd, niet als kale exit_nonzero.

OI-106 | ACHTERHAALD | scripts/lib/providers/smart_router/__init__.py:59 normaliseert nu: `_env.get("VNX_AUTO_ROUTE","").strip().lower() not in ("1","true","yes","on")` → return None. Alleen truthy-waarden activeren; test tests/test_smart_router_cost_aware.py:371 verifieert dat 0/empty/unset NIET enableren. Bewijs: 127 smart-router-tests groen.

OI-108 | ACHTERHAALD | Betekenis gewijzigd: bulk-pattern-mutaties schrijven nu per-item NDJSON-audit (action=pattern_close, item_id, to_status, reason, pattern) naar STATE_DIR/open_items_audit.jsonl via audit_log_entry (scripts/open_items_manager.py:842-849 + 149-166). Test tests/oi/test_pattern_subcommands.py::test_apply_mutates_and_writes_audit bevestigt het bestand + entries (4/4 groen). Niet het voorgestelde single-per-run bulk-event in .vnx-data/events/; actor hardcoded 'T0'.

OI-109 | ACHTERHAALD | scripts/lib/staging_validator.py schrijft nog steeds alleen unstaged-overrides (via _write_audit_event, regels 143-162) en failures; het success-pad retourneert stil. Dit is exact het ontwerp dat het item zelf voorstelde ("log only override + failure"). Design geratificeerd, geen defect.

OI-110 | ACHTERHAALD | Beide concrete instanties zijn aan de bron dicht: staging_validator.py:27+90-92 valideert dispatch_id tegen _DISPATCH_ID_RE + resolve().is_relative_to-containment (regels 30-47); atomic_io.py:91-101 valideert event_type tegen _EVENT_TYPE_RE + resolve().is_relative_to. De voorgestelde generieke lint in ci_lint_patterns.py is NIET toegevoegd (alleen pattern A bare-except + pattern B non-atomic state-writes bestaan). De twee gemelde kwetsbaarheden zijn dicht.

OI-111 | ACHTERHAALD | scripts/build_decisions_digest.py:80-94 schrijft decisions_digest.md nog steeds atomair zonder NDJSON-event. Dat matcht de in het item vastgelegde ontwerpkeuze (ADR-021 narrow exception; regel 84 refereert ADR-021). Design geratificeerd, geen defect.

OI-112 | ACHTERHAALD | Betekenis gewijzigd: report_to_receipt_converter.py:285 mapt nu terminal (frontmatter/body) + status naar de receipt (test tests/test_report_to_receipt_converter.py:373-386: terminal=T2, status=success); rp_delivery.sh:129 emitteert nu `RECEIPT:${terminal}:${footer_status}` met echte waarden i.p.v. unknown:unknown. De T1/T2/T3 'idle stats'-formaat is vervangen door een live STATE-regel (_build_state_line, rp_extract.sh:22-40, leest de T0-brief). NB: de 'exit_code→status'-afleiding uit OI-113 is niet zo geïmplementeerd — status komt uit het status-veld van het rapport.

OI-113 | ACHTERHAALD | Branch dispatch/20260603-deferred-receipt-format-fix en commit 88d7fe35 bestaan niet meer (git branch -a + git log --all). De genoemde inhoud (terminal-mapping, rp-format-update) is via latere PR's op main geabsorbeerd (#788, #1231/#1232). Recovery-anchor weg → ledger-entry is stale.

OI-114 | ACHTERHAALD | Betekenis gewijzigd: staging-afdwinging is verhuisd naar de single-entry door — dispatch_cli.py:529-583 (_check_staging_binding_verdict, ADR-006) weigert bundles die pending/<id>/ ontsnappen, voor ALLE lanes incl. provider (uitgevoerd via dispatch_envelope.run_envelope_plan, niet via provider_dispatch.main). PR-12 bridge + dispatch_sidedoor_audit.py behandelen provider_dispatch als geauditeerde lane. provider_dispatch.py main() heeft nog steeds geen --allow-unstaged/--reason (geverifieerd: geen validate_staging_path-call in scripts/lib/provider_dispatch.py) — directe lane-script-invocatie blijft een side door, maar dat is het gedocumenteerde PR-12-consolidatie-doel, geen onafgedekte bypass.

OI-126 | ACHTERHAALD | provider_dispatch._resolve_data_dir()/._resolve_state_dir() (scripts/lib/provider_dispatch.py:93-134) resolven nu standaard naar centraal ~/.vnx-data/<project_id>; VNX_STATE_DIR wordt alleen gehonoreerd met VNX_DATA_DIR_EXPLICIT=1 (OI-126-commentaar op regels 112 en 127). Reports worden naar data_dir/unified_reports geschreven (regel 602) = centraal.

OI-127 | ACHTERHAALD | routing_policy.yaml default_lane is nu 'kimi' (worker-provider-kimi-flip 2026-07-23); deepseek komt alleen voor als operator opt-in cost-lane (VNX_USE_CHEAP_LANE, routing_policy.yaml:30-31). Smart router tier_routing.py:96-106 routeert tier-low via claude_harness_keyed (deepseek-harness, DEEPSEEK_API_KEY-gated) met kimi-fallback. Beide honoreerden de deepseek-harness-subscription-blocked constraint; geen litellm:deepseek-default meer.

OI-128 | ACHTERHAALD | De kalibratie-gebieden zijn in latere PR's geadresseerd: #818 null-cost sort (_cost_aware_sort_key hybrid, smart_router.py:197-212) + strategy-tag (write_route_decision per-dispatch JSON → receipt route_decision-veld); #822 quality_tier-gates; #965 capability-threshold hybrid; deepseek-chat→deepseek-v4-flash migratie (commit 534696b4). Router blijft default-off (route_dispatch retourneert None tenzij VNX_AUTO_ROUTE truthy; __init__.py:59). Sub-bullet-4-tekst was afgekapt in de opdracht; de kernclaim ('default-on blocked') is moot — de router is nooit default-on gegaan.

## Open Items

- Geen nieuwe open items uit deze triage. T0 kan OI-106, OI-108, OI-109, OI-110, OI-111, OI-112, OI-113, OI-114, OI-126, OI-127 en OI-128 als ACHTERHAALD sluiten op basis van bovenstaand bewijs; OI-105 blijft ONBEOORDEELBAAR (of sluiten op basis van mitigatie als T0 dat passend vindt).
- Pre-bestaande (niet door deze dispatch veroorzaakte) test-failures op main: tests/test_open_items_gate_certification.py::TestContractAlignment::test_read_gate_config_returns_dict en test_write_read_round_trip falen met AttributeError (module 'serve_dashboard' heeft geen '_read_gate_config' meer). Niet gerelateerd aan deze items; apart te triagen.
