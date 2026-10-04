## Verification

**Red run** op de oude code (`2ec08f8e`, tijdelijke worktree van HEAD met de nieuwe testbestanden erin):
`python3 -m pytest tests/test_no_end_state_without_outcome.py tests/unit/vnx_cli/commands/test_doctor_ledger_health.py -q -p no:cacheprovider -rf`
→ **16 failed, 15 passed**. Alle 16 falen op gedrag, geen enkele op KeyError, TypeError of ImportError. Fragment:
```
E  AssertionError: assert 'dead_letter' == 'active'            (leeftijdsregel, geen timestamp, failure-receipt)
E  AssertionError: d-evidence is not an open point: []         (evidence-only)
E  AssertionError: d-gate-only is not an open point: []        (receipt zonder eigen uitkomst)
E  AssertionError: assert ('dead_letter', 'dead_letter') == ('active', 'active')   (ADR-007: botsend id, besluit van ander project)
E  AssertionError: assert False  (janitor promoveerde de failure en de receipt van het andere project)
E  AssertionError: assert 'move-to-completed' != 'move-to-completed'  (OI-1907, 2x)
E  AssertionError: assert True is False  (DeliverableAcceptance(acceptable=True, code='no_outcome_receipt')  OI-1917)
E  AssertionError: a dead worker must leave a unified report
E  AssertionError: assert [] == ['20260929-failing-A.md']  (daemon dead-letterde de mislukte delivery)
E  AssertionError: 'no findings could be derived' is contained here: ledger-health beacon reports fail, but no findings could be derived ...
E  AssertionError: assert 'PASS' == 'WARN'  (open_outcomes onmeetbaar)
E  - receipt coverage, open outcomes, and chain status all healthy / + receipt coverage, pull-cursor age, ...
```

**Groene run** (nieuwe code), dezelfde twee bestanden: **31 passed**.

**Buurtests groen** (voorgrond):
`python3 -m pytest tests/test_no_end_state_without_outcome.py tests/unit/vnx_cli/commands/test_doctor_ledger_health.py tests/test_active_drain.py tests/test_active_drain_outcome.py tests/test_open_outcomes.py tests/test_active_dispatch_janitor.py tests/test_dispatch_cleanup.py tests/test_contract_invalid_ledger.py tests/test_contract_invalid_window.py tests/test_crash_recovery_sweep.py tests/test_receipt_outcome*.py tests/test_ledger_health.py tests/test_orphan_sweep.py tests/test_outcome_vocab_sync.py tests/test_manifest_lifecycle.py tests/test_build_t0_state.py tests/test_build_t0_state_central.py tests/test_governance_emit.py tests/test_governance_emit_report_wrapper.py tests/test_receipt_query.py tests/test_t0_index_split.py tests/test_t0_live_work.py tests/test_vnx_doctor_strict.py tests/test_report_body_contract.py tests/test_pr_merge_contract_invalid.py tests/test_t0_role_audit_static.py tests/unit/vnx_cli -q` → **947 passed**.

**Rood vóór en na, niet door deze PR:**
`python3 -m pytest tests/test_dispatch_daemon.py tests/test_dispatcher_drain_lifecycle.py -q` → 5 failed, 24 passed. Dezelfde 5 falen op origin/main (2ec08f8e) en staan in `scripts/ci/test_exclusions.txt:131,133` onder OI-1227. De daemon-tests falen daar omdat de governance-precheck de dispatch blokkeert. Mijn daemon-test patcht die precheck.

**Rol-audit van D10:**
- `python3 scripts/lib/t0_role_sources_audit.py . .` → eerst `SCRIPT-MISSING: DISPATCH_RULES.md names 'subprocess_dispatch_internals/recovery.py'`. Het pad is gecorrigeerd naar `scripts/lib/...`, daarna **exit 0, geen bevindingen**.
- `bash scripts/commands/t0_role_audit.sh --static "$PWD"` → `(clean — no role<->skill invocability drift found …)`, exit 0.
- Rol- en doc-tests: `tests/test_t0_skill_slim.py`, `test_t0_role_audit_static.py`, `test_role_orchestrator_sentinel.py`, `test_role_orchestrator_lane_rule.py`, `test_ci_check_docs_file_line_refs.py`, `test_pr_merge_contract_invalid.py` en `test_role_sync.py` → 210 passed.

**Metingen op vnx-dev (read-only):**
- `open_outcomes.build_open_outcomes(~/.vnx-data/vnx-dev/state, project_id="vnx-dev", limit=10)` → **total 36** (reject 4, investigate 31, unknown 1), `by_kind {receipt_outcome: 36}`, 10 getoond en `more` 26. Op de oude code was dat total 35 (reject 4, investigate 31). `dispatches/active/` op vnx-dev is leeg, dus de nieuwe kind `active_dispatch` telt daar 0.
- `ledger_health.check_open_outcomes` in geheugen, niets geschreven → `finding`: 15 van 36 ouder dan 24h. Eerste ids: `20260929-fsh-d5-live-work-ff2`, `20260929-fsh-d10-rol-audit`, …
- Doctor, alleen `_check_ledger_health(~/.vnx-data/vnx-dev)`:
  - nieuw: `WARN | beacon is stale (108.9h old) — rerun ledger_health.py; 1 dispatch(es) in the register have no matching receipt`
  - oud: dezelfde melding plus `receipt pull cursor is 48.4h old (backlog 487 receipt(s))`

  De beacon op vnx-dev dateert van 25-09 en is dus van vóór D4b1. De nieuwe doctor negeert het dode pull_cursor-veld. Na een verse `ledger_health.py`-run na de 1.7.0-uitrol toont hij de open-outcomes-bevinding hierboven.

**ADR-007** is expliciet getoetst. Er komt geen nieuwe tabel en geen DB-query. Elke lezer scopet de receipts op de project_id van de store (`scope_receipts` / `noise_reason`). T0-besluiten worden per project gelezen (`read_outcome_decisions`). Elke nieuwe test draagt een tweede project met een botsend id dat niets verplaatst, niets promoveert, de merge-poort niet heelt en geen rapport in de store van het andere project schrijft.
