# Laya spike — gestratificeerde steekproef (n=60)

Dispatch-ID: **20260922-laya-ronde2-taalconfound**

Gestratificeerd op de td_en-voorspelling (15 per klasse, label-taal en). Vul per
bevinding het `WAAR LABEL:` in met een van: `stijl`, `correctheid`,
`beveiliging`, `beleid` (Nederlandse labels, ongeacht de run-taal).
De operator of T0 vult dit in, niet de bouwer.

---

### 1. `1814#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 23  |  **~Tokens:** 29

**Bericht:**

> De review-CLI-subcommando stelt geen --project-id beschikbaar, dus project_id is altijd None; consistent met de per-gate-publicatie maar de docstring-waarschuwing over scope blijft van kracht.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `beveiliging` (confidence 0.6982)

**Laya td_nl:** `correctheid` (confidence 0.0234)

**Laya ane_en:** `correctness` (confidence 0.6934)

**Laya td_en:** `style` (confidence 0.0347)

WAAR LABEL:

---

### 2. `1790#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 44  |  **~Tokens:** 57

**Bericht:**

> test_govern_synthesized_pr_section_passes_validate_body (test_dispatch_govern.py:863) name/docstring claim the synthesized body 'passes validate_body(pr_id=...)', which is intentionally no longer true post-ADR (the ## Response shape fails validate_body by design). The test body only asserts contract_status=='synthesized', so it stays green, but the name is now misleading and should be renamed/updated.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0013)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `style` (confidence 0.0273)

WAAR LABEL:

---

### 3. `1808#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 29  |  **~Tokens:** 37

**Bericht:**

> ADR-005: `scripts/lib/forge_check_run.py::publish_check_run` performs the GitHub check-run state mutation at `+    status, response_body = _api_request(`, but this diff adds no `.vnx-data/events/` ledger entry or `gate_recorder.py` persistence around that publication path.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0083)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `style` (confidence 0.0124)

WAAR LABEL:

---

### 4. `1471#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 26  |  **~Tokens:** 33

**Bericht:**

> `check_pull_cursor` exceeds the 70 executable-line threshold; the oversized function starts at `+def check_pull_cursor(` and spans about 92 executable lines after excluding blanks, comments, and its docstring.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `stijl` (confidence 0.7327)

**Laya td_nl:** `beleid` (confidence 0.0387)

**Laya ane_en:** `correctness` (confidence 0.8474)

**Laya td_en:** `style` (confidence 0.0600)

WAAR LABEL:

---

### 5. `1288#advisory#3`

**Severity:** warning (advisory)  |  **Woorden:** 23  |  **~Tokens:** 29

**Bericht:**

> Function size: `emit_unified_report` crosses the 70-executable-line gate with the new partial-preservation branch beginning at `+        if preserve_partial:`; it is now 79 executable lines.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `correctheid` (confidence 0.5118)

**Laya td_nl:** `beleid` (confidence 0.0131)

**Laya ane_en:** `correctness` (confidence 0.8224)

**Laya td_en:** `style` (confidence 0.0044)

WAAR LABEL:

---

### 6. `1718#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 32  |  **~Tokens:** 41

**Bericht:**

> De lokale import `from providers.provider_registry import load` in _output_cost (tier_routing.py) mist de `# noqa: PLC0415` die de andere twee lokale imports in deze module consistent dragen. Puur een stijl-inconsistentie, geen functionele impact.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `stijl` (confidence 0.2750)

**Laya td_nl:** `stijl` (confidence 0.0548)

**Laya ane_en:** `correctness` (confidence 0.5830)

**Laya td_en:** `style` (confidence 0.0216)

WAAR LABEL:

---

### 7. `1468#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 19  |  **~Tokens:** 24

**Bericht:**

> Function size threshold exceeded in scripts/pre_merge_gate.py: added line `+    parser.add_argument(` is inside `main`, which now exceeds 70 executable lines.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `correctheid` (confidence 0.2013)

**Laya td_nl:** `beleid` (confidence 0.0197)

