<!-- DERIVED VIEW — DO NOT HAND-EDIT -->
<!-- SSOT is ROADMAP.yaml. This file is materialized at runtime by the roadmap/pr-queue -->
<!-- tooling (scripts/roadmap_manager.py via scripts/pr_queue_manager.py) from the ACTIVE -->
<!-- feature when a feature is loaded, and test runs may also overwrite it. The snapshot -->
<!-- below is the 1.0 clean-launch queue as of 2026-05-31; the live truth is -->
<!-- ROADMAP.yaml features[].pr_queue + `gh pr list`. Do not treat this file as the -->
<!-- source of truth and do not hand-maintain it. -->

# PR Queue — VNX 1.0 clean-launch (DERIVED VIEW)

> Generated view, not the SSOT. Authoritative source: `ROADMAP.yaml` (`launch_state` +
> `features[].pr_queue`). Live PR state: `gh pr list`. Materialized per active feature by
> `roadmap_manager` at runtime; this committed snapshot is informational.

## Progress Overview
Snapshot date: 2026-05-31 | Launch status: **pre-launch** (in-flight, NOT shipped)
Open launch PRs: 10 (#756-#765) + ADR-020 doc (#751) | Merged: 0 of the launch queue
Note: #762 is a mis-titled duplicate of #764 (close before gating).

## Status

### Queued PRs (open, gated — codex gate resets ~20:28 on 2026-05-31)
- #756 — Wave 0 hygiene: untrack .venv + scratch, strip emoji/buzzwords [risk=low, merge=human, review=codex_gate] (merge first)
- #759 — Wave 1 renames: _vN -> canonical + compat shims [risk=high, merge=human, review=gemini_review,codex_gate] (merge ALONE; grep-zero + 4 smoke-tests + net-deletion sanity)
- #758 — Wave 1 scrub: remove residual private-project artifacts [risk=low, merge=human, review=codex_gate] (rebase onto #759)
- #757 — Wave 1 credibility: README control-plane + ADRs + arch diagram [risk=medium, merge=human, review=codex_gate] (rebase onto #759)
- #760 — Wave 1b: role-based manager block + auto-inject footer [risk=high, merge=human, review=gemini_review,codex_gate] (freeze trio)
- #761 — Wave 2: provider-aware intelligence + receipt token/cost [risk=high, merge=human, review=gemini_review,codex_gate] (freeze trio)
- #763 — Wave 2: kimi 1.44.0 robust spawn + fail-loud [risk=medium, merge=human, review=gemini_review,codex_gate]
- #764 — Wave 2: scope worker capabilities (drop skip-permissions, empty MCP) [risk=high, merge=human, review=gemini_review,codex_gate] (close #762 dup; acceptEdits no-stall smoke-test)
- #765 — governed DeepSeek-harness lane [risk=medium, merge=human, review=gemini_review,codex_gate]
- #751 — ADR-020 parallel multi-track execution contract (doc; design-ratified, impl 1.2) [risk=low, merge=human, review=gemini_review]

### Not-yet-PR'd (planned for 1.0)
- Option B parity: extract prepare()/govern() uniform across lanes (depends on #759/#760/#761 freeze)

### Final gate
- LB-3: rebuild wheel from final main -> zero-hit security grep on THAT exact artifact -> fresh-venv acceptance -> PyPI publish (immutable; last)

## Dependency Flow
```
#756 (hygiene, first)
  └─> #759 (renames, ALONE: grep-zero + smoke-tests + net-deletion sanity)
        ├─> #760 + #761 (freeze trio)
        │     ├─> #765 (deepseek lane)
        │     └─> Option B parity (prepare()/govern())  ──> LB-3 (rebuild + grep + PyPI)
        ├─> #764 (capability interim; close #762)
        ├─> #763 (kimi robust spawn)
        └─> #757 + #758 (docs/scrub)
#751 (ADR-020 doc, independent)
```
