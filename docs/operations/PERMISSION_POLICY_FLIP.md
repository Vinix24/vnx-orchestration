# PERMISSION_POLICY_FLIP.md: handhaving van de permissiepolicy aanzetten

Operator-runbook van track `provider-agnostic-permissions` (P8, ADR-057, besluit B7). `VNX_ENFORCE_WORKER_PERMISSIONS` staat standaard uit en geen enkel codepad zet hem aan. Het rapport `scripts/permission_posture_report.py` zegt of aanzetten veilig is. Het voert de stap niet uit.

## Eerste voorwaarde: OI-2661 is dicht

De fire-time `Reject("permission-policy-mismatch")` in `scripts/lib/dispatch_cli.py` moet een receipt schrijven via `emit_governance_receipt` voordat `bridge_dispatch` de bundel naar `failed/` verplaatst (codex-waarschuwing op #2533). Zolang dat niet zo is, laat een geweigerde dispatch geen spoor na in het grootboek.

Controleer: `python3 scripts/open_items_manager.py list` en zoek `OI-2661`. Staat hij open, dan wacht de flip. Alle stappen hieronder komen daarna.

## Het rapport lezen

```
python3 scripts/permission_posture_report.py            # tekst
python3 scripts/permission_posture_report.py --json --out /tmp/posture.json
```

Opties: `--state-dir` (standaard de opgeloste `VNX_STATE_DIR`), `--project-root`, `--project-id`, `--min-consecutive` (N, standaard 20). Het script leest en schrijft niets in de store. Exitcode 0 is `flip-allowed`, 1 is `blocked`, 2 is een bron die niet te lezen was (kapotte record, YAML of receipt-regel, ontbrekende trackdatabase). Exit 2 is geen pass.

Per rijstrook (zes, uit de capaciteitsmatrix) staat per capability `supported`, `degraded` of `unsupported`, het aantal records en de digest-status van het laatste record.

## De vijf voorwaarden

Elke voorwaarde is `pass`, `fail` of `unmeasured`. Alleen vijf keer `pass` geeft `flip-allowed`. `unmeasured` (er is niets om op te beslissen) telt als niet gehaald.

| | Voorwaarde | Bewijs |
|---|---|---|
| a | Per rijstrook een groene kwalificatie | Het laatste record in `state/permission_records/` van P3 en P6 bestaat en is gelijk aan de matrix. Een record van een onbekende rijstrook is `fail`. |
| b | Nul fallback-rollen | Elke rol uit `.vnx/worker_permissions.yaml` en `agents/` wordt door `compose_policy` gehaald. Eén `is_fallback=True` is `fail`. |
| c | Nul `unsupported` restricties die een actieve policy eist | Per rol en rijstrook: eist de policy een dimensie (fs lezen/schrijven/deny, tools allow/deny, mcp, netwerk) die de rijstrook niet kan uitdrukken. Met de matrix van vandaag blijft dit `fail` voor kimi en codex: handhaving zou die bouwen weigeren (B3). |
| d | N opeenvolgende dispatches met `permission_policy: match` | De staart van `permission_policy_checked` in `state/t0_receipts.ndjson` (P7). Telt alleen `outcome == match` met `check_status == GO` en een dispatch-id. Elk ander of ontbrekend veld breekt de reeks. |
| e | Expliciet besluit van Vincent | Sleutel `flip_decision` in `metadata_json` van de track, zie hieronder. Het script schrijft die sleutel nooit. |

## Aanzetten

1. Draai het rapport. Alles moet `pass` zijn, ook (c). Is dat niet zo, dan verandert het rapport niets en wacht de flip.
2. Vincent beslist. T0 legt dat vast in de track: `flip_decision` = `{"decision": "flip-approved", "decided_by": "Vincent", "decided_at": "<UTC-tijdstip>"}`. Zonder dit besluit blijft (e) `fail`.
3. Draai het rapport opnieuw: exit 0, `verdict: flip-allowed`. Bewaar de uitvoer met `--out` bij het besluit.
4. Zet `VNX_ENFORCE_WORKER_PERMISSIONS=1` in de omgeving van de dispatcher en de workers, op de plek waar de andere `VNX_*`-variabelen staan, en herstart de daemons. Dit is een handmatige stap van de operator.
5. Meet na: `python3 -c "from worker_permissions import worker_permission_enforcement_enabled as f; print(f())"` vanuit `scripts/lib` geeft `True`, en de eerstvolgende merge-gate-receipt heeft `enforcement: on`.

## Terugdraaien

1. Haal `VNX_ENFORCE_WORKER_PERMISSIONS` weg uit die omgeving (ongezet is uit) en herstart de daemons.
2. Meet: dezelfde `python3 -c`-regel geeft `False`.
3. Verwijder of overschrijf `flip_decision` in de track alleen met een nieuw besluit van Vincent. Het oude besluit blijft anders staan en lijkt na een nieuwe meting nog te gelden.