**Laya ane_en:** `correctness` (confidence 0.9014)

**Laya td_en:** `style` (confidence 0.0228)

WAAR LABEL:

---

### 8. `1846#advisory#5`

**Severity:** warning (advisory)  |  **Woorden:** 9  |  **~Tokens:** 11

**Bericht:**

> instruction-like text in diff at line 87 (pattern: verdict-value-dictation)

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.1709)

**Laya td_nl:** `correctheid` (confidence 0.0111)

**Laya ane_en:** `correctness` (confidence 0.6551)

**Laya td_en:** `style` (confidence 0.0292)

WAAR LABEL:

---

### 9. `1232#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 15  |  **~Tokens:** 19

**Bericht:**

> _load_receipt_roles loads the whole append-only t0_receipts.ndjson into memory; prefer streaming iteration for large receipt files.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `stijl` (confidence 0.3002)

**Laya td_nl:** `correctheid` (confidence 0.0037)

**Laya ane_en:** `correctness` (confidence 0.3738)

**Laya td_en:** `style` (confidence 0.0048)

WAAR LABEL:

---

### 10. `1709#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 31  |  **~Tokens:** 40

**Bericht:**

> scripts/lib/providers/smart_router/staging.py:106-109 dispatch_group_key docstring ('the real door call site does not (yet) pass it') becomes stale on merge — the PR fixes the twin stale claim in apply_door_route but misses this one.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `beleid` (confidence 0.0844)

**Laya td_nl:** `stijl` (confidence 0.0084)

**Laya ane_en:** `correctness` (confidence 0.7925)

**Laya td_en:** `style` (confidence 0.0119)

WAAR LABEL:

---

### 11. `1232#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 30  |  **~Tokens:** 39

**Bericht:**

> A genuinely instruction-declared 'Role: backend-developer' is extracted then normalized to NULL by upsert — the fake-default contract makes a real backend-developer identity unrepresentable via this lane. Deliberate, but worth documenting.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.2498)

**Laya td_nl:** `beleid` (confidence 0.0174)

**Laya ane_en:** `correctness` (confidence 0.6408)

**Laya td_en:** `style` (confidence 0.0136)

WAAR LABEL:

---

### 12. `1425#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 17  |  **~Tokens:** 22

**Bericht:**

> Function size: `release_locked_worktrees` exceeds the 70 executable-line threshold; cited new line inside the oversized function: `+def release_locked_worktrees(`.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `beveiliging` (confidence 0.2789)

**Laya td_nl:** `beleid` (confidence 0.0048)

**Laya ane_en:** `security` (confidence 0.4114)

**Laya td_en:** `style` (confidence 0.0135)

WAAR LABEL:

---

### 13. `1881#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 18  |  **~Tokens:** 23

**Bericht:**

> Unused `pytest` import causes `ruff check` on the touched files to fail; remove the import or use it.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `correctheid` (confidence 0.3178)

**Laya td_nl:** `stijl` (confidence 0.0063)

**Laya ane_en:** `correctness` (confidence 0.8967)

**Laya td_en:** `style` (confidence 0.0346)

WAAR LABEL:

---

### 14. `1797#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 26  |  **~Tokens:** 33

**Bericht:**

> gate_runner (codex/gemini) merged scan-findings niet zelf in een record; ze bereiken het record via het model-verdict. Zwakkere garantie dan glm/kimi, expliciet becommentarieerd, nog steeds een verbetering.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `stijl` (confidence 0.5487)

**Laya td_nl:** `beleid` (confidence 0.0104)

**Laya ane_en:** `correctness` (confidence 0.6366)

**Laya td_en:** `style` (confidence 0.0177)

WAAR LABEL:

---

### 15. `1771#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 33  |  **~Tokens:** 42

**Bericht:**

> codex_spawn.py (_drain_stream): first-error-wins capture means a preceding malformed-line parse error (JSONDecodeError reason text) shadows the real terminal task_complete quota message, reintroducing the no_verdict/unknown misclassification OI-1628 fixes whenever the stream is even slightly messy.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0172)

