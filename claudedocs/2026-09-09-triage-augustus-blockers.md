# Triage augustus-blockers — Golf C, punt C4

Dispatch: `20260909-golfc-c4-triage-augustus-blockers` · Track: `open-items-backlog-triage` · Gemeten: 2026-09-09, op main `962d57f8`.

## Filter-check

```
python3 -c "... status=='open' and severity=='blocker' and created_at.startswith('2026-08')"
```

Resultaat: **76 items**. Dat is exact de telling uit de opdracht (76 augustus-blockers, van 84 blockers totaal, van 330 open items totaal — alle drie de tellingen kloppen). Geen afwijking te verklaren.

Elke regel hieronder noemt een commit-SHA, een PR-nummer, of een exact commando+uitkomst. Onderzoek is verdeeld over 8 parallelle read-only triage-runs (elk ~10 items), gevolgd door een hertriage van batch 1 (de eerste poging raakte in de war en leverde geen bruikbaar resultaat op — verse poging met dezelfde methode leverde alle 10 alsnog op). Niets in `open_items.json` is aangeraakt.

---

## Gerepareerd door PR (42 items)

| OI | Titel | Oordeel | Bewijs | Aanbevolen actie |
|---|---|---|---|---|
| OI-1259 | 95 PR's gemerged zonder review-gate 12-16 aug, twee oorzaken | gerepareerd door PR #1588 | commit 353b0322: `dispatch_cli.py` verwijdert stille return op leeg `gate`; `pr_merge.py` roept `_run_review_gate` aan v.h. merge-pad (regel 1581-1585). Raakt exact de 3 genoemde bestanden/oorzaken. | sluiten met PR #1588 |
| OI-1287 | Geweigerde receipt boekt dispatch als succes zonder proof chain | gerepareerd door PR #1634 | commit de0a0ba6: `dispatch_envelope.py` degradeert `work_status=="success"` + `receipt_path is None` naar status `receipt_missing`. Test pint dit op 4 plekken. | sluiten met PR #1634 |
| OI-1289 | Rapportcontract belooft OI-grootboek-sync, niets leest de sectie | gerepareerd door PR #1643 | commit 493b94d2: nieuw `open_items_from_report.py::sync_open_items_from_report`, aangeroepen vanuit `report_to_receipt_converter.py:1239-1240`. | sluiten met PR #1643 |
| OI-1382 | 2 van 5 tmux-lane dispatches: geldig rapport+PR, geen receipt | gerepareerd door PR #1635 | commit d6a1ada1: `_fallback_book_ledger_row` draait de generieke converter als vangnet wanneer geen ledger-rij bestaat. | sluiten met PR #1635 |
| OI-1383 | Oorzaak OI-1382: tmux-receipt hangt aan workercommando | gerepareerd door PR #1635 | zelfde commit d6a1ada1 — docstring noemt OI-1383/OI-1382 letterlijk. | sluiten met PR #1635 |
| OI-1384 | Obligation-runner verbrandt verplichtingen onherroepelijk bij poort-uit | gerepareerd door PR #1633 (+ #1672) | commit 57cf12ec: `_TEMPORARY_NOT_EXECUTABLE_REASONS` behandelt `provider_disabled` als tijdelijk. Vervolg db987b76 dekt restvorm. | sluiten met PR #1633 en #1672 |
| OI-1387 | Merge-gate en review-gate lezen andere bron, spreken elkaar tegen | gerepareerd door PR #1673 | commit 6b5e10a6: query nu `gh run list --commit` i.p.v. branch-gescoopt. Follow-up #1746 verfijnt verder. | sluiten met PR #1673 |
| OI-1390 | dispatch-agent kent geen --base-ref/--work-ref/--pr-id | gerepareerd door PR #1642 | commit 19121abb: drie vlaggen toegevoegd aan subparser en doorgegeven aan `deliver_via_door(...)`. | sluiten met PR #1642 |
| OI-1392 | WeesPR-regressie uit #1623: work_ref bereikt pr_enforcement niet | gerepareerd door PR #1640 | commit 445c5a3c: `_enforce_pr_exists_for_work_ref` maakt work_ref autoritatief, opent nooit een tweede PR. | sluiten met PR #1640 |
| OI-1394 | Merge-gate telt rauwe codex-toolstream als blocking finding | gerepareerd door PR #1636 | commit 1936812c: scant nu alleen genormaliseerde `## Findings`-sectie. Regressietest pint alle 3 bewijsstukken. | sluiten met PR #1636 |
| OI-1399 | pr_merge geeft exit 0 terwijl de merge mislukte | gerepareerd door PR #1660 | commit 7c933b30: `result["success"]=False` blijft False bij mislukte merge, exit-logica geeft `EXIT_ERROR`. | sluiten met PR #1660 |
| OI-1400 | Gate-uitval boekt verplichting als fulfilled met leeg bewijs | gerepareerd door PR #1641 (+residu #1672) | commit 07c217e4: `unavailable` behandeld als tijdelijk, niet terminal. Vervolg db987b76 dekt `provider_not_installed`. | sluiten met PR #1641 en #1672 |
| OI-1403 | Test vervuilt process-wide state zonder teardown | gerepareerd door PR #1644 | commit 06179237: contextmanager herstelt attributen in `finally`. Let op: `pr_id=1643` in het record is de MELDENDE PR, niet de fix. | sluiten met PR #1644, niet #1643 |
| OI-1414 | 21 permissie-escalaties pending sinds 2 augustus, T0 ziet ze niet | gerepareerd door PR #1658 | commit 4da9c98c: schema 2.2 sectie `permission_escalations`. Live bevestigd op `t0_state.json`: `pending_count=22`. | sluiten met PR #1658 |
| OI-1417 | Concurrency-lock dekt headless-lane niet | gerepareerd door PR #1657 | commit 02796887: `serialization_class` niet meer uitgezonderd voor `claude_headless`. | sluiten met PR #1657 |
| OI-1422 | Panelzetels task_class=implementation, kimi-fabrication-guard fout gestempeld | gerepareerd door PR #1659 | commit 9474e84f: `--task-class research_structured` toegevoegd op beide dispatcher-paden (regel 1239-1243, 1395-1413). | sluiten met PR #1659 |
| OI-1424 | Orphan-sweep per project, tmux-lijst accountbreed | gerepareerd door PR #1690 | commit 1fdc3d99: account-brede owner-lookup, valt terug op elk ander project-register. | sluiten met PR #1690 |
| OI-1433 | Lane-uitputting (402) niet te onderscheiden van inhoudelijk oordeel | gerepareerd door PR #1668 | commit 192af8a5: leest conversation-log bij lege response, classificeert naar `lane_exhausted`/`unreadable_verdict`/`no_response`. | sluiten met PR #1668 |
| OI-1439 | kimi_gate/glm_gate gooien voltooide reviews weg | gerepareerd door PR #1681 | commits cf27f3c9+df9244e9: eigen `role="review-gate"` + `gate_report_recovery.py`. Let op: `pr_id=1674` was de MEETENDE PR, fix landde een dag later. | sluiten met PR #1681, niet #1674 |
| OI-1462 | Gate-eis en -vervulling draaien in verschillende omgevingen, geen test | gerepareerd door PR #1694 | commit fb0acb44: `_check_ci_gate_requirement_mismatch()` vergelijkt eiser tegen vervuller. `test_gate_stack_cross_process_contract.py` → 2 passed. | sluiten met PR #1694 |
| OI-1475 | close --evidence wordt geaccepteerd en nooit weggeschreven | gerepareerd (vóór het item bestond) door PR #1665 | commit b9210e37, 4 dagen VÓÓR dit item werd aangemaakt, voegde de schrijfregel al toe. Item's eigen premisse (functienaam `cmd_close`) bestond nooit. | sluiten met PR #1665; item was al verholpen bij aanmaak |
| OI-1485 | Onderzoeksdiepte codex_gate varieert 0-32 shell-aanroepen | gerepareerd door PR #1712 | commit d800f56f: `gate_depth.py` met `MIN_INVESTIGATIVE_ACTIONS`. 17 tests passed. Later fleet-wide uitgebreid door #1800. | sluiten met PR #1712 |
| OI-1486 | gate_recorder deelt vaste tmp-naam tussen gelijktijdige schrijvers | gerepareerd door PR #1710 | commit 59694f2e: delegeert naar `atomic_io.atomic_write_json` (unieke tmp-naam + flock). 6 tests passed. | sluiten met PR #1710 |
| OI-1488 | request-and-execute geeft verdict van andere sha terug | gerepareerd door PR #1714 | commit c91a4a85: `_classify_sha_binding` zet `decided_pass=False` bij mismatch, `annotate_refused_write()`. 10 tests passed. | sluiten met PR #1714 |
| OI-1501 | Ondergrens onderzoeksdiepte dekt alleen codex_gate | gerepareerd door PR #1800 | commit 29e607da: `single_shot_depth()` voor glm/kimi_gate. Titel citeert OI-1618, inhoud is exact de gevraagde reparatie. | sluiten met PR #1800 |
| OI-1510 | gate-obligation-runner vast launchd-label, projecten overschrijven elkaar | gerepareerd door PR #1769 | commit d3944a52: Label per-project. Live bevestigd: twee losse labels naast elkaar in `launchctl list`. | sluiten met PR #1769 |
| OI-1511 | system_health=healthy naast beacon_health=fail | gerepareerd door PR #1732 | commit 86df44c3: `worst_status()` aggregeert. Live vandaag: `system_health.status: "degraded"` correct naast `beacon_health.overall: "fail"`. | sluiten met PR #1732 |
| OI-1512 | t0_state.json 22 dagen bevroren, drie onafhankelijke oorzaken | gerepareerd door PR #1732 + #1816 (+ #1760) | 86df44c3 (Poort A + redirect-append), 58330dd4/#1816 (Poort B, git-toplevel-fallback), c1a643e6/#1760 (staleness-refusal). Live: t0_state.json vandaag vers gegenereerd. | sluiten met PR #1732, #1760, #1816 |
| OI-1514 | Operator-uitzondering 29-08: 12 PR's buiten de deur om | gerepareerd (acceptatietest gehaald) | Item's eigen sluitcriterium gehaald: PR #1828 en #1829 (huidige HEAD van main) liepen door de deur met verplichting+poortresultaat+merge-receipt, zonder handmatige stap. | sluiten, acceptatietest gehaald |
| OI-1516 | SessionStart-hook kent centrale store niet | gerepareerd door PR #1731 | commit 355acc16: hook resolvet via `vnx_paths.resolve_paths()` (ADR-026), luide fallback i.p.v. stille lege sectie. | sluiten met PR #1731 |
| OI-1518 | pr_merge zet success=True vóór receipt-emissie | gerepareerd door PR #1722 | commit e9693592: apart `receipt_ok`-veld, exit alleen OK bij succes ÉN receipt. | sluiten met PR #1722 |
| OI-1519 | Panel telt getimede-out zetel als aanwezig via eigen exit_code | gerepareerd door PR #1723 | commit 0d510576: `_measure_seat` reconcilieert tegen `_ReceiptLedger`, ledger wint bij afwijking. | sluiten met PR #1723 |
| OI-1520 | Plist-template gate-obligation-runner is ongeldige XML | gerepareerd door PR #1763 (getrackt onder OI-1621) | commit e2b8e9f7 herschrijft de dubbele-min-in-comment. Alle 8 plist-bestanden nu geldig geparsed. Fix landde onder een ander OI-nummer. | sluiten; noteer OI-nummer-mismatch in het grootboek |
| OI-1532 | Verplichtingsrunner boekt levende dispatch af als 'nooit gereviewd' | gerepareerd door PR #1729 | commit 3600279f: `dispatch_live`-discriminator splitst branch_exists=False in drie takken; levend wordt nooit meer geretired. | sluiten met PR #1729 (plus OI-1587-verfijning) |
| OI-1534 | Volgorde-eis op OI-1532 + spiegelbeeld onbegrensd | technisch gerepareerd door PR #1729; sequencing-premisse ingetrokken door OI-1536 | Zelfde #1729-fix begrenst ook branch_exists=True/None-tak. De volgorde-eis erboven is apart ingetrokken (zie OI-1536). Dubbel achterhaald. | sluiten met PR #1729 |
| OI-1536 | Corrigeert OI-1534: retire-tak alleen in bron, niet in installatie | gerepareerd door PR #1729 (blokkerende premisse vervallen) | Herziene embargo-voorwaarde ("wacht tot OI-1532 gedicht is") is voldaan. Launchd-agent draait vandaag op eigen repo-bron met correcte retirements. | sluiten, embargo vervuld door PR #1729 |
| OI-1540 | Plan-poort verliest leesbare oordelen aan bekend defect (fence) | gerepareerd door PR #1799 | commit 1ffe8e82: `SEAT_PROSE_NO_FENCE` + `_reextract_verdict()`. Andere route dan gesuggereerd (reextractie i.p.v. bestandssysteem-recovery), zelfde gemeten defect gedicht. | sluiten met PR #1799 |
| OI-1558 | Verplichting volgt takeover-keten niet | gerepareerd door PR #1730 | commit 55d6e61b: `_build_review_gate_takeover_chain` geïmporteerd en doorlopen, stempelt `resolved_by_gate`/`takeover_hops`. `pr_id=1726` in het record is de destijds geblokkeerde PR, niet de fix. | sluiten met PR #1730, niet #1726 |
| OI-1561 | Daemon-liveness-detector meldt 'ok' bij leeg register | gerepareerd | commit 86df44c3/b54e0c11: lege parse geeft nu `ValueError` i.p.v. stille lege tuple; caller geeft `"unknown"`, nooit `"ok"`. | sluiten met PR #1732 |
| OI-1564 | Overnameketen kan tijdelijke quota-uitval niet opnieuw proberen | gerepareerd | commit 0faeccce/#1740: TTL-gate (3600s) op `lane_exhausted`-records. Verscherpt door #1808. `pr_id=1729` was de destijds geblokkeerde PR, niet de fix. | sluiten met PR #1740 (aangevuld door #1808), niet #1729 |
| OI-1576 | Overnameketen levert geldig bewijs op kop die merge-deur niet ziet | gerepareerd | commit fc5ff1e7/#1741: `_find_takeover_successor_results()` valt terug op opvolger-record. `pr_id=1726` was de destijds geblokkeerde PR. | sluiten met PR #1741, niet #1726 |
| OI-1580 | PR 1729 rebase-conflict is ontwerpsamenvoeging, geen mechanisch conflict | gerepareerd | commit 3600279f/#1729: exacte combinatietabel uit het item geïmplementeerd (levendheid × bewijs → vervullen/pending/retire). | sluiten met PR #1729 |

