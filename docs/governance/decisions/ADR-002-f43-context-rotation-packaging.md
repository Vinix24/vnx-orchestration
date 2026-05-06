# ADR-002 — F43 Context Rotation: Revive + Package as Standalone Module

**Status:** Accepted — pending implementation
**Date:** 2026-05-01
**Decided by:** Operator (Vincent van Deth)
**Resolves:** OI-1164 — F43 context-rotation feature revival (closed branches)

## Context

F43 ("auto-handover at 65% context pressure for headless workers") is a feature that detects when a headless `claude -p` conversation is approaching the model's context window limit and triggers a handover dispatch — the worker writes a context summary, the orchestrator reads it, and dispatches a fresh worker that continues seamlessly. Without this, headless workers die abruptly when context fills up, losing in-progress work.

Two F43 branches exist on origin but were closed without merging:
- `feat/f43-context-rotation-headless` — 757 LOC, headless path
- `feat/f43-context-rotation-interactive` — 603 LOC, interactive path

Files involved (per the original work):
- `scripts/lib/headless_context_tracker.py` (new)
- `scripts/lib/subprocess_adapter.py` (+44 LOC)
- `scripts/lib/subprocess_dispatch.py` (+142 LOC)
- Tests +501 LOC

The operator considers this feature valuable and wants it both:
1. Revived in the main VNX repo (so VNX dispatches benefit), AND
2. Packaged as a **standalone Python module** that anyone running `claude -p` headless workers can `pip install` and use — independent of VNX. Distributed via PyPI + community giveaway (Reddit, HN, LinkedIn).

## Decision

Implement F43 in three phases:

### Phase A — Revive into main VNX (1 PR)

1. Check out `origin/feat/f43-context-rotation-headless` into a fresh worktree.
2. Rebase onto current main (post-W3J state). Conflicts likely in:
   - `scripts/lib/subprocess_adapter.py` (W4C/W3E/W3I/W3J all touched it)
   - `scripts/lib/subprocess_dispatch.py` (W1A split it into a facade + 11 modules — F43's edits target the OLD layout)
3. Re-apply F43 edits onto the new W1A layout: `subprocess_dispatch_internals/delivery.py` is now where most of F43's `+142 LOC` goes.
4. Run all tests + the F43 test suite (501 LOC).
5. Open PR, gemini-gate, merge.

### Phase B — Carve out as standalone module (1 PR + new repo)

Within the VNX repo, refactor F43 into a self-contained package under `scripts/lib/context_rotation/`:

```
context_rotation/
├── __init__.py          # public API: track_usage(), should_rotate(), build_handover()
├── tracker.py           # token usage tracking (input + output + cumulative)
├── thresholds.py        # configurable trigger logic (default: 65% of model max)
├── handover.py          # handover prompt builder
├── adapters/
│   ├── claude_cli.py    # parses claude -p stream-json events for token usage
│   └── anthropic_sdk.py # parses Anthropic SDK responses (optional, for non-CLI users)
├── tests/
│   └── ...              # all 501 LOC of tests, dependency-free
└── README.md            # standalone module README (separate from VNX)
```

Constraints:
- **stdlib only.** No `anthropic` SDK in core; the CLI adapter parses JSONL events. SDK adapter is optional (extras: `pip install context-rotation[anthropic]`).
- **No VNX imports** in `context_rotation/`. The package must work standalone. VNX-specific glue (subprocess adapter wiring, dispatch register events) lives outside the package, in `scripts/lib/context_rotation_vnx_glue.py`.
- **Public API surface:** `Tracker.update(event)`, `should_rotate(tracker)`, `build_handover(tracker, last_user_message)`. Three functions/classes — that's it.

Once carved out:
- Create separate GitHub repo: `Vinix24/claude-headless-context-rotation` (or chosen name).
- Mirror `scripts/lib/context_rotation/` as the repo root via a sync script (`scripts/maintenance/sync_context_rotation_module.py`).
- Add `pyproject.toml` for PyPI publish.
- License: MIT (low friction for community adoption).
- README sections: problem statement, 30-second integration, model-by-model context limits table, FAQ.

### Phase C — Distribution

1. Publish to PyPI: `pip install headless-context-rotation` (or chosen name).
2. **Reddit** r/LocalLLaMA + r/ClaudeAI: "Anyone else hit context limit mid-task on headless Claude CLI? Open-sourced our solution."
3. **Hacker News** "Show HN" post focused on the technical bit (token tracking + handover prompt design).
4. **LinkedIn** post for the operator's network.
5. Pin a feedback issue on the standalone repo for community input.

## Reasoning

- **The core problem is universal.** Anyone running headless Claude (or any LLM CLI) hits context limits eventually. VNX's solution generalizes.
- **A standalone package gets adoption that an embedded module never will.** People won't fork VNX to extract one feature — they will `pip install` a focused tool.
- **The community-giveaway angle gives VNX visibility.** Each install is a soft endorsement of VNX's engineering. It builds the "this team knows what they're doing" reputation that makes operators trust VNX itself.
- **stdlib-only constraint protects VNX from package drift.** If the standalone module suddenly required new SDK versions, VNX's dispatch path would break. Keeping it stdlib means VNX always works against any pinned version.

## Consequences

### Accepted

- F43 work returns to active development (Phase A as next session's first task).
- Standalone-package work is a multi-day effort — split across at least 2 dispatches (carve-out + sync-script).
- Maintenance burden: a separate PyPI package to keep current. Mitigated by the sync script + CI that runs the package's tests on every VNX commit that touches `scripts/lib/context_rotation/`.

### Open questions for the operator

1. **Package name.** Candidates: `headless-context-rotation`, `claude-context-handover`, `llm-context-rotation`. Pick before Phase B.
2. **License.** Default MIT unless operator wants Apache-2.0 (slightly stronger patent grant).
3. **PyPI account.** Operator needs to claim the package name and link a 2FA-enabled account before Phase C.
4. **VNX rebrand?** Should the standalone package mention VNX in README, or stay generic? (Recommendation: a single "extracted from VNX" line in the README for cred + back-link.)

## Next-session task list

- [ ] **W6A** (Phase A): revive F43 into main. Rebase `feat/f43-context-rotation-headless` onto post-W3J main, re-apply over W1A subprocess_dispatch facade.
- [ ] **W6B** (Phase B-1): carve out `context_rotation/` package within VNX. stdlib-only, public API frozen.
- [ ] **W6C** (Phase B-2): create separate GitHub repo + sync script + pyproject.toml.
- [ ] **W6D** (Phase C-1): PyPI publish.
- [ ] **W6E** (Phase C-2): Reddit/HN/LinkedIn launch posts (operator-reviewed).