**Laya ane_en:** `correctness` (confidence 0.6536)

**Laya td_en:** `style` (confidence 0.0167)

WAAR LABEL:

---

### 16. `1797#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 22  |  **~Tokens:** 28

**Bericht:**

> build_review_prompt en glm_gate/kimi_gate.main draaien scan_diff_for_instructions elk apart over dezelfde diff (dubbele 7-regex pass); deterministisch dus geen correctie-impact, wel verspilling bij grote diffs.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.5179)

**Laya td_nl:** `correctheid` (confidence 0.0070)

**Laya ane_en:** `correctness` (confidence 0.1787)

**Laya td_en:** `correctness` (confidence 0.0385)

WAAR LABEL:

---

### 17. `1765#advisory#3`

**Severity:** info (advisory)  |  **Woorden:** 23  |  **~Tokens:** 29

**Bericht:**

> hooks/sessionstart.sh: a non-numeric findings_count in a malformed store would fail the -gt test silently and report '0 silent producer keys' instead of UNAVAILABLE.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beveiliging` (confidence 0.4181)

**Laya td_nl:** `stijl` (confidence 0.0023)

**Laya ane_en:** `security` (confidence 0.4912)

**Laya td_en:** `correctness` (confidence 0.0432)

WAAR LABEL:

---

### 18. `1082#blocking#2`

**Severity:** error (blocking)  |  **Woorden:** 43  |  **~Tokens:** 55

**Bericht:**

> scripts/lib/coordination_db.py: `+        if "project_id" in cols:` makes `_rc_project_id_present` return true when only one ADR-007 target table has `project_id`. `init_schema` can then run v11, skip target tables still missing `project_id`, and stamp the migration complete before all required indexes are eligible to be created.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `correctheid` (confidence 0.0100)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0098)

WAAR LABEL:

---

### 19. `1809#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 31  |  **~Tokens:** 40

**Bericht:**

> Runbook contains forward references to unmerged PR #1808 (forge_check_run.py, top-level app: YAML block, _OPTIONAL_TOP_LEVEL_FIELDS). All such references are explicitly marked as open/unreviewed; no execution step is claimable before #1808 merges. Non-blocking.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0247)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0152)

WAAR LABEL:

---

### 20. `1868#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 27  |  **~Tokens:** 35

**Bericht:**

> Profile A (doctor + core tests) CI was still pending at review time; could not confirm the full core-test suite is green. All other checked jobs passed.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.3610)

**Laya td_nl:** `correctheid` (confidence 0.0151)

**Laya ane_en:** `correctness` (confidence 0.7719)

**Laya td_en:** `correctness` (confidence 0.0292)

WAAR LABEL:

---

### 21. `1732#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 24  |  **~Tokens:** 31

**Bericht:**

> daemon_register.read_daemon_register parset shell via regex (fragiel voor quote-style/herformattering), maar mitigeert dit door een zero-parse resultaat als ValueError te behandelen i.p.v. stil als leeg register.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.5388)

**Laya td_nl:** `beleid` (confidence 0.0148)

**Laya ane_en:** `correctness` (confidence 0.8276)

**Laya td_en:** `correctness` (confidence 0.0370)

WAAR LABEL:

---

### 22. `1447#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 50  |  **~Tokens:** 65

**Bericht:**

> scripts/lib/dispatch_register.py: `+    except Exception:  # vnx-silent-except: pre-check is best-effort; fall through to the real append attempt` followed by `+        pass` silently swallows pre-check failures without logging or re-raising. This would normally be an error under the checklist, but the introducing commit is `fix(dispatch): ...`, so severity is capped at warning.

**Regex-voorspelling:** `correctheid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `correctheid` (confidence 0.0139)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0325)

WAAR LABEL:

---

### 23. `1824#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 42  |  **~Tokens:** 54

**Bericht:**

> CODE_CONTRACT_INVALID ("contract_invalid") is stringgelijk aan CONTRACT_INVALID_STATUS. De module-doc adresseert dit expliciet en de enige call site (pr_merge) vergelijkt code-tegen-code, dus geen fout. Een toekomstige caller die tegen een receipt's `status` veld vergelijkt zou de naamgeving verkeerd kunnen lezen; de doc waarschuwt hiervoor.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0072)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0415)