## Niet meer meetbaar (3 items)

| OI | Titel | Oordeel | Bewijs | Aanbevolen actie |
|---|---|---|---|---|
| OI-1300 | test pint OI-1287-defect vast op verkeerde laag | niet meer meetbaar | De echte OI-1287-fix (PR #1634) raakt de gevreesde `_govern`-raise niet — hij degradeert een laag hoger, met eigen testdekking. De gevreesde blokkade is nooit opgetreden. | sluiten, premisse vervallen |
| OI-1423 | Opus-panelzetel crasht op gepinde install (tmux-pad) | niet meer meetbaar | Sinds PR #1803 routeert een opus-panelzetel via `_run_claude_headless_seat`, niet meer via het aangewezen `tmux_interactive_dispatch.py`-mechanisme. Geen enkele referentie naar OI-1423 in de repo. | sluiten, panel-opus-zetel routeert sinds #1803 niet meer via dit pad |
| OI-1549 | Eis: droge run vóór aanzetten verplichtingenrunner (68/210-splitsing) | niet meer meetbaar | De precieze precondiie (278 wezen, 210 zou retiren) is door reeds uitgevoerde verwerking ingehaald: vandaag 717 records, 22 op attempts=0. Je kunt een moment dat al voorbij is niet meer droog draaien. | sluiten als niet-meetbaar, precondiie ingehaald |

## Nog waar (31 items)

| OI | Titel | Oordeel | Bewijs | Aanbevolen actie |
|---|---|---|---|---|
| OI-1276 | Datadir-fix OI-1179 half toegepast | nog waar | Gereproduceerd: bare `VNX_DATA_DIR` zonder expliciete vlag wordt geëerd door `event_store` maar genegeerd (met DeprecationWarning) door `vnx_paths.resolve_paths()`. | openhouden, asymmetrie live bevestigd |
| OI-1302 | test_outcome_vocab_sync: canonical reference is zelf verouderde kopie | nog waar | `event_outcome_semantics.FAILURE_STATUSES` gemuteerd → `check_active_drain.FAILURE_STATUSES` (hardcoded) blijft ongewijzigd, test slaagt toch (5 passed). | openhouden, drift-gat live bevestigd |
| OI-1337 | Default build-lane wijst naar kimi zonder cooldown-check | nog waar | `routing_policy.yaml:7` heeft nog `default_lane: "kimi"`, `resolve_lane` checkt geen cooldown/health. Geen OI-1337-referentie in de boom. | openhouden, structureel gat bevestigd |
| OI-1411 | Wiring-gate merkt testklassen aan als unwired | nog waar | Live gereproduceerd: geen uitsluiting voor testklassen in `_extract_new_public_defs`/`_extract_defs_via_ast`/`_extract_defs_via_regex`. Gate staat wel op advisory, dus geen actieve blokkade vandaag. | openhouden, blokkeert unparken van de gate |
| OI-1420 | 3126 testfuncties buiten CI, footer belooft volle sweep | nog waar | PR #1661 reduceerde 136→121 bestanden (2716 testfuncties resterend), maar rol-footer belooft nog steeds volle sweep. | openhouden, kern gereduceerd maar niet gesloten |
| OI-1436 | 20 unified_reports zonder receipt | nog waar | `panel-`/`worktree-release-`-prefixen permanent geclassificeerd als `non_dispatch_tool_output`. Live: recente panel- en worktree-release-bestanden missen nog altijd een receipt. | openhouden |
| OI-1438 | Rol schrijft 7 sluitingsinvarianten voor, code dwingt er 6 af | nog waar | `_find_gate_request_payload` wordt alleen aangeroepen vanuit de claude/github-tak, niet vanuit codex/gemini/ci_gate. Geen commit sinds 23-08 dicht dit. | openhouden |
| OI-1447 | Worktree-isolatie dekt git stash niet | nog waar | Live: 66 worktrees delen één stash-ref. Geen `git stash drop/clear` in de deny-patterns van enig workerprofiel. | openhouden |
| OI-1448 | kimi_gate/glm_gate emitteren nul review_gate-receipts met een echt verdict | nog waar | Wel `status="requested"`-receipts sinds de takeover-chain-feature, maar geen enkele met status pass/fail/completed. Mutatie blijft in-place. | openhouden |
| OI-1451 | API-sleutels staan voluit in Claude Code transcript-bestanden | nog waar | Live scan vandaag: 4 bestanden dragen nog een echte OpenRouter-sleutel in plaintext. Buiten repo-scope. | openhouden; sleutel roteren + transcripts scrubben is operator-actie |
| OI-1454 | is_available() meet aanwezigheid niet bereikbaarheid | nog waar | 3 van 4 adapters doen nog alleen `shutil.which`. Latere PR (#1765) noemt dit expliciet buiten eigen scope. | openhouden |
| OI-1455 | kimi_gate/glm_gate blokkeren zelden bij severity=warning | nog waar | Gates gaten nog steeds alleen op error/blocked/blocker. Operator-beslissing over de severity-ladder ontbreekt. | openhouden |
| OI-1456 | OI-1384-fix dekt config_runtime- vs os.environ-pad niet | nog waar | Live gereproduceerd met exact dezelfde monkeypatch-methode als het item: DB=0/env=1 geeft nog `provider_not_configured`, niet in de tijdelijke-redenen-set. | openhouden |
| OI-1457 | Geen toets tussen draaiende glm-proxy en enforcer-allowlist | nog waar | Beide voorgestelde controles (`constraint_runtime_mismatch`, model-vergelijking) ontbreken nog volledig. | openhouden |
| OI-1502 | 23 provider-tests falen NotADirectoryError op .git/worktrees | nog waar | Live: nu 50 failed (was 23) met dezelfde `NotADirectoryError` in `dispatch_worktree_isolation.py:491`. | openhouden, defect verergerd qua aantal, mechanisme ongewijzigd |
| OI-1504 | Poort stempelt commit_sha op aanvraagtijd, hertoetst niet | nog waar | PR #1714 dempt alleen (mismatch blokkeert bij MERGE), maar herverificatie-bij-afronding zelf is niet gebouwd — exact de indamming die het item al beschrijft. | openhouden, kernrepararatie ontbreekt |
| OI-1506 | glm-5.2 staat 55% te laag geprijsd in het register | nog waar | Registerwaarde ongewijzigd. Live herbevestiging via OpenRouter toont een DERDE, weer afwijkend prijspunt — het register beweegt sowieso niet mee. | openhouden |
| OI-1509 | vnx-dev heeft geen receipt-processor en geen gate-obligation-runner | nog waar (half ingelopen) | gate-obligation-runner-deel is klaar (PR #1769, live bevestigd via launchctl). receipt-processor-plist bestaat in repo maar is nooit geïnstalleerd. | openhouden voor het receipt-processor-deel |
| OI-1517 | Horizon drift-signaal is 96% ruis | nog waar | Live vandaag: 88 van 96 divergenties (91.7%) zijn dezelfde structurele plan-gate-reden — verhouding zelfs iets hoger dan de gemeten 81/84. | openhouden |
| OI-1530 | Skills verschillen tussen skills/ en .claude/skills/, hook-drift | nog waar | 22 gedeelde skills, 0 identiek, zelfde deltas als 30-08. Hook-drift VERERGERD: bron gegroeid naar 21511 bytes, draaiende kopie ongewijzigd op 7296 bytes. | openhouden, verergerd |
| OI-1533 | 366/463 pending poortverplichtingen zonder pr_number/branch/pad | nog waar (vlootbreed voor consumer-projecten) | vnx-dev grotendeels ingehaald door OI-1532-fix (47 pending, was 366). seocrawler-v2/sales-copilot/mission-control staan nog op 100% orphan — consumer-projecten hebben geen branches, runner-pass helpt daar niet. | openhouden voor consumer-projecten |
| OI-1535 | Geïnstalleerde fabric draagt OI-1518/1519-fixes niet | nog waar | `~/.vnx-system/current` wijst nog naar v1.6.0 (gebouwd 20-08). Geen van de latere fixes (30-08 t/m 04-09) zit in de installatie. | openhouden, `vnx update` niet uitgevoerd sinds 20-08 |
| OI-1542 | glm-litellm-proxy is SPOF zonder herstartpad | nog waar | Geen plist/supervisor gecommit. Huidig proces draait toevallig weer op de juiste config na een handmatige herstart — geen gecodeerde fix. | openhouden |
| OI-1544 | Twee receipts spreken elkaar tegen over welke provider draaide | nog waar | Grondoorzaak (OI-1546/1547) niet gedicht; 674 van 675 route-decisions dragen nog de "verkeerde" vorm. Geen mismatch in laatste 3 dagen bewijst niet dat het mechanisme werkt. | openhouden, grondoorzaak niet gedicht |
| OI-1546 | Converter leest selected_model op topniveau, route-besluiten nesten dit | nog waar | Identieke reproductie: 674/675 bestanden dragen de geneste vorm (was 518/519 op 30-08 — patroon stabiel, niet gerepareerd). | openhouden |
| OI-1547 | Twee schrijvers naar hetzelfde pad, onverenigbare schema's | nog waar | Beide schrijvers ongewijzigd bevestigd. Geëiste derde tak ("geen route-besluit") niet gebouwd. | openhouden |
| OI-1550 | Codex-storing beschermt retire-kandidaten niet, decision valt vóór poortaanroep | nog waar | Structurele volgorde (`_pre_execution_decision` vóór `request_and_execute`) reproduceert ongewijzigd. Geen mens-bevestigingsstap toegevoegd. | openhouden, structureel risico blijft |
| OI-1553 | Orchestrator valt stil terwijl proces leeft, geen detector | nog waar | `DAEMON_LIVENESS.md` telt 9 achtergronddaemons, geen enkele is een interactieve T0-sessie. Geen "stall"-detector toegevoegd. | openhouden |
| OI-1555 | PR #1731 repareert bron-hook, draaiende sessies lezen kopie van 31 juli | nog waar | 10 dagen na de merge: uitgerolde kopie nog 7296 bytes/31 juli, bron nu 21511 bytes/6 september. `.claude/hooks/` is gitignored — vraagt `vnx init` of symlink, geen van beide gebeurd. | openhouden, uitrolstap nog niet uitgevoerd |
| OI-1563 | Dispatch met twee PR's krijgt verplichting voor maar één | nog waar | `register_obligation()` registreert nog altijd één verplichting per dispatch_id met enkelvoudige pr_number. Geen multi-PR-mechanisme toegevoegd. | openhouden, structureel |
| OI-1572 | PR-base wees naar voorganger-branch, pr_merge weigert geen base-mismatch | nog waar (structureel deel) | Geen guard in `pr_merge.py` die baseRefName tegen main vergelijkt. De specifieke casus (#1737) is wel via work-around herstoken (re-cut PR #1739 vanaf main). | openhouden voor het structurele acceptatiecriterium; casus zelf al opgelost |

---

## Opvallende patronen

- **Het pr_id-veld op het item is herhaaldelijk NIET de fix-PR.** Zes keer gemeten: OI-1403 (1643 = meldende PR, fix in #1644), OI-1439 (1674 = metende PR, fix in #1681), OI-1520 (fix landde onder OI-1621, niet dit nummer), OI-1558 (1726 = destijds geblokkeerde PR, fix in #1730), OI-1564 (1729 = destijds geblokkeerde PR, fix in #1740), OI-1576 (1726 = destijds geblokkeerde PR, fix in #1741). Dit is precies de OI-1631/PR-1780-val uit de opdracht, en hij komt vaker voor dan het ene bekende voorbeeld deed vermoeden.
- **Item-ID's zijn historisch niet collision-vrij.** `git log --grep` op OI-1424/1438/1448 raakte ook ongerelateerde commits uit een cyclus van 2026-05-15 die dezelfde nummers voor ander werk gebruikte. Verificatie liep op INHOUD, niet op ID-match, dus geen fout hierdoor te verwachten in deze triage — wel iets voor T0 om te weten bij toekomstige `git log --grep`-zoekopdrachten.
- **"Nog waar" is niet altijd stilstand.** OI-1420 (136→121 bestanden), OI-1502 (23→50 failures), OI-1517 (81/84→88/96), OI-1533 (366→174 wezen fleet-wide) en OI-1546 (518/519→674/675) zijn allemaal bewogen sinds de oorspronkelijke meting — drie kanten op verbeterd, twee kanten op verergerd, geen enkele gesloten.
- **Twee items zijn technisch gedicht maar de premisse erboven is apart herroepen** (OI-1534, OI-1536) — beide door dezelfde PR #1729, via een aparte correctie op de sequencing-eis.

## Telling per oordeel

| Oordeel | Aantal |
|---|---|
| Gerepareerd door PR | 42 |
| Niet meer meetbaar | 3 |
| Nog waar | 31 |
| **Totaal** | **76** |
