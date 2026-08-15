# Open-items dispositie-voorstel — 2026-08-15

> Meetopdracht. Geen statuswijziging uitgevoerd, geen code veranderd. T0 voert de sluitingen uit op basis van dit bewijs.

## Nulmeting (correctie ten opzichte van de opdracht)

De opdracht ging uit van 53 open items (46 warn, 7 info). Gemeten uit
`~/.vnx-data/vnx-dev/state/open_items.json` (`status == "open"`, op main `45ace207`)
staan er **55** open: **48 warn + 7 info, nul blockers**. De oudste is van
2026-08-04, alle zijn van augustus.

Het verschil van 2 is geen meetfout maar een beweging tussen de nulmeting en nu:
`open_items.json` heeft `last_updated=2026-08-15T14:25:45`, en **OI-1209** en
**OI-1210** (beide warn, 2026-08-15) zijn ná de nulmeting bijgekomen. Ik werk met
de echte 55; de som van de slottelling hieronder is 55.

Severity-verdeling van de 55: 48 warn, 7 info.

## Tabel: dispositie per item

| OI-id | severity | voorstel | bewijs in één regel | herzieningsmoment (bij uitstellen) |
|---|---|---|---|---|
| OI-1037 | warn | OPEN HOUDEN | grep op `402`/`Insufficient Balance`/retryable in `providers/*`, `provider_dispatch.py`, `failure_classification.py` = 0 treffers; concurrency-cap noch 402-als-retryable bestaat | — |
| OI-1040 | warn | OPEN HOUDEN | geen mechanisme dat deferred items heropent; operator-keuze (freshness-koppeling / cadans / accepteer-als-archief) nog onbeslist | — |
| OI-1058 | warn | OPEN HOUDEN | fix ligt deels in de fabric (`vnx doctor` staleness-check op t0_state.json / ontbrekende SessionStart-hook); nog niet in `doctor` | — |
| OI-1061 | warn | OPEN HOUDEN | geen fix gevonden: worktree-aanmaak legt symlink nog bevroren; geen "weiger bij versie-afwijking"-check in `dispatch_worktree_isolation.py` | — |
| OI-1067 | warn | OPEN HOUDEN | item is ongemeten hypothese; lane kan capaciteitsweigering nog niet onderscheiden van trage worker (geen code gevonden) | — |
| OI-1069 | warn | OPEN HOUDEN | `technical-writer` zit niet in de canonieke rolset; stil falen van projectlokale rol zonder centrale tegenhanger niet geclassificeerd afgehandeld | — |
| OI-1091 | warn | OPEN HOUDEN | geen gestructureerd file-scope veld in dispatch-spec; overlap-detectie (a/b/c) niet gebouwd | — |
| OI-1107 | warn | OPEN HOUDEN | lane-conformity-matrix verwijst nog op regelnummers; geen CI-check die doc-regelrefs toetst tegen inhoud | — |
| OI-1108 | warn | OPEN HOUDEN | `test_t24_envelope_subpath_verification_from_report` faalt lokaal (reproduced: `assert 'reject' == 'accept'`); `run_envelope` is levend (provider_dispatch.py:1717/1757) en genereert niet-contractconform rapport | — |
| OI-1118 | warn | OPEN HOUDEN | 19 unknown-dispatches nog niet per stuk getrieerd; oorzaak (rapportage- vs leveringsgat) onbepaald | — |
| OI-1121 | warn | OPEN HOUDEN | test staat nog per-naam uitgesloten in `vnx-ci.yml`; tweede pollution-kandidaat niet gevonden | — |
| OI-1137 | warn | OPEN HOUDEN | phantom-guard meet nog primair worktree-diff; gepushte-ref-afweging voor fix-forward niet geverifieerd als geleverd | — |
| OI-1138 | info | OPEN HOUDEN | `dispatch()` in `tmux_interactive_dispatch.py` is nu **1031 regels** (2464-3494); refactor naar helpers niet gebeurd | — |
| OI-1141 | warn | OPEN HOUDEN | `scripts/pre_merge_gate.py:463` `head_ref: str = "HEAD"` staat er nog; `--pr` is nog een label | — |
| OI-1144 | warn | OPEN HOUDEN | drie naamloze `load-buffer`/`paste-buffer`-gebruikers (rp_delivery.sh, dispatch_deliver.sh, heartbeat_ack_monitor.py) nog zonder `-b` | — |
| OI-1145 | warn | OPEN HOUDEN | `plan_gate_panel.py:1100` `root = _find_repo_root(Path(__file__).resolve().parent) or _find_repo_root(Path.cwd())` ongewijzigd | — |
| OI-1147 | warn | OPEN HOUDEN | config-artefact `scripts/lib/providers/glm_harness_litellm_proxy.yaml` bestaat nu; startscript/launchd om de proxy op :4141 te starten ontbreekt nog | — |
| OI-1148 | info | OPEN HOUDEN | `scan_proposals.py` (user-skills, buiten repo) telt nog `subprocess_completion`/`task_complete` mee; semantiek-pin niet vastgelegd | — |
| OI-1149 | warn | OPEN HOUDEN | geen "local main loopt N commits voor op origin/main"-waarschuwing in worktree-prepare/deur | — |
| OI-1150 | warn | **SLUITEN** | `deliberation_panel.py:134-142` rendert `"X/Y lenses present; N failed"`; `:363-369` vult `failed_seats` via `_is_error()` | — |
| OI-1151 | warn | OPEN HOUDEN | geen `vnx deliverable close --evidence` CLI; deliverable-afboeken buiten dispatch-lane niet gebouwd | — |
| OI-1153 | warn | OPEN HOUDEN | seat-CLI krijgt `VNX_DATA_DIR` niet geëxporteerd in de gedeelde subprocess-env; legacy-root `~/.vnx-data/unified_reports/` niet opgeruimd | — |
| OI-1154 | warn | OPEN HOUDEN | synthese nog `_first_ok(...)` zonder drempel; geen `--allow-degraded`/min-zetel-rem gevonden | — |
| OI-1155 | warn | OPEN HOUDEN | `track_reconciler.py:211` bron-4 nog opt-in (`VNX_RECONCILE_GIT`); `gh pr merge` emit geen `pr_merged`-event; default-flip onbeslist | — |
| OI-1156 | warn | OPEN HOUDEN | `dispatch_plan.py:251-252` `if is_claude_headless: billing = "api_metered"` nog lane-afgeleid, niet auth-afgeleid | — |
| OI-1157 | info | **UITSTELLEN** | release-helft gedaan (VERSION=1.4.7, #1461 gemerged); kimi-quota-reset onbevestigd | kimi CLI OAuth-quota-reset (operator-bevestiging) |
| OI-1159 | warn | **SLUITEN** | `planning_cli.py:766-777` link-pr --delivery emit ERROR+json-error (OI-1167); `track_reconciler.py:601-610` `_blocking_detail`+`_delivery_hold` houden auto-close | — |
| OI-1160 | warn | OPEN HOUDEN | zes bestanden staan nog in `scripts/ci/test_exclusions.txt` (regels 117/146/151/152/175/176); volgorde-/image-oorzaak niet gevonden | — |
| OI-1161 | warn | OPEN HOUDEN | `_write_worker_scope_hook_settings` merge't alleen de eigen worktree-settings, niet het permissions-blok van de hoofdcheckout | — |
| OI-1168 | warn | OPEN HOUDEN | `seed_tracks_from_roadmap.py` doet geen sleutelvalidatie (grep unknown/strict/allowed_keys = 0); typo kost PR-koppeling | — |
| OI-1172 | warn | **SLUITEN** | `headless_dispatch_daemon.py:41-55` `_default_data_dir()` roept nu `resolve_data_dir_fail_loud()` i.p.v. `_repo_root()/'.vnx-data'` (c3f92f6e) | — |
| OI-1173 | info | OPEN HOUDEN | herontwerp-reeks 1-4 deels geleverd (decision_ref #1493, delivery-hold #1479); transition_phase-poort + versiestempel + typed seed-validatie resteren | — |
| OI-1174 | warn | OPEN HOUDEN | `vnx_cli/commands/dispatch_agent.py` kent `allow_headless`/`headless_reason` niet (grep leeg); `force_headless` nog dode config | — |
| OI-1177 | info | **SLUITEN** | #1481 gemerged als `65c37756` op origin/main — de voorwaarde "ZODRA #1481 daadwerkelijk merget" is vervuld | — |
| OI-1180 | warn | OPEN HOUDEN | contract staat in DISPATCH_RULES §9 maar niet gekoppeld aan lane-tabel §8 (regels 103-110) | — |
| OI-1181 | warn | OPEN HOUDEN | SSE-test in `test_pipeline_integration.py` niet deterministisch gemaakt, niet gequarantaineerd | — |
| OI-1182 | warn | OPEN HOUDEN | ADR-003/004/005/006/007/008/009/010/011/013/026/031/032 verwijzen nog naar `claudedocs` (grep bevestigt) | — |
| OI-1183 | warn | OPEN HOUDEN | release-notes vermelden de current-flip→consumer-gate-interactie nog niet | — |
| OI-1185 | warn | **SLUITEN** | `tier_routing.py:100` "walk the fallback chain, skipping unavailable lanes (OI-1185)"; missing-key/CLI-absent/cooldown één keten (`:13-16`) | — |
| OI-1186 | warn | **SLUITEN** | `availability.py:130` `cooldown_seconds(failure_class)` delegeert naar `incident_taxonomy.get_cooldown_seconds`; vaste 3600s weg | — |
| OI-1190 | warn | **SLUITEN** | `planning_cli.py:2466-2477` persist decision_ref; `migrations/apply_0033.py` voegt `tracks.decision_ref` toe (e39a1b6f) | — |
| OI-1192 | warn | OPEN HOUDEN | `crash_recovery_sweep.py` dekt alleen `dispatches/active/`-entries, niet tmux-sessies/worktrees; `cleanup_stale_vnx_sessions.sh` nog 7-dagen+interactief | — |
| OI-1193 | warn | OPEN HOUDEN | deur-consolidatie gedaan (door_routing leest wave7_models.yaml, fail-loud); maar `routing_recommendations.yaml` bevat nog gedeprecieerde `glm-5` (regels 67/189/288) + geërfde mei-scores | — |
| OI-1194 | info | **SLUITEN** | `model_normalizer.py:16` `kimi-for-coding -> kimi-k2-7` alias; prose-modelwaarden geweigerd (82729192, #1497) | — |
| OI-1200 | warn | **SLUITEN** | `test_t0_with_opus_allowed` assert nu `model="opus-5"` en slaagt (1 passed); beslissing = 4.8 geen geldige T0 (45ace207) | — |
| OI-1201 | warn | **WONTFIX** | structurele eigenschap: dispatcher bouwt spawn-argv met main-code; post-merge-toets is de vlootconventie (OI-1209 bevestigt) | — |
| OI-1202 | warn | OPEN HOUDEN | placeholder-receipt draagt nog status `failed`/`done`, geen `running`; niet-gecorrigeerde placeholder leest nog als echte failure | — |
| OI-1203 | warn | OPEN HOUDEN | parallelle dispatch actief: `d066189f` "enforce push-verified delivery (OI-1203)" op `origin/dispatch/20260815-opsch-w1-push-verified`, niet op main | — |
| OI-1204 | warn | OPEN HOUDEN | solo 9 passed, niet gequarantaineerd; full-sweep-flake niet reproduceerbaar zonder full suite (verboden in deze dispatch) | — |
| OI-1205 | warn | OPEN HOUDEN | `provider_constraints.yaml:109-117` `zai-via-openrouter-only` is nog `forbid_route/via:direct` (blocklist), niet omgezet naar allowlist | — |
| OI-1206 | info | OPEN HOUDEN | `~/.claude/rules/provider-constraints.md` ligt buiten de repo; operator-werk | — |
| OI-1207 | warn | OPEN HOUDEN | `import_open_items_to_tracks.py:201/310/387` gebruikt nog `_parse_pr_number` (geeft None op multi-PR); `_parse_pr_numbers` bestaat al | — |
| OI-1208 | warn | OPEN HOUDEN | geen timeout/receipt op onbeantwoordbare permissie-prompt in scoped worker-mode | — |
| OI-1209 | warn | OPEN HOUDEN | `VNX_WORKER_ROLE` wordt alleen geëxporteerd op `tmux_interactive_dispatch.py:1439`; provider-lane exporteert hem niet | — |
| OI-1210 | warn | OPEN HOUDEN | `Content block not found` niet herleid tot harness vs vertaallaag; glm-harness onbetrouwbaar voor lange instructies | — |

## Sluitredenen (letterlijk overneembaar door T0)

### OI-1150 — panel coverage-teller telt geleverd, niet gestart
`scripts/lib/deliberation_panel.py:134-142` rendert de coverage-regel nu als
`"{present}/{total} lenses present; {failed} failed"` en `:363-369` vult
`failed_seats` via `_is_error(fo["text"])`; een zetel met exit 1 en nul tokens
wordt dus niet meer als "present" geteld maar bij naam genoemd. De teller telt
geleverd, niet gestart.

### OI-1159 — horizon-sluitpad legt nu zichtbaar vast en houdt op open items
`scripts/planning_cli.py:766-777` + `_write_pr_delivery()` retourneren `False`
zonder migratie 0032 en de caller emit een ERROR-regel op stderr plus een
`"error"`-key in `--json` (OI-1167), in plaats van een begraven WARNING.
`scripts/lib/track_reconciler.py:601-610` rekent `_blocking_detail` (blocking_ois
uit `track_open_items`) plus `_delivery_hold` mee in de derived status, zodat
`reconcile --apply` niet langer blind alle CONFIRMED tracks sluit. Beide claims
van het item zijn daarmee weg.

### OI-1172 — headless-daemon valt niet meer terug op __file__-root
`scripts/lib/headless_dispatch_daemon.py:41-55` (`_default_data_dir`) roept nu
`resolve_data_dir_fail_loud()` aan en raiset `DataDirResolutionError` met de
resolutie-keten in plaats van terug te vallen op `_repo_root()/'.vnx-data'`
(=`~/.vnx-system/versions/<v>/.vnx-data` in een central install). Commit
`c3f92f6e` (#1489).

### OI-1177 — integriteitscorrectie: #1481 is daadwerkelijk gemerged
`#1481` ("fix(headless): honor base_ref when creating the dispatch worktree")
staat als commit `65c37756` op `origin/main`. De voorwaarde uit het item
("OI-1171 blijft dicht ZODRA #1481 daadwerkelijk merget") is vervuld; de
sluitreden van OI-1171 klopt nu achteraf.

### OI-1185 — router-terugval is symmetrisch
`scripts/lib/providers/smart_router/tier_routing.py:13-16` documenteert dat een
lane die "missing key, CLI absent, or in cooldown" is, in dezelfde
`fallback`-keten wordt overgeslagen; `:100` implementeert "walk the fallback
chain, skipping unavailable lanes (OI-1185)". Het ontbrekende-sleutel-pad slaat
kimi niet langer over. Commits `2cb18258` (#1484) en `066882aa` (#1494).

### OI-1186 — afkoelperiode is per faalklasse
`scripts/lib/providers/smart_router/availability.py:130`
`cooldown_seconds(failure_class)` delegeert naar
`incident_taxonomy.get_cooldown_seconds`, in plaats van een vaste
`DEFAULT_COOLDOWN_SECONDS=3600`. Commit `066882aa` (#1494).

### OI-1190 — plan-gate-uitkomst heeft een terugverwijzing op de track
`scripts/planning_cli.py:2466-2477` schrijft de `decision_ref` (payload uit
`plan_gate_panel.build_decision_ref`) via `tracks_lib.set_decision_ref` bij de
gate, en `scripts/lib/migrations/apply_0033.py` voegt de kolom
`tracks.decision_ref` toe. Commit `e39a1b6f` (#1493).

### OI-1194 — niet-canonieke modelwaarden zijn opgeruimd
`scripts/lib/providers/model_normalizer.py:16` voegt de alias
`kimi-for-coding -> kimi-k2-7` toe (oorzaak 2), en de prose-afwijzing in de
receipt-converter (`scripts/lib/report_to_receipt_converter.py:76-78`) weigert
instructietekst die als modelnaam werd gelezen (oorzaak 1). Commit `82729192`
(#1497).

### OI-1200 — t0-opus-only test past bij de canonieke registry-sleutel
`tests/test_constraint_enforcer.py:90-93` (`test_t0_with_opus_allowed`) enforcet
nu `model="opus-5"` en slaagt (`1 passed`); de test verwacht niet langer dat
`claude-opus-4-8` is toegestaan. De beslissing is daarmee genomen: 4.8 is geen
geldige T0 meer, `opus-5` wel. Gewijzigd in `45ace207` (#1513).

## WONTFIX-reden (met risico)

### OI-1201 — spawn-wijziging is per constructie niet pre-merge toetsbaar
De dispatcher bouwt de spawn-argv met main-code voordat de worktree (met de
PR-code) bestaat; dit is een structurele eigenschap van de tmux-spawn-architectuur,
geen bug die je even wegpoetst. We accepteren dit bewust en toetsen
spawn-wijzigingen post-merge (OI-1209 legt die regel al vast). **Risico:** een
spawn-PR kan stil breken tot de post-merge meting; daarom geldt dat
constraint-documentatie geen dichtheid claimt vóór die meting, en dat de
post-merge toets verplicht is bij elke spawn-domein-PR.

## Slottelling (som = 55)

| Uitkomst | aantal |
|---|---|
| SLUITEN | 9 |
| UITSTELLEN | 1 |
| WONTFIX | 1 |
| OPEN HOUDEN | 44 |
| ONTOETSBAAR | 0 |
| **totaal** | **55** |

## ONTOETSBAAR-lijst

Geen. Alle 55 items dragen een concrete, controleerbare claim (bestand-plus-regel,
een commit-sha, of een commando dat het patroon meet). Ik heb bij elk item óf de
beschreven code aangetroffen (→ open), óf de fix geverifieerd (→ sluiten). Geen
enkel item hoefde ik op "niet te toetsen" te zetten.

## Methodologische opmerkingen

- **Meet-afbakening:** de full-suite is in deze read-only meetopdracht verboden
  (`pytest tests/`), dus sweep-only flakes (OI-1121/1160/1181/1204) kon ik niet
  herreproduceren; ik heb solo-groen en quarantaine-status gemeten in plaats van
  "bestaat niet meer" te concluderen uit een gerichte run (deelsweep-is-een-andere-omgeving).
- **Twee routers (OI-1193):** de structurele helft (deur op hardgecodeerde
  model-literals, stille None) is opgelost; wat resteert is de stale data in
  `routing_recommendations.yaml` (gedeprecieerde `glm-5` als kandidaat) die router 1
  nog raadpleegt. Daarom OPEN, niet SLUITEN.
- **OI-1200 vs OI-1205:** beide raken #1513 maar verschillend. #1513 zette
  `deprecated-glm-models` om naar allowlist én corrigeerde de t0-opus-test (OI-1200
  sluit). De zuster-constraint `zai-via-openrouter-only` is bewust niet meegenomen
  (OI-1205 blijft open).
- **Telling afgekapt:** geen `| head` gebruikt in een pad dat een aantal oplevert;
  de 55 is een volledige telling uit het hele bestand.