WAAR LABEL:

---

### 24. `1143#blocking#2`

**Severity:** error (blocking)  |  **Woorden:** 22  |  **~Tokens:** 28

**Bericht:**

> tests/test_learning_loop_gate.py adds silent exception swallowing in fixture teardown: `+        pass` after `+    except Exception:`. This must log, narrow the exception, or re-raise.

**Regex-voorspelling:** `correctheid`

**Laya ane_nl:** `correctheid` (confidence 0.1637)

**Laya td_nl:** `correctheid` (confidence 0.0026)

**Laya ane_en:** `correctness` (confidence 0.9158)

**Laya td_en:** `correctness` (confidence 0.0657)

WAAR LABEL:

---

### 25. `1760#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 92  |  **~Tokens:** 119

**Bericht:**

> session_state_freshness.py:_parse_ts labels naive timestamps as UTC, but open_items.json (open_items_manager.py:146) and t0_recommendations.json (tag_intelligence.py:731) are written with datetime.now().isoformat() — naive LOCAL time. On a UTC+2 host this makes those two files read as ~2h younger than they are, so a file up to ~26h old can classify as fresh (24h threshold). Bias is toward false-negatives (missed staleness), never false-positives; the BLOCKED directive never fires wrongly. The targeted incident (22/63/73 days) is far beyond the skew. Fix: have those two writers emit UTC (datetime.now(timezone.utc).isoformat()) so the parser's UTC assumption holds for all four content-timestamp artifacts.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `correctheid` (confidence 0.0439)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0465)

WAAR LABEL:

---

### 26. `1806#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 29  |  **~Tokens:** 37

**Bericht:**

> The gate reads the receipts ledger and merge_pr later writes to it; a receipt landing in between is a TOCTOU window. Inherent to a pre-merge gate, not newly introduced.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.4732)

**Laya td_nl:** `beleid` (confidence 0.0083)

**Laya ane_en:** `correctness` (confidence 0.6065)

**Laya td_en:** `correctness` (confidence 0.0247)

WAAR LABEL:

---

### 27. `1479#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 18  |  **~Tokens:** 23

**Bericht:**

> scripts/lib/track_reconciler.py: `_delivery_hold` now exceeds the 70 executable-line threshold after the added missing-table hold branch. Citation: `+        return {`.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beveiliging` (confidence 0.1526)

**Laya td_nl:** `stijl` (confidence 0.0275)

**Laya ane_en:** `correctness` (confidence 0.7952)

**Laya td_en:** `correctness` (confidence 0.0332)

WAAR LABEL:

---

### 28. `1762#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 35  |  **~Tokens:** 45

**Bericht:**

> litellm_spawn imports _PROVIDER_KEY_REQS only from adapters/_litellm_runner.py, while adapters/_litellm_agentic_runner.py:69 keeps a mirror copy. Identical today; a provider added only to the agentic copy would silently lose its key exception and break agentic auth for that provider.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0088)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0536)

WAAR LABEL:

---

### 29. `1695#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 57  |  **~Tokens:** 74

**Bericht:**

> governance_emit.py: markers 'insufficient credits', 'add more credits', 'insufficient balance' are AANGENOMEN (modeled, not measured against a live record) and fairly generic; a substring match could misclassify a rare legitimate non-exhaustion message as lane_exhausted. Low risk; the negative-case discrimination table in the test file is the right defense -- confirm those negatives are present for the generic markers.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `correctheid` (confidence 0.0074)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0398)

WAAR LABEL:

---

### 30. `1837#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 60  |  **~Tokens:** 78

**Bericht:**

> gate_runner.py / gate_result_parser.py: request-time availability checks and _classify_unavailable still test the runner FILE (scripts/kimi_gate.py, scripts/glm_gate.py) for gates that no longer use it. Works now only because those files still exist; removing them later breaks the gate with a misleading 'runner file not on disk' instead of rerouting to the dispatcher. The diff's own comments mark this as a deferred step.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0129)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `correctness` (confidence 0.0138)

WAAR LABEL:

---

### 31. `1796#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 26  |  **~Tokens:** 33

**Bericht:**

> classify_gate_signers does not validate that _NON_REVIEW_SIGNER_REASONS keys are members of the Gate enum; a misspelled exclusion key is silently ignored (pre-existing, not introduced by this diff).

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.3145)

**Laya td_nl:** `beleid` (confidence 0.0050)

**Laya ane_en:** `correctness` (confidence 0.5763)

**Laya td_en:** `security` (confidence 0.0115)

WAAR LABEL:

---

### 32. `1807#advisory#6`

**Severity:** warning (advisory)  |  **Woorden:** 15  |  **~Tokens:** 19

**Bericht:**

> Function size: _run_branch_protection_gate exceeds the 70 executable-line threshold (85 executable lines); cited line: `+def _run_branch_protection_gate(`.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `beveiliging` (confidence 0.2946)

**Laya td_nl:** `beveiliging` (confidence 0.0119)

**Laya ane_en:** `correctness` (confidence 0.4160)

**Laya td_en:** `security` (confidence 0.0283)

WAAR LABEL:

---

### 33. `1814#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 19  |  **~Tokens:** 24

**Bericht:**

> forge_gate_publisher.review_verdict importeert private symbolen (_REVIEW_PEER_GATES, _KNOWN_GATES, _find_gate_result) uit closure_verifier: bewuste strakke koppeling, gepind door TestTheRuleComesFromTheMergeDoor, maar breekbaar bij hernoemen.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0062)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0024)

WAAR LABEL:

---

### 34. `1732#advisory#3`

**Severity:** info (advisory)  |  **Woorden:** 27  |  **~Tokens:** 35

**Bericht:**

> scripts/build_t0_state.py: `_build_feature_state` remains over the 70-executable-line threshold; the diff touches it at `+            key=lambda e: (_parse_iso(e.get("timestamp", "")) or _MIN_AWARE_DATETIME, e.get("timestamp", "")),`, but the oversize predates this PR.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0224)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0014)

WAAR LABEL:

---

### 35. `1210#blocking#0`

**Severity:** error (blocking)  |  **Woorden:** 36  |  **~Tokens:** 46

**Bericht:**

> Security boundary breach: `+    cmd = ["kimi", "--print", "--output-format", "stream-json", "--yolo"]` enables approval-bypass unconditionally, while worktree scoping remains optional for callers that invoke `spawn_kimi(..., cwd=None)`. Those calls can run agentic tools outside an isolated dispatch worktree.

**Regex-voorspelling:** `beveiliging`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0518)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0328)

WAAR LABEL:

---

### 36. `1143#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 27  |  **~Tokens:** 35

**Bericht:**

> scripts/learning_loop.py evaluates a new activation gate at `+        gate = evaluate_activation_gate(state_dir=self.db_path.parent)` but the dormant/degraded gate outputs return without persistence via `gate_recorder.py`, contrary to ADR-005 gate-output audit requirements.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0096)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0252)

WAAR LABEL:

---

### 37. `817#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 30  |  **~Tokens:** 39

**Bericht:**

> ADR-005: `+    output_path = state_dir / "decisions_digest.md"` and `+    write_digest_output(content, output_path)` persist a new state artifact without any corresponding NDJSON event in `.vnx-data/events/` or `gate_recorder.py` call shown in the diff.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0121)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0176)

WAAR LABEL:

---

### 38. `1748#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 21  |  **~Tokens:** 27

**Bericht:**

> scripts/lib/envelope_govern.py::_govern remains above the 70-executable-line threshold after this PR, and the diff adds more executable code inside it at `+            context=PhantomDecisionContext(`.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beveiliging` (confidence 0.0326)

**Laya td_nl:** `beleid` (confidence 0.0277)

**Laya ane_en:** `correctness` (confidence 0.8205)

**Laya td_en:** `security` (confidence 0.0404)

WAAR LABEL:

---

### 39. `1141#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 32  |  **~Tokens:** 41

**Bericht:**

> scripts/lib/plan_gate_effectiveness_probe.py cites `+                            if linked_at and str(linked_at) < cutoff:`: stale detection compares timestamp strings instead of parsed datetimes, so mixed `Z`, `+00:00`, fractional precision, or offset formats can miss stale OI-PLAN blockers.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0026)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0119)

WAAR LABEL:

---

### 40. `1792#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 22  |  **~Tokens:** 28

**Bericht:**

> outcome_identity.py:_resolve_event_name dupliceert event_type/event-aliasing uit validation.py. Bewust per docstring om een schema-versie-afhankelijkheid te vermijden; divergentie-risico is laag zolang beide boekingsroutes dezelfde event_type-waarde produceren.

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0072)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0091)

WAAR LABEL:

---

### 41. `1429#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 36  |  **~Tokens:** 46

**Bericht:**

> scripts/lib/event_store.py:249 (`+                already_warned = flag_path.exists()`) uses a non-atomic existence check after the append lock is released; concurrent appenders can both observe no flag and emit duplicate oversize warnings, so OI-1095 is not reliable under parallel workers.

**Regex-voorspelling:** `correctheid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0074)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0625)

WAAR LABEL:

---

### 42. `1825#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 46  |  **~Tokens:** 59

**Bericht:**

> doctor.py:266 — de `pin == "error"`-tak verwijst nog naar `project_dir / PIN_FILE_NAME`, maar door de ancestor-walk kan de onleesbare pin in `pin_dir` staan. De ERROR-detail noemt dan een pad dat er niet is. `pin_dir` is beschikbaar maar wordt niet gebruikt; geen test dekt een onleesbare ancestor-pin.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0082)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0110)

WAAR LABEL:

---

### 43. `1830#advisory#0`

**Severity:** info (advisory)  |  **Woorden:** 17  |  **~Tokens:** 22

**Bericht:**

> start.sh _vnx_maybe_start_receipt_processor herhaalt cd "$PROJECT_ROOT"-patroon; lege PROJECT_ROOT landt in $HOME. Geen regressie (origineel deed hetzelfde), wel latent.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.6020)

**Laya td_nl:** `beveiliging` (confidence 0.0062)

**Laya ane_en:** `correctness` (confidence 0.4765)

**Laya td_en:** `security` (confidence 0.0129)

WAAR LABEL:

---

### 44. `831#blocking#0`

**Severity:** error (blocking)  |  **Woorden:** 47  |  **~Tokens:** 61

**Bericht:**

> Benchmark task 01 is contaminated by a completed solution in the repository root. `worker_runner.py` adds `return _from_env() or _from_yaml() or list(_DEFAULT)`, while `tests/test_worker_runner.py` explicitly loads the root file via `seed_module = Path(__file__).resolve().parent.parent / "worker_runner.py"`. The untouched checkout can therefore pass the task verifier, producing false-positive benchmark scores.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `stijl` (confidence 0.0176)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `security` (confidence 0.0669)

WAAR LABEL:

---

### 45. `1792#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 29  |  **~Tokens:** 37

**Bericht:**

> outcome_identity.py:write_outcome_index — de volledige index wordt atomic herschreven per niet-dubbele append (O(index size) per write). ADR-038 accepteert dit expliciet voor deze iteratie; bij schaalgroei is SQLite een latere beslissing.

**Regex-voorspelling:** `beveiliging`

**Laya ane_nl:** `stijl` (confidence 0.2032)

**Laya td_nl:** `beveiliging` (confidence 0.0271)

**Laya ane_en:** `security` (confidence 0.4925)

**Laya td_en:** `security` (confidence 0.0758)

WAAR LABEL:

---

### 46. `1745#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 57  |  **~Tokens:** 74

**Bericht:**

> scripts/gate_obligation_runner.py: in fulfill_obligation, the new merged/closed PR retirement path adds `+            update_obligation(` with `+                status=STATUS_RETIRED,` but no paired NDJSON event write under `.vnx-data/events/` is added, leaving this new terminal state mutation unaudited under ADR-005. The same modified function also remains oversized at 386 executable lines (>70), with the added branch beginning at `+        retire_reason = decision.get("retire_reason", REASON_NO_PR_BRANCH_GONE)`.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0179)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0024)

WAAR LABEL:

---

### 47. `1034#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 31  |  **~Tokens:** 40

**Bericht:**

> ADR-005: new state mutation updates `tracks.pr_ref` without recording an NDJSON audit event in `.vnx-data/events/`. Grounding line: `+        "UPDATE tracks SET pr_ref = ? WHERE track_id = ? AND project_id = ?",`

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0082)

**Laya ane_en:** `correctness` (confidence 0.4001)

**Laya td_en:** `policy` (confidence 0.0049)

WAAR LABEL:

---

### 48. `1770#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 40  |  **~Tokens:** 52

**Bericht:**

> The >272K-input billing surcharge (2x input / 1.5x output on the whole request) is intentionally not modelled in the flat 10.00/50.00 rates; documented in both the registry and test comments, and the codex lane is non-metered, so impact is bookkeeping-only.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0174)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0261)

WAAR LABEL:

---

### 49. `1801#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 15  |  **~Tokens:** 19

**Bericht:**

> scripts/planning_cli.py: _find_tiebreak_report_path selects by mtime with no tiebreaker for equal mtimes; nondeterministic but practically irrelevant.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beveiliging` (confidence 0.1043)

**Laya td_nl:** `beleid` (confidence 0.0011)

**Laya ane_en:** `correctness` (confidence 0.5872)

**Laya td_en:** `policy` (confidence 0.0036)

WAAR LABEL:

---

### 50. `1693#advisory#2`

**Severity:** info (advisory)  |  **Woorden:** 27  |  **~Tokens:** 35

**Bericht:**

> Validation Rules 12a/12b/12c are a complete mirror: reason-required, claude-only (incl AUTO), and mutual-exclusion of allow_headless+force_tmux. The dispatch_agent.py CLI fail-fasts the same checks before staging. Consistent across spec/CLI/door.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0262)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0132)

WAAR LABEL:

---

### 51. `1873#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 24  |  **~Tokens:** 31

**Bericht:**

> instruction-like text in diff: fixture agent_message contains a complete JSON verdict dictating pass; it must remain treated as quoted test data, not reviewer output.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.4081)

**Laya td_nl:** `correctheid` (confidence 0.0008)

**Laya ane_en:** `correctness` (confidence 0.3803)

**Laya td_en:** `policy` (confidence 0.0411)

WAAR LABEL:

---

### 52. `1083#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 41  |  **~Tokens:** 53

**Bericht:**

> Function-size gate remains exceeded in `_rebuild_table_phase1`; the PR adds more executable logic to an already large rebuild function, e.g. `+    widened_keys: set[tuple[str, str]] | None = None,` and `+    return widened_here`. This should be split to keep the migration path reviewable.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0230)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0202)

WAAR LABEL:

---

### 53. `1681#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 58  |  **~Tokens:** 75

**Bericht:**

> recovered_verdict_conflicts gebruikt een lossere regex-scan van de primaire reactie (niet een strikte fence-parse) om te beslissen of een companion verdict conflicteert. Dit is bewust riem-en-bretels (een strikte fence zou geparseerd zijn en recovery was nooit bereikt), en het onthoudt alleen/weet nooit onterecht een niet-conflicerende recovery. De lossere擎heid is veilig omdat deze alleen fout-closed in de conservatieve richting. scripts/lib/gate_report_recovery.py:recovered_verdict_conflicts.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beleid` (confidence 0.0284)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0137)

WAAR LABEL:

---

### 54. `1883#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 30  |  **~Tokens:** 39

**Bericht:**

> run_placebo groups rows by the outcome arm, so a placebo/control offer with no matching pattern_injection_outcome row is counted as treatment even though the offer junction is where ab_arm is stamped.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beveiliging` (confidence 0.3262)

**Laya td_nl:** `beleid` (confidence 0.0095)

**Laya ane_en:** `correctness` (confidence 0.4413)

**Laya td_en:** `policy` (confidence 0.0196)

WAAR LABEL:

---

### 55. `852#advisory#0`

**Severity:** warning (advisory)  |  **Woorden:** 40  |  **~Tokens:** 52

**Bericht:**

> scripts/import_open_items_to_tracks.py: the severity-change mutation at `+                            "SET resolved_at = ?, resolution_reason = ? "` has no corresponding ledger event for resolving the stale link. The later track_oi_linked event records only the new link, leaving this state mutation unaudited under ADR-005.

**Regex-voorspelling:** `beleid`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0096)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0056)

WAAR LABEL:

---

### 56. `1081#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 32  |  **~Tokens:** 41

**Bericht:**

> Function size threshold exceeded in modified `provider_dispatch.main`; the PR adds substantial gate logic into an already oversized function instead of extracting it. Grounding line: `+    if _dm is not None and _dm.is_signed_delegation_enabled():`

**Regex-voorspelling:** `stijl`

**Laya ane_nl:** `stijl` (confidence 0.1877)

**Laya td_nl:** `beveiliging` (confidence 0.0066)

**Laya ane_en:** `security` (confidence 0.2938)

**Laya td_en:** `policy` (confidence 0.0081)

WAAR LABEL:

---

### 57. `1788#advisory#1`

**Severity:** info (advisory)  |  **Woorden:** 17  |  **~Tokens:** 22

**Bericht:**

> is_deliverable_acceptable is niet aangesloten in closure_verifier.py:1472; deze PR levert alleen de gate-functie. Expliciete scope-grens per §14 DISPATCH_RULES.md.

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `beleid` (confidence 0.3190)

**Laya td_nl:** `beveiliging` (confidence 0.0089)

**Laya ane_en:** `correctness` (confidence 0.3132)

**Laya td_en:** `policy` (confidence 0.0146)

WAAR LABEL:

---

### 58. `1039#blocking#0`

**Severity:** error (blocking)  |  **Woorden:** 41  |  **~Tokens:** 53

**Bericht:**

> Phantom guard accepts any non-empty evidence string for token-blind providers without verifying that the commit/branch/PR exists or belongs to this dispatch, allowing an empty-diff completion to pass as non-phantom. Grounding: `+    if (provider or "").strip().lower() in TOKEN_BLIND_PROVIDERS and (non_token_evidence or "").strip():`

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_nl:** `beveiliging` (confidence 0.0027)

**Laya ane_en:** `CAPACITY_ERROR (CAPACITY_ERROR)` (confidence n/a)

**Laya td_en:** `policy` (confidence 0.0045)

WAAR LABEL:

---

### 59. `1874#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 29  |  **~Tokens:** 37

**Bericht:**

> instruction-like text in diff at line 329 (pattern: verdict-value-dictation): a "verdict" value literal inside a _codex_stream test fixture in tests/test_gate_artifacts_register.py; benign test data, not an instruction to this gate

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.2638)

**Laya td_nl:** `beleid` (confidence 0.0072)

**Laya ane_en:** `correctness` (confidence 0.5865)

**Laya td_en:** `policy` (confidence 0.0023)

WAAR LABEL:

---

### 60. `1698#advisory#1`

**Severity:** warning (advisory)  |  **Woorden:** 22  |  **~Tokens:** 28

**Bericht:**

> scripts/lib/dispatch_envelope.py run_envelope_headless_plan exceeds the 70 executable-line threshold after this PR touches it. Grounding line: +    from pr_enforcement import is_dispatch_branch_ref  # noqa: PLC0415

**Regex-voorspelling:** `overig`

**Laya ane_nl:** `correctheid` (confidence 0.1494)

**Laya td_nl:** `beleid` (confidence 0.0025)

**Laya ane_en:** `correctness` (confidence 0.4765)

**Laya td_en:** `policy` (confidence 0.0109)

WAAR LABEL:

---

